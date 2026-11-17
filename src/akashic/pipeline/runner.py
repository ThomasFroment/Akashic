"""Runs queued jobs, oldest first, one at a time, asking the scheduler before heavy ones.

A handler declares its ``kind`` and whether it is ``heavy`` (GPU or long CPU work). Light kinds
always run; heavy kinds wait while the scheduler says no (a game, a busy GPU) or while the
handler itself is ``blocked`` (e.g. the Whisper model has not been downloaded yet). Waiting jobs
stay queued: nothing is lost, and the backlog and a transcription stall are reported to the
tray. A job outcome the database refused to record is written again at the next step, before
anything else is claimed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar, Final, Literal, Protocol

from akashic.pipeline.llm import LlamaError
from akashic.storage.jobs import DEFER_MS, DeferJob, PermanentJobError

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from akashic.pipeline.scheduler import Scheduler
    from akashic.storage.jobs import Job, JobQueue

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
        """Do the work. Raise PermanentJobError when retrying cannot help, DeferJob when the job
        must wait without counting an attempt; any other exception is retried with backoff."""
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
        # Outcome whose database write failed: job, error type name (None: success), permanent,
        # deferred.
        self._unsettled: tuple[Job, str | None, bool, bool] | None = None
        self.deferred_reason = ""
        # Time transcribe was runnable with jobs pending and nothing completed.
        self._transcribe_stalled_ms = 0
        self._stall_mark_ms = clock_ms()

    def step(self, *, only: Collection[str] | None = None) -> Outcome:
        """Run at most one job, optionally restricted to explicit user requests."""
        if self._unsettled is not None:
            job, error, permanent, deferred = self._unsettled
            self._settle(job, error, permanent=permanent, deferred=deferred)
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
        elapsed = now - self._stall_mark_ms
        self._stall_mark_ms = now
        if "transcribe" in runnable and self._queue.pending_count(["transcribe"]):
            self._transcribe_stalled_ms += elapsed
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
        except DeferJob as exc:
            log.info("job %s deferred: %s", job.kind, exc)
            self._settle(job, None, permanent=False, deferred=True)
        except PermanentJobError as exc:
            log.warning("job %s failed for good: %s", job.kind, exc)
            self._settle(job, type(exc).__name__, permanent=True)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the queue
            detail = str(exc) if isinstance(exc, LlamaError) else type(exc).__name__
            log.warning("job %s failed (%s)", job.kind, detail)
            self._settle(job, type(exc).__name__, permanent=False)
        else:
            self._settle(job, None, permanent=False)

    def _settle(
        self, job: Job, error: str | None, *, permanent: bool, deferred: bool = False
    ) -> None:
        """Record the outcome. If the database refuses, it is retried at the next step."""
        self._unsettled = (job, error, permanent, deferred)
        if deferred:
            self._queue.defer(job, self._clock_ms(), DEFER_MS)
            self._unsettled = None
            return
        if error is None:
            completed_ms = self._clock_ms()
            self._queue.complete(job.id, completed_ms)
            self._unsettled = None
            self._transcribe_stalled_ms = 0
            self._stall_mark_ms = completed_ms
            return
        retry = self._queue.fail(job, error, self._clock_ms(), permanent=permanent)
        self._unsettled = None
        log.warning("job %s %s", job.kind, "will retry" if retry else "giving up")
        if not retry:
            self._handlers[job.kind].give_up(job)

    def release_interrupted(self) -> None:
        """At worker start: give up the jobs that were interrupted once too often."""
        for job in self._queue.release_running(self._clock_ms()):
            log.warning("job %s %d interrupted %d times, giving up", job.kind, job.id, job.attempts)
            handler = self._handlers.get(job.kind)
            if handler is not None:
                handler.give_up(job)

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
            "transcribe_stalled_ms": self._transcribe_stalled_ms,
        }

    def backlog_active(self) -> bool:
        """True while many jobs are waiting (with hysteresis, so the tray doesn't flicker)."""
        pending = self._queue.pending_count()
        if self._in_backlog:
            self._in_backlog = pending > BACKLOG_OFF
        else:
            self._in_backlog = pending >= BACKLOG_ON
        return self._in_backlog
