"""UI Automation reads on worker threads, one worker per process.

A stalled process (a hung app, a browser busy loading) makes every UIA call into it, and into its
sibling windows, wait out the 1000 ms connection timeout. Measured (docs/baselines/
2026-11-17-uia-under-load.md): stalls cost 1973-2026 ms per check, siblings stall with their
process, 0 of 1128 warm reads took over 50 ms, and reads serialize across processes, so the screen
loop waits on one read at a time. A worker per pid keeps a stalled process from delaying the
others, and the screen loop waits only a short time for a read: ``WAIT_S`` (50 ms) for a caption
or an address, the 0 of 1128 warm reads that took longer; ``TEXT_WAIT_S`` (1.2 s) for a window's
text, whose walk took Chrome up to 996 ms and Zen up to 434 ms on a 30k-character page (docs/
baselines/2026-11-17-large-page-text.md) and is cut off by the 2 s UIA transaction timeout.
At ``MAX_WORKERS`` a new pid takes the place of the idle worker read least recently.

Only the screen-loop thread asks for reads, one at a time. A worker that is still busy with an
earlier call answers at once with an unfinished read (``UiaRead(None, None)``); nothing is queued.
A read that misses the wait keeps running and its result is kept under (kind, hwnd): the next read
of that kind for that window gets it once, without a new call.

Each worker builds its own ``TextSource`` on its own thread, so no COM pointer crosses threads. A
worker whose ``new_uia`` raised stays broken (every read is unfinished) until its process has no
window left or its worker is replaced at the cap.
Fails closed: an unfinished read is never an answer, callers treat it as unknown.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from akashic.capture.ports import BrowserFamily, TextBlock, TextSource, WindowInfo
    from akashic.log import FaultLog

WAIT_S: Final = 0.05  # caption and address reads
TEXT_WAIT_S: Final = 1.2  # text walks: above the measured 996 ms, below the 2 s UIA timeout
MAX_WORKERS: Final = 16
SHARED_PID: Final = 0  # windows whose process is unknown share one worker
_END: Final = object()


@dataclass(frozen=True, slots=True)
class UiaRead[T]:
    """One read: ``started_at`` is None while it is unfinished; ``ms`` is the call's time."""

    value: T | None
    started_at: float | None
    ms: float = 0.0

    def started_since(self, t: float) -> float | None:
        """When its call started, if it is finished and started at or after ``t``."""
        if self.started_at is None or self.started_at < t:
            return None
        return self.started_at


@dataclass(slots=True)
class _Job:
    kind: str
    hwnd: int
    call: Callable[[TextSource], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: UiaRead[Any] | None = None
    abandoned: bool = False


class _Worker:
    def __init__(
        self, new_uia: Callable[[], TextSource], faults: FaultLog, clock: Callable[[], float]
    ) -> None:
        self._new_uia = new_uia
        self._faults = faults
        self._clock = clock
        self._jobs: queue.SimpleQueue[_Job | object] = queue.SimpleQueue()
        self._lock = threading.Lock()  # guards the three below and every job's result/abandoned
        self._busy = False
        self._broken = False
        self._late: dict[tuple[str, int], UiaRead[Any]] = {}
        threading.Thread(target=self._run, name="uia", daemon=True).start()

    def read[T](
        self, kind: str, hwnd: int, wait_s: float, call: Callable[[TextSource], T | None]
    ) -> UiaRead[T]:
        with self._lock:
            if self._broken:
                return UiaRead(None, None)
            late = self._late.pop((kind, hwnd), None)
            if late is not None:
                return late
            if self._busy:
                return UiaRead(None, None)
            self._busy = True
        job = _Job(kind, hwnd, call)
        self._jobs.put(job)
        job.done.wait(wait_s)
        with self._lock:
            if job.result is None:
                job.abandoned = True
                return UiaRead(None, None)
            return job.result

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def end(self) -> None:
        self._jobs.put(_END)

    def _run(self) -> None:
        try:
            uia = self._new_uia()
        except Exception as exc:  # noqa: BLE001 - any failure leaves this worker unusable
            self._faults.note("uia worker", exc)
            with self._lock:
                self._broken = True
                self._busy = False  # its first read set it; only an idle worker can be evicted
            return
        while (job := self._jobs.get()) is not _END:
            if isinstance(job, _Job):
                self._execute(uia, job)

    def _execute(self, uia: TextSource, job: _Job) -> None:
        started_at = self._clock()
        began = time.perf_counter()
        try:
            value = job.call(uia)
        except Exception as exc:  # noqa: BLE001 - a failed read is an answer of None
            self._faults.note("uia worker", exc)
            value = None
        read = UiaRead(value, started_at, (time.perf_counter() - began) * 1000)
        with self._lock:
            self._busy = False
            if job.abandoned:
                self._late[job.kind, job.hwnd] = read
            else:
                job.result = read
        job.done.set()


class ThreadedUia:
    def __init__(
        self, new_uia: Callable[[], TextSource], faults: FaultLog, clock: Callable[[], float]
    ) -> None:
        self._new_uia = new_uia
        self._faults = faults
        self._clock = clock
        self._workers: dict[int, _Worker] = {}

    def caption(self, window: WindowInfo, family: BrowserFamily) -> UiaRead[str]:
        return self._read(
            "caption", window, WAIT_S, lambda uia: uia.browser_caption(window, family)
        )

    def address(self, window: WindowInfo, family: BrowserFamily) -> UiaRead[str]:
        return self._read("address", window, WAIT_S, lambda uia: uia.read_url(window, family))

    def text(self, window: WindowInfo, max_nodes: int) -> UiaRead[Sequence[TextBlock]]:
        return self._read("text", window, TEXT_WAIT_S, lambda uia: uia.read_text(window, max_nodes))

    def retire(self, live_pids: Collection[int]) -> None:
        """End the workers of processes that have no window left."""
        for pid in [p for p in self._workers if p != SHARED_PID and p not in live_pids]:
            self._workers.pop(pid).end()

    def close(self) -> None:
        """End every worker without joining: one stuck in a COM call must not hold up the stop."""
        for worker in self._workers.values():
            worker.end()
        self._workers.clear()

    def _read[T](
        self, kind: str, window: WindowInfo, wait_s: float, call: Callable[[TextSource], T | None]
    ) -> UiaRead[T]:
        worker = self._worker(window.pid)
        if worker is None:
            return UiaRead(None, None)
        return worker.read(kind, window.hwnd, wait_s, call)

    def _worker(self, pid: int) -> _Worker | None:
        """The pid's worker, now the most recently read; at the cap the idle worker read least
        recently is ended for a new pid, and None when all are busy."""
        worker = self._workers.pop(pid, None)
        if worker is None:
            if len(self._workers) >= MAX_WORKERS and not self._end_least_recent_idle():
                self._faults.note("uia worker", "cap")
                return None
            worker = _Worker(self._new_uia, self._faults, self._clock)
        self._workers[pid] = worker
        return worker

    def _end_least_recent_idle(self) -> bool:
        for pid, worker in self._workers.items():
            if not worker.busy:
                del self._workers[pid]
                worker.end()
                return True
        return False
