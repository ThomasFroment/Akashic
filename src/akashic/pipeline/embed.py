"""Embedding jobs: vectors for episodes, transcripts, screen text and search questions.

- ``embed``: a batch of episodes, transcripts or screen text missing vectors for the current model.
  Heavy (it wakes the GPU), so games and busy GPUs hold it back. Queued by housekeeping, a batch at
  a time, only while no earlier batch is waiting.
- ``embed_query``: the vector of one search question, asked for by the API process, which has no
  model of its own (the worker owns llama-server). Light, and queued at the front, so a question
  is answered even while the backlog is large or a game is running.

Vectors record the model that made them; changing ``[search] embedding_model`` simply starts a new
set, and the old one is no longer read.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, ClassVar, Final, Protocol

from akashic.pipeline.jobs import NewJob, PermanentJobError
from akashic.storage import embeddings, passages

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from sqlite3 import Connection

    import numpy as np
    from numpy.typing import NDArray

    from akashic.pipeline.jobs import Job, JobQueue

    type Vector = NDArray[np.float32]

log = logging.getLogger(__name__)

EMBED: Final = "embed"
EMBED_QUERY: Final = "embed_query"
BATCH: Final = 32
MIN_TEXT_CHARS: Final = 12  # shorter utterances/fragments retain keyword search only


class Embedder(Protocol):
    model: str

    def available(self) -> bool: ...

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]: ...

    def embed_query(self, text: str) -> Vector: ...

    def unload_if_idle(self, *, force: bool = False) -> None: ...


def episode_text(title: str, body: str) -> str:
    """What an episode's vector is made from: its title and the digest of its evidence."""
    return f"{title}\n{body}"


def enqueue_missing(  # noqa: PLR0913 - the queue, the model and the retention cut-off
    conn: Connection,
    queue: JobQueue,
    model: str,
    now_ms: int,
    *,
    since_ms: int = 0,
    batch: int = BATCH,
) -> bool:
    """Queue one batch of things without a vector. False when nothing is missing or an earlier
    batch is still waiting."""
    if conn.execute(
        "SELECT 1 FROM jobs WHERE kind = ? AND state IN ('pending', 'running') LIMIT 1", (EMBED,)
    ).fetchone():
        return False
    for kind, sql in (
        ("episode", _MISSING_EPISODES),
        ("segment", _MISSING_SEGMENTS),
        ("frame", _MISSING_FRAMES),
    ):
        rows = conn.execute(
            sql, {"model": model, "since": since_ms, "limit": batch, "min": MIN_TEXT_CHARS}
        ).fetchall()
        if rows:
            ids = [int(r[0]) for r in rows]
            job = NewJob(
                EMBED,
                f"{EMBED}:{model}:{kind}:{ids[0]}-{ids[-1]}:{len(ids)}:{now_ms}",
                int(rows[0][1]),
                {"kind": kind, "ids": ids, "model": model},
            )
            return queue.enqueue(job, now_ms)
    return False


_MISSING_EPISODES: Final = (
    "SELECT e.id, e.start_ms FROM episodes e WHERE e.start_ms >= :since"
    " AND NOT EXISTS (SELECT 1 FROM embeddings v WHERE v.owner_kind = 'episode'"
    "                 AND v.owner_id = e.id AND v.model = :model)"
    " ORDER BY e.start_ms, e.id LIMIT :limit"
)
_MISSING_SEGMENTS: Final = (
    "SELECT s.id, s.start_ms FROM audio_segments s WHERE s.start_ms >= :since"
    " AND s.status = 'done' AND length(s.transcript) >= :min"
    " AND NOT EXISTS (SELECT 1 FROM segment_passages p"
    "                 WHERE p.segment_id = s.id AND p.model = :model)"
    " ORDER BY s.start_ms, s.id LIMIT :limit"
)
_MISSING_FRAMES: Final = (
    "SELECT f.id, f.ts_ms FROM frames f WHERE f.ts_ms >= :since AND length(f.text) >= :min"
    " AND NOT EXISTS (SELECT 1 FROM frame_passages p"
    "                 WHERE p.frame_id = f.id AND p.model = :model)"
    " ORDER BY f.ts_ms, f.id LIMIT :limit"
)


def _ids(job: Job) -> list[int]:
    raw = job.payload.get("ids")
    if not isinstance(raw, list) or not all(isinstance(i, int) for i in raw):  # pyright: ignore[reportUnknownVariableType]
        msg = "embed job without ids"
        raise PermanentJobError(msg)
    return [int(i) for i in raw]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


class EmbedHandler:
    kind: ClassVar[str] = EMBED
    heavy: ClassVar[bool] = True

    def __init__(
        self,
        conn: Connection,
        embedder: Embedder,
        model_present: Callable[[], bool],
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._model_present = model_present
        self._clock_ms = clock_ms

    def blocked(self) -> str | None:
        if not self._embedder.available():
            return "llama-server not downloaded yet"
        if not self._model_present():
            return "embedding model not downloaded yet"
        return None

    def run(self, job: Job) -> None:
        kind = str(job.payload.get("kind"))
        model = self._embedder.model
        if kind not in {"episode", "segment", "frame"} or job.payload.get("model") != model:
            return  # queued for another model (the setting changed): nothing to do
        ids = _ids(job)
        marks = ",".join("?" * len(ids))
        if kind == "episode":
            rows = self._conn.execute(
                f"SELECT id, start_ms, title, body FROM episodes WHERE id IN ({marks})",  # noqa: S608
                ids,
            ).fetchall()
            texts = {int(r[0]): (int(r[1]), episode_text(r[2], r[3])) for r in rows}
        elif kind == "segment":
            rows = self._conn.execute(
                f"SELECT id, start_ms, transcript FROM audio_segments WHERE id IN ({marks})"  # noqa: S608
                " AND transcript IS NOT NULL",
                ids,
            ).fetchall()
            texts = {int(r[0]): (int(r[1]), str(r[2])) for r in rows}
        else:
            rows = self._conn.execute(
                f"SELECT id, ts_ms, text FROM frames WHERE id IN ({marks})"  # noqa: S608
                " AND text IS NOT NULL",
                ids,
            ).fetchall()
            texts = {int(r[0]): (int(r[1]), str(r[2])) for r in rows}
        todo = [i for i in ids if i in texts]
        if not todo:
            return
        if kind in {"segment", "frame"}:
            offsets = {i: passages.windows(texts[i][1]) for i in todo}
            vectors = self._embedder.embed_documents(
                [texts[i][1][start:end] for i in todo for start, end in offsets[i]]
            )
            at = 0
            for owner_id in todo:
                _, text = texts[owner_id]
                end = at + len(offsets[owner_id])
                # No partial set becomes searchable if inference fails or source text changes.
                passages.publish(
                    self._conn,
                    owner_id,
                    model,
                    text,
                    vectors[at:end],
                    kind="frame" if kind == "frame" else "segment",
                )
                at = end
            return
        vectors = self._embedder.embed_documents([texts[i][1] for i in todo])
        now = self._clock_ms()
        for owner_id, vector in zip(todo, vectors, strict=True):
            embeddings.put(
                self._conn,
                kind,
                owner_id,
                model,
                vector,
                texts[owner_id][0],
                now,
                expected_episode_text=texts[owner_id][1],
            )
        log.info("embedded %d %s(s)", len(todo), kind)

    def give_up(self, job: Job) -> None:
        """Housekeeping queues the same things again later."""

    def idle(self, *, force: bool = False) -> None:
        self._embedder.unload_if_idle(force=force)


class QueryEmbedHandler:
    kind: ClassVar[str] = EMBED_QUERY
    heavy: ClassVar[bool] = False

    def __init__(
        self,
        conn: Connection,
        embedder: Embedder,
        model_present: Callable[[], bool],
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._conn = conn
        self._embedder = embedder
        self._model_present = model_present
        self._clock_ms = clock_ms

    def blocked(self) -> str | None:
        return (
            None if self._embedder.available() and self._model_present() else "no embedding model"
        )

    def run(self, job: Job) -> None:
        text = job.payload.get("text")
        if not isinstance(text, str) or job.payload.get("model") != self._embedder.model:
            msg = "bad embed_query job"
            raise PermanentJobError(msg)
        owner_id = embeddings.query_id(text, self._embedder.model)
        now = self._clock_ms()
        embeddings.put(
            self._conn,
            "query",
            owner_id,
            self._embedder.model,
            self._embedder.embed_query(text),
            now,
            now,
        )

    def give_up(self, job: Job) -> None:
        """The API stops waiting on its own and falls back to keywords."""

    def idle(self, *, force: bool = False) -> None:
        self._embedder.unload_if_idle(force=force)


def query_job(text: str, model: str, now_ms: int) -> NewJob:
    """The job asking for a question's vector. ``data_ms`` 0 puts it first in the queue; the key
    includes the time so asking again later is a new request, not a no-op."""
    return NewJob(
        EMBED_QUERY,
        f"{EMBED_QUERY}:{now_ms}:{embeddings.query_id(text, model)}",
        0,
        {"text": text, "model": model},
    )
