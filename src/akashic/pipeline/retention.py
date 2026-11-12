"""The daily retention job: the worker queues one per day, and one per change of settings.

The job key holds the local date and a short fingerprint of the retention settings, so the same
day and the same settings never run twice, while changing a limit (followed by the restart that
applies it) runs a pass right away instead of tomorrow. With every type on ``forever`` (the
default) no job is queued at all. The job is light: it never waits for the scheduler, so it also
runs during games.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, Final

from akashic.pipeline.jobs import NewJob
from akashic.storage.retention import apply_retention

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from sqlite3 import Connection

    from akashic.pipeline.jobs import Job
    from akashic.storage.media import MediaStore

log = logging.getLogger(__name__)

RETENTION: Final = "retention"


def retention_job(retention: Mapping[str, int | None], now_ms: int) -> NewJob | None:
    """Today's job for these limits, or None when nothing is ever deleted."""
    limits = sorted((k, d) for k, d in retention.items() if d is not None)
    if not limits:
        return None
    local_day = datetime.fromtimestamp(now_ms / 1000, UTC).astimezone().strftime("%Y-%m-%d")
    fingerprint = hashlib.sha256(repr(limits).encode()).hexdigest()[:8]
    # data_ms 0: oldest first, so it is not stuck behind a backlog of transcriptions.
    return NewJob(RETENTION, f"{RETENTION}:{local_day}:{fingerprint}", 0)


class RetentionHandler:
    kind: ClassVar[str] = RETENTION
    heavy: ClassVar[bool] = False

    def __init__(
        self,
        conn: Connection,
        media: MediaStore,
        retention: Mapping[str, int | None],
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._conn = conn
        self._media = media
        self._retention = dict(retention)
        self._clock_ms = clock_ms

    def blocked(self) -> str | None:
        return None

    def run(self, job: Job) -> None:
        del job  # the limits come from the configuration, not the payload
        removed = apply_retention(self._conn, self._media, self._retention, self._clock_ms())
        # Counts only: what was deleted is never logged.
        log.info("retention pass done: %s", ", ".join(f"{k}={n}" for k, n in removed.items()))

    def give_up(self, job: Job) -> None:
        del job  # tomorrow's pass tries again

    def idle(self, *, force: bool = False) -> None:
        """Nothing is held in memory."""
