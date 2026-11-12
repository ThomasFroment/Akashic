"""Passage backfill, atomic publication, eligible ranking and source preservation."""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING

import numpy as np
import pytest
import sqlcipher3
from ask_fakes import DocumentEmbedder
from memory_data import add_segment, add_speaker

from akashic.pipeline.embed import EMBED, EmbedHandler, enqueue_missing
from akashic.pipeline.jobs import JobQueue
from akashic.search.answer import build_sources
from akashic.search.hybrid import search_memory
from akashic.search.planner import Plan
from akashic.search.timeparse import TimeRange
from akashic.storage import embeddings, passages
from akashic.storage.media import MediaStore
from akashic.storage.retention import expire_embeddings
from akashic.storage.speakers import NewTurn, SpeakerStore
from akashic.storage.usage import storage_report

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Paths


def vectors(text: str) -> list[np.ndarray[tuple[int], np.dtype[np.float32]]]:
    return [
        np.array([1, 0] if "vendredi" in text[a:b] else [0, 1], dtype=np.float32)
        for a, b in passages.windows(text)
    ]


def test_passage_offsets_cover_original_unicode_text() -> None:
    text = "Une stratégie 🐈 puis a game. " * 100
    offsets = passages.windows(text)
    assert offsets[0][0] == 0
    assert offsets[-1][1] == len(text)
    assert all(0 < end - start <= 400 for start, end in offsets)
    assert all(left[1] >= right[0] for left, right in pairwise(offsets))
    assert passages.windows("") == []


def test_old_segment_vectors_are_backfilled_and_revisions_can_be_requeued(conn: Connection) -> None:
    text = "Introduction. " * 100
    segment = add_segment(conn, 1000, text=text)
    embeddings.put(conn, "segment", segment, "m", np.array([1, 0], dtype=np.float32), 1000, 0)
    queue = JobQueue(conn)
    assert enqueue_missing(conn, queue, "m", 1)
    job = queue.claim([EMBED], 1)
    assert job is not None
    assert not enqueue_missing(conn, queue, "m", 2)  # running also blocks a duplicate batch
    handler = EmbedHandler(conn, DocumentEmbedder(), lambda: True)
    handler.run(job)
    queue.complete(job.id, 2)
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == len(
        passages.windows(text)
    )
    assert not enqueue_missing(conn, queue, "m", 3)
    conn.execute("UPDATE audio_segments SET transcript = ? WHERE id = ?", (text + "suite", segment))
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == 0
    assert enqueue_missing(conn, queue, "m", 4)
    assert embeddings.get(conn, "segment", segment, "m") is None


def test_publication_rejects_changed_or_deleted_transcript(conn: Connection) -> None:
    segment = add_segment(conn, 0, text="original transcript")
    conn.execute("UPDATE audio_segments SET transcript = 'replacement' WHERE id = ?", (segment,))
    assert not passages.publish(
        conn, segment, "m", "original transcript", vectors("original transcript")
    )
    conn.execute("DELETE FROM audio_segments WHERE id = ?", (segment,))
    assert not passages.publish(conn, segment, "m", "replacement", vectors("replacement"))
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == 0


def test_interrupted_publication_rolls_back_and_retry_is_idempotent(conn: Connection) -> None:
    text = "unrelated " * 100 + "vendredi"
    segment = add_segment(conn, 0, text=text)
    conn.execute(
        "CREATE TRIGGER simulate_failure BEFORE INSERT ON segment_passages"
        " WHEN new.start_char > 0 BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END"
    )
    with pytest.raises(sqlcipher3.IntegrityError):
        passages.publish(conn, segment, "m", text, vectors(text))
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == 0
    assert embeddings.get(conn, "segment", segment, "m") is None
    conn.execute("DROP TRIGGER simulate_failure")
    assert passages.publish(conn, segment, "m", text, vectors(text))
    assert passages.publish(conn, segment, "m", text, vectors(text))
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == len(
        passages.windows(text)
    )


def test_worker_retry_keeps_legacy_evidence_until_a_complete_set_exists(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "Introduction. " * 100
    segment = add_segment(conn, 0, text=text)
    old = np.array([1, 0], dtype=np.float32)
    embeddings.put(conn, "segment", segment, "m", old, 0, 0)
    queue = JobQueue(conn)
    enqueue_missing(conn, queue, "m", 1)
    job = queue.claim([EMBED], 1)
    assert job is not None
    model = DocumentEmbedder()

    def interrupted(_texts: object) -> list[np.ndarray[tuple[int], np.dtype[np.float32]]]:
        raise RuntimeError("simulated inference interruption")

    with monkeypatch.context() as patched:
        patched.setattr(model, "embed_documents", interrupted)
        with pytest.raises(RuntimeError):
            EmbedHandler(conn, model, lambda: True).run(job)
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == 0
    np.testing.assert_array_equal(embeddings.get(conn, "segment", segment, "m"), old)
    assert queue.release_running() == 1
    retried = queue.claim([EMBED], 2)
    assert retried is not None
    EmbedHandler(conn, model, lambda: True).run(retried)
    queue.complete(retried.id, 2)
    assert embeddings.get(conn, "segment", segment, "m") is None
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == len(
        passages.windows(text)
    )


@pytest.mark.parametrize("removal", ["transcript", "segment", "vectors"])
def test_retention_removes_passages(conn: Connection, paths: Paths, removal: str) -> None:
    media = MediaStore(paths.media_dir, bytes(32))
    text = "hello world transcript"
    segment = add_segment(conn, 0, text=text)
    passages.publish(conn, segment, "m", text, vectors(text))
    if removal == "transcript":
        conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (segment,))
    elif removal == "segment":
        conn.execute("DELETE FROM audio_segments WHERE id = ?", (segment,))
    else:
        expire_embeddings(conn, media, 1)
    assert conn.execute("SELECT count(*) FROM segment_passages").fetchone()[0] == 0


def test_usage_counts_passage_vectors(conn: Connection, paths: Paths) -> None:
    text = "unrelated " * 100
    segment = add_segment(conn, 0, text=text)
    passages.publish(conn, segment, "m", text, vectors(text))
    report = storage_report(conn, paths, 0)
    usage = next(u for u in report.usage if u.kind == "embeddings")
    assert usage.bytes == len(passages.windows(text)) * 2 * 2  # two float16 components


def test_one_long_recording_does_not_consume_the_segment_limit(conn: Connection) -> None:
    text = "vendredi " * 5000
    first = add_segment(conn, 0, text=text)
    second = add_segment(conn, 1000, text="vendredi prochain")
    passages.publish(conn, first, "m", text, vectors(text))
    passages.publish(conn, second, "m", "vendredi prochain", vectors("vendredi prochain"))
    hits = passages.top(
        conn, "m", np.array([1, 0], dtype=np.float32), start_ms=0, end_ms=2000, limit=2
    )
    assert [h.owner_id for h in hits] == [first, second]


def test_passage_limit_counts_segments_and_filters_first(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "CHUNK_ROWS", 2)  # winning passages can cross scan chunks
    person, other = add_speaker(conn, "Minlo"), add_speaker(conn, "Alex")
    for i in range(110):
        text = "vendredi " * 200
        segment = add_segment(conn, i + 1000, text=text, speaker=other)
        passages.publish(conn, segment, "m", text, vectors(text))
    text = "Introduction. " * 240 + "Le jeu sort vendredi."
    target = add_segment(conn, 2000, text=text, speaker=person)
    passages.publish(conn, target, "m", text, vectors(text))
    found = search_memory(
        conn,
        Plan(people=("Minlo",), time=TimeRange(1000, 3000, "test")),
        np.array([1, 0], dtype=np.float32),
        "m",
        limit=1,
    )
    assert [line.segment_id for line in found.lines] == [target]
    (source,) = build_sources(found)
    assert "Le jeu sort vendredi." in source.text
    assert source.start_ms == 2000
    assert source.ref_id == target
    assert source.label == "Minlo said"
    assert (
        passages.top(conn, "m", np.array([1, 0], dtype=np.float32), start_ms=0, end_ms=1000) == []
    )


def test_passages_keep_mixed_speech_unattributed(conn: Connection) -> None:
    text = "Introduction. " * 240 + "Le jeu sort vendredi."
    segment = add_segment(conn, 0, text=text)
    store = SpeakerStore(conn)
    store.record_turns(
        segment,
        [
            NewTurn(0, 1000, 1000, np.array([1, 0], dtype=np.float32)),
            NewTurn(1000, 4000, 3000, np.array([0, 1], dtype=np.float32)),
        ],
        0,
    )
    person = store.all_speakers()[0].id
    store.rename(person, "Minlo")
    passages.publish(conn, segment, "m", text, vectors(text))
    found = search_memory(conn, Plan(people=("Minlo",)), np.array([1, 0], dtype=np.float32), "m")
    (source,) = build_sources(found)
    assert source.label == "someone heard said"
    assert "Le jeu sort vendredi." in source.text


def test_incidental_keyword_cannot_erase_winning_semantic_passage(conn: Connection) -> None:
    text = "The laundry service is available. " + "Unrelated introduction. " * 60
    text += "The Bergen train departs at 23:40 from platform seven."
    segment = add_segment(conn, 1000, text=text)
    values = [
        np.array([1, 0] if "Bergen" in text[a:b] else [0, 1], dtype=np.float32)
        for a, b in passages.windows(text)
    ]
    assert passages.publish(conn, segment, "m", text, values)
    found = search_memory(
        conn, Plan(keywords=("service",)), np.array([1, 0], dtype=np.float32), "m"
    )
    assert "23:40" in found.lines[0].text
    assert any("23:40" in source.text for source in build_sources(found))
