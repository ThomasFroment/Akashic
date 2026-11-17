"""The screen-loop rig: fakes for every port, a real ScreenWriter over a disposable store.

``Rig.tick`` drives the loop at a given time. Pictures wait in the writer's queue until
``Rig.drain`` stores them, on the calling thread, so a test reads rows deterministically.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from capture_fakes import (
    LONG_TEXT,
    SCREEN,
    FakeDisplays,
    FakeMedia,
    FakeOcr,
    FakeUia,
    FakeWindowCaptures,
    FakeWindowEvents,
    FakeWindowFacts,
    FakeWindows,
    blank,
    monitor,
    top_window,
    window,
)
from store_reads import latest_frames

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.ports import BROWSERS, CapturePlatform, Rect
from akashic.capture.screen import ScreenCapture
from akashic.capture.screen_writer import Picture, ScreenWriter
from akashic.config import Exclusions as UserExclusions
from akashic.storage.frames import FrameStore
from akashic.storage.monitors import MonitorLog
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from sqlite3 import Connection

    from PIL import Image

    from akashic.capture.gaming import GameSettings
    from akashic.capture.ports import MonitorInfo, WindowInfo
    from akashic.capture.screen import Settings
    from akashic.capture.uia_budget import UiaBudget
    from akashic.storage.media import MediaStore
    from akashic.storage.monitors import MonitorState

MON_B = Rect(800, 0, 1600, 600)


@dataclass
class ImageCaptures(FakeWindowCaptures):
    """A started capture has its frame at once, so the next check can paint it."""

    images: dict[int, Image.Image] = field(default_factory=dict[int, "Image.Image"])

    def start(self, hwnd: int, min_interval_ms: int) -> bool:
        started = super().start(hwnd, min_interval_ms)
        if started and hwnd in self.images:
            self.push(hwnd, self.images[hwnd], 0.0)
        return started


class RecordingWriter(ScreenWriter):
    """The real writer; it also remembers what the loop handed it."""

    def __init__(self, store: FrameStore, monitors: MonitorLog, gate: OcrGate) -> None:
        super().__init__(store, monitors, gate)
        self.pictures: list[Picture] = []
        self.notes: list[tuple[str, MonitorState, int]] = []
        self.layouts: list[tuple[tuple[MonitorInfo, ...], int]] = []
        self.failures: list[tuple[str, int]] = []

    def submit(self, picture: Picture) -> None:
        self.pictures.append(picture)
        super().submit(picture)

    def note(self, monitor_key: str, state: MonitorState, ts_ms: int) -> None:
        self.notes.append((monitor_key, state, ts_ms))
        super().note(monitor_key, state, ts_ms)

    def layout(self, monitors: Sequence[MonitorInfo], ts_ms: int) -> None:
        self.layouts.append((tuple(monitors), ts_ms))
        super().layout(monitors, ts_ms)

    def capture_failures(self, monitor_key: str, count: int) -> None:
        self.failures.append((monitor_key, count))
        super().capture_failures(monitor_key, count)


@dataclass
class Rig:
    capture: ScreenCapture
    displays: FakeDisplays
    windows: FakeWindows
    facts: FakeWindowFacts
    events: FakeWindowEvents
    captures: ImageCaptures
    uia: FakeUia
    ocr: FakeOcr
    writer: RecordingWriter
    store: FrameStore
    conn: Connection
    browsers: BrowserState
    clock: list[float] = field(default_factory=lambda: [0.0])
    game_flags: list[bool] = field(default_factory=list[bool])  # what on_game was told
    own_flags: list[bool] = field(default_factory=list[bool])  # what on_own_address was told

    def tick(self, now: float) -> None:
        self.clock[0] = now
        self.capture.tick(now)

    def settle(self, now: float) -> None:
        """Two passes 0.1 s apart: the first starts the captures, the second paints them."""
        self.tick(now)
        self.tick(now + 0.1)

    def drain(self) -> None:
        """Store everything queued, on this thread."""
        stop = threading.Event()
        stop.set()
        self.writer.run(stop)

    def show(  # noqa: PLR0913 - one window, every property defaulted
        self,
        hwnd: int = 1,
        process: str = "notepad.exe",
        title: str = "Notes",
        rect: Rect = SCREEN,
        *,
        image: Image.Image | None = None,
        focus: bool = True,
        url: str | None = None,
        has_frame: bool = True,
        **top: object,
    ) -> WindowInfo:
        """Put a window at the front of the list (or update it in place) and give it pixels."""
        info = window(hwnd, process, title, rect)
        if not has_frame:
            info = replace(info, has_frame=False)
        entry = top_window(
            hwnd=hwnd,
            process=process,
            title=title,
            rect=rect,
            has_frame=has_frame,
            **top,  # type: ignore[arg-type]
        )
        current = self.facts.window_list or []
        if any(w.hwnd == hwnd for w in current):
            current = [entry if w.hwnd == hwnd else w for w in current]
        else:
            current = [entry, *current]
        self.facts.window_list = current
        self.captures.images[hwnd] = image if image is not None else blank()
        if process in BROWSERS and url is None:
            url = self.uia.urls.get(hwnd) or "https://example.org/page"
        if url is not None:
            self.uia.urls[hwnd] = url
        if focus:
            self.windows.focused = info
        return info

    def hide(self, hwnd: int) -> None:
        self.facts.window_list = [w for w in self.facts.window_list or [] if w.hwnd != hwnd]
        self.captures.images.pop(hwnd, None)

    def repaint(self, hwnd: int, image: Image.Image) -> None:
        """New pixels for a started window."""
        self.captures.images[hwnd] = image
        self.captures.push(hwnd, image, self.clock[0])

    def count(self, key: str = "m1") -> int:
        """Snapshots handled for one monitor: each became a picture or a note."""
        return sum(p.monitor_key == key for p in self.writer.pictures) + sum(
            n[0] == key for n in self.writer.notes
        )

    def pictures(self, key: str = "m1") -> list[Picture]:
        return [p for p in self.writer.pictures if p.monitor_key == key]

    def last_note(self, key: str = "m1") -> MonitorState | None:
        notes: list[MonitorState] = [
            state for note_key, state, _ in self.writer.notes if note_key == key
        ]
        return notes[-1] if notes else None

    def quiet(self) -> None:
        """Settle at 0.0, then run the start-up focus trigger (fires at 1.1 s)."""
        self.settle(0.0)
        self.tick(0.4)
        self.tick(1.1)

    def frames(self) -> list[tuple[str, int, str | None]]:
        rows = latest_frames(self.conn, 100)
        return [(f.trigger, f.monitor_id, f.text_source) for f in reversed(rows)]


def monitor_infos(rects: Sequence[Rect]) -> list[MonitorInfo]:
    return [
        monitor(key=f"m{i + 1}", name=f"Monitor {i + 1}", rect=rect, primary=i == 0)
        for i, rect in enumerate(rects)
    ]


def build_rig(  # noqa: PLR0913 - one switch per scenario input
    conn: Connection,
    media: MediaStore,
    *,
    monitors: Sequence[Rect] | None = None,
    user: UserExclusions | None = None,
    settings: Settings | None = None,
    game_settings: GameSettings | None = None,
    budget: UiaBudget | None = None,
    clock: Callable[[], float] | None = None,
    api_origin: str | None = None,
    browsers: BrowserState | None = None,
    on_game: Callable[[bool], None] | None = None,
    shown: bool = True,
    ts_start: int = 1_000_000,
    now_ms: Callable[[], int] | None = None,
    uia: FakeUia | None = None,
    windows: FakeWindows | None = None,
) -> Rig:
    """Every port faked, a recording ScreenWriter on ``conn``; ``shown`` puts Notepad in front."""
    clock_cell = [0.0]
    flags: list[bool] = []
    own_flags: list[bool] = []
    shared = browsers or BrowserState(clock=lambda: clock_cell[0])
    displays = FakeDisplays(monitor_infos(monitors or [SCREEN]))
    windows = windows or FakeWindows()
    facts = FakeWindowFacts()
    events = FakeWindowEvents()
    captures = ImageCaptures()
    uia = uia or FakeUia(texts={1: LONG_TEXT})
    ocr = FakeOcr()
    store = FrameStore(conn, media)
    writer = RecordingWriter(store, MonitorLog(conn), OcrGate(ocr))
    counter = iter(range(ts_start, ts_start + 10**9))
    capture = ScreenCapture(
        displays=displays,
        windows=windows,
        facts=facts,
        events=events,
        captures=captures,
        uia=uia,
        writer=writer,
        exclusions=Exclusions(user or UserExclusions(), api_origin),
        browsers=shared,
        games=GameLog(conn),
        settings=settings,
        game_settings=game_settings,
        budget=budget,
        on_game=on_game or flags.append,
        on_own_address=own_flags.append,
        clock=clock or (lambda: clock_cell[0]),
        now_ms=now_ms or (lambda: next(counter)),
    )
    rig = Rig(
        capture, displays, windows, facts, events, captures, uia, ocr, writer, store, conn,
        shared, clock_cell, flags, own_flags,
    )  # fmt: skip
    if shown:
        rig.show()
    return rig


def fake_platform(
    *, ocr: FakeOcr | None, locked: bool = False, text: str = "x" * 80
) -> CapturePlatform:
    """A platform of fakes with Notepad on one monitor, for tests that run ``run_capture``."""
    return CapturePlatform(
        displays=FakeDisplays(),
        windows=FakeWindows(focused=window(), locked=locked),
        facts=FakeWindowFacts([top_window(hwnd=1)]),
        events=FakeWindowEvents(),
        captures=ImageCaptures(images={1: blank()}),
        uia=FakeUia(texts={1: [text]}),
        ocr=ocr,
        media=FakeMedia(),
    )
