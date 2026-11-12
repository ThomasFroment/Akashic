"""Runs queued jobs, oldest first, one at a time, asking the scheduler before heavy ones.

A handler declares its ``kind`` and whether it is ``heavy`` (GPU or long CPU work). Light kinds
always run; heavy kinds wait while the scheduler says no (a game, a busy GPU) or while the
handler itself is ``blocked`` (e.g. the Whisper model has not been downloaded yet). Waiting jobs
stay queued: nothing is lost, and the backlog is reported to the tray.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Protocol

from akashic.pipeline.jobs import PermanentJobError
from akashic.pipeline.llm import LlamaError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from akashic.pipeline.jobs import Job, JobQueue
    from akashic.pipeline.scheduler import Scheduler

log = logging.getLogger(__name__)

# Pending jobs at which the tray starts saying "catching up", and the level it stops at.
BACKLOG_ON: Final = 10
BACKLOG_OFF: Final = 2

type Outcome = Literal["ran", "idle", "deferred"]


class Handler(Protocol):
    kind: ClassVar[str]
    heavy: ClassVar[bool]

    def blocked(self) -> str | None:
        """Why this handler can't run right now ("model missing"), or None."""
        ...

    def run(self, job: Job) -> None:
        """Do the work. Raise PermanentJobError when retrying cannot help; any other exception
        is retried with backoff."""
        ...

    def give_up(self, job: Job) -> None:
        """Called once when a job has failed for good."""
        ...

    def idle(self, *, force: bool = False) -> None:
        """Called when there is nothing to do: a chance to free memory."""
        ...


class JobRunner:
    def __init__(
        self,
        queue: JobQueue,
        scheduler: Scheduler,
        handlers: list[Handler],
        clock_ms: Callable[[], int],
    ) -> None:
        self._queue = queue
        self._scheduler = scheduler
        self._handlers = {h.kind: h for h in handlers}
        self._clock_ms = clock_ms
        self._in_backlog = False
        self.deferred_reason = ""

    def step(self, *, only: Collection[str] | None = None) -> Outcome:
        """Run at most one job, optionally restricted to explicit user requests."""
        now = self._clock_ms()
        runnable: list[str] = []
        waiting: list[str] = []
        reason = ""
        allowed = None
        for kind, handler in self._handlers.items():
            if only is not None and kind not in only:
                continue
            blocked = handler.blocked()
            if not blocked and handler.heavy:
                allowed = allowed or self._scheduler.heavy_allowed(now)
                blocked = None if allowed.allowed else allowed.reason
            if blocked:
                waiting.append(kind)
                reason = reason or blocked
            else:
                runnable.append(kind)
        job = self._queue.claim(runnable, now)
        if job is None:
            self.deferred_reason = reason if self._queue.pending_count(waiting) else ""
            return "deferred" if self.deferred_reason else "idle"
        self.deferred_reason = ""
        self._run(job)
        return "ran"

    def _run(self, job: Job) -> None:
        handler = self._handlers[job.kind]
        try:
            # Serial jobs alone do not prevent simultaneously resident GPU models.
            release = (
                ("ask", "embed", "embed_query", "transcribe")
                if job.kind == "stt_compare"
                else ("ask", "embed", "embed_query")
                if job.kind == "transcribe"
                else ("transcribe",)
                if job.kind in {"ask", "embed", "embed_query"}
                else ()
            )
            for kind in release:
                if kind in self._handlers:
                    self._handlers[kind].idle(force=True)
            handler.run(job)
        except PermanentJobError as exc:
            log.warning("job %s failed for good: %s", job.kind, exc)
            self._queue.fail(job, type(exc).__name__, self._clock_ms(), permanent=True)
            handler.give_up(job)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the queue
            retry = self._queue.fail(job, type(exc).__name__, self._clock_ms())
            detail = str(exc) if isinstance(exc, LlamaError) else type(exc).__name__
            log.warning("job %s failed (%s), %s", job.kind, detail,
                        "will retry" if retry else "giving up")  # fmt: skip
            if not retry:
                handler.give_up(job)
        else:
            self._queue.complete(job.id, self._clock_ms())

    def idle(self, *, force: bool = False) -> None:
        for handler in self._handlers.values():
            handler.idle(force=force)

    def status(self) -> dict[str, object]:
        """Why jobs are (not) running, for the Debug page: numbers and reasons, no content."""
        verdict = self._scheduler.last
        return {
            "deferred_reason": self.deferred_reason,
            "blocked": {k: h.blocked() for k, h in self._handlers.items() if h.blocked()},
            "scheduler": None
            if verdict is None
            else {
                "allowed": verdict.allowed,
                "reason": verdict.reason,
                "cpu": None if verdict.cpu is None else round(verdict.cpu, 1),
                "gpu": None if verdict.gpu is None else round(verdict.gpu, 1),
            },
            "limits": self._scheduler.limits(),
        }

    def backlog_active(self) -> bool:
        """True while many jobs are waiting (with hysteresis, so the tray doesn't flicker)."""
        pending = self._queue.pending_count()
        if self._in_backlog:
            self._in_backlog = pending > BACKLOG_OFF
        else:
            self._in_backlog = pending >= BACKLOG_ON
        return self._in_backlog
