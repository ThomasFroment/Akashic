"""Records when capture is not recording, and why (``capture_gaps``).

Capture threads only report facts to a ``GapSink``: a state that began (``open``), ended
(``close``), or a finished span they threw away (``drop``). ``CaptureGaps`` queues them and a
thread of its own writes them, so a busy database can never hold capture up. Events keep their
original times and their order. Nothing is written before the previous run has been settled
(``GapLog.recover``), so leftover rows are closed before any new row exists.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from akashic.log import FaultLog
from akashic.storage import db
from akashic.storage.gaps import HEARTBEAT_MS

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.storage.gaps import GapKind, GapLog, GapReason

log = logging.getLogger(__name__)

TICK_S: Final = 1.0
STALL_MS: Final = 30_000  # a tick this late means the process was asleep or frozen
MERGE_WITHIN_MS: Final = 30_000
MAX_PENDING_DROPS: Final = 10_000
TICK_FLUSH_S: Final = 0.5  # keeps the pause and lock watch timely after a long backlog
STOP_FLUSH_S: Final = 1.0
STOP_RETRY_S: Final = 0.05  # between attempts to write the stop
STOP_BUSY_TIMEOUT_MS: Final = 250  # capture's stop must end before the supervisor's 10 s kill


def limit_wait_at_stop(conn: Connection) -> None:
    """Shorten the busy wait of ``conn`` to ``STOP_BUSY_TIMEOUT_MS``. Call it from the thread that
    uses ``conn``. A failure leaves the full busy timeout in place."""
    try:
        db.limit_wait(conn, STOP_BUSY_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 - the stop then waits the full busy timeout
        log.warning("shortening the busy wait at stop failed: %s", type(exc).__name__)


class GapSink(Protocol):
    def open(self, kind: GapKind, reason: GapReason, ts_ms: int) -> None: ...

    def close(self, kind: GapKind, ts_ms: int) -> None: ...

    def drop(
        self, kind: GapKind, reason: GapReason, start_ms: int, end_ms: int, *, merge: bool = False
    ) -> None: ...


class _NoGaps:
    def open(self, kind: GapKind, reason: GapReason, ts_ms: int) -> None:
        pass

    def close(self, kind: GapKind, ts_ms: int) -> None:
        pass

    def drop(
        self, kind: GapKind, reason: GapReason, start_ms: int, end_ms: int, *, merge: bool = False
    ) -> None:
        pass


NO_GAPS: Final[GapSink] = _NoGaps()


@dataclass(frozen=True, slots=True)
class _Open:
    kind: GapKind
    reason: GapReason
    ts_ms: int


@dataclass(frozen=True, slots=True)
class _Close:
    kind: GapKind
    ts_ms: int


@dataclass(frozen=True, slots=True)
class _Span:
    """A finished gap. ``bounded`` spans count against the pending limit."""

    kind: GapKind
    reason: GapReason
    start_ms: int
    end_ms: int
    merge: bool
    bounded: bool


type _Event = _Open | _Close | _Span


@dataclass(slots=True)
class _Row:
    gap_id: int
    end_ms: int


class CaptureGaps:
    def __init__(self, gap_log: GapLog, now_ms: Callable[[], int], start_ms: int) -> None:
        self._log = gap_log
        self._now_ms = now_ms
        self._start_ms = start_ms
        self.stats: Counter[str] = Counter()
        self._faults = FaultLog(log)
        self._lock = threading.Lock()
        self._events: deque[_Event] = deque()
        self._wanted: dict[GapKind, GapReason] = {}
        self._pending_spans = 0
        self._after_lost_drop: set[tuple[GapKind, GapReason]] = set()
        self._stopping = False
        self._stop_written = False
        # Used only by whoever flushes: the gap thread, then the caller after it has returned.
        self._recovered = False
        self._open_rows: dict[GapKind, int] = {}
        self._last_span: dict[tuple[GapKind, GapReason], _Row] = {}
        self._last_tick_ms: int | None = None
        self._last_beat_ms = 0

    def open(self, kind: GapKind, reason: GapReason, ts_ms: int) -> None:
        with self._lock:
            if self._stopping or self._wanted.get(kind) == reason:
                return
            if kind in self._wanted:
                self._events.append(_Close(kind, ts_ms))
            self._wanted[kind] = reason
            self._events.append(_Open(kind, reason, ts_ms))

    def close(self, kind: GapKind, ts_ms: int) -> None:
        with self._lock:
            if self._stopping or kind not in self._wanted:
                return
            del self._wanted[kind]
            self._events.append(_Close(kind, ts_ms))

    def drop(
        self, kind: GapKind, reason: GapReason, start_ms: int, end_ms: int, *, merge: bool = False
    ) -> None:
        key = (kind, reason)
        with self._lock:
            if self._pending_spans >= MAX_PENDING_DROPS:
                self.stats["drops_overflowed"] += 1
                self._after_lost_drop.add(key)
                return
            if key in self._after_lost_drop:
                self._after_lost_drop.discard(key)
                merge = False  # the drop it would continue never reached the database
            self._pending_spans += 1
            self._events.append(_Span(kind, reason, start_ms, end_ms, merge, bounded=True))

    def stats_snapshot(self) -> dict[str, int]:
        """Gap and fault counters merged into a copy."""
        return dict(self.stats) | self._faults.counts()

    def run(
        self,
        stop: threading.Event,
        paused: threading.Event,
        halted: threading.Event,
        locked: Callable[[], bool],
    ) -> None:
        while not stop.is_set():
            try:
                self._tick(paused, halted, locked)
            except Exception as exc:  # noqa: BLE001 - gap bookkeeping must never stop capture
                self.stats["tick_failed"] += 1
                self._faults.note("capture gap tick", exc)
            stop.wait(TICK_S)

    def finish(self, stop_ms: int, until: float) -> None:
        """Write the clean stop. Call after ``run`` has returned. A busy database is retried until
        ``until`` (a ``time.monotonic`` time); then the events still unwritten are dropped and no
        stop is written, so the next start reads the heartbeat and records a crash instead."""
        with self._lock:
            self._stopping = True  # recorder threads still running can no longer open or close rows
        limit_wait_at_stop(self._log.conn)
        while not self._try_stop(stop_ms, until):
            if time.monotonic() >= until:
                self._drop_pending()
                return
            time.sleep(STOP_RETRY_S)

    def _try_stop(self, stop_ms: int, until: float) -> bool:
        flush_s = min(STOP_FLUSH_S, max(0.0, until - time.monotonic()))
        if not self._settle() or not self._flush(flush_s) or self._has_pending():
            return False
        try:
            self._log.stop(stop_ms)
        except Exception as exc:
            if not db.is_busy(exc):
                raise
            self._faults.note("writing the capture stop (busy database)", exc)
            return False
        with self._lock:
            self._stop_written = True
        return True

    def _drop_pending(self) -> None:
        with self._lock:
            dropped = len(self._events)
            self._events.clear()
            self._pending_spans = 0
        self.stats["stop_dropped"] += dropped
        log.warning("capture stop not written; dropped %d pending gap events", dropped)

    def flush_late(self, until: float) -> None:
        """After ``finish``, write until ``until`` (a ``time.monotonic`` time) the drops reported
        while the audio writer drained. Only drops can be queued by then, because ``finish``
        stopped accepting transitions."""
        with self._lock:
            stop_written = self._stop_written
        if stop_written:
            self._flush(min(STOP_FLUSH_S, max(0.0, until - time.monotonic())))

    def _tick(
        self, paused: threading.Event, halted: threading.Event, locked: Callable[[], bool]
    ) -> None:
        now = self._now_ms()
        self._watch(
            now, paused=paused.is_set(), halted=halted.is_set(), locked=self._is_locked(locked)
        )
        if not self._settle():
            return
        if self._flush(TICK_FLUSH_S):
            self._beat(now)

    def _watch(self, now: int, *, paused: bool, halted: bool, locked: bool) -> None:
        previous, self._last_tick_ms = self._last_tick_ms, now
        if previous is not None and now - previous > STALL_MS:
            with self._lock:
                self._events.append(
                    _Span("all", "stalled", previous, now, merge=False, bounded=False)
                )
        if paused:
            self.open("all", "paused", now)
        elif halted:
            self.open("all", "ui_open", now)
        else:
            self.close("all", now)
        self._set("screen", "locked", wanted=locked, ts_ms=now)

    def _set(self, kind: GapKind, reason: GapReason, *, wanted: bool, ts_ms: int) -> None:
        if wanted:
            self.open(kind, reason, ts_ms)
        else:
            self.close(kind, ts_ms)

    def _is_locked(self, locked: Callable[[], bool]) -> bool:
        try:
            return locked()
        except Exception as exc:  # noqa: BLE001 - keep the last known state
            self._faults.note("reading the lock state", exc)
            with self._lock:
                return self._wanted.get("screen") == "locked"

    def _settle(self) -> bool:
        if not self._recovered:
            try:
                self._log.recover(self._start_ms)
            except Exception as exc:  # retried next tick or by finish; at stop others raise
                if self._stopping and not db.is_busy(exc):
                    raise
                self.stats["recovery_failed"] += 1
                self._faults.note("settling the previous capture run", exc)
                return False
            self._recovered = True
        return True

    def _beat(self, now: int) -> None:
        if abs(now - self._last_beat_ms) < HEARTBEAT_MS:
            return
        try:
            self._log.heartbeat(now)
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            self.stats["heartbeat_failed"] += 1
            self._faults.note("writing the capture heartbeat", exc)
            return
        self._last_beat_ms = now

    def _has_pending(self) -> bool:
        with self._lock:
            return bool(self._events)

    def _flush(self, budget_s: float) -> bool:
        """Write queued events in order for at most ``budget_s``, so the watch stays timely and
        the stop stays short; the rest waits for the next pass. False when a busy database
        stopped the pass."""
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            with self._lock:
                if not self._events:
                    return True
                event = self._events[0]
            try:
                self._write(event)
            except Exception as exc:  # noqa: BLE001 - one bad event must not block the queue
                if db.is_busy(exc):
                    self.stats["write_busy"] += 1
                    self._faults.note("writing a capture gap (busy database)", exc)
                    return False
                self.stats["write_rejected"] += 1
                self._faults.note("writing a capture gap", exc)
                if isinstance(event, _Span):
                    self._last_span.pop((event.kind, event.reason), None)
            self._discard(event)
        return True

    def _discard(self, event: _Event) -> None:
        with self._lock:
            self._events.popleft()
            if isinstance(event, _Span) and event.bounded:
                self._pending_spans -= 1

    def _write(self, event: _Event) -> None:
        match event:
            case _Open():
                self._open_rows[event.kind] = self._log.open(event.kind, event.reason, event.ts_ms)
            case _Close():
                gap_id = self._open_rows.get(event.kind)
                if gap_id is not None:
                    self._log.close(gap_id, event.ts_ms)
                    del self._open_rows[event.kind]
            case _Span():
                self._write_span(event)

    def _write_span(self, span: _Span) -> None:
        key = (span.kind, span.reason)
        previous = self._last_span.get(key)
        if (
            span.merge
            and previous is not None
            and span.start_ms - previous.end_ms <= MERGE_WITHIN_MS
        ):
            self._log.extend(previous.gap_id, span.end_ms)
            previous.end_ms = max(previous.end_ms, span.end_ms)
            return
        gap_id = self._log.add(span.kind, span.reason, span.start_ms, span.end_ms)
        self._last_span[key] = _Row(gap_id, span.end_ms)
