"""The screen loop, driven by fakes and a manual clock."""

from __future__ import annotations

import io
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from capture_fakes import (
    LONG_TEXT,
    SCREEN,
    FakeMedia,
    FakeOcr,
    FakeScreen,
    FakeUia,
    FakeWindows,
    blank,
    window,
    with_text_block,
)
from store_reads import latest_frames
from waits import wait_for

from akashic import child
from akashic.capture import main as capture_main
from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.ocr_gate import MAX_CACHED_WINDOWS, OcrGate
from akashic.capture.ports import CapturePlatform, Rect, TextBlock, WindowInfo
from akashic.capture.screen import STATS_LOG_INTERVAL_S, ScreenCapture, Settings
from akashic.capture.text import MAX_TEXT_CHARS
from akashic.capture.uia_budget import UiaBudget
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from sqlite3 import Connection

    from akashic.config import Paths

MON_B = Rect(800, 0, 1600, 600)


@dataclass
class Rig:
    capture: ScreenCapture
    screen: FakeScreen
    windows: FakeWindows
    uia: FakeUia
    ocr: FakeOcr
    store: FrameStore
    conn: Connection
    clock: list[float] = field(default_factory=lambda: [0.0])

    def frames(self) -> list[tuple[str, int, str | None]]:
        rows = latest_frames(self.conn, 100)
        return [(f.trigger, f.monitor, f.text_source) for f in reversed(rows)]


@pytest.fixture
def make_rig(paths: Paths) -> Iterator[Callable[..., Rig]]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    store = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))

    def build(
        *,
        monitors: list[Rect] | None = None,
        user: UserExclusions | None = None,
        settings: Settings | None = None,
        budget: UiaBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Rig:
        screen = FakeScreen(rects=monitors or [SCREEN])
        windows = FakeWindows(focused=window())
        uia = FakeUia(texts={1: LONG_TEXT})
        ocr = FakeOcr()
        counter = iter(range(1_000_000, 10**9))
        capture = ScreenCapture(
            screen=screen,
            windows=windows,
            uia=uia,
            gate=OcrGate(ocr),
            store=store,
            exclusions=Exclusions(user or UserExclusions()),
            settings=settings or Settings(min_gap_s=0.2),  # tests step in 0.1 s
            budget=budget,
            clock=clock,
            now_ms=lambda: next(counter),
            browsers=BrowserState(),
            games=GameLog(conn),
        )
        return Rig(capture, screen, windows, uia, ocr, store, conn)

    yield build
    conn.close()


def test_ocr_cache_forgets_old_windows_and_recomputes_their_text() -> None:
    gate = OcrGate(FakeOcr())
    image = with_text_block(blank())
    for index in range(MAX_CACHED_WINDOWS):
        result = gate.read(str(index), image, SCREEN)
        gate.commit(str(index), result)
    recent = gate.read("0", image, SCREEN)
    assert not recent.ocr_ran
    gate.commit("0", recent)
    gate.commit("new-window", gate.read("new-window", image, SCREEN))
    assert gate.read("1", image, SCREEN).ocr_ran
    assert not gate.read("0", image, SCREEN).ocr_ran


def test_first_tick_stores_a_frame_with_uia_text(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    assert (frame.process, frame.window_title, frame.text_source) == ("notepad.exe", "Notes", "uia")
    assert frame.text is not None
    assert "real window text" in frame.text
    assert rig.store.image(frame) is not None
    assert rig.ocr.calls == 0
    assert rig.capture.stats["stored"] == 1


def test_unchanged_screen_is_not_stored_again(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.capture.tick(0.0)
    rig.capture.note("input", 0.0)
    rig.capture.tick(0.5)  # input fires; same pixels, same window
    rig.capture.tick(6.0)  # idle fires; still the same
    assert rig.capture.stats["stored"] == 1
    assert rig.capture.stats["unchanged"] >= 2


def test_window_image_covers_only_the_selected_window(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.focused = window(rect=Rect(100, 80, 500, 380))
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    data = rig.store.image(frame)
    assert data is not None
    from PIL import Image  # noqa: PLC0415 - decode just this fixture

    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (400, 300)


@pytest.mark.parametrize("change", ["focus", "title", "url", "lock", "password"])
def test_changes_during_capture_are_dropped_before_storage(
    make_rig: Callable[..., Rig], change: str, paths: Paths
) -> None:
    rig = make_rig()
    rig.windows.focused = window(process="chrome.exe")
    rig.uia.urls[1] = "https://example.org/one"

    def switch() -> None:
        if change == "focus":
            rig.windows.focused = window(hwnd=2, process="taskmgr.exe")
        elif change == "title":
            rig.windows.focused = window(process="chrome.exe", title="New tab")
        elif change == "url":
            rig.uia.urls[1] = "https://example.org/two"
        elif change == "lock":
            rig.windows.locked = True
        else:
            rig.uia.password = True

    rig.screen.on_grab = switch
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn) == []
    assert rig.capture.stats["window_changed"] == 1
    assert list(paths.media_dir.rglob("*.enc")) == []


def test_unavailable_window_does_not_fall_back_to_monitor(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    assert rig.screen.grabs == 0  # never a monitor grab
    frames = latest_frames(rig.conn)
    assert len(frames) == 1  # a text-only row
    assert frames[0].image_path is None
    assert rig.capture.stats["stored_no_image"] == 1


def test_changed_pixels_or_title_are_stored(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.capture.tick(0.0)
    rig.capture.tick(0.4)  # the initial focus trigger fires: nothing changed
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.note("input", 1.0)
    rig.capture.tick(1.6)
    rig.windows.focused = window(title="Notes - edited")  # title only
    rig.capture.tick(2.0)  # poll notes "title"
    rig.capture.tick(2.6)
    titles = [f.window_title for f in reversed(latest_frames(rig.conn))]
    assert titles == ["Notes", "Notes", "Notes - edited"]
    assert [f[0] for f in rig.frames()] == ["idle", "input", "title"]


def test_focus_change_triggers_a_capture(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.capture.tick(0.0)
    rig.windows.focused = window(hwnd=2, process="code.exe", title="main.py")
    rig.uia.texts[2] = LONG_TEXT
    rig.capture.tick(1.0)  # poll sees a new hwnd, debounce 0.3 s
    rig.capture.tick(1.4)
    assert [f[0] for f in rig.frames()] == ["idle", "focus"]
    assert latest_frames(rig.conn)[0].process == "code.exe"


def test_ocr_gate_is_the_fallback_and_is_reused(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.uia.texts[1] = ["too short"]
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.tick(0.0)
    first = latest_frames(rig.conn)[0]
    assert (first.text, first.text_source) == ("recognized words", "ocr")
    assert rig.ocr.calls == 1

    # Something unrelated far from the text changes: the frame is new, but the OCR crop is not.
    changed = with_text_block(blank())
    changed.paste("#e1e1e1", (700, 500, 790, 590))  # no strong edges: not text
    rig.screen.frames[0] = changed
    rig.capture.tick(6.0)
    second = latest_frames(rig.conn)[0]
    assert second.id != first.id
    assert (second.text, second.text_source) == ("recognized words", "ocr")
    assert rig.ocr.calls == 1


def test_uia_text_wins_over_empty_ocr(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.uia.texts[1] = ["short"]
    rig.ocr.text = ""
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    assert (frame.text, frame.text_source) == ("short", "uia")


def test_controls_only_uia_read_falls_back_to_ocr(make_rig: Callable[..., Rig]) -> None:
    """Buttons, tabs and menus are not content: a window showing only them is read by OCR."""
    rig = make_rig()
    controls = "File Edit View Selection Go Run Terminal Help Settings Accounts Explorer"
    rig.uia.texts[1] = [TextBlock(controls, Rect(0, 0, 800, 30), content=False)]
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    assert (frame.text, frame.text_source) == ("recognized words", "ocr")
    assert rig.ocr.calls == 1


def test_content_beside_controls_is_kept_without_the_controls(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.uia.texts[1] = [
        TextBlock("Back Forward Reload", Rect(0, 0, 800, 30), content=False),
        *LONG_TEXT,
    ]
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    assert frame.text_source == "uia"
    assert frame.text == LONG_TEXT[0].text
    assert rig.ocr.calls == 0


def test_stored_window_text_is_capped(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    line = "Minlo a parle du nouveau jeu de survie pendant tout l'apres-midi. " * 4
    blocks = [TextBlock(f"{i:05d} {line}", Rect(0, 20 * i, 800, 20 * i + 18)) for i in range(400)]
    assert sum(len(b.text) for b in blocks) > 2 * MAX_TEXT_CHARS
    rig.uia.texts[1] = blocks
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    assert frame.text is not None
    assert frame.text_source == "uia"
    assert MAX_TEXT_CHARS - len(blocks[0].text) - 2 < len(frame.text) <= MAX_TEXT_CHARS
    assert frame.text.startswith("00000 ")  # the cap keeps the start, in reading order


def test_failed_uia_read_falls_back_to_ocr(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.uia.texts[1] = None
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn)[0].text_source == "ocr"


def test_expensive_app_skips_uia_and_uses_ocr(make_rig: Callable[..., Rig]) -> None:
    budget = UiaBudget()
    budget.record("notepad.exe", 400, 0.0)
    rig = make_rig(budget=budget)
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.tick(1.0)  # 1 s after a critical walk: still inside the 20 s backoff
    assert rig.uia.text_reads == []
    assert latest_frames(rig.conn)[0].text_source == "ocr"


def test_screen_without_text_stores_a_frame_without_text(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.uia.texts[1] = []
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    assert (frame.text, frame.text_source) == (None, None)
    assert rig.ocr.calls == 0


def test_black_frames_keep_metadata_but_drop_the_image(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.frames[0] = blank("black")
    rig.uia.texts[1] = ["Some Show - Netflix"]
    rig.capture.tick(0.0)
    frame = latest_frames(rig.conn)[0]
    assert frame.image_path is None
    assert frame.text == "Some Show - Netflix"
    assert rig.ocr.calls == 0
    assert rig.capture.stats["stored_black"] == 1


# --- exclusions: nothing is grabbed, read or written ---------------------------------------


def test_browser_url_is_normalized_and_stored(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.focused = window(process="zen.exe", title="Video - YouTube")
    rig.uia.urls[1] = "youtube.com/watch?v=abc"
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn)[0].url == "https://youtube.com/watch?v=abc"


def test_locked_session_and_missing_window(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.locked = True
    rig.capture.tick(0.0)
    assert rig.capture.stats["locked"] == 1
    rig.windows.locked = False
    rig.windows.focused = None
    rig.capture.tick(6.0)
    assert rig.capture.stats["no_window"] == 1
    assert rig.screen.grabs == 0


# --- multiple monitors ---------------------------------------------------------------------


def test_second_monitor_uses_the_window_under_it_and_ocr(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(monitors=[SCREEN, MON_B])
    rig.windows.at[MON_B.center] = window(hwnd=7, process="vlc.exe", title="Movie", rect=MON_B)
    rig.screen.frames[1] = with_text_block(blank())
    rig.capture.tick(0.0)
    assert rig.frames() == [("idle", 0, "uia"), ("idle", 1, "ocr")]
    assert rig.uia.text_reads == [1]  # UIA only ever walks the focused window


def test_other_monitors_are_captured_less_often(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(monitors=[SCREEN, MON_B])
    rig.windows.at[MON_B.center] = window(hwnd=7, process="vlc.exe", title="Movie", rect=MON_B)
    rig.capture.tick(0.0)
    rig.screen.frames[0] = with_text_block(blank())
    rig.screen.frames[1] = with_text_block(blank())
    rig.capture.tick(6.0)  # focused monitor's idle interval (5 s) passed, the other's (15 s) not
    assert rig.frames() == [("idle", 0, "uia"), ("idle", 1, None), ("focus", 0, "uia")]
    rig.capture.tick(16.0)  # now the second monitor is due, and its pixels changed
    assert rig.frames()[-1] == ("idle", 1, "ocr")


def test_input_captures_focus_and_cursor_monitors(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(monitors=[SCREEN, MON_B])
    rig.windows.at[MON_B.center] = window(hwnd=7, process="vlc.exe", title="Movie", rect=MON_B)
    rig.capture.tick(0.0)
    rig.capture.tick(0.4)  # the start-up focus trigger
    rig.screen.frames[0] = with_text_block(blank())
    rig.screen.frames[1] = with_text_block(blank())
    rig.windows.cursor = (900, 100)  # scrolling the unfocused monitor
    rig.capture.note("input", 1.0)
    rig.capture.tick(1.5)
    assert [f[:2] for f in rig.frames()[-2:]] == [("input", 0), ("input", 1)]
    rig.windows.cursor = (-50, -50)  # off every monitor: only the focused one
    rig.screen.frames[0] = blank("gray")
    rig.capture.note("input", 2.0)
    rig.capture.tick(2.5)
    assert rig.frames()[-1][:2] == ("input", 0)


def test_input_activity_is_noticed_by_polling(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.capture.tick(0.0)
    rig.capture.tick(0.4)  # the start-up focus trigger
    rig.screen.frames[0] = with_text_block(blank())
    rig.windows.last_input = 123  # the user typed or moved the mouse
    rig.capture.tick(1.0)
    rig.capture.tick(1.5)
    assert rig.frames()[-1][0] == "input"


def test_continuous_input_still_captures(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.capture.tick(0.0)
    rig.capture.tick(0.4)
    rig.screen.frames[0] = with_text_block(blank())
    now = 1.0
    while now < 3.2:  # the mouse never stops moving
        rig.windows.last_input += 1
        rig.capture.tick(now)
        now += 0.1
    assert "input" in [f[0] for f in rig.frames()]


# --- rate limit, errors, loop --------------------------------------------------------------


def test_captures_are_spaced_by_the_minimum_gap(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(settings=Settings(min_gap_s=1.0))
    rig.capture.tick(0.0)
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.note("input", 0.0)
    rig.capture.tick(0.5)  # too soon: deferred, not lost
    assert rig.capture.stats["stored"] == 1
    rig.capture.tick(1.0)
    assert rig.capture.stats["stored"] == 2  # the deferred triggers ran once the gap passed


def test_a_failing_capture_is_counted_and_the_loop_survives(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.fail = True
    rig.capture.tick(0.0)
    assert rig.capture.stats["error"] == 1
    rig.screen.fail = False
    rig.capture.tick(6.0)
    assert rig.capture.stats["stored"] == 1


def test_failed_write_does_not_mark_ocr_as_indexed(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.uia.texts[1] = None
    rig.screen.frames[0] = with_text_block(blank())
    real_add = rig.store.add
    calls: list[int] = []

    def flaky(frame: object) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise OSError
        return real_add(frame)  # type: ignore[arg-type]

    monkeypatch.setattr(rig.store, "add", flaky)
    rig.capture.tick(0.0)
    assert rig.capture.stats["error"] == 1
    rig.capture.tick(6.0)
    assert rig.ocr.calls == 2  # the first result was never committed, so OCR ran again
    assert rig.capture.stats["stored"] == 1


def test_run_loop_captures_pauses_and_stops(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(settings=Settings(focus_poll_s=0.01, min_gap_s=0.0))
    paused_passes: list[int] = []
    real_reset = rig.capture.reset

    def counting_reset() -> None:
        paused_passes.append(1)  # the loop calls reset() on every pass while paused
        real_reset()

    monkeypatch.setattr(rig.capture, "reset", counting_reset)
    stop, paused = threading.Event(), threading.Event()
    thread = threading.Thread(target=rig.capture.run, args=(stop, paused))
    thread.start()

    try:
        assert wait_for(lambda: rig.capture.stats["stored"] >= 1)
        passes_before = len(paused_passes)
        paused.set()
        assert wait_for(lambda: len(paused_passes) >= passes_before + 2)  # the loop noticed
        grabs = rig.screen.grabs
        rig.screen.frames[0] = with_text_block(blank())
        rig.windows.last_input += 1
        passes_before = len(paused_passes)
        assert wait_for(lambda: len(paused_passes) >= passes_before + 3)
        assert rig.screen.grabs == grabs  # paused: nothing is read or grabbed
        paused.clear()
        assert wait_for(lambda: rig.capture.stats["stored"] >= 2)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


# --- wiring --------------------------------------------------------------------------------


class OpenStdin:
    """The parent's end of the pipe: the key, then nothing until ``close()`` (no EOF stop)."""

    def __init__(self, key: DataKey) -> None:
        self._key = io.BytesIO(key.raw())
        self._closed = threading.Event()

    def readinto(self, buffer: memoryview, /) -> int:
        return self._key.readinto(buffer)

    def readline(self, size: int = -1, /) -> bytes:
        del size
        self._closed.wait()
        return b""

    def close(self) -> None:
        self._closed.set()


def run_capture_child(
    paths: Paths, key: DataKey, *, paused: bool, until: Callable[[], bool]
) -> FakeScreen:
    """Run the capture child's body over fakes until ``until()`` holds, then stop it."""
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    conn.close()
    screen = FakeScreen(frames={0: with_text_block(blank())})
    platform = CapturePlatform(
        screen=screen,
        windows=FakeWindows(focused=window()),
        uia=FakeUia(texts={1: LONG_TEXT}),
        ocr=FakeOcr(),
        media=FakeMedia(),
    )
    stdin = OpenStdin(key)
    reached: list[bool] = []

    def watch(ctx: child.ChildContext) -> None:
        deadline = time.monotonic() + 15
        while not ctx.stop.is_set() and time.monotonic() < deadline:
            if until():
                reached.append(True)
                break
            time.sleep(0.02)
        ctx.stop.set()
        stdin.close()

    def body(ctx: child.ChildContext) -> None:
        threading.Thread(target=watch, args=(ctx,), daemon=True).start()
        capture_main.run_capture(ctx, platform)

    argv = ["--data-dir", str(paths.root), *(["--paused"] if paused else [])]
    code = child.run_child("capture", argv, body, stdin, io.BytesIO())
    stdin.close()
    assert code == child.EXIT_OK
    assert reached == [True], "the capture child never reached the expected state"
    assert "capture stats" in (paths.logs_dir / "capture.log").read_text("utf-8")
    return screen


def frame_rows(paths: Paths, key: DataKey) -> list[tuple[str, str | None, str | None]]:
    reopened = db.connect(paths.db_file, key.subkey("db"))
    try:
        return reopened.execute("SELECT process, text_source, image_path FROM frames").fetchall()
    finally:
        reopened.close()


def test_run_capture_wires_storage(paths: Paths) -> None:
    """The real wiring (FrameStore over SQLCipher, encrypted media) stores what it captures."""
    key = DataKey.generate()

    def stored() -> bool:
        return bool(frame_rows(paths, key)) and any(paths.media_dir.rglob("*.enc"))

    run_capture_child(paths, key, paused=False, until=stored)
    rows = frame_rows(paths, key)
    assert rows[0][:2] == ("notepad.exe", "uia")
    image_path = rows[0][2]
    assert image_path is not None
    encrypted = paths.media_dir / (image_path + ".enc")  # only the encrypted file exists
    assert encrypted.is_file()
    assert not (paths.media_dir / image_path).exists()
    data = MediaStore(paths.media_dir, key.subkey("media")).read(image_path)
    from PIL import Image  # noqa: PLC0415 - decode just this fixture

    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (SCREEN.width, SCREEN.height)


def test_run_capture_started_paused_captures_nothing(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused_passes: list[int] = []
    real_reset = ScreenCapture.reset

    def counting_reset(self: ScreenCapture) -> None:
        paused_passes.append(1)  # the loop calls reset() on every pass while paused
        real_reset(self)

    monkeypatch.setattr(ScreenCapture, "reset", counting_reset)
    key = DataKey.generate()
    screen = run_capture_child(paths, key, paused=True, until=lambda: len(paused_passes) >= 5)
    assert screen.grabs == 0  # started paused: nothing was read or grabbed
    assert frame_rows(paths, key) == []
    assert list(paths.media_dir.rglob("*.enc")) == []


def test_body_stays_idle_when_capture_is_disabled(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    conn.close()
    monkeypatch.setenv("AKASHIC_CAPTURE", "0")
    stdin = io.BytesIO(key.raw())  # EOF right after the key: the child stops itself
    code = child.run_child(
        "capture", ["--data-dir", str(paths.root)], capture_main._body, stdin, io.BytesIO()
    )
    assert code == child.EXIT_OK
    assert "disabled" in (paths.logs_dir / "capture.log").read_text("utf-8")


# --- the loop survives a failing iteration ---------------------------------------


def test_screen_loop_survives_a_failing_iteration(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    calls = [0]
    real = rig.screen.monitors

    def flaky() -> list[Rect]:
        calls[0] += 1
        if calls[0] == 1:
            msg = "display configuration changed"
            raise OSError(msg)
        return real()

    rig.screen.monitors = flaky  # type: ignore[method-assign]  # fault injection
    stop, paused = threading.Event(), threading.Event()
    died: list[BaseException] = []

    def body() -> None:
        try:
            rig.capture.run(stop, paused)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            died.append(exc)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: rig.screen.grabs > 0 or bool(died), 10)
        assert died == []
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=10)


# --- a window without pixels still stores its text -------------------------------


def test_window_without_pixels_still_stores_its_text(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    frames = latest_frames(rig.conn)
    assert len(frames) == 1
    assert frames[0].image_path is None
    assert frames[0].text is not None
    assert "real window text" in frames[0].text


def test_a_window_without_pixels_and_without_text_stores_a_bare_row(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.uia.texts.clear()  # UI Automation has nothing to read
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    frames = latest_frames(rig.conn)
    assert len(frames) == 1
    assert (frames[0].process, frames[0].window_title) == ("notepad.exe", "Notes")
    assert (frames[0].text, frames[0].text_source, frames[0].image_path) == (None, None, None)
    assert rig.capture.stats["stored_no_image"] == 1
    assert rig.screen.grabs == 0


def test_a_window_without_pixels_is_never_read_by_ocr(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.uia.texts[1] = ["short"]  # under the UIA threshold: with pixels this would go to OCR
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    assert rig.ocr.calls == 0
    assert rig.capture.stats["stored_no_image"] == 1
    assert latest_frames(rig.conn)[0].text_source in ("uia", None)


def test_repeated_no_image_captures_of_the_same_text_store_one_row(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    rig.capture.note("input", 0.5)
    rig.capture.tick(1.0)
    rig.capture.tick(7.0)  # idle
    rig.capture.tick(14.0)
    assert len(latest_frames(rig.conn)) == 1
    assert rig.capture.stats["stored_no_image"] == 1
    assert rig.capture.stats["unchanged_no_image"] >= 2


def test_a_changed_title_or_text_stores_a_new_no_image_row(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    rig.windows.focused = window(title="Notes - edited")
    rig.capture.tick(2.0)
    assert len(latest_frames(rig.conn)) == 2
    rig.uia.texts[1] = ["Completely different words that are long enough to count as real text."]
    rig.capture.note("input", 3.0)
    rig.capture.tick(4.0)
    assert len(latest_frames(rig.conn)) == 3
    assert rig.capture.stats["stored_no_image"] == 3


def test_a_pixel_row_between_two_identical_no_image_captures_makes_the_second_new(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    first_text = rig.uia.texts[1]
    rig.screen.unavailable = True
    rig.capture.tick(0.0)  # no-image row (K, T)
    rig.screen.unavailable = False
    rig.uia.texts[1] = ["Another text that is long enough to count as real window text."]
    rig.capture.note("input", 1.0)
    rig.capture.tick(1.5)  # pixel row for K with text X
    rig.screen.unavailable = True
    rig.uia.texts[1] = first_text
    rig.capture.note("input", 3.0)
    rig.capture.tick(3.5)  # no-image (K, T) again: not a duplicate of the pixel row
    assert rig.capture.stats["stored"] == 1
    assert rig.capture.stats["stored_no_image"] == 2
    assert len(latest_frames(rig.conn)) == 3


def test_pixels_coming_back_are_stored_normally(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    rig.screen.unavailable = False
    rig.capture.note("input", 1.0)
    rig.capture.tick(1.5)  # same window, same text, but now with an image
    assert rig.capture.stats["stored"] == 1
    frames = latest_frames(rig.conn)
    assert len(frames) == 2
    assert rig.store.image(frames[0]) is not None


@pytest.mark.parametrize("change", ["focus", "title", "url", "lock", "password"])
def test_a_text_only_row_is_dropped_when_the_window_changes(
    make_rig: Callable[..., Rig], change: str
) -> None:
    rig = make_rig()
    rig.windows.focused = window(process="chrome.exe")
    rig.uia.urls[1] = "https://example.org/one"

    def switch() -> None:
        if change == "focus":
            rig.windows.focused = window(hwnd=2, process="taskmgr.exe")
        elif change == "title":
            rig.windows.focused = window(process="chrome.exe", title="New tab")
        elif change == "url":
            rig.uia.urls[1] = "https://example.org/two"
        elif change == "lock":
            rig.windows.locked = True
        else:
            rig.uia.password = True

    rig.screen.unavailable = True
    rig.screen.on_grab = switch
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn) == []
    assert rig.capture.stats["window_changed"] == 1
    assert rig.screen.grabs == 0


def test_an_excluded_window_without_pixels_stores_nothing_and_is_not_read(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(user=UserExclusions(processes=("notepad.exe",)))
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn) == []
    assert rig.uia.text_reads == []
    assert rig.capture.stats["stored_no_image"] == 0


@pytest.mark.parametrize("password", [True, None])
def test_a_password_field_without_pixels_stores_nothing(
    make_rig: Callable[..., Rig], *, password: bool | None
) -> None:
    rig = make_rig()
    rig.uia.password = password
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn) == []
    assert rig.uia.text_reads == []


def test_an_excluded_url_without_pixels_stores_nothing(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(user=UserExclusions(urls=("*mybank.example*",)))
    rig.windows.focused = window(process="chrome.exe", title="Bank - Google Chrome")
    rig.uia.urls[1] = "https://mybank.example/compte"
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn) == []
    assert rig.uia.text_reads == []


def test_the_text_only_row_keeps_the_url(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.focused = window(process="chrome.exe", title="Page - Google Chrome")
    rig.uia.urls[1] = "https://example.org/page"
    rig.uia.texts[1] = LONG_TEXT
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    frames = latest_frames(rig.conn)
    assert len(frames) == 1
    assert frames[0].url is not None
    assert "example.org" in frames[0].url
    assert frames[0].image_path is None


def test_game_mode_without_pixels_still_stores_nothing(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.unavailable = True
    rig.windows.focused = WindowInfo(7, "game.exe", "The Game", SCREEN, has_frame=False)
    rig.capture.tick(0.0)
    assert rig.capture.stats["image_unavailable"] == 1
    assert rig.capture.stats["stored_no_image"] == 0
    assert latest_frames(rig.conn) == []


# --- loop guard: faults, backoff, log hygiene ------------------------------------------------


class CountingStop(threading.Event):
    """Records each wait without waiting; stops the loop after ``limit`` waits."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout)
        if len(self.timeouts) >= self.limit:
            self.set()
        return self.is_set()


def stepping_clock(step: float) -> Callable[[], float]:
    now = [0.0]

    def clock() -> float:
        now[0] += step
        return now[0]

    return clock


def loop_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_failed_tick_is_counted_logged_by_type_and_the_loop_goes_on(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(clock=stepping_clock(1.0))
    real_tick = rig.capture.tick
    calls = [0]

    def flaky(now: float) -> None:
        calls[0] += 1
        if calls[0] == 1:
            msg = "secret title https://private.example/x"
            raise RuntimeError(msg)
        real_tick(now)

    monkeypatch.setattr(rig.capture, "tick", flaky)
    stop = CountingStop(6)
    with caplog.at_level(logging.WARNING, logger="akashic.capture.screen"):
        rig.capture.run(stop, threading.Event())
    assert rig.capture.stats["loop_error"] == 1
    assert calls[0] > 1  # the loop kept going
    assert rig.capture.stats["stored"] >= 1  # and a healthy iteration stored a frame
    warnings = loop_warnings(caplog)
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0]
    assert "secret" not in caplog.text
    assert "private.example" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_a_monitor_enumeration_error_does_not_end_the_loop(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(clock=stepping_clock(10.0))  # every iteration refreshes the monitor list
    real = rig.screen.monitors
    calls = [0]

    def flaky() -> list[Rect]:
        calls[0] += 1
        if calls[0] == 1:
            msg = "display configuration changed"
            raise OSError(msg)
        return real()

    monkeypatch.setattr(rig.screen, "monitors", flaky)
    rig.capture.run(CountingStop(5), threading.Event())
    assert rig.capture.stats["loop_error"] == 1
    assert rig.screen.grabs > 0


def test_a_failing_game_close_while_paused_does_not_end_the_loop(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(clock=stepping_clock(1.0))
    rig.windows.focused = WindowInfo(7, "game.exe", "The Game", SCREEN, has_frame=False)
    rig.capture.tick(0.0)  # a game session opens
    msg = "database is locked"

    def failing_close(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(GameLog, "close", failing_close)
    paused = threading.Event()
    paused.set()
    stop = CountingStop(4)
    rig.capture.run(stop, paused)  # reset() raises while paused
    assert rig.capture.stats["loop_error"] >= 1
    assert len(stop.timeouts) == 4  # the loop ran to the stop, not to its death


def test_a_persistent_fault_neither_spins_nor_floods_the_log(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(clock=stepping_clock(0.01))
    focus_poll_s = rig.capture._settings.focus_poll_s

    def always(now: float) -> None:
        del now
        msg = "boom"
        raise OSError(msg)

    monkeypatch.setattr(rig.capture, "tick", always)
    rig.capture.note("input", -1.0)  # a trigger already due would otherwise give a 0 s wait
    stop = CountingStop(300)
    with caplog.at_level(logging.WARNING, logger="akashic.capture.screen"):
        rig.capture.run(stop, threading.Event())
    assert rig.capture.stats["loop_error"] == 299
    assert all(t is not None and t >= focus_poll_s for t in stop.timeouts[1:])
    assert len(loop_warnings(caplog)) == 1


def test_a_fault_with_a_due_trigger_waits_at_least_the_focus_poll(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(clock=stepping_clock(1.0))
    focus_poll_s = rig.capture._settings.focus_poll_s
    first = [True]
    real_tick = rig.capture.tick

    def once(now: float) -> None:
        if first[0]:
            first[0] = False
            rig.capture.note("input", 0.0)  # a trigger that is already due
            msg = "boom"
            raise OSError(msg)
        real_tick(now)

    monkeypatch.setattr(rig.capture, "tick", once)
    stop = CountingStop(3)
    rig.capture.run(stop, threading.Event())
    assert stop.timeouts[1] is not None
    assert stop.timeouts[1] >= focus_poll_s


def test_alternating_fault_types_are_each_logged_once_per_interval(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(clock=stepping_clock(1.0))
    calls = [0]

    def alternating(now: float) -> None:
        del now
        calls[0] += 1
        if calls[0] % 2:
            msg = "a"
            raise OSError(msg)
        msg = "b"
        raise ValueError(msg)

    monkeypatch.setattr(rig.capture, "tick", alternating)
    with caplog.at_level(logging.WARNING, logger="akashic.capture.screen"):
        rig.capture.run(CountingStop(40), threading.Event())
    warnings = loop_warnings(caplog)
    assert len(warnings) == 2
    assert sum("OSError" in w for w in warnings) == 1
    assert sum("ValueError" in w for w in warnings) == 1


def test_a_fault_type_is_logged_again_after_the_interval(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(clock=stepping_clock(STATS_LOG_INTERVAL_S / 4))

    def always(now: float) -> None:
        del now
        msg = "boom"
        raise OSError(msg)

    monkeypatch.setattr(rig.capture, "tick", always)
    with caplog.at_level(logging.WARNING, logger="akashic.capture.screen"):
        rig.capture.run(CountingStop(30), threading.Event())
    assert 2 <= len(loop_warnings(caplog)) < 30


def test_a_base_exception_is_not_swallowed_by_the_loop(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()

    def interrupt(now: float) -> None:
        del now
        raise KeyboardInterrupt

    monkeypatch.setattr(rig.capture, "tick", interrupt)
    with pytest.raises(KeyboardInterrupt):
        rig.capture.run(CountingStop(5), threading.Event())
    assert rig.capture.stats["loop_error"] == 0
