"""The persistent job queue: a SQLite table every heavy task goes through.

Properties the rest of the app relies on (AGENTS.md, "Pipeline and GPU scheduling"):

- Durable: jobs survive restarts; a job left ``running`` by a crash goes back to ``pending``.
- Idempotent: ``key`` is unique, so enqueueing the same work twice does nothing.
- Oldest first: ordered by ``data_ms``, the capture time of the data, not by when it was queued.
- Retried with exponential backoff, then marked ``failed`` (kept, so the failure is visible).

Payloads hold ids only, never captured content, and ``error`` holds an exception type name only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from akashic.jsonish import as_dict
from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Collection
    from sqlite3 import Connection

MAX_ATTEMPTS: Final = 5
BACKOFF_BASE_MS: Final = 30_000
BACKOFF_FACTOR: Final = 4
BACKOFF_MAX_MS: Final = 3_600_000


class PermanentJobError(Exception):
    """Raised by a handler when retrying cannot help (e.g. the audio file is gone)."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    key: str
    payload: dict[str, object]
    data_ms: int
    attempts: int  # including the run that was just claimed


@dataclass(frozen=True, slots=True)
class NewJob:
    kind: str
    key: str  # unique: the same key enqueued twice is one job
    data_ms: int  # capture time of the data, so the oldest is processed first
    payload: dict[str, object] = field(default_factory=dict[str, object])


def enqueue_in(conn: Connection, job: NewJob, now_ms: int) -> bool:
    """Enqueue inside the caller's open transaction. False when the key already exists."""
    cursor = conn.execute(
        "INSERT INTO jobs (kind, key, payload, data_ms, created_ms) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (key) DO NOTHING",
        (job.kind, job.key, json.dumps(job.payload, separators=(",", ":")), job.data_ms, now_ms),
    )
    return cursor.rowcount == 1


def _backoff_ms(attempts: int) -> int:
    return min(BACKOFF_BASE_MS * BACKOFF_FACTOR ** max(attempts - 1, 0), BACKOFF_MAX_MS)


class JobQueue:
    def __init__(self, conn: Connection, max_attempts: int = MAX_ATTEMPTS) -> None:
        self._conn = conn
        self._max_attempts = max_attempts

    def enqueue(self, job: NewJob, now_ms: int) -> bool:
        with db.transaction(self._conn) as conn:
            return enqueue_in(conn, job, now_ms)

    def claim(self, kinds: Collection[str], now_ms: int) -> Job | None:
        """Take the oldest runnable job of one of ``kinds`` and mark it running."""
        if not kinds:
            return None
        marks = ",".join("?" * len(kinds))
        with db.transaction(self._conn) as conn:
            row: tuple[int, str, str, str, int, int] | None = conn.execute(
                f"SELECT id, kind, key, payload, data_ms, attempts FROM jobs"  # noqa: S608 - marks are "?"
                f" WHERE state = 'pending' AND run_after_ms <= ? AND kind IN ({marks})"
                f" ORDER BY data_ms, id LIMIT 1",
                (now_ms, *kinds),
            ).fetchone()
            if row is None:
                return None
            job_id, kind, key, payload, data_ms, attempts = row
            conn.execute(
                "UPDATE jobs SET state = 'running', attempts = attempts + 1 WHERE id = ?",
                (job_id,),
            )
        return Job(job_id, kind, key, as_dict(json.loads(payload)) or {}, data_ms, attempts + 1)

    def complete(self, job_id: int, now_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "UPDATE jobs SET state = 'done', finished_ms = ?, error = NULL WHERE id = ?",
                (now_ms, job_id),
            )

    def fail(self, job: Job, error: str, now_ms: int, *, permanent: bool = False) -> bool:
        """Record a failed run. True when it will be retried, False when it gave up."""
        retry = not permanent and job.attempts < self._max_attempts
        with db.transaction(self._conn) as conn:
            if retry:
                conn.execute(
                    "UPDATE jobs SET state = 'pending', run_after_ms = ?, error = ? WHERE id = ?",
                    (now_ms + _backoff_ms(job.attempts), error, job.id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET state = 'failed', finished_ms = ?, error = ? WHERE id = ?",
                    (now_ms, error, job.id),
                )
        return retry

    def release_running(self) -> int:
        """Crash recovery: jobs a dead worker was running become pending again. Call at
        worker start, when no other worker can be running."""
        with db.transaction(self._conn) as conn:
            return conn.execute(
                "UPDATE jobs SET state = 'pending' WHERE state = 'running'"
            ).rowcount

    def pending_count(self, kinds: Collection[str] | None = None) -> int:
        """Jobs waiting to run (including ones waiting out a retry delay)."""
        if kinds is None:
            row: tuple[int] = self._conn.execute(
                "SELECT count(*) FROM jobs WHERE state = 'pending'"
            ).fetchone()
            return row[0]
        marks = ",".join("?" * len(kinds))
        row = self._conn.execute(
            f"SELECT count(*) FROM jobs WHERE state = 'pending' AND kind IN ({marks})",  # noqa: S608
            tuple(kinds),
        ).fetchone()
        return row[0]

    def purge_done(self, finished_before_ms: int) -> int:
        with db.transaction(self._conn) as conn:
            return conn.execute(
                "DELETE FROM jobs WHERE state = 'done' AND finished_ms < ?", (finished_before_ms,)
            ).rowcount
