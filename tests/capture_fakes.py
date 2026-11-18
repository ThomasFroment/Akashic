"""In-memory stand-ins for the platform ports, shared by the capture tests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from akashic.capture.ports import (
    CapturedFrame,
    MonitorInfo,
    Rect,
    TextBlock,
    TopWindow,
    WindowChanges,
    WindowEvent,
    WindowInfo,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from akashic.capture.ports import BrowserFamily, MediaState, WindowChange
    from akashic.capture.uia_threads import ThreadedUia

SCREEN = Rect(0, 0, 800, 600)


def blank(color: str = "white", size: tuple[int, int] = (800, 600)) -> Image.Image:
    return Image.new("RGB", size, color)


def with_text_block(
    image: Image.Image, box: tuple[int, int, int, int] = (100, 100, 400, 200)
) -> Image.Image:
    """Paint vertical bars that look like a block of text to the text-region detector."""
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = box
    for x in range(left, right, 24):
        draw.rectangle((x, top, x + 5, bottom), fill="black")
    return image


def window(
    hwnd: int = 1,
    process: str = "notepad.exe",
    title: str = "Notes",
    rect: Rect = SCREEN,
) -> WindowInfo:
    return WindowInfo(hwnd, process, title, rect)


def monitor(
    *,
    key: str = "m1",
    name: str = "Monitor 1",
    rect: Rect = SCREEN,
    scale: float = 1.0,
    primary: bool = True,
) -> MonitorInfo:
    return MonitorInfo(key, name, rect, scale, primary)


def top_window(  # noqa: PLR0913 - one TopWindow, every field defaulted
    *,
    hwnd: int = 1,
    pid: int = 100,
    process: str = "notepad.exe",
    uwp_unresolved: bool = False,
    class_name: str = "Notepad",
    title: str = "Notes",
    rect: Rect = SCREEN,
    minimized: bool = False,
    cloaked: bool = False,
    owner: int | None = None,
    popup: bool = False,
    tool: bool = False,
    alpha: int | None = None,
    is_ghost: bool = False,
    ghost_of: int | None = None,
    foreign_children: tuple[str, ...] | None = (),
    has_frame: bool = True,
) -> TopWindow:
    return TopWindow(
        hwnd=hwnd,
        pid=pid,
        process=process,
        uwp_unresolved=uwp_unresolved,
        class_name=class_name,
        title=title,
        rect=rect,
        minimized=minimized,
        cloaked=cloaked,
        owner=owner,
        popup=popup,
        tool=tool,
        alpha=alpha,
        is_ghost=is_ghost,
        ghost_of=ghost_of,
        foreign_children=foreign_children,
        has_frame=has_frame,
    )


@dataclass
class FakeDisplays:
    monitor_list: list[MonitorInfo] | None = field(default_factory=lambda: [monitor()])
    reads: int = 0

    def monitors(self) -> list[MonitorInfo] | None:
        self.reads += 1
        return None if self.monitor_list is None else list(self.monitor_list)


@dataclass
class FakeWindowFacts:
    window_list: list[TopWindow] | None = field(default_factory=list[TopWindow])
    reads: int = 0

    def windows(self) -> list[TopWindow] | None:
        self.reads += 1
        return None if self.window_list is None else list(self.window_list)


@dataclass
class FakeWindowEvents:
    healthy: bool = True
    queued: list[WindowEvent] = field(default_factory=list[WindowEvent])
    starts: int = 0
    closes: int = 0
    drains: int = 0

    def push(self, hwnd: int, change: WindowChange, at: float) -> None:
        self.queued.append(WindowEvent(hwnd, change, at))

    def start(self) -> None:
        self.starts += 1

    def drain(self) -> WindowChanges:
        self.drains += 1
        changes = WindowChanges(tuple(self.queued), self.healthy)
        self.queued.clear()
        return changes

    def close(self) -> None:
        self.closes += 1


@dataclass
class FakeWindowCaptures:
    refuse: set[int] = field(default_factory=set[int])
    failures: set[int] = field(default_factory=set[int])
    intervals: dict[int, int] = field(default_factory=dict[int, int])
    frames: dict[int, CapturedFrame] = field(default_factory=dict[int, CapturedFrame])
    starts: list[int] = field(default_factory=list[int])
    stops: list[int] = field(default_factory=list[int])
    seq_counter: int = 0
    faults: dict[str, int] = field(default_factory=dict[str, int])

    def fault_counts(self) -> dict[str, int]:
        return dict(self.faults)

    def start(self, hwnd: int, min_interval_ms: int) -> bool:
        if hwnd in self.refuse:
            return False
        self.starts.append(hwnd)
        self.intervals[hwnd] = min_interval_ms
        return True

    def push(self, hwnd: int, image: Image.Image, at: float) -> None:
        if hwnd in self.intervals:
            self.seq_counter += 1
            self.frames[hwnd] = CapturedFrame(self.seq_counter, at, image)

    def stop(self, hwnd: int) -> None:
        self.stops.append(hwnd)
        self.intervals.pop(hwnd, None)
        self.failures.discard(hwnd)
        self.frames.pop(hwnd, None)

    def latest_seq(self, hwnd: int) -> int | None:
        frame = self.frames.get(hwnd)
        return None if frame is None else frame.seq

    def grab(self, hwnd: int) -> CapturedFrame | None:
        frame = self.frames.get(hwnd)
        return None if frame is None else CapturedFrame(frame.seq, frame.at, frame.image.copy())

    def failed(self) -> frozenset[int]:
        return frozenset(self.failures & self.intervals.keys())

    def close(self) -> None:
        for hwnd in list(self.intervals):
            self.stop(hwnd)


@dataclass
class FakeWindows:
    focused: WindowInfo | None = None
    cursor: tuple[int, int] = (10, 10)
    locked: bool = False
    last_input: int = 0
    awake_clock: Callable[[], float] = time.monotonic  # seconds

    def focused_window(self) -> WindowInfo | None:
        return self.focused

    def cursor_pos(self) -> tuple[int, int]:
        return self.cursor

    def is_locked(self) -> bool:
        return self.locked

    def last_input_ms(self) -> int:
        return self.last_input

    def awake_ms(self) -> int:
        return int(self.awake_clock() * 1000)


@dataclass
class FakeUia:
    """Plain strings in ``texts`` are shorthand for content blocks; ``read_text`` always returns
    ``TextBlock``s, as the production UIA reader does."""

    texts: dict[int, Sequence[TextBlock | str] | None] = field(
        default_factory=dict[int, "Sequence[TextBlock | str] | None"]
    )
    urls: dict[int, str | None] = field(default_factory=dict[int, str | None])
    captions: dict[int, str | None] = field(default_factory=dict[int, str | None])
    text_reads: list[int] = field(default_factory=list[int])
    url_reads: int = 0
    on_read: Callable[[], None] | None = None  # runs while a text read is in progress

    def read_text(self, window: WindowInfo, max_nodes: int) -> list[TextBlock] | None:
        del max_nodes
        self.text_reads.append(window.hwnd)
        if self.on_read is not None:
            self.on_read()
        parts = self.texts.get(window.hwnd)
        if parts is None:
            return None
        return [TextBlock(p) if isinstance(p, str) else p for p in parts]

    def browser_caption(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        del family
        # A window's caption is its title unless a test says otherwise; None is "unreadable".
        return self.captions.get(window.hwnd, window.title)

    def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        del family
        self.url_reads += 1
        return self.urls.get(window.hwnd)


@dataclass
class GatedUia(FakeUia):
    """Reads of the ``gated`` kinds (the address by default) block until ``release`` is set.
    ``threads`` holds the thread of each gated read."""

    release: threading.Event = field(default_factory=threading.Event)
    threads: list[int] = field(default_factory=list[int])
    gated: frozenset[str] = frozenset({"address"})

    def _gate(self, kind: str) -> None:
        if kind in self.gated:
            self.threads.append(threading.get_ident())
            assert self.release.wait(5)

    def read_text(self, window: WindowInfo, max_nodes: int) -> list[TextBlock] | None:
        self._gate("text")
        return super().read_text(window, max_nodes)

    def browser_caption(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        self._gate("caption")
        return super().browser_caption(window, family)

    def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        self._gate("address")
        return super().read_url(window, family)


def wait_until_idle(threads: ThreadedUia, pid: int = 100, within: float = 5.0) -> None:
    """Wait until the pid's worker has finished its read and kept the late result; fail after
    ``within`` seconds."""
    probe = replace(window(hwnd=999), pid=pid)
    deadline = time.monotonic() + within
    while threads.caption(probe, "chromium").started_at is None:
        assert time.monotonic() < deadline, "the UIA worker stayed busy"
        time.sleep(0.01)


@dataclass
class FakeOcr:
    text: str = "recognized words"
    calls: int = 0

    def recognize(self, image: Image.Image) -> str:
        del image
        self.calls += 1
        return self.text


@dataclass
class FakeMedia:
    states: list[MediaState] = field(default_factory=list["MediaState"])
    names: dict[str, str] = field(default_factory=dict[str, str])
    fail: bool = False

    def sessions(self) -> list[MediaState]:
        if self.fail:
            msg = "media unavailable"
            raise OSError(msg)
        return list(self.states)

    def app_name(self, app_id: str) -> str | None:
        return self.names.get(app_id)


LONG_TEXT = [TextBlock("A line of text that is long enough to count as real window text.")]

AUMID = "308046B0AF4A39CB"  # a browser's app id, as Windows reports it for media sessions
