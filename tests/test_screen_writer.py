"""The screen writer: queue bounds, drops, ordering, stored rows, text sources and health."""

from __future__ import annotations

import errno
import io
import itertools
import logging
import threading
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast, get_args

import pytest
import sqlcipher3
from capture_fakes import blank, monitor, with_text_block
from PIL import Image

from akashic.capture import imaging, ports, screen_writer
from akashic.capture.health import (
    SCREEN_DISK_FULL,
    SCREEN_FRAMES_DROPPED,
    SCREEN_IMAGES_FAILING,
)
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.ports import Entry, Rect
from akashic.capture.screen_writer import Picture, ScreenWriter
from akashic.capture.text import MIN_UIA_CHARS
from akashic.storage.frames import FrameStore, NewFrame, NewWindow
from akashic.storage.monitors import MonitorLog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from sqlite3 import Connection

    from akashic.capture.ports import EntryRole, MonitorInfo, Trigger
    from akashic.storage.media import MediaStore
    from akashic.storage.monitors import MonitorRow, MonitorState

LONG_UIA = "Window text read through accessibility, long enough."
RECT = Rect(0, 0, 800, 600)


class SpyOcr:
    def __init__(self, text: str = "recognized words") -> None:
        self.text = text
        self.calls = 0
        self.threads: set[int] = set()
        self.error: Exception | None = None

    def recognize(self, image: Image.Image) -> str:
        del image
        self.calls += 1
        self.threads.add(threading.get_ident())
        if self.error is not None:
            raise self.error
        return self.text


class SpyStore(FrameStore):
    def __init__(self, conn: Connection, media: MediaStore) -> None:
        super().__init__(conn, media)
        self.added: list[NewFrame] = []
        self.threads: set[int] = set()
        self.fail_ts: set[int] = set()
        self.entered = threading.Event()
        self.gate: threading.Event | None = None  # while set up and unset, add blocks

    def add(self, frame: NewFrame) -> int:
        self.threads.add(threading.get_ident())
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if frame.ts_ms in self.fail_ts:
            msg = "secret title"
            raise RuntimeError(msg)
        row = super().add(frame)
        self.added.append(frame)
        return row


class SpyMonitors(MonitorLog):
    def __init__(self, conn: Connection) -> None:
        super().__init__(conn)
        self.threads: set[int] = set()
        self.fail_state_ts: set[int] = set()
        self.fail_present = False
        self.on_state: Callable[[MonitorState, int], None] | None = None
        self.writes: list[tuple[MonitorState, int]] = []

    def present(self, rows: Sequence[MonitorRow], ts_ms: int) -> dict[str, int]:
        self.threads.add(threading.get_ident())
        if self.fail_present:
            msg = "secret layout"
            raise RuntimeError(msg)
        return super().present(rows, ts_ms)

    def state(self, monitor_id: int, state: MonitorState, ts_ms: int) -> None:
        self.threads.add(threading.get_ident())
        if self.on_state is not None:
            self.on_state(state, ts_ms)
        if ts_ms in self.fail_state_ts:
            msg = "secret state"
            raise RuntimeError(msg)
        super().state(monitor_id, state, ts_ms)
        self.writes.append((state, ts_ms))


@dataclass
class Rig:
    conn: Connection
    store: SpyStore
    monitors: SpyMonitors
    ocr: SpyOcr
    writer: ScreenWriter

    def layout(self, *keys: str, ts: int = 0) -> None:
        infos: list[MonitorInfo] = [
            monitor(key=k, name=k.upper(), primary=i == 0) for i, k in enumerate(keys)
        ]
        self.writer.layout(infos, ts)

    def drain(self) -> None:
        stop = threading.Event()
        stop.set()
        self.writer.run(stop)

    def start(self) -> tuple[threading.Thread, threading.Event]:
        stop = threading.Event()
        thread = threading.Thread(target=self.writer.run, args=(stop,), daemon=True)
        thread.start()
        return thread, stop

    def states(self, key: str) -> list[tuple[str, int, int]]:
        rows = self.conn.execute(
            "SELECT s.state, s.start_ms, s.checked_ms FROM monitor_states s"
            " JOIN monitors m ON m.id = s.monitor_id WHERE m.key = ? ORDER BY s.id",
            (key,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def stored_ts(self) -> list[int]:
        return [f.ts_ms for f in self.store.added]


@pytest.fixture
def rig(conn: Connection, media: MediaStore) -> Rig:
    store = SpyStore(conn, media)
    monitors = SpyMonitors(conn)
    ocr = SpyOcr()
    ticks: itertools.count[int] = itertools.count(1)
    writer = ScreenWriter(store, monitors, OcrGate(ocr), clock=lambda: float(next(ticks)))
    return Rig(conn, store, monitors, ocr, writer)


def text_image() -> Image.Image:
    return with_text_block(blank())


def entry(  # noqa: PLR0913 - an Entry with defaults
    z: int = 0,
    hwnd: int | None = 1,
    *,
    role: EntryRole = "app",
    focused: bool = False,
    painted: bool = True,
    image: Image.Image | None = None,
    process: str | None = "app.exe",
    title: str | None = "Title",
    url: str | None = None,
) -> Entry:
    return Entry(
        role,
        hwnd,
        RECT,
        z,
        process,
        title,
        url,
        focused=focused,
        painted=painted,
        image=image,
    )


def picture(  # noqa: PLR0913 - a Picture with defaults
    key: str = "m1",
    ts: int = 1000,
    *,
    entries: Sequence[Entry] = (),
    image: Image.Image | None = None,
    uia: str | None = None,
    defer: bool = False,
    side: bool = False,
    trigger: Trigger = "focus",
) -> Picture:
    return Picture(
        key, ts, trigger, image or blank(size=(64, 48)), tuple(entries), uia, defer, side
    )


# --- types ---------------------------------------------------------------------------------


def test_picture_is_a_frozen_dataclass_with_the_named_fields() -> None:
    pic = picture()
    assert list(Picture.__dataclass_fields__) == [
        "monitor_key",
        "ts_ms",
        "trigger",
        "image",
        "entries",
        "uia_text",
        "defer_ocr",
        "side",
    ]
    with pytest.raises(FrozenInstanceError):
        pic.ts_ms = 1  # pyright: ignore[reportAttributeAccessIssue]


def test_entry_lives_in_ports_and_neither_module_imports_the_desk() -> None:
    writer = screen_writer
    assert Entry.__module__ == "akashic.capture.ports"
    assert list(Entry.__dataclass_fields__) == [
        "role",
        "hwnd",
        "rect",
        "z",
        "process",
        "title",
        "url",
        "focused",
        "painted",
        "image",
    ]
    for module in (ports, writer):
        assert module.__file__ is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "capture.desk" not in source
        assert "import desk" not in source
    assert "from akashic.capture.ports import Entry" in Path(writer.__file__ or "").read_text(
        encoding="utf-8"
    )


def test_trigger_includes_window() -> None:
    assert "window" in get_args(ports.Trigger.__value__)


# --- thread discipline ---------------------------------------------------------------------


def test_only_the_run_thread_touches_store_log_and_ocr(rig: Rig) -> None:
    thread, stop = rig.start()
    rig.layout("m1")
    rig.writer.submit(picture(entries=[entry(image=text_image())]))
    rig.writer.note("m1", "hidden", 2000)
    rig.writer.capture_failures("m1", 1)
    rig.writer.lost("m1")
    rig.writer.problems(0)
    stop.set()
    thread.join(10)
    assert not thread.is_alive()
    assert thread.ident is not None
    for touched in (rig.store.threads, rig.monitors.threads, rig.ocr.threads):
        assert touched == {thread.ident}


def test_submit_and_the_rest_return_while_the_writer_is_stuck_in_the_store(rig: Rig) -> None:
    rig.store.gate = threading.Event()
    rig.layout("m1")
    thread, stop = rig.start()
    rig.writer.submit(picture(ts=1))
    assert rig.store.entered.wait(10)
    done = threading.Event()

    def loop_side() -> None:
        rig.writer.submit(picture(ts=2))
        rig.writer.note("m1", "hidden", 3)
        rig.writer.layout([monitor(key="m1")], 4)
        rig.writer.lost("m1")
        rig.writer.capture_failures("m1", 1)
        rig.writer.problems(0)
        done.set()

    threading.Thread(target=loop_side, daemon=True).start()
    assert done.wait(5)
    rig.store.gate.set()
    stop.set()
    thread.join(10)
    assert rig.stored_ts() == [1, 2]


# --- queue bounds and drops ----------------------------------------------------------------


def test_the_fourth_queued_picture_drops_the_oldest(rig: Rig) -> None:
    rig.layout("m1")
    for ts in (1, 2, 3, 4):
        rig.writer.submit(picture(ts=ts))
    assert rig.writer.lost("m1")
    rig.drain()
    assert rig.stored_ts() == [2, 3, 4]
    assert rig.states("m1") == [("dropped", 1, 1), ("shown", 2, 4)]
    assert not rig.writer.lost("m1")


def test_a_side_picture_is_dropped_before_older_non_side_ones(rig: Rig) -> None:
    rig.layout("m1")
    rig.writer.submit(picture(ts=1))
    rig.writer.submit(picture(ts=2, side=True))
    rig.writer.submit(picture(ts=3))
    rig.writer.submit(picture(ts=4))
    rig.drain()
    assert rig.stored_ts() == [1, 3, 4]
    assert ("dropped", 2, 2) in rig.states("m1")


def test_the_new_side_picture_counts_and_can_be_the_one_dropped(rig: Rig) -> None:
    rig.layout("m1")
    for ts in (1, 2, 3):
        rig.writer.submit(picture(ts=ts))
    rig.writer.submit(picture(ts=4, side=True))
    assert rig.writer.lost("m1")
    rig.drain()
    assert rig.stored_ts() == [1, 2, 3]
    assert rig.states("m1") == [("shown", 1, 3), ("dropped", 4, 4)]
    assert rig.writer.lost("m1")
    rig.writer.submit(picture(ts=5))
    rig.drain()
    assert not rig.writer.lost("m1")


def test_a_drop_marks_only_its_own_monitor(rig: Rig) -> None:
    rig.layout("m1", "m2")
    for ts, key in ((1, "m1"), (2, "m2"), (3, "m2"), (4, "m2")):
        rig.writer.submit(picture(key, ts))
    assert rig.writer.lost("m1")
    assert not rig.writer.lost("m2")
    rig.drain()
    assert rig.states("m1") == [("dropped", 1, 1)]
    assert rig.writer.lost("m1")


def test_notes_and_layouts_do_not_count_toward_the_bound(rig: Rig) -> None:
    rig.layout("m1")
    for ts in (1, 2, 3):
        rig.writer.submit(picture(ts=ts))
        rig.writer.note("m1", "hidden", ts)
        rig.layout("m1", ts=ts)
    assert not rig.writer.lost("m1")
    rig.drain()
    assert rig.stored_ts() == [1, 2, 3]
    assert rig.writer.problems(0) == []


@dataclass
class _Huge:
    """Reports the size of a 201 MB image without allocating one."""

    width: int = 8192
    height: int = 8192

    def getbands(self) -> tuple[str, ...]:
        return ("R", "G", "B")


def huge() -> Image.Image:
    return cast("Image.Image", _Huge())


def test_the_pixel_byte_bound_counts_the_composite_and_every_entry_image(rig: Rig) -> None:
    rig.layout("m1", "m2")
    rig.writer.submit(picture("m1", 1, image=huge()))
    rig.writer.submit(picture("m2", 2))
    assert not rig.writer.lost("m1")
    rig.writer.submit(picture("m2", 3, entries=[entry(image=huge())]))
    assert rig.writer.lost("m1")
    assert not rig.writer.lost("m2")
    rig.writer.close()


def test_the_byte_bound_prefers_a_side_picture(rig: Rig) -> None:
    rig.layout("m1", "m2")
    rig.writer.submit(picture("m1", 1, image=huge()))
    rig.writer.submit(picture("m2", 2, image=huge(), side=True))
    assert rig.writer.lost("m2")
    assert not rig.writer.lost("m1")
    rig.writer.close()


def test_drop_runs_grow_in_health_and_a_stored_picture_resets_them(rig: Rig) -> None:
    rig.layout("m1")
    for ts in range(1, 7):
        rig.writer.submit(picture(ts=ts))
    assert rig.writer.problems(0) == [f"{SCREEN_FRAMES_DROPPED}@Monitor 1"]
    rig.drain()
    assert rig.writer.problems(0) == []


# --- ordering ------------------------------------------------------------------------------


def test_a_note_is_written_after_the_earlier_picture(rig: Rig) -> None:
    rig.layout("m1")
    rig.writer.submit(picture(ts=1))
    rig.writer.note("m1", "hidden", 2)
    rig.drain()
    assert rig.states("m1") == [("shown", 1, 1), ("hidden", 2, 2)]


def test_layout_maps_monitors_and_its_ids_resolve_later_pictures(rig: Rig) -> None:
    rig.writer.layout(
        [
            monitor(key="a", name="Left", rect=Rect(0, 0, 100, 50), scale=1.5, primary=False),
            monitor(key="b", name="Main", rect=Rect(100, 0, 300, 80), scale=1.0, primary=True),
        ],
        10,
    )
    rig.writer.submit(picture("b", 20))
    rig.drain()
    rows = rig.conn.execute(
        "SELECT m.key, m.name, l.x0, l.y0, l.x1, l.y1, l.scale, l.is_main FROM layout_monitors l"
        " JOIN monitors m ON m.id = l.monitor_id ORDER BY m.key"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("a", "Left", 0, 0, 100, 50, 1.5, 0),
        ("b", "Main", 100, 0, 300, 80, 1.0, 1),
    ]
    assert rig.states("b") == [("shown", 20, 20)]
    assert len(rig.store.added) == 1


# --- stored picture ------------------------------------------------------------------------


def test_a_picture_is_stored_with_its_native_webp_windows_in_entry_order_and_shown(
    rig: Rig,
) -> None:
    rig.layout("m1")
    composite = blank("white", (1920, 1080))
    entries = [
        entry(0, 7, focused=True, painted=True, process="a.exe", title="A", url="http://u"),
        entry(1, 8, painted=False, process="b.exe", title="B"),
    ]
    rig.writer.submit(picture(ts=5000, image=composite, entries=entries, trigger="window"))
    rig.drain()
    (frame,) = rig.store.added
    assert (frame.ts_ms, frame.trigger) == (5000, "window")
    assert [(w.z, w.hwnd, w.process, w.title, w.focused, w.painted) for w in frame.windows] == [
        (0, 7, "a.exe", "A", True, True),
        (1, 8, "b.exe", "B", False, False),
    ]
    assert frame.windows[0].url == "http://u"
    assert frame.image_webp is not None
    assert Image.open(io.BytesIO(frame.image_webp)).size == (1920, 1080)
    assert rig.states("m1") == [("shown", 5000, 5000)]


def test_a_black_monitor_stores_a_frame_without_image_but_with_windows_and_uia(rig: Rig) -> None:
    rig.layout("m1")
    pic = Picture(
        "m1", 1, "focus", None, (entry(focused=True),), LONG_UIA, defer_ocr=False, side=False
    )
    rig.writer.submit(pic)
    rig.drain()
    (frame,) = rig.store.added
    assert frame.image_webp is None
    assert frame.windows[0].text == LONG_UIA
    assert frame.windows[0].text_source == "uia"
    stored = rig.store.get(1)
    assert stored is not None
    assert stored.image_path is None
    assert stored.windows[0].text == LONG_UIA


def test_hidden_and_shell_entries_keep_only_role_z_rect_and_painted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.layout("m1")
    encoded: list[object] = []
    real = imaging.encode_webp

    def spy(image: Image.Image, quality: int = imaging.WEBP_QUALITY) -> bytes:
        encoded.append(image)
        return real(image, quality)

    monkeypatch.setattr(imaging, "encode_webp", spy)
    hidden = entry(0, 5, role="hidden", focused=True, painted=True, image=text_image(), url="u")
    shell = entry(1, 6, role="shell", painted=False, image=text_image())
    rig.writer.submit(picture(entries=[hidden, shell], uia=LONG_UIA))
    rig.drain()
    (frame,) = rig.store.added
    assert len(encoded) == 1  # the composite only
    assert rig.ocr.calls == 0
    for window, painted in zip(frame.windows, (True, False), strict=True):
        assert (window.hwnd, window.process, window.title, window.url) == (None,) * 4
        assert (window.focused, window.text, window.text_source, window.capture_webp) == (
            False,
            None,
            None,
            None,
        )
        assert window.painted is painted
    assert [(w.role, w.z, w.left, w.top, w.right, w.bottom) for w in frame.windows] == [
        ("hidden", 0, 0, 0, 800, 600),
        ("shell", 1, 0, 0, 800, 600),
    ]


# --- app rows: text sources ----------------------------------------------------------------


def stored_window(rig: Rig, pic: Picture, index: int = 0) -> NewWindow:
    rig.layout("m1")
    rig.writer.submit(pic)
    rig.drain()
    return rig.store.added[-1].windows[index]


def test_a_focused_entry_with_long_uia_text_uses_it_and_skips_ocr(rig: Rig) -> None:
    assert len(LONG_UIA) >= MIN_UIA_CHARS
    window = stored_window(
        rig, picture(entries=[entry(focused=True, image=text_image())], uia=LONG_UIA)
    )
    assert (window.text, window.text_source, window.capture_webp) == (LONG_UIA, "uia", None)
    assert rig.ocr.calls == 0


def test_live_mode_reads_other_app_entries_with_ocr_and_strips(rig: Rig) -> None:
    rig.ocr.text = "  hello there \n"
    window = stored_window(rig, picture(entries=[entry(image=text_image())]))
    assert (window.text, window.text_source) == ("hello there", "ocr")
    assert rig.ocr.calls == 1


def test_live_mode_stores_none_when_ocr_finds_nothing(rig: Rig) -> None:
    rig.ocr.text = "   "
    window = stored_window(rig, picture(entries=[entry(image=text_image())]))
    assert (window.text, window.text_source) == (None, None)


def test_a_focused_entry_with_short_uia_keeps_the_longer_of_ocr_and_uia(rig: Rig) -> None:
    rig.ocr.text = "recognized words"
    first = stored_window(
        rig, picture(entries=[entry(focused=True, image=text_image())], uia="short")
    )
    assert (first.text, first.text_source) == ("recognized words", "ocr")


def test_short_uia_wins_over_shorter_ocr(rig: Rig) -> None:
    rig.ocr.text = "tiny"
    window = stored_window(
        rig, picture(entries=[entry(focused=True, image=text_image())], uia="a bit longer text")
    )
    assert (window.text, window.text_source) == ("a bit longer text", "uia")


def test_the_gate_reuses_text_for_an_unchanged_window_keyed_by_hwnd(rig: Rig) -> None:
    rig.layout("m1")
    for ts in (1, 2):
        rig.writer.submit(picture(ts=ts, entries=[entry(hwnd=9, image=text_image())]))
    rig.drain()
    assert rig.ocr.calls == 1
    assert [f.windows[0].text for f in rig.store.added] == ["recognized words"] * 2


def test_the_gate_commits_only_after_the_store_write_succeeded(rig: Rig) -> None:
    rig.layout("m1")
    rig.store.fail_ts = {1}
    for ts in (1, 2):
        rig.writer.submit(picture(ts=ts, entries=[entry(hwnd=9, image=text_image())]))
    rig.drain()
    assert rig.stored_ts() == [2]
    assert rig.ocr.calls == 2


def test_deferred_mode_stores_the_window_capture_and_no_text(rig: Rig) -> None:
    rig.layout("m1")
    rig.writer.submit(picture(defer=True, entries=[entry(hwnd=3, image=text_image())]))
    rig.drain()
    window = rig.store.added[0].windows[0]
    assert (window.text, window.text_source) == (None, None)
    assert window.capture_webp is not None
    assert Image.open(io.BytesIO(window.capture_webp)).size == (800, 600)
    assert rig.ocr.calls == 0
    stored = rig.store.get(1)
    assert stored is not None
    assert rig.store.window_capture(stored.windows[0].id) == window.capture_webp


def test_deferred_mode_with_long_uia_stores_text_and_no_capture(rig: Rig) -> None:
    window = stored_window(
        rig,
        picture(defer=True, entries=[entry(focused=True, image=text_image())], uia=LONG_UIA),
    )
    assert (window.text, window.text_source, window.capture_webp) == (LONG_UIA, "uia", None)


def test_deferred_mode_with_short_uia_still_stores_a_capture(rig: Rig) -> None:
    window = stored_window(
        rig, picture(defer=True, entries=[entry(focused=True, image=text_image())], uia="short")
    )
    assert window.capture_webp is not None
    assert window.text is None


@pytest.mark.parametrize("defer", [False, True])
def test_an_app_entry_without_image_gets_uia_text_if_focused_else_none(
    rig: Rig, *, defer: bool
) -> None:
    pic = picture(
        defer=defer,
        entries=[entry(0, 1, focused=True), entry(1, 2)],
        uia="short",
    )
    focused = stored_window(rig, pic, 0)
    other = rig.store.added[-1].windows[1]
    assert (focused.text, focused.text_source, focused.capture_webp) == ("short", "uia", None)
    assert (other.text, other.text_source, other.capture_webp) == (None, None, None)
    assert rig.ocr.calls == 0


def test_the_composite_is_never_read_and_uia_is_ignored_without_a_focused_entry(
    rig: Rig,
) -> None:
    window = stored_window(
        rig, picture(image=text_image(), entries=[entry(focused=False)], uia=LONG_UIA)
    )
    assert (window.text, window.text_source) == (None, None)
    assert rig.ocr.calls == 0


# --- failures ------------------------------------------------------------------------------


def test_a_store_failure_is_recorded_logged_by_type_and_the_writer_goes_on(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.layout("m1")
    rig.store.fail_ts = {1}
    rig.writer.submit(picture(ts=1, entries=[entry(title="secret title")]))
    assert not rig.writer.lost("m1")
    rig.writer.submit(picture(ts=2))
    with caplog.at_level(logging.DEBUG):
        rig.drain()
    assert rig.states("m1") == [("failed", 1, 1), ("shown", 2, 2)]
    assert rig.stored_ts() == [2]
    assert not rig.writer.lost("m1")  # the later picture stored
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_an_ocr_failure_fails_the_picture_without_raising(rig: Rig) -> None:
    rig.layout("m1")
    rig.ocr.error = OSError("boom")
    rig.writer.submit(picture(ts=1, entries=[entry(image=text_image())]))
    rig.drain()
    assert rig.states("m1") == [("failed", 1, 1)]
    assert rig.writer.lost("m1")
    assert rig.stored_ts() == []


def test_an_encode_failure_fails_the_picture(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(image: Image.Image, quality: int = 70) -> bytes:
        del image, quality
        raise ValueError

    monkeypatch.setattr(imaging, "encode_webp", broken)
    rig.layout("m1")
    rig.writer.submit(picture(ts=1))
    rig.drain()
    assert rig.states("m1") == [("failed", 1, 1)]
    assert rig.writer.lost("m1")


def test_a_picture_for_an_unknown_monitor_fails_without_a_state_row(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.layout("m1")
    for ts in (1, 2, 3):
        rig.writer.submit(picture("ghost", ts))
    with caplog.at_level(logging.DEBUG):
        rig.drain()
    assert rig.writer.lost("ghost")
    assert rig.stored_ts() == []
    assert rig.states("ghost") == []
    assert rig.writer.problems(0) == [f"{SCREEN_IMAGES_FAILING}@ghost"]


def test_a_monitor_log_failure_on_a_note_or_layout_is_logged_and_skipped(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.layout("m1")
    rig.drain()
    rig.monitors.fail_state_ts = {2}
    rig.writer.note("m1", "hidden", 2)
    rig.monitors.fail_present = True
    rig.layout("m1", "m2", ts=3)
    rig.writer.submit(picture(ts=4))
    with caplog.at_level(logging.DEBUG):
        rig.drain()
    assert rig.states("m1") == [("shown", 4, 4)]
    assert rig.stored_ts() == [4]
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


def test_failing_pictures_become_a_health_problem_after_three_in_a_row(rig: Rig) -> None:
    rig.layout("m1")
    rig.store.fail_ts = {1, 2, 3}
    for ts in (1, 2):
        rig.writer.submit(picture(ts=ts))
    rig.drain()
    assert rig.writer.problems(0) == []
    rig.writer.submit(picture(ts=3))
    rig.drain()
    assert rig.writer.problems(0) == [f"{SCREEN_IMAGES_FAILING}@Monitor 1"]
    assert rig.writer.problems(4.5) == []  # runs older than `since` do not count


def test_a_stored_picture_resets_the_failure_run(rig: Rig) -> None:
    rig.layout("m1")
    rig.store.fail_ts = {1, 2, 4}
    for ts in (1, 2, 3, 4):
        rig.writer.submit(picture(ts=ts))
    rig.drain()
    assert rig.writer.problems(0) == []


def test_one_monitor_failing_is_enough_while_others_store(rig: Rig) -> None:
    rig.layout("m1", "m2")
    for ts in (1, 2, 3):
        rig.writer.submit(picture("m1", ts))
        rig.writer.submit(picture("m2", ts + 10))
        rig.drain()
    assert rig.writer.problems(0) == []
    rig.store.fail_ts = {4, 5, 6}
    for ts in (4, 5, 6):
        rig.writer.submit(picture("m1", ts))
        rig.writer.submit(picture("m2", ts + 10))
        rig.drain()
    assert rig.writer.problems(0) == [f"{SCREEN_IMAGES_FAILING}@Monitor 1"]


def test_each_failing_monitor_is_reported_with_its_label_left_to_right(rig: Rig) -> None:
    rig.writer.layout(
        [
            monitor(key="right", rect=Rect(1920, 0, 3840, 1080), primary=False),
            monitor(key="left", rect=Rect(0, 0, 1920, 1080)),
        ],
        0,
    )
    rig.store.fail_ts = {4, 5, 6}
    for ts in (4, 5, 6):
        rig.writer.submit(picture("left", ts))
        rig.writer.submit(picture("right", ts))
        rig.drain()
    assert rig.writer.problems(0) == [
        f"{SCREEN_IMAGES_FAILING}@Monitor 1",
        f"{SCREEN_IMAGES_FAILING}@Monitor 2",
    ]


def test_only_the_failing_monitor_is_named_while_the_other_stores(rig: Rig) -> None:
    rig.layout("m1", "m2")
    rig.store.fail_ts = {4, 5, 6}
    for ts in (1, 2, 3):
        rig.writer.submit(picture("m1", ts + 3))
        rig.writer.submit(picture("m2", ts))
        rig.drain()
    assert rig.writer.problems(0) == [f"{SCREEN_IMAGES_FAILING}@Monitor 1"]


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.ENOSPC, "No space left on device"),
        sqlcipher3.OperationalError("database or disk is full"),
    ],
)
def test_a_full_disk_is_its_own_problem_and_marks_the_monitor_lost(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def full(frame: NewFrame) -> int:
        del frame
        raise error

    rig.layout("m1")
    monkeypatch.setattr(rig.store, "add", full)
    for ts in (1, 2, 3):
        rig.writer.submit(picture(ts=ts))
    rig.drain()
    assert rig.writer.problems(0) == [f"{SCREEN_DISK_FULL}@Monitor 1"]
    assert rig.writer.lost("m1")
    assert [state for state, _, _ in rig.states("m1")] == ["failed"]
    monkeypatch.undo()
    rig.writer.submit(picture(ts=4))
    rig.drain()
    assert rig.writer.problems(0) == []


def test_a_layout_that_failed_to_write_is_written_before_the_next_picture(rig: Rig) -> None:
    rig.monitors.fail_present = True
    rig.layout("m1")
    rig.writer.submit(picture(ts=1))
    rig.drain()
    assert rig.stored_ts() == []
    assert rig.writer.lost("m1")
    rig.monitors.fail_present = False
    rig.writer.submit(picture(ts=2))
    rig.drain()
    assert rig.stored_ts() == [2]
    assert rig.states("m1") == [("shown", 2, 2)]


def test_a_layout_that_stays_unwritten_is_logged_once(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.monitors.fail_present = True
    rig.layout("m1")
    with caplog.at_level(logging.WARNING):
        for ts in (1, 2, 3):
            rig.writer.note("m1", "hidden", ts)
            rig.writer.submit(picture(ts=ts))
        rig.drain()
    assert caplog.text.count("monitor layout not written") == 1


def test_a_full_disk_while_writing_the_layout_is_a_full_disk_problem(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(rows: object, ts_ms: int) -> dict[str, int]:
        del rows, ts_ms
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(rig.monitors, "present", full)
    rig.layout("m1")
    for ts in (1, 2, 3):
        rig.writer.submit(picture(ts=ts))
    rig.drain()
    assert rig.writer.problems(0) == [f"{SCREEN_DISK_FULL}@Monitor 1"]


def test_capture_failures_count_one_event_per_call_and_zero_resets(rig: Rig) -> None:
    rig.writer.capture_failures("m1", 5)
    rig.writer.capture_failures("m1", 2)
    assert rig.writer.problems(0) == []
    rig.writer.capture_failures("m1", 1)
    assert rig.writer.problems(0) == [f"{SCREEN_IMAGES_FAILING}@m1"]
    rig.writer.capture_failures("m1", 0)
    assert rig.writer.problems(0) == []
    rig.writer.capture_failures("m1", 1)
    rig.writer.capture_failures("m1", 1)
    rig.writer.capture_failures("m1", 0)
    rig.writer.capture_failures("m1", 1)
    assert rig.writer.problems(0) == []


def test_a_stored_picture_does_not_reset_the_capture_run(rig: Rig) -> None:
    rig.layout("m1")
    rig.writer.capture_failures("m1", 1)
    rig.writer.capture_failures("m1", 1)
    rig.writer.submit(picture())
    rig.drain()
    rig.writer.capture_failures("m1", 1)
    assert rig.writer.problems(0) == [f"{SCREEN_IMAGES_FAILING}@Monitor 1"]


# --- lost ----------------------------------------------------------------------------------


def test_while_lost_shown_notes_are_discarded_but_hidden_and_failed_are_written(
    rig: Rig,
) -> None:
    rig.layout("m1")
    rig.store.fail_ts = {1}
    rig.writer.submit(picture(ts=1))
    rig.writer.note("m1", "shown", 2)
    rig.writer.note("m1", "hidden", 3)
    rig.writer.note("m1", "failed", 4)
    rig.drain()
    assert rig.states("m1") == [("failed", 1, 1), ("hidden", 3, 3), ("failed", 4, 4)]
    assert rig.writer.lost("m1")  # notes never clear it


def test_an_older_picture_stored_after_the_drop_does_not_clear_lost(rig: Rig) -> None:
    rig.layout("m1")
    rig.store.gate = threading.Event()
    seen: list[bool] = []

    def watch(state: MonitorState, ts: int) -> None:
        if state == "dropped":
            seen.append(rig.writer.lost("m1"))
        del ts

    rig.monitors.on_state = watch
    thread, stop = rig.start()
    rig.writer.submit(picture(ts=1))
    assert rig.store.entered.wait(10)
    for ts in (2, 3, 4, 5):
        rig.writer.submit(picture(ts=ts))  # the fourth queued one drops ts 2
    rig.store.gate.set()
    stop.set()
    thread.join(10)
    assert seen == [True]  # picture 1 was stored before the drop run was written
    assert rig.stored_ts() == [1, 3, 4, 5]
    assert not rig.writer.lost("m1")


# --- run and close -------------------------------------------------------------------------


def test_run_writes_what_is_queued_when_stopped_and_returns(rig: Rig) -> None:
    rig.layout("m1")
    rig.store.gate = threading.Event()
    thread, stop = rig.start()
    rig.writer.submit(picture(ts=1))
    assert rig.store.entered.wait(10)
    rig.writer.submit(picture(ts=2))
    rig.writer.note("m1", "hidden", 3)
    stop.set()
    rig.store.gate.set()
    thread.join(10)
    assert not thread.is_alive()
    assert rig.stored_ts() == [1, 2]
    assert rig.states("m1") == [("shown", 1, 2), ("hidden", 3, 3)]


def test_close_discards_whatever_is_still_queued(rig: Rig) -> None:
    rig.layout("m1")
    rig.writer.submit(picture(ts=1))
    rig.writer.note("m1", "hidden", 2)
    rig.writer.close()
    rig.drain()
    assert rig.stored_ts() == []
    assert rig.states("m1") == []
