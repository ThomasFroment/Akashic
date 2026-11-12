"""Embedding jobs, question vectors, and the worker's housekeeping that queues them."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest
from ask_fakes import DocumentEmbedder
from memory_data import HOUR, MINUTE, NOW, add_episode, add_media, add_segment

from akashic.config import Config
from akashic.config import Search as SearchSettings
from akashic.pipeline.embed import (
    BATCH,
    EMBED,
    EMBED_QUERY,
    EmbedHandler,
    QueryEmbedHandler,
    enqueue_missing,
    episode_text,
    query_job,
)
from akashic.pipeline.jobs import Job, JobQueue, PermanentJobError
from akashic.pipeline.main import housekeeping, model_folder_has_gguf
from akashic.search.semantic import query_vector, semantic_ready
from akashic.storage import embeddings

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path
    from sqlite3 import Connection

    from numpy.typing import NDArray

    from akashic.child import ChildContext

    type Vector = NDArray[np.float32]

LONG_AGO = NOW - 6 * HOUR


def claimed(queue: JobQueue, kind: str) -> Job:
    job = queue.claim([kind], NOW)
    assert job is not None
    return job


# --- what to embed -------------------------------------------------------------------------


def test_an_episodes_text_is_its_title_and_its_digest() -> None:
    assert episode_text("T", "digest") == "T\ndigest"


def test_missing_vectors_are_queued_a_batch_at_a_time_episodes_first(conn: Connection) -> None:
    queue = JobQueue(conn)
    e1 = add_episode(conn, "video", 100, 200, "One")
    e2 = add_episode(conn, "video", 300, 400, "Two")
    s1 = add_segment(conn, 50, text="a long enough sentence")
    add_segment(conn, 60, text="ok")  # too short to carry anything
    add_segment(conn, 70, text=None, status="pending")
    assert enqueue_missing(conn, queue, "m", NOW)
    job = claimed(queue, EMBED)
    assert job.payload == {"kind": "episode", "ids": [e1, e2], "model": "m"}
    assert job.data_ms == 100
    assert not enqueue_missing(conn, queue, "m", NOW)  # one batch at a time
    queue.complete(job.id, NOW)
    embeddings.put(conn, "episode", e1, "m", np.zeros(2, dtype=np.float32), 100, NOW)
    embeddings.put(conn, "episode", e2, "m", np.zeros(2, dtype=np.float32), 300, NOW)
    assert enqueue_missing(conn, queue, "m", NOW)
    assert claimed(queue, EMBED).payload == {"kind": "segment", "ids": [s1], "model": "m"}


def test_batches_are_limited_and_the_retention_cut_off_is_respected(conn: Connection) -> None:
    queue = JobQueue(conn)
    for i in range(BATCH + 5):
        add_episode(conn, "work", 1_000 + i, 2_000 + i, f"w{i}")
    assert enqueue_missing(conn, queue, "m", NOW, since_ms=1_000 + 3)
    ids = cast("list[int]", claimed(queue, EMBED).payload["ids"])
    assert len(ids) == BATCH
    assert 1 not in ids  # older than the limit: never embedded


def test_nothing_is_queued_when_nothing_is_missing(conn: Connection) -> None:
    assert not enqueue_missing(conn, JobQueue(conn), "m", NOW)


def test_vectors_of_another_model_do_not_count(conn: Connection) -> None:
    episode = add_episode(conn, "game", 1, 2, "G")
    embeddings.put(conn, "episode", episode, "old-model", np.zeros(2, dtype=np.float32), 1, NOW)
    assert enqueue_missing(conn, JobQueue(conn), "m", NOW)


# --- the jobs ------------------------------------------------------------------------------


def test_the_embed_job_stores_one_vector_per_episode_with_its_capture_time(
    conn: Connection,
) -> None:
    queue, embedder = JobQueue(conn), DocumentEmbedder()
    one = add_episode(conn, "video", 111, 200, "Title", "digest")
    two = add_episode(conn, "video", 222, 300, "Other", "more")
    enqueue_missing(conn, queue, "m", NOW)
    handler = EmbedHandler(conn, embedder, lambda: True, lambda: 5)
    handler.run(claimed(queue, EMBED))
    assert embedder.documents == ["Title\ndigest", "Other\nmore"]
    rows = conn.execute(
        "SELECT owner_id, start_ms, created_ms, dim FROM embeddings ORDER BY owner_id"
    ).fetchall()
    assert rows == [(one, 111, 5, 2), (two, 222, 5, 2)]


def test_the_embed_job_does_transcripts_and_skips_rows_that_vanished(conn: Connection) -> None:
    queue, embedder = JobQueue(conn), DocumentEmbedder()
    keep = add_segment(conn, 1_000, text="this one stays around")
    gone = add_segment(conn, 2_000, text="this one is deleted")
    enqueue_missing(conn, queue, "m", NOW)
    conn.execute("DELETE FROM audio_segments WHERE id = ?", (gone,))
    EmbedHandler(conn, embedder, lambda: True).run(claimed(queue, EMBED))
    assert embedder.documents == ["this one stays around"]
    assert conn.execute("SELECT segment_id FROM segment_passages").fetchall() == [(keep,)]
    assert semantic_ready(conn, "m")


@pytest.mark.parametrize("delete", [False, True])
def test_episode_embedding_cannot_publish_text_invalidated_during_inference(
    conn: Connection, monkeypatch: pytest.MonkeyPatch, *, delete: bool
) -> None:
    episode = add_episode(conn, "conversation", 100, 200, "Call", "secretphrase")
    seg = add_segment(conn, 100, text="secretphrase")
    conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (episode, seg))
    queue, embedder = JobQueue(conn), DocumentEmbedder()
    enqueue_missing(conn, queue, "m", NOW)
    original = embedder.embed_documents

    def infer(texts: Sequence[str]) -> list[Vector]:
        if delete:
            conn.execute("DELETE FROM episodes WHERE id = ?", (episode,))
        else:
            conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (seg,))
        return original(texts)

    monkeypatch.setattr(embedder, "embed_documents", infer)
    EmbedHandler(conn, embedder, lambda: True).run(claimed(queue, EMBED))
    assert embeddings.get(conn, "episode", episode, "m") is None


def test_a_job_for_another_model_or_with_nothing_left_does_nothing(conn: Connection) -> None:
    queue, embedder = JobQueue(conn), DocumentEmbedder()
    add_episode(conn, "game", 1, 2, "G")
    enqueue_missing(conn, queue, "other", NOW)
    handler = EmbedHandler(conn, embedder, lambda: True)
    handler.run(claimed(queue, EMBED))
    assert embedder.documents == []
    job = Job(1, EMBED, "k", {"kind": "episode", "ids": [999], "model": "m"}, 0, 1)
    handler.run(job)
    assert embedder.documents == []


def test_a_malformed_embed_job_is_permanent(conn: Connection) -> None:
    handler = EmbedHandler(conn, DocumentEmbedder(), lambda: True)
    for payload in (
        {"kind": "episode", "model": "m"},
        {"kind": "episode", "ids": ["x"], "model": "m"},
    ):
        with pytest.raises(PermanentJobError):
            handler.run(Job(1, EMBED, "k", cast("dict[str, object]", payload), 0, 1))
    handler.give_up(Job(1, EMBED, "k", {}, 0, 1))


def test_handlers_wait_for_the_program_and_the_model_and_free_memory_when_idle(
    conn: Connection,
) -> None:
    present = {"model": False}
    embedder = DocumentEmbedder(available=False)
    handlers = [
        EmbedHandler(conn, embedder, lambda: present["model"]),
        QueryEmbedHandler(conn, embedder, lambda: present["model"]),
    ]
    assert handlers[0].blocked() == "llama-server not downloaded yet"
    assert handlers[1].blocked() == "no embedding model"
    embedder._available = True
    assert handlers[0].blocked() == "embedding model not downloaded yet"
    present["model"] = True
    assert [h.blocked() for h in handlers] == [None, None]
    assert (EmbedHandler.heavy, QueryEmbedHandler.heavy) == (True, False)
    for h in handlers:
        h.idle()
    assert embedder.unloads == 2


def test_a_question_job_stores_the_vector_where_the_api_will_look(conn: Connection) -> None:
    queue, embedder = JobQueue(conn), DocumentEmbedder()
    queue.enqueue(query_job("what video?", "m", 7), 7)
    job = claimed(queue, EMBED_QUERY)
    assert job.data_ms == 0
    QueryEmbedHandler(conn, embedder, lambda: True, lambda: 9).run(job)
    stored = embeddings.get(conn, "query", embeddings.query_id("what video?", "m"), "m")
    assert stored is not None
    np.testing.assert_allclose(stored, [0.0, 1.0])
    assert embedder.queries == ["what video?"]


def test_asking_twice_queues_two_jobs_but_a_bad_job_is_permanent(conn: Connection) -> None:
    queue = JobQueue(conn)
    assert queue.enqueue(query_job("q", "m", 1), 1)
    assert queue.enqueue(query_job("q", "m", 2), 2)
    handler = QueryEmbedHandler(conn, DocumentEmbedder(), lambda: True)
    for payload in ({}, {"text": 5, "model": "m"}, {"text": "q", "model": "other"}):
        with pytest.raises(PermanentJobError):
            handler.run(Job(1, EMBED_QUERY, "k", cast("dict[str, object]", payload), 0, 1))
    handler.give_up(Job(1, EMBED_QUERY, "k", {}, 0, 1))


# --- the API's side: waiting for a question's vector ---------------------------------------


class Access:
    """What Services.db is: a context manager around the one connection."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def __call__(self) -> AbstractContextManager[Connection]:
        from contextlib import nullcontext  # noqa: PLC0415

        return nullcontext(self.conn)


@pytest.mark.parametrize(
    ("kind", "model", "ready"),
    [("episode", "m", True), ("segment", "m", False), ("episode", "other", False)],
)
def test_semantic_ready_needs_an_episode_vector_or_a_passage_for_the_model(
    conn: Connection, kind: str, model: str, *, ready: bool
) -> None:
    embeddings.put(conn, kind, 1, model, np.array([1.0, 0.0], dtype=np.float32), 1, 1)
    assert semantic_ready(conn, "m") is ready


def test_without_any_vector_there_is_nothing_to_wait_for(conn: Connection) -> None:
    slept: list[float] = []
    assert not semantic_ready(conn, "m")
    assert query_vector(Access(conn), "hello", "m", lambda: 1, sleep=slept.append) is None
    assert slept == []
    assert JobQueue(conn).pending_count() == 0
    assert query_vector(Access(conn), "  ", "m", lambda: 1) is None


def test_a_cached_question_is_answered_at_once(conn: Connection) -> None:
    add_media(conn, "x", LONG_AGO, LONG_AGO + MINUTE)
    embeddings.put(conn, "episode", 1, "m", np.zeros(2, dtype=np.float32), 0, 0)
    embeddings.put(
        conn, "query", embeddings.query_id("hello", "m"), "m", np.ones(2, dtype=np.float32), 0, 0
    )
    found = query_vector(
        Access(conn), "hello", "m", lambda: 1, sleep=lambda _: pytest.fail("slept")
    )
    assert found is not None
    assert JobQueue(conn).pending_count() == 0


def test_the_api_queues_the_question_and_picks_up_the_worker_s_answer(conn: Connection) -> None:
    embeddings.put(conn, "episode", 1, "m", np.zeros(2, dtype=np.float32), 0, 0)
    embedder = DocumentEmbedder()
    handler = QueryEmbedHandler(conn, embedder, lambda: True)
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) == 3:  # the worker gets to it on the third poll
            job = JobQueue(conn).claim([EMBED_QUERY], 10)
            assert job is not None
            handler.run(job)

    found = query_vector(Access(conn), "what video?", "m", lambda: 10, sleep=sleep)
    assert found is not None
    np.testing.assert_allclose(found, [0.0, 1.0])
    assert len(polls) == 3


def test_when_the_worker_does_not_answer_the_api_gives_up(conn: Connection) -> None:
    embeddings.put(conn, "episode", 1, "m", np.zeros(2, dtype=np.float32), 0, 0)
    polls: list[float] = []
    assert query_vector(Access(conn), "q", "m", lambda: 1, wait_s=0.5, sleep=polls.append) is None
    assert len(polls) == 5


# --- housekeeping --------------------------------------------------------------------------


def test_housekeeping_builds_episodes_and_queues_their_embedding(conn: Connection) -> None:
    add_media(conn, "A video", LONG_AGO, LONG_AGO + 10 * MINUTE)
    queue = JobQueue(conn)
    ctx = cast("ChildContext", SimpleNamespace(conn=conn, config=Config()))
    housekeeping(ctx, queue, NOW)
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1
    assert claimed(queue, EMBED).payload["kind"] == "episode"


def test_housekeeping_does_not_queue_embeddings_older_than_their_retention(
    conn: Connection,
) -> None:
    add_media(conn, "Old video", LONG_AGO, LONG_AGO + 10 * MINUTE)
    config = Config(
        retention_days={**Config().retention_days, "embeddings": 1},
        search=SearchSettings("m"),
    )
    queue = JobQueue(conn)
    ctx = cast("ChildContext", SimpleNamespace(conn=conn, config=config))
    housekeeping(ctx, queue, NOW + 3 * 86_400_000)
    assert queue.pending_count([EMBED]) == 0


def test_the_model_folder_check(tmp_path: Path) -> None:
    assert not model_folder_has_gguf(tmp_path / "missing")
    (tmp_path / "m").mkdir()
    assert not model_folder_has_gguf(tmp_path / "m")
    (tmp_path / "m" / "model.gguf").write_bytes(b"")
    assert model_folder_has_gguf(tmp_path / "m")
