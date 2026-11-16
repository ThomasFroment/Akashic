"""Screen passage indexing, strict scope, matched excerpts and deletion in encrypted stores."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest
import sqlcipher3
from ask_fakes import DocumentEmbedder, FakeChat, ask, make
from memory_data import NOW, add_frame, add_window, window_of

from akashic.pipeline.embed import BATCH, EMBED, EmbedHandler, enqueue_missing
from akashic.search.answer import build_sources
from akashic.search.hybrid import search_memory
from akashic.search.planner import Plan
from akashic.search.semantic import semantic_ready
from akashic.search.timeparse import TimeRange
from akashic.storage import embeddings, passages
from akashic.storage.jobs import JobQueue
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
    """A frame with one window; returns the window id (frame ids match it, one window each)."""
    frame_id = add_frame(conn, ts, text, "Invented page", process=app, url="https://example.test")
    return window_of(conn, frame_id)


def vectors(text: str) -> list[Vector]:
    return [
        np.array([1, 0] if "sauvegardes" in text[a:b] else [0, 1], dtype=np.float32)
        for a, b in passages.windows(text)
    ]


def test_semantic_screen_hit_keeps_late_fact_and_exact_frame_link(conn: Connection) -> None:
    target = frame(conn)
    assert passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    plan = Plan(keywords=("backup", "privacy"), apps=("Zen",), time=TimeRange(900, 1100, "test"))
    assert not search_memory(conn, plan, None, "m").frames
    found = search_memory(conn, plan, QUERY, "m")
    assert [f.window_id for f in found.frames] == [target]
    (source,) = build_sources(found)
    assert source.kind == "frame"
    assert (
        source.ref_id
        == conn.execute("SELECT frame_id FROM frame_windows WHERE id = ?", (target,)).fetchone()[0]
    )
    assert source.window_id == target
    assert source.start_ms == 1000
    assert FACT in source.text
    assert "zen.exe" in source.label


def test_frame_only_history_is_semantically_ready(conn: Connection) -> None:
    target = frame(conn)
    assert passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    assert semantic_ready(conn, "m")
    assert not semantic_ready(conn, "other-model")


def test_ask_with_only_screen_passages_embeds_and_passes_fact_to_answer(conn: Connection) -> None:
    target = frame(conn, ts=NOW)
    passages.publish(conn, target, "emb", TEXT, vectors(TEXT), kind="window")
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
    assert json.loads(row[1])[0]["window_id"] == target
    assert json.loads(row[1])[0]["kind"] == "frame"


def test_frame_time_and_app_constraints_precede_ranking_limits(conn: Connection) -> None:
    for index in range(210):
        wrong = frame(conn, ts=2000 + index % 50, app="discord.exe")
        passages.publish(conn, wrong, "m", TEXT, vectors(TEXT), kind="window")
    outside = frame(conn, ts=3000)
    passages.publish(conn, outside, "m", TEXT, vectors(TEXT), kind="window")
    target = frame(conn, ts=2000, app="ZEN.EXE")
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    plan = Plan(apps=("zen",), time=TimeRange(1900, 2100, "test"))
    found = search_memory(conn, plan, QUERY, "m", limit=1)
    assert [f.window_id for f in found.frames] == [target]
    words = Plan(apps=plan.apps, time=plan.time, keywords=("sauvegardes",))
    assert [f.window_id for f in search_memory(conn, words, None, "m", limit=1).frames] == [target]
    assert not search_memory(conn, Plan(apps=("absent",)), QUERY, "m").frames


@pytest.mark.parametrize("constraint", [Plan(people=("Minlo",)), Plan(kinds=("conversation",))])
def test_screen_semantics_cannot_satisfy_people_or_kind_constraints(
    conn: Connection, constraint: Plan
) -> None:
    conn.execute("INSERT INTO speakers (name, created_ms) VALUES ('Minlo', 0)")
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    assert not search_memory(conn, constraint, QUERY, "m").frames


def test_window_best_passage_overrides_incidental_keyword_and_deduplicates(
    conn: Connection,
) -> None:
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    found = search_memory(conn, Plan(keywords=("Settings",)), QUERY, "m")
    assert [f.window_id for f in found.frames] == [target]
    assert FACT in build_sources(found)[0].text


def test_frame_semantics_respect_model_dimensions_and_exclusive_time_end(conn: Connection) -> None:
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    assert not search_memory(conn, Plan(), QUERY, "other-model").frames
    assert not search_memory(conn, Plan(), np.ones(3, dtype=np.float32), "m").frames
    outside = Plan(time=TimeRange(0, 1000, "test"))
    assert not search_memory(conn, outside, QUERY, "m").frames


def test_keyword_screen_search_still_works_without_embeddings(conn: Connection) -> None:
    target = frame(conn)
    found = search_memory(conn, Plan(keywords=("sauvegardes",)), None, "m")
    assert [f.window_id for f in found.frames] == [target]
    assert "sauvegardes" in found.frames[0].snippet


def test_frame_scan_chunks_and_multiple_passages_count_one_frame(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "CHUNK_ROWS", 2)
    first = frame(conn)
    second = frame(conn, text=FACT, ts=1001)
    passages.publish(conn, first, "m", TEXT, vectors(TEXT), kind="window")
    passages.publish(conn, second, "m", FACT, vectors(FACT), kind="window")
    found = search_memory(conn, Plan(), QUERY, "m", limit=2)
    assert [f.window_id for f in found.frames] == [first, second]


def test_missing_frames_queue_in_batches_with_capture_retention_cutoff(conn: Connection) -> None:
    old = frame(conn, ts=1)
    for index in range(BATCH + 5):
        frame(conn, ts=index + 1000)
    queue = JobQueue(conn)
    assert enqueue_missing(conn, queue, "m", 1, since_ms=1000)
    job = queue.claim([EMBED], 1)
    assert job is not None
    assert job.payload["kind"] == "window"
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
            conn.execute("DELETE FROM frame_windows WHERE id = ?", (target,))
        else:
            conn.execute("UPDATE frame_windows SET text = 'replacement' WHERE id = ?", (target,))
        return original(texts)

    monkeypatch.setattr(model, "embed_documents", infer)
    EmbedHandler(conn, model, lambda: True).run(job)
    assert conn.execute("SELECT count(*) FROM window_passages").fetchone()[0] == 0


def test_interrupted_frame_publication_rolls_back_and_worker_can_retry(conn: Connection) -> None:
    frame(conn)
    conn.execute(
        "CREATE TRIGGER break_frame_publish BEFORE INSERT ON window_passages"
        " WHEN new.start_char > 0 BEGIN SELECT RAISE(ABORT, 'interruption'); END"
    )
    queue = JobQueue(conn)
    enqueue_missing(conn, queue, "m", 1)
    job = queue.claim([EMBED], 1)
    assert job is not None
    handler = EmbedHandler(conn, DocumentEmbedder(), lambda: True)
    with pytest.raises(sqlcipher3.IntegrityError):
        handler.run(job)
    assert conn.execute("SELECT count(*) FROM window_passages").fetchone()[0] == 0
    conn.execute("DROP TRIGGER break_frame_publish")
    assert queue.release_running() == 1
    retry = queue.claim([EMBED], 2)
    assert retry is not None
    handler.run(retry)
    handler.run(retry)
    assert conn.execute("SELECT count(*) FROM window_passages").fetchone()[0] == len(
        passages.windows(TEXT)
    )


@pytest.mark.parametrize("removal", ["text", "row", "vectors", "all"])
def test_frame_deletion_and_retention_remove_derived_vectors(
    conn: Connection, paths: Paths, removal: str
) -> None:
    target = frame(conn)
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    media = MediaStore(paths.media_dir, bytes(32))
    if removal == "text":
        expire_frame_text(conn, media, 1001)
    elif removal == "row":
        conn.execute("DELETE FROM frame_windows WHERE id = ?", (target,))
    elif removal == "vectors":
        expire_embeddings(conn, media, 1001)
    else:
        delete_all_recorded_data(conn, media)
    assert not search_memory(conn, Plan(), QUERY, "m").frames
    assert conn.execute("SELECT count(*) FROM window_passages").fetchone()[0] == 0


def test_screenshot_only_expiry_keeps_screen_text_index_and_usage(
    conn: Connection, paths: Paths
) -> None:
    target = frame(conn)
    media = MediaStore(paths.media_dir, bytes(32))
    media.write_unique("invented.webp", b"invented image")
    conn.execute("UPDATE frames SET image_path = 'invented.webp'")
    passages.publish(conn, target, "m", TEXT, vectors(TEXT), kind="window")
    expire_screenshots(conn, media, 1001)
    usage = next(u for u in storage_report(conn, paths, 1000).usage if u.kind == "embeddings")
    assert usage.bytes == len(passages.windows(TEXT)) * 4
    assert search_memory(conn, Plan(), QUERY, "m").frames[0].window_id == target
    conn.execute("UPDATE frame_windows SET text = 'replacement text' WHERE id = ?", (target,))
    assert conn.execute("SELECT count(*) FROM window_passages").fetchone()[0] == 0
    assert enqueue_missing(conn, JobQueue(conn), "m", 3)


# --- windows of one frame and runs of identical rows ---------------------------------------------


def two_window_frame(conn: Connection) -> tuple[int, int, int]:
    """A frame with a chrome window (z 0) and a code window (z 1); returns frame and both ids."""
    frame_id = add_frame(conn, 1000, TEXT, "Docs", process="chrome.exe", hwnd=1)
    code = add_window(conn, frame_id, 1, TEXT, process="code.exe", title="main.py")
    return frame_id, window_of(conn, frame_id), code


def test_keyword_frame_hits_are_windows_filtered_by_the_windows_own_process(
    conn: Connection,
) -> None:
    frame_id, chrome, code = two_window_frame(conn)
    both = search_memory(conn, Plan(keywords=("sauvegardes",)), None, "m").frames
    assert {(f.frame_id, f.window_id, f.process) for f in both} == {
        (frame_id, chrome, "chrome.exe"),
        (frame_id, code, "code.exe"),
    }
    only = search_memory(conn, Plan(keywords=("sauvegardes",), apps=("code",)), None, "m").frames
    assert [(f.window_id, f.process, f.title) for f in only] == [(code, "code.exe", "main.py")]


def test_semantic_frame_hits_filter_passages_by_the_windows_own_process(
    conn: Connection,
) -> None:
    _, chrome, code = two_window_frame(conn)
    for window in (chrome, code):
        passages.publish(conn, window, "m", TEXT, vectors(TEXT), kind="window")
    found = search_memory(conn, Plan(apps=("code.exe",)), QUERY, "m").frames
    assert [f.window_id for f in found] == [code]
    assert FACT in found[0].snippet


def repeat(conn: Connection, texts: list[str], hwnd: int = 7, start: int = 2_000) -> list[int]:
    return [
        window_of(conn, add_frame(conn, start + n, text, "Page", process="zen.exe", hwnd=hwnd))
        for n, text in enumerate(texts)
    ]


def frame_hits(conn: Connection, word: str, limit: int = 5) -> list[int]:
    plan = Plan(keywords=(word,))
    return [f.window_id for f in search_memory(conn, plan, None, "m", limit=limit).frames]


def test_identical_consecutive_rows_of_one_hwnd_become_one_hit_from_the_run(
    conn: Connection,
) -> None:
    ids = repeat(conn, ["quux same"] * 3)
    hits = frame_hits(conn, "quux")
    assert len(hits) == 1
    assert hits[0] in ids


def test_a_different_row_between_identical_ones_keeps_three_hits(conn: Connection) -> None:
    ids = repeat(conn, ["quux a", "quux b", "quux a"])
    assert sorted(frame_hits(conn, "quux")) == sorted(ids)


def test_identical_content_on_two_hwnds_is_two_hits(conn: Connection) -> None:
    repeat(conn, ["quux same"], hwnd=7)
    repeat(conn, ["quux same"], hwnd=8, start=3_000)
    assert len(frame_hits(conn, "quux")) == 2


def test_a_run_does_not_use_up_the_hit_slots(conn: Connection) -> None:
    repeat(conn, ["quux same"] * 20)
    others = [
        window_of(conn, add_frame(conn, 5_000 + n, f"quux other {n}", "P", hwnd=20 + n))
        for n in range(4)
    ]
    hits = frame_hits(conn, "quux", limit=5)
    assert len(hits) == 5
    assert set(others) <= set(hits)


def test_only_windows_with_enough_text_and_no_passages_are_queued_oldest_frame_first(
    conn: Connection,
) -> None:
    late = frame(conn, "texte assez long ici", ts=3000)
    early = frame(conn, "texte assez long là", ts=1000)
    frame(conn, "court", ts=500)  # under 12 characters: keyword search only
    done = frame(conn, "texte déjà vectorisé", ts=2000)
    passages.publish(conn, done, "m", "texte déjà vectorisé", [QUERY], kind="window")
    queue = JobQueue(conn)
    assert enqueue_missing(conn, queue, "m", 1)
    job = queue.claim([EMBED], 1)
    assert job is not None
    assert job.payload["kind"] == "window"
    assert job.payload["ids"] == [early, late]
    assert job.data_ms == 1000
    queue.complete(job.id, 2)
    assert enqueue_missing(conn, queue, "other-model", 3)  # passages are per model
    other = queue.claim([EMBED], 3)
    assert other is not None
    assert other.payload["ids"] == [early, done, late]


def test_the_worker_publishes_one_passage_set_per_window(conn: Connection) -> None:
    frame_id = add_frame(conn, 1000, TEXT, "Docs", process="chrome.exe", hwnd=1)
    first = window_of(conn, frame_id)
    second = add_window(conn, frame_id, 1, FACT, process="code.exe")
    queue = JobQueue(conn)
    assert enqueue_missing(conn, queue, DocumentEmbedder.model, 1)
    job = queue.claim([EMBED], 1)
    assert job is not None
    EmbedHandler(conn, DocumentEmbedder(), lambda: True).run(job)
    counts = dict(
        conn.execute(
            "SELECT window_id, count(*) FROM window_passages GROUP BY window_id"
        ).fetchall()
    )
    assert counts == {first: len(passages.windows(TEXT)), second: len(passages.windows(FACT))}
    assert semantic_ready(conn, DocumentEmbedder.model)
