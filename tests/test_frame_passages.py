"""Screen passage indexing, strict scope, matched excerpts and deletion in encrypted stores."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest
import sqlcipher3
from ask_fakes import DocumentEmbedder, FakeChat, ask, make
from memory_data import NOW

from akashic.pipeline.embed import BATCH, EMBED, EmbedHandler, enqueue_missing
from akashic.pipeline.jobs import JobQueue
from akashic.search.answer import build_sources
from akashic.search.hybrid import search_memory
from akashic.search.planner import Plan
from akashic.search.semantic import semantic_ready
from akashic.search.timeparse import TimeRange
from akashic.storage import embeddings, passages
from akashic.storage.media import MediaStore
from akashic.storage.retention import (
    delete_all_recorded_data,
    expire_embeddings,
    expire_frame_text,
    expire_screenshots,
)
from akashic.storage.usage import storage_report

if TYPE_CHECKING:
    from collections.abc import Sequence
    from sqlite3 import Connection

    from numpy.typing import NDArray

    from akashic.config import Paths

    type Vector = NDArray[np.float32]

QUERY = np.array([1, 0], dtype=np.float32)
FACT = "Les sauvegardes sont chiffrées sur un disque externe, sans envoi sur Internet."
TEXT = "Settings and notifications. " * 100 + FACT


def frame(
    conn: Connection,
    text: str = TEXT,
    *,
    ts: int = 1000,
    app: str = "zen.exe",
) -> int:
    row = conn.execute(
        "INSERT INTO frames (ts_ms, monitor, process, window_title, url, text, text_source,"
        " trigger) VALUES (?, 0, ?, 'Invented page', 'https://example.test', ?,"
        " 'uia', 'idle')",
        (ts, app, text),
    )
    assert row.lastrowid is not None
    return row.lastrowid


def vectors(text: str) -> list[Vector]:
    return [
        np.array([1, 0] if "sauvegardes" in text[a:b] else [0, 1], dtype=np.float32)
        for a, b in passages.windows(text)
    ]


def test_semantic_screen_hit_keeps_late_fact_and_exact_frame_link(conn: Connection) -> None:
    target = frame(conn)
    assert passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    plan = Plan(keywords=("backup", "privacy"), apps=("Zen",), time=TimeRange(900, 1100, "test"))
    assert not search_memory(conn, plan, None, "m").frames
    found = search_memory(conn, plan, QUERY, "m")
    assert [f.id for f in found.frames] == [target]
    (source,) = build_sources(found)
    assert source.kind == "frame"
    assert source.ref_id == target
    assert source.start_ms == 1000
    assert FACT in source.text
    assert "zen.exe" in source.label


def test_frame_only_history_is_semantically_ready(conn: Connection) -> None:
    target = frame(conn)
    assert passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    assert semantic_ready(conn, "m")
    assert not semantic_ready(conn, "other-model")


def test_ask_with_only_screen_passages_embeds_and_passes_fact_to_answer(conn: Connection) -> None:
    target = frame(conn, ts=NOW)
    passages.publish(conn, target, "emb", TEXT, vectors(TEXT), kind="frame")
    raw_plan = json.dumps(
        {
            "people": [],
            "kinds": [],
            "keywords": ["backup", "privacy"],
            "semantic_query": "backup privacy",
        }
    )
    chat = FakeChat(pieces=("They stay on an external disk [1].",), plan=raw_plan)
    handler, _, model = make(conn, chat)
    job = ask(conn, "How were the backup copies kept private?")
    handler.run(job)
    assert model.queries == ["backup privacy"]
    assert FACT in chat.prompts[0][1]
    row = conn.execute("SELECT state, sources FROM questions").fetchone()
    assert row[0] == "done"
    assert json.loads(row[1])[0]["id"] == target
    assert json.loads(row[1])[0]["kind"] == "frame"


def test_frame_time_and_app_constraints_precede_ranking_limits(conn: Connection) -> None:
    for index in range(210):
        wrong = frame(conn, ts=2000 + index % 50, app="discord.exe")
        passages.publish(conn, wrong, "m", TEXT, vectors(TEXT), kind="frame")
    outside = frame(conn, ts=3000)
    passages.publish(conn, outside, "m", TEXT, vectors(TEXT), kind="frame")
    target = frame(conn, ts=2000, app="ZEN.EXE")
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    plan = Plan(apps=("zen",), time=TimeRange(1900, 2100, "test"))
    found = search_memory(conn, plan, QUERY, "m", limit=1)
    assert [f.id for f in found.frames] == [target]
    words = Plan(apps=plan.apps, time=plan.time, keywords=("sauvegardes",))
    assert [f.id for f in search_memory(conn, words, None, "m", limit=1).frames] == [target]
    assert not search_memory(conn, Plan(apps=("absent",)), QUERY, "m").frames


@pytest.mark.parametrize("constraint", [Plan(people=("Minlo",)), Plan(kinds=("conversation",))])
def test_screen_semantics_cannot_satisfy_people_or_kind_constraints(
    conn: Connection, constraint: Plan
) -> None:
    conn.execute("INSERT INTO speakers (name, created_ms) VALUES ('Minlo', 0)")
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    assert not search_memory(conn, constraint, QUERY, "m").frames


def test_frame_best_passage_overrides_incidental_keyword_and_deduplicates(conn: Connection) -> None:
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    found = search_memory(conn, Plan(keywords=("Settings",)), QUERY, "m")
    assert [f.id for f in found.frames] == [target]
    assert FACT in build_sources(found)[0].text


def test_frame_semantics_respect_model_dimensions_and_exclusive_time_end(conn: Connection) -> None:
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    assert not search_memory(conn, Plan(), QUERY, "other-model").frames
    assert not search_memory(conn, Plan(), np.ones(3, dtype=np.float32), "m").frames
    outside = Plan(time=TimeRange(0, 1000, "test"))
    assert not search_memory(conn, outside, QUERY, "m").frames


def test_keyword_screen_search_still_works_without_embeddings(conn: Connection) -> None:
    target = frame(conn)
    found = search_memory(conn, Plan(keywords=("sauvegardes",)), None, "m")
    assert [f.id for f in found.frames] == [target]
    assert "sauvegardes" in found.frames[0].snippet


def test_frame_scan_chunks_and_multiple_passages_count_one_frame(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "CHUNK_ROWS", 2)
    first = frame(conn)
    second = frame(conn, text=FACT, ts=1001)
    passages.publish(conn, first, "m", TEXT, vectors(TEXT), kind="frame")
    passages.publish(conn, second, "m", FACT, vectors(FACT), kind="frame")
    found = search_memory(conn, Plan(), QUERY, "m", limit=2)
    assert [f.id for f in found.frames] == [first, second]


def test_missing_frames_queue_in_batches_with_capture_retention_cutoff(conn: Connection) -> None:
    old = frame(conn, ts=1)
    for index in range(BATCH + 5):
        frame(conn, ts=index + 1000)
    queue = JobQueue(conn)
    assert enqueue_missing(conn, queue, "m", 1, since_ms=1000)
    job = queue.claim([EMBED], 1)
    assert job is not None
    assert job.payload["kind"] == "frame"
    assert isinstance(job.payload["ids"], list)
    ids = cast("list[int]", job.payload["ids"])
    assert old not in ids
    assert len(ids) == BATCH
    assert job.data_ms == 1000
    assert not enqueue_missing(conn, queue, "m", 2)
    EmbedHandler(conn, DocumentEmbedder(), lambda: True).run(job)
    queue.complete(job.id, 2)
    assert enqueue_missing(conn, queue, "m", 3, since_ms=1000)
    last = queue.claim([EMBED], 3)
    assert last is not None
    EmbedHandler(conn, DocumentEmbedder(), lambda: True).run(last)
    queue.complete(last.id, 4)
    assert not enqueue_missing(conn, queue, "m", 5, since_ms=1000)


@pytest.mark.parametrize("delete", [False, True])
def test_worker_rejects_changed_or_deleted_frame_during_inference(
    conn: Connection, monkeypatch: pytest.MonkeyPatch, *, delete: bool
) -> None:
    target = frame(conn)
    queue = JobQueue(conn)
    enqueue_missing(conn, queue, "m", 1)
    job = queue.claim([EMBED], 1)
    assert job is not None
    model = DocumentEmbedder()
    original = model.embed_documents

    def infer(texts: Sequence[str]) -> list[Vector]:
        if delete:
            conn.execute("DELETE FROM frames WHERE id = ?", (target,))
        else:
            conn.execute("UPDATE frames SET text = 'replacement' WHERE id = ?", (target,))
        return original(texts)

    monkeypatch.setattr(model, "embed_documents", infer)
    EmbedHandler(conn, model, lambda: True).run(job)
    assert conn.execute("SELECT count(*) FROM frame_passages").fetchone()[0] == 0


def test_interrupted_frame_publication_rolls_back_and_worker_can_retry(conn: Connection) -> None:
    frame(conn)
    conn.execute(
        "CREATE TRIGGER break_frame_publish BEFORE INSERT ON frame_passages"
        " WHEN new.start_char > 0 BEGIN SELECT RAISE(ABORT, 'interruption'); END"
    )
    queue = JobQueue(conn)
    enqueue_missing(conn, queue, "m", 1)
    job = queue.claim([EMBED], 1)
    assert job is not None
    handler = EmbedHandler(conn, DocumentEmbedder(), lambda: True)
    with pytest.raises(sqlcipher3.IntegrityError):
        handler.run(job)
    assert conn.execute("SELECT count(*) FROM frame_passages").fetchone()[0] == 0
    conn.execute("DROP TRIGGER break_frame_publish")
    assert queue.release_running() == 1
    retry = queue.claim([EMBED], 2)
    assert retry is not None
    handler.run(retry)
    handler.run(retry)
    assert conn.execute("SELECT count(*) FROM frame_passages").fetchone()[0] == len(
        passages.windows(TEXT)
    )


@pytest.mark.parametrize("removal", ["text", "row", "vectors", "all"])
def test_frame_deletion_and_retention_remove_derived_vectors(
    conn: Connection, paths: Paths, removal: str
) -> None:
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    media = MediaStore(paths.media_dir, bytes(32))
    if removal == "text":
        expire_frame_text(conn, media, 1001)
    elif removal == "row":
        conn.execute("DELETE FROM frames WHERE id = ?", (target,))
    elif removal == "vectors":
        expire_embeddings(conn, media, 1001)
    else:
        delete_all_recorded_data(conn, media)
    assert not search_memory(conn, Plan(), QUERY, "m").frames
    assert conn.execute("SELECT count(*) FROM frame_passages").fetchone()[0] == 0


def test_screenshot_only_expiry_keeps_screen_text_index_and_usage(
    conn: Connection, paths: Paths
) -> None:
    target = frame(conn)
    media = MediaStore(paths.media_dir, bytes(32))
    media.write_unique("invented.webp", b"invented image")
    conn.execute("UPDATE frames SET image_path = 'invented.webp' WHERE id = ?", (target,))
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="frame")
    expire_screenshots(conn, media, 1001)
    usage = next(u for u in storage_report(conn, paths, 1000).usage if u.kind == "embeddings")
    assert usage.bytes == len(passages.windows(TEXT)) * 4
    assert search_memory(conn, Plan(), QUERY, "m").frames[0].id == target
    conn.execute("UPDATE frames SET text = 'replacement text' WHERE id = ?", (target,))
    assert conn.execute("SELECT count(*) FROM frame_passages").fetchone()[0] == 0
    assert enqueue_missing(conn, JobQueue(conn), "m", 3)
