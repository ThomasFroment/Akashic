"""Stores monitor pictures off the capture loop.

The loop hands over pictures, notes and layouts and returns at once. One thread, the one that runs
``ScreenWriter.run``, encodes, reads text and writes; it is the only one that touches the
``FrameStore``, the ``MonitorLog`` and the OCR engine. Everything is applied in submission order.
When saving cannot keep up the oldest pictures are dropped and the monitor is "lost" until a newer
picture of it is stored.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture import imaging
from akashic.capture.health import MonitorHealth
from akashic.capture.text import MIN_UIA_CHARS
from akashic.storage.db import is_disk_full
from akashic.storage.frames import NewFrame, NewWindow
from akashic.storage.monitors import MonitorRow

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from PIL import Image

    from akashic.capture.health import RunKind
    from akashic.capture.ocr_gate import GateResult, OcrGate
    from akashic.capture.ports import Entry, MonitorInfo, Trigger
    from akashic.storage.frames import FrameStore, TextSource
    from akashic.storage.monitors import MonitorLog, MonitorState

log = logging.getLogger(__name__)

MAX_QUEUED_PICTURES: Final = 3
MAX_QUEUED_BYTES: Final = 256 * 1024 * 1024
POLL_S: Final = 0.1  # how often an idle writer looks at its stop event


@dataclass(frozen=True, slots=True)
class Picture:
    monitor_key: str
    ts_ms: int
    trigger: Trigger
    image: Image.Image | None  # the composite; None for a black monitor
    entries: tuple[Entry, ...]
    uia_text: str | None  # the focused window's text; ignored when no app entry is focused
    defer_ocr: bool  # store window captures for the OCR job instead of reading them here
    side: bool  # a side picture is dropped before the others


@dataclass(frozen=True, slots=True)
class _Queued:
    picture: Picture
    seq: int


@dataclass(frozen=True, slots=True)
class _Note:
    monitor_key: str
    state: MonitorState
    ts_ms: int


@dataclass(frozen=True, slots=True)
class _Layout:
    monitors: Sequence[MonitorInfo]
    ts_ms: int


@dataclass(frozen=True, slots=True)
class _Dropped:
    monitor_key: str
    ts_ms: int


type _Item = _Queued | _Note | _Layout | _Dropped


def _raw_bytes(picture: Picture) -> int:
    images = (picture.image, *(entry.image for entry in picture.entries))
    return sum(
        image.width * image.height * len(image.getbands()) for image in images if image is not None
    )


class ScreenWriter:
    def __init__(
        self,
        store: FrameStore,
        monitors: MonitorLog,
        gate: OcrGate,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._monitors = monitors
        self._gate = gate
        self._clock = clock
        self._cond = threading.Condition()
        self._queue: deque[_Item] = deque()
        self._seq = 0
        self._lost: dict[str, int] = {}  # monitor key -> sequence at which it was lost
        self._health = MonitorHealth()
        self._labels: dict[str, str] = {}  # monitor key -> "Monitor 2", left to right
        self._closed = False
        # Writer thread only: monitor ids, and the layout that is not in the database yet.
        self._ids: dict[str, int] = {}
        self._unwritten: tuple[list[MonitorRow], int] | None = None

    # --- loop side: never blocks on the store --------------------------------------------------

    def submit(self, picture: Picture) -> None:
        with self._cond:
            self._seq += 1
            self._queue.append(_Queued(picture, self._seq))
            while self._over_bounds():
                self._drop_oldest()
            self._cond.notify()

    def note(self, monitor_key: str, state: MonitorState, ts_ms: int) -> None:
        self._enqueue(_Note(monitor_key, state, ts_ms))

    def layout(self, monitors: Sequence[MonitorInfo], ts_ms: int) -> None:
        ordered = sorted(monitors, key=lambda m: (m.rect.left, m.rect.top))
        with self._cond:
            self._labels = {m.key: f"Monitor {number}" for number, m in enumerate(ordered, 1)}
            self._queue.append(_Layout(tuple(monitors), ts_ms))
            self._cond.notify()

    def lost(self, monitor_key: str) -> bool:
        with self._cond:
            return monitor_key in self._lost

    def capture_failures(self, monitor_key: str, count: int) -> None:
        """Report the failed window captures of one loop tick; none resets the run."""
        with self._cond:
            if count > 0:
                self._health.record(monitor_key, "capture", self._clock())
            else:
                self._health.clear(monitor_key, ("capture",))

    def problems(self, since: float) -> list[str]:
        with self._cond:
            return self._health.problems(since, self._labels)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()

    def _enqueue(self, item: _Item) -> None:
        with self._cond:
            self._queue.append(item)
            self._cond.notify()

    def _over_bounds(self) -> bool:
        pictures = [item.picture for item in self._queue if isinstance(item, _Queued)]
        return (
            len(pictures) > MAX_QUEUED_PICTURES
            or sum(_raw_bytes(picture) for picture in pictures) > MAX_QUEUED_BYTES
        )

    def _drop_oldest(self) -> None:
        queued = [
            (index, item) for index, item in enumerate(self._queue) if isinstance(item, _Queued)
        ]
        index, dropped = next(((i, q) for i, q in queued if q.picture.side), queued[0])
        picture = dropped.picture
        self._queue[index] = _Dropped(picture.monitor_key, picture.ts_ms)
        self._mark_lost(picture.monitor_key, dropped.seq)
        self._health.record(picture.monitor_key, "drop", self._clock())

    def _mark_lost(self, monitor_key: str, seq: int) -> None:
        self._lost[monitor_key] = max(seq, self._lost.get(monitor_key, seq))

    # --- writer side ---------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Apply queued items until ``stop`` is set, write what is left, then return."""
        while (item := self._next(stop)) is not None:
            self._apply(item)

    def _next(self, stop: threading.Event) -> _Item | None:
        with self._cond:
            while not self._queue and not stop.is_set() and not self._closed:
                self._cond.wait(POLL_S)
            return self._queue.popleft() if self._queue else None

    def _apply(self, item: _Item) -> None:
        match item:
            case _Queued(picture, seq):
                self._store_picture(picture, seq)
            case _Note(key, state, ts_ms):
                self._write_note(key, state, ts_ms)
            case _Layout(monitors, ts_ms):
                self._write_layout(monitors, ts_ms)
            case _Dropped(key, ts_ms):
                self._write_state(key, "dropped", ts_ms)

    def _write_layout(self, monitors: Sequence[MonitorInfo], ts_ms: int) -> None:
        rows = [
            MonitorRow(
                m.key,
                m.name,
                (m.rect.left, m.rect.top, m.rect.right, m.rect.bottom),
                m.scale,
                is_main=m.primary,
            )
            for m in monitors
        ]
        self._unwritten = (rows, ts_ms)
        try:
            self._ensure_layout()
        except Exception as error:  # noqa: BLE001 - the writer outlives any one item
            log.warning("monitor layout not written: %s", type(error).__name__)

    def _ensure_layout(self) -> None:
        """Write the pending layout; a failed write raises and is tried again before the next
        state."""
        if self._unwritten is None:
            return
        self._ids = self._monitors.present(*self._unwritten)
        self._unwritten = None

    def _write_note(self, key: str, state: MonitorState, ts_ms: int) -> None:
        if state == "shown" and self.lost(key):
            return
        self._write_state(key, state, ts_ms)

    def _write_state(self, key: str, state: MonitorState, ts_ms: int) -> None:
        with suppress(Exception):  # _write_layout already logged it
            self._ensure_layout()
        monitor_id = self._ids.get(key)
        if monitor_id is None:
            return
        try:
            self._monitors.state(monitor_id, state, ts_ms)
        except Exception as error:  # noqa: BLE001 - the writer outlives any one item
            log.warning("monitor state not written: %s", type(error).__name__)

    def _monitor_id(self, key: str) -> int:
        """A layout that cannot be written only fails the monitors it has no id for yet."""
        try:
            self._ensure_layout()
        except Exception:
            if key not in self._ids:
                raise
        return self._ids[key]

    def _store_picture(self, picture: Picture, seq: int) -> None:
        key = picture.monitor_key
        try:
            monitor_id = self._monitor_id(key)
            frame, results = self._build_frame(picture, monitor_id)
            self._store.add(frame)
            for window_key, result in results:
                self._gate.commit(window_key, result)
            self._monitors.state(monitor_id, "shown", picture.ts_ms)
        except Exception as error:  # noqa: BLE001 - the writer outlives any one picture
            kind: RunKind = "disk_full" if is_disk_full(error) else "failure"
            self._fail(picture, seq, type(error).__name__, kind)
            return
        with self._cond:
            if self._lost.get(key, seq) < seq:
                del self._lost[key]
            self._health.clear(key, ("failure", "disk_full", "drop"))

    def _fail(self, picture: Picture, seq: int, reason: str, kind: RunKind = "failure") -> None:
        log.warning("screen picture not stored: %s", reason)
        with self._cond:
            self._mark_lost(picture.monitor_key, seq)
            self._health.record(picture.monitor_key, kind, self._clock())
        self._write_state(picture.monitor_key, "failed", picture.ts_ms)

    def _build_frame(
        self, picture: Picture, monitor_id: int
    ) -> tuple[NewFrame, list[tuple[str, GateResult]]]:
        results: list[tuple[str, GateResult]] = []
        windows = tuple(self._new_window(picture, entry, results) for entry in picture.entries)
        image = None if picture.image is None else imaging.encode_webp(picture.image)
        return NewFrame(picture.ts_ms, monitor_id, picture.trigger, image, windows), results

    def _new_window(
        self, picture: Picture, entry: Entry, results: list[tuple[str, GateResult]]
    ) -> NewWindow:
        rect = entry.rect
        if entry.role != "app":
            return NewWindow(
                entry.role,
                entry.z,
                None,
                rect.left,
                rect.top,
                rect.right,
                rect.bottom,
                painted=entry.painted,
            )
        text, source, capture = self._read_app(picture, entry, results)
        return NewWindow(
            "app",
            entry.z,
            entry.hwnd,
            rect.left,
            rect.top,
            rect.right,
            rect.bottom,
            entry.process,
            entry.title,
            entry.url,
            entry.focused,
            entry.painted,
            text,
            source,
            capture,
        )

    def _read_app(
        self, picture: Picture, entry: Entry, results: list[tuple[str, GateResult]]
    ) -> tuple[str | None, TextSource | None, bytes | None]:
        uia = (picture.uia_text or "") if entry.focused else ""
        if len(uia) >= MIN_UIA_CHARS:
            return uia, "uia", None
        image = entry.image
        if image is None:
            return uia or None, "uia" if uia else None, None
        if picture.defer_ocr:
            return None, None, imaging.encode_webp(image)
        window_key = str(entry.hwnd)
        result = self._gate.read(window_key, image)
        ocr = result.text.strip()
        use_ocr = len(ocr) > len(uia)
        if use_ocr or not uia:
            results.append((window_key, result))
        if use_ocr:
            return ocr, "ocr", None
        return uia or None, "uia" if uia else None, None
