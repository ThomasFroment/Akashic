"""Gaming mode (detector and its place in the capture loop), game rows, app-session derivation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from capture_fakes import SCREEN, FakeOcr, FakeScreen, FakeUia, FakeWindows, blank, window

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.gaming import GameMode, GameSettings, covers_monitor, is_game_window
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.ports import Rect, WindowInfo
from akashic.capture.screen import ScreenCapture
from akashic.config import Exclusions as UserExclusions
from akashic.pipeline.sessions import IDLE_GAP_MS, derive_app_sessions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GAME_HEARTBEAT_MAX_AGE_MS, GameLog, gaming_now

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from sqlite3 import Connection

    from akashic.config import Paths

SECOND = Rect(800, 0, 1600, 600)


def game(process: str = "game.exe", title: str = "The Game", rect: Rect = SCREEN) -> WindowInfo:
    return WindowInfo(7, process, title, rect, has_frame=False)


def side(
    hwnd: int = 2, process: str = "discord.exe", title: str = "Discord", rect: Rect = SECOND
) -> WindowInfo:
    return window(hwnd=hwnd, process=process, title=title, rect=rect)


# --- detector ------------------------------------------------------------------------------


def test_covers_monitor() -> None:
    assert covers_monitor(SCREEN, [SCREEN]) == SCREEN
    assert covers_monitor(Rect(-5, -5, 805, 605), [SCREEN]) == SCREEN  # borders outside is fine
    assert covers_monitor(Rect(0, 0, 800, 560), [SCREEN]) is None  # taskbar visible
    assert covers_monitor(SCREEN, [SECOND]) is None


@pytest.mark.parametrize(
    ("win", "expected"),
    [
        (game(), True),
        (game(process="chrome.exe"), False),  # F11 / fullscreen video
        (game(process="Explorer.exe".lower()), False),  # desktop shell
        (game(rect=Rect(0, 0, 800, 560)), False),  # not covering the monitor
        (window(process="game.exe", rect=SCREEN), False),  # has a title bar: just maximized
        (game(process=""), False),
    ],
)
def test_is_game_window(win: WindowInfo, expected: bool) -> None:  # noqa: FBT001
    assert is_game_window(win, [SCREEN], GameSettings()) is expected


def test_user_lists_override_the_heuristic() -> None:
    settings = GameSettings(games=("framed.exe",), not_games=("odd.exe",))
    assert is_game_window(window(process="framed.exe", rect=SCREEN), [SCREEN], settings)
    assert not is_game_window(game(process="odd.exe"), [SCREEN], settings)
    assert not is_game_window(
        window(process="framed.exe", rect=Rect(0, 0, 10, 10)), [SCREEN], settings
    )


def test_game_mode_start_grace_end_and_switch() -> None:
    mode = GameMode(GameSettings(end_grace_s=5))
    assert mode.observe(window(), [SCREEN], 0).started is None
    started = mode.observe(game(), [SCREEN], 1)
    assert started.started is not None
    assert started.started.process == "game.exe"
    assert mode.active
    assert mode.observe(game(), [SCREEN], 2) == type(started)()  # no change
    assert mode.observe(None, [SCREEN], 4).ended is None  # alt-tab flicker
    assert mode.observe(game(), [SCREEN], 5).ended is None  # back: grace restarts
    assert mode.observe(window(), [SCREEN], 9).ended is None
    ended = mode.observe(window(), [SCREEN], 10.5)
    assert ended.ended is not None
    assert not mode.active
    mode.observe(game(process="a.exe"), [SCREEN], 20)
    switched = mode.observe(game(process="b.exe"), [SCREEN], 21)
    assert switched.ended is not None
    assert switched.ended.process == "a.exe"
    assert switched.started is not None
    assert switched.started.process == "b.exe"
    assert mode.stop() is not None
    assert mode.stop() is None


# --- the loop in gaming mode -------------------------------------------------------------


@dataclass
class Rig:
    capture: ScreenCapture
    screen: FakeScreen
    windows: FakeWindows
    uia: FakeUia
    ocr: FakeOcr
    store: FrameStore
    conn: Connection
    reports: list[bool]

    def frames(self) -> list[tuple[str, str | None, str | None, int]]:
        return self.conn.execute(
            "SELECT trigger, process, text, image_path IS NOT NULL FROM frames ORDER BY id"
        ).fetchall()

    def games(self) -> list[tuple[str, str | None, int, int, int]]:
        return self.conn.execute(
            "SELECT process, window_title, start_ms, end_ms, closed FROM game_sessions ORDER BY id"
        ).fetchall()


@pytest.fixture
def make_rig(paths: Paths) -> Iterator[Callable[..., Rig]]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    store = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))

    def build(user: UserExclusions | None = None, interval_s: float = 60.0) -> Rig:
        screen = FakeScreen(frames={0: blank("gray")})
        windows = FakeWindows(focused=window())
        uia = FakeUia(texts={1: ["x" * 80], 7: ["should never be read"]})
        ocr = FakeOcr()
        counter = iter(range(1_000_000, 10**9))
        reports: list[bool] = []
        capture = ScreenCapture(
            screen=screen,
            windows=windows,
            uia=uia,
            gate=OcrGate(ocr),
            store=store,
            exclusions=Exclusions(user or UserExclusions()),
            now_ms=lambda: next(counter),
            games=GameLog(conn),
            game_settings=GameSettings(interval_s=interval_s, heartbeat_s=10),
            on_game=reports.append,
            browsers=BrowserState(),
        )
        return Rig(capture, screen, windows, uia, ocr, store, conn, reports)

    yield build
    conn.close()


def test_game_session_is_logged_with_one_screenshot_per_interval(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.windows.focused = game()
    for t in (0.0, 0.5, 30.0, 59.0):  # inside the first minute: exactly one frame
        rig.capture.tick(t)
    assert [f[:2] for f in rig.frames()] == [("game", "game.exe")]
    rig.capture.tick(60.5)
    assert len(rig.frames()) == 2
    assert all(f[3] == 1 and f[2] is None for f in rig.frames())  # images kept, no text
    assert rig.uia.text_reads == []
    assert rig.ocr.calls == 0
    assert rig.reports == [True]
    assert rig.games()[0][:2] == ("game.exe", "The Game")
    assert rig.games()[0][4] == 0


def test_locking_clears_gaming_even_when_the_end_row_cannot_be_written(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.windows.focused = game()
    rig.capture.tick(0.0)

    def busy(*_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(GameLog, "close", busy)
    rig.windows.focused = window(process="lockapp.exe")
    rig.capture.tick(6.0)  # past the grace period
    assert rig.reports == [True, False]


def test_input_triggers_are_ignored_and_other_monitors_use_the_idle_rate_in_game_mode(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.screen.rects = [SCREEN, SECOND]
    rig.windows.focused = game()
    rig.windows.at[SECOND.center] = side()
    rig.capture.tick(0.0)
    rig.capture.note("input", 1.0)
    rig.capture.tick(2.0)
    assert [f[:2] for f in rig.frames()] == [("game", "game.exe"), ("game", "discord.exe")]
    rig.windows.at[SECOND.center] = side(title="Discord 2")
    rig.capture.tick(20.0)  # monitor 2 is due again at the idle rate; the input trigger is not
    assert [f[1] for f in rig.frames()] == ["game.exe", "discord.exe", "discord.exe"]
    assert {f[0] for f in rig.frames()} == {"game"}


def test_game_ends_after_grace_and_normal_capture_resumes(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.focused = game()
    rig.capture.tick(0.0)
    rig.windows.focused = window()
    rig.capture.tick(3.0)  # within the grace period: still gaming, nothing captured
    assert len(rig.frames()) == 1
    rig.capture.tick(6.0)  # grace over: the game row closes and a normal frame is taken
    row = rig.games()[0]
    assert row[4] == 1
    assert rig.reports == [True, False]
    assert [f[0] for f in rig.frames()][-1] != "game"
    assert len(rig.frames()) == 2


def test_heartbeat_keeps_the_open_row_fresh(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.focused = game()
    rig.capture.tick(0.0)
    first_end = rig.games()[0][3]
    rig.capture.tick(5.0)
    assert rig.games()[0][3] == first_end
    rig.capture.tick(11.0)
    assert rig.games()[0][3] > first_end
    assert gaming_now(rig.conn, rig.games()[0][3])
    assert not gaming_now(rig.conn, rig.games()[0][3] + GAME_HEARTBEAT_MAX_AGE_MS + 1)


def test_reset_and_close_end_the_game_row(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.focused = game()
    rig.capture.tick(0.0)
    rig.capture.reset()  # pausing
    assert rig.games()[0][4] == 1
    assert not gaming_now(rig.conn, rig.games()[0][3])
    rig.capture.tick(1.0)  # resumed, still in the game: a new row
    assert len(rig.games()) == 2
    rig.capture.close()
    assert [g[4] for g in rig.games()] == [1, 1]
    rig.capture.close()  # idempotent


def test_excluded_or_locked_games_get_no_frame_but_are_still_logged(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(UserExclusions(window_titles=("*the game*",)))
    rig.windows.focused = game()
    rig.capture.tick(0.0)
    assert rig.frames() == []
    assert rig.capture.stats["excluded:user_title"] == 1
    assert len(rig.games()) == 1
    rig2 = make_rig()
    rig2.windows.focused = game()
    rig2.windows.locked = True
    rig2.capture.tick(0.0)
    assert rig2.capture.stats["locked"] == 1


def test_black_exclusive_fullscreen_frame_drops_only_the_image(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.screen.frames[0] = blank("black")
    rig.windows.focused = game()
    rig.capture.tick(0.0)
    assert rig.frames() == [("game", "game.exe", None, 0)]
    assert rig.capture.stats["game_black"] == 1


def test_game_failure_is_contained(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.screen.fail = True
    rig.windows.focused = game()
    rig.capture.tick(0.0)
    assert rig.capture.stats["error"] == 1
    assert len(rig.games()) == 1


def test_stale_game_rows_are_closed_at_startup(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    log = GameLog(rig.conn)
    log.open(process="x.exe", window_title=None, ts_ms=5)
    assert log.close_stale() == 1
    assert not gaming_now(rig.conn, 6)


# --- app sessions ----------------------------------------------------------------------------


def add(  # noqa: PLR0913 - a frame's worth of fields
    c: Connection,
    ts_ms: int,
    process: str = "a.exe",
    title: str = "T",
    url: str | None = None,
    *,
    focused: bool = True,
) -> None:
    FrameStore(c, None).add(  # pyright: ignore[reportArgumentType] - no image is written
        NewFrame(
            ts_ms=ts_ms,
            monitor=0,
            trigger="idle",
            process=process,
            window_title=title,
            url=url,
            focused=focused,
        )
    )


def sessions(c: Connection) -> list[tuple[str, str, int, int, int, int]]:
    return c.execute(
        "SELECT process, window_title, start_ms, end_ms, frame_count, closed"
        " FROM app_sessions ORDER BY id"
    ).fetchall()


def test_consecutive_frames_of_one_window_form_one_session(conn: Connection) -> None:
    for ts in (1000, 6000, 11000):
        add(conn, ts)
    assert derive_app_sessions(conn, 12_000) == 3
    assert sessions(conn) == [("a.exe", "T", 1000, 11000, 3, 0)]


def test_changing_window_title_or_url_or_process_starts_a_new_session(conn: Connection) -> None:
    add(conn, 1000)
    add(conn, 2000, title="T2")
    add(conn, 3000, title="T2", url="https://x.example")
    add(conn, 4000, process="b.exe", title="T2", url="https://x.example")
    derive_app_sessions(conn, 5000)
    assert [(s[0], s[1], s[2], s[3], s[5]) for s in sessions(conn)] == [
        ("a.exe", "T", 1000, 1000, 1),
        ("a.exe", "T2", 2000, 2000, 1),
        ("a.exe", "T2", 3000, 3000, 1),
        ("b.exe", "T2", 4000, 4000, 0),
    ]


def test_derivation_is_incremental_and_idempotent(conn: Connection) -> None:
    add(conn, 1000)
    derive_app_sessions(conn, 2000)
    assert derive_app_sessions(conn, 2000) == 0
    add(conn, 3000)  # same window: extends the open row
    add(conn, 4000, title="Other")
    assert derive_app_sessions(conn, 5000) == 2
    assert [(s[2], s[3], s[4], s[5]) for s in sessions(conn)] == [
        (1000, 3000, 2, 1),
        (4000, 4000, 1, 0),
    ]


def test_unfocused_monitor_frames_do_not_break_a_focus_period(conn: Connection) -> None:
    add(conn, 1000)
    add(conn, 2000, process="chat.exe", title="Chat", focused=False)
    add(conn, 3000)
    derive_app_sessions(conn, 4000)
    assert sessions(conn) == [("a.exe", "T", 1000, 3000, 2, 0)]


def test_idle_gap_closes_the_session_and_a_return_starts_a_new_one(conn: Connection) -> None:
    add(conn, 1000)
    derive_app_sessions(conn, 1000 + IDLE_GAP_MS + 1)  # nothing newer for a long while
    assert sessions(conn)[0][5] == 1
    add(conn, 1000 + IDLE_GAP_MS + 5000)  # same window again
    derive_app_sessions(conn, 1000 + IDLE_GAP_MS + 6000)
    assert [(s[2], s[5]) for s in sessions(conn)] == [(1000, 1), (1000 + IDLE_GAP_MS + 5000, 0)]


def test_a_gap_between_frames_splits_even_when_derived_in_one_pass(conn: Connection) -> None:
    add(conn, 1000)
    add(conn, 1000 + IDLE_GAP_MS + 1)
    derive_app_sessions(conn, 1000 + IDLE_GAP_MS + 2)
    assert len(sessions(conn)) == 2


def test_no_frames_no_sessions(conn: Connection) -> None:
    assert derive_app_sessions(conn, 1) == 0
    assert sessions(conn) == []


# --- game mode: windows on the other monitors ----------------------------------------------


def side_rig(make_rig: Callable[..., Rig], **kw: object) -> Rig:
    rig = make_rig(**kw)
    rig.screen.rects = [SCREEN, SECOND]
    rig.screen.frames[1] = blank("blue")
    rig.windows.focused = game()
    rig.windows.at[SECOND.center] = side()
    return rig


def side_frames(rig: Rig) -> list[tuple[object, ...]]:
    return rig.conn.execute(
        "SELECT trigger, monitor, focused, process, window_title, url, text, image_path IS NOT NULL"
        " FROM frames WHERE process != 'game.exe' ORDER BY id"
    ).fetchall()


def job_keys(rig: Rig) -> list[str]:
    return [r[0] for r in rig.conn.execute("SELECT key FROM jobs ORDER BY rowid")]


def side_id(rig: Rig) -> int:
    return rig.conn.execute("SELECT id FROM frames WHERE process='discord.exe'").fetchone()[0]


def test_side_window_is_stored_as_a_game_frame_without_text_and_queues_ocr(
    make_rig: Callable[..., Rig],
) -> None:
    rig = side_rig(make_rig)
    rig.uia.texts[2] = ["x" * 80]
    rig.capture.tick(0.0)
    assert side_frames(rig) == [("game", 1, 0, "discord.exe", "Discord", None, None, 1)]
    assert job_keys(rig).count(f"ocr_frame:{side_id(rig)}") == 1
    assert rig.uia.text_reads == []
    assert rig.ocr.calls == 0


def test_side_window_is_searchable_after_the_worker_ran(make_rig: Callable[..., Rig]) -> None:
    from akashic.pipeline.jobs import Job  # noqa: PLC0415
    from akashic.pipeline.ocr_frames import OcrFrameHandler  # noqa: PLC0415

    rig = side_rig(make_rig)
    rig.capture.tick(0.0)
    fid = side_id(rig)
    handler = OcrFrameHandler(rig.store, lambda: FakeOcr(text="zebrafish party"))
    handler.run(Job(1, "ocr_frame", f"ocr_frame:{fid}", {"frame_id": fid}, 0, 1))
    assert rig.conn.execute(
        "SELECT rowid FROM frames_fts WHERE frames_fts MATCH 'zebrafish'"
    ).fetchall() == [(fid,)]


def test_side_captures_follow_the_15_second_interval_and_the_game_shot_the_60(
    make_rig: Callable[..., Rig],
) -> None:
    rig = side_rig(make_rig)
    rig.capture.tick(0.0)
    rig.windows.at[SECOND.center] = side(title="Discord 2")
    rig.capture.tick(14.0)
    assert len(side_frames(rig)) == 1
    rig.capture.tick(15.5)
    assert len(side_frames(rig)) == 2
    assert [f[1] for f in rig.frames()].count("game.exe") == 1
    rig.capture.tick(60.5)
    assert [f[1] for f in rig.frames()].count("game.exe") == 2


def test_unchanged_side_window_writes_nothing(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    rig.capture.tick(0.0)
    rig.capture.tick(16.0)
    assert len(side_frames(rig)) == 1
    assert rig.capture.stats["unchanged"] == 1


def test_black_side_frame_stores_no_image_and_no_job(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    rig.screen.frames[1] = blank("black")
    rig.capture.tick(0.0)
    rows = side_frames(rig)
    assert len(rows) == 1
    assert rows[0][7] == 0
    assert len(job_keys(rig)) == 1  # the game screenshot only
    assert rig.capture.stats["stored_black"] == 1


def test_side_window_of_the_game_process_is_skipped(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    rig.windows.at[SECOND.center] = side(hwnd=9, process="game.exe", title="The Game")
    rig.capture.tick(0.0)
    assert rig.capture.stats["game_window"] == 1
    assert side_frames(rig) == []
    assert rig.uia.text_reads == []


def test_game_spanning_monitors_is_never_a_side_window(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    wide = game(rect=Rect(0, 0, 1598, 600))
    rig.windows.focused = wide
    rig.windows.at[SECOND.center] = wide  # topmost at the second centre too
    rig.capture.tick(0.0)
    assert rig.capture.stats["game_focused"] == 1
    assert side_frames(rig) == []
    assert [f[1] for f in rig.frames()] == ["game.exe"]


def test_side_capture_needs_a_focused_window_on_a_known_monitor(
    make_rig: Callable[..., Rig],
) -> None:
    rig = side_rig(make_rig)
    rig.capture.tick(0.0)
    rig.capture.tick(14.0)  # still a game; the side monitor is due again at 15
    rig.capture.stats.clear()
    before = rig.screen.grabs
    rig.windows.focused = None  # in the grace period: no foreground window
    rig.capture.tick(16.0)
    rig.windows.focused = game(rect=Rect(5000, 5000, 5800, 5600))  # centre off every monitor
    rig.capture.tick(17.0)
    assert rig.capture.stats["game_started"] == 0
    assert rig.capture.stats["stored"] == 0
    assert rig.screen.grabs == before
    assert "loop_error" not in rig.capture.stats
    assert "error" not in rig.capture.stats


def test_single_monitor_game_mode_is_unchanged(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.windows.focused = game()
    for t in (0.0, 20.0, 40.0):
        rig.capture.tick(t)
    assert rig.screen.grabs == 1
    assert set(rig.capture.stats) == {"game_started", "game_stored"}


def test_side_browser_runs_readers_and_stores_the_url(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    rig.windows.at[SECOND.center] = side(process="chrome.exe", title="Video - Google Chrome")
    rig.uia.urls[2] = "youtube.com/watch?v=abc"
    rig.capture.tick(0.0)
    rows = side_frames(rig)
    assert len(rows) == 1
    assert rows[0][5] == "https://youtube.com/watch?v=abc"
    assert rig.uia.text_reads == []


@pytest.mark.parametrize("reason", ["url", "title"])
def test_excluded_side_browser_writes_nothing(make_rig: Callable[..., Rig], reason: str) -> None:
    user = (
        UserExclusions(urls=("*mybank.example*",))
        if reason == "url"
        else UserExclusions(window_titles=("*secret*",))
    )
    rig = side_rig(make_rig, user=user)
    rig.windows.at[SECOND.center] = side(process="chrome.exe", title="secret - Google Chrome")
    rig.uia.urls[2] = "https://mybank.example/compte"
    rig.capture.tick(0.0)
    assert side_frames(rig) == []
    assert any(k.startswith("excluded:") for k in rig.capture.stats)


def test_locked_screen_stores_no_side_frame(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    rig.windows.locked = True
    rig.capture.tick(0.0)
    assert side_frames(rig) == []
    assert rig.capture.stats["locked"] >= 1


@pytest.mark.parametrize("change", ["title", "focus", "lock"])
def test_side_window_changed_during_the_grab_is_dropped(
    make_rig: Callable[..., Rig], change: str
) -> None:
    rig = side_rig(make_rig)
    del rig.windows.at[SECOND.center]
    rig.capture.tick(0.0)  # game starts, no side window yet
    rig.windows.at[SECOND.center] = side()

    def switch() -> None:
        if change == "title":
            rig.windows.at[SECOND.center] = side(title="Other")
        elif change == "focus":
            rig.windows.focused = side()
        else:
            rig.windows.locked = True

    rig.screen.on_grab = switch
    rig.capture.tick(20.0)
    assert side_frames(rig) == []
    assert rig.capture.stats["window_changed"] == 1


def test_hung_side_window_keeps_a_metadata_only_row(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    rig.screen.unavailable = True
    rig.capture.tick(0.0)
    assert side_frames(rig) == [("game", 1, 0, "discord.exe", "Discord", None, None, 0)]
    assert f"ocr_frame:{side_id(rig)}" not in job_keys(rig)


def test_input_noted_during_a_game_is_not_replayed_after_it(make_rig: Callable[..., Rig]) -> None:
    rig = side_rig(make_rig)
    rig.capture.tick(0.0)
    rig.capture.note("input", 1.0)
    rig.capture.note("focus", 1.0)
    rig.capture.tick(2.0)
    rig.windows.focused = window()
    rig.capture.tick(6.0)  # grace over: normal mode
    rig.capture.tick(7.0)
    assert {"input", "focus"}.isdisjoint(f[0] for f in rig.frames())


def test_a_fault_in_one_side_capture_does_not_stop_the_others_or_the_game_shot(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    third = Rect(1600, 0, 2400, 600)
    rig.screen.rects = [SCREEN, SECOND, third]
    rig.windows.focused = game()
    rig.windows.at[SECOND.center] = side()
    rig.windows.at[third.center] = side(hwnd=3, process="obs.exe", title="OBS", rect=third)
    real = rig.screen.grab_window

    def flaky(win: WindowInfo) -> object:
        if win.hwnd == 2:
            msg = "boom"
            raise OSError(msg)
        return real(win)

    monkeypatch.setattr(rig.screen, "grab_window", flaky)
    rig.capture.tick(0.0)
    procs = [f[1] for f in rig.frames()]
    assert "obs.exe" in procs
    assert "game.exe" in procs
    assert rig.capture.stats["error"] == 1
    assert len(rig.games()) == 1


def test_failed_side_grabs_count_toward_failing_images_and_a_good_one_clears_them(
    make_rig: Callable[..., Rig],
) -> None:
    rig = side_rig(make_rig)
    rig.screen.unavailable = True
    for t in (0.0, 16.0, 32.0):
        rig.windows.at[SECOND.center] = side(title=f"W{t}")
        rig.capture.tick(t)
    assert rig.capture.images_failing(0.0)
    rig.screen.unavailable = False
    rig.windows.at[SECOND.center] = side(title="ok")
    rig.capture.tick(48.0)
    assert not rig.capture.images_failing(0.0)


def test_every_other_monitor_gets_one_attempt(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rects = [Rect(800 * i, 0, 800 * (i + 1), 600) for i in range(4)]
    rig.screen.rects = rects
    rig.windows.focused = game(rect=rects[1])
    for i in (0, 2, 3):
        rig.windows.at[rects[i].center] = side(hwnd=10 + i, process=f"app{i}.exe", rect=rects[i])
    rig.capture.tick(0.0)
    assert sorted(str(f[3]) for f in side_frames(rig)) == ["app0.exe", "app2.exe", "app3.exe"]
