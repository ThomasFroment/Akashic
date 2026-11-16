"""Gaming mode (detector and its place in the capture loop), game rows, app-session derivation."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest
from capture_fakes import SCREEN, FakeOcr, blank, window, with_text_block
from memory_data import add_frame, add_monitor

from akashic.capture.gaming import GameMode, GameSettings, covers_monitor, is_game_window
from akashic.capture.health import SCREEN_IMAGES_FAILING
from akashic.capture.ports import Rect, WindowInfo
from akashic.config import Exclusions as UserExclusions
from akashic.pipeline.sessions import IDLE_GAP_MS, AppBand, app_bands, derive_app_sessions
from akashic.storage.sessions import GAME_HEARTBEAT_MAX_AGE_MS, GameLog, gaming_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from screen_rig import Rig

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


@pytest.fixture
def make_game(make_rig: Callable[..., Rig]) -> Callable[..., Rig]:
    def build(*, interval_s: float = 60.0, end_grace_s: float = 5.0, **kwargs: object) -> Rig:
        return make_rig(
            game_settings=GameSettings(
                interval_s=interval_s, end_grace_s=end_grace_s, heartbeat_s=10
            ),
            shown=False,
            **kwargs,
        )

    return build


def frames(rig: Rig) -> list[tuple[str, str | None, str | None, int]]:
    return rig.conn.execute(
        "SELECT f.trigger, w.process, w.text, f.image_path IS NOT NULL FROM frames f"
        " JOIN frame_windows w ON w.frame_id = f.id ORDER BY f.id"
    ).fetchall()


def games(rig: Rig) -> list[tuple[str, str | None, int, int, int]]:
    return rig.conn.execute(
        "SELECT process, window_title, start_ms, end_ms, closed FROM game_sessions ORDER BY id"
    ).fetchall()


def job_keys(rig: Rig) -> list[str]:
    return [r[0] for r in rig.conn.execute("SELECT key FROM jobs ORDER BY rowid")]


def test_game_session_is_logged_with_one_screenshot_per_interval(
    make_game: Callable[..., Rig],
) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    for t in (0.0, 0.1, 0.5, 30.0, 59.0):  # inside the first minute: exactly one picture
        rig.tick(t)
    rig.drain()
    assert [f[:2] for f in frames(rig)] == [("game", "game.exe")]
    rig.repaint(7, blank("white"))
    rig.repaint(7, with_text_block(blank()))  # the screen moved on
    rig.tick(60.2)
    rig.drain()
    assert len(frames(rig)) == 2
    assert all(f[3] == 1 and f[2] is None for f in frames(rig))  # images kept, no text
    assert rig.uia.text_reads == []
    assert rig.ocr.calls == 0
    assert rig.game_flags == [True]
    assert rig.conn.execute(
        "SELECT count(*), count(capture_path) FROM frame_windows"
    ).fetchone() == (2, 2)
    assert job_keys(rig) == [f"ocr_window:{i}" for i in (1, 2)]
    assert games(rig)[0][:2] == ("game.exe", "The Game")
    assert games(rig)[0][4] == 0


def test_locking_clears_gaming_even_when_the_end_row_cannot_be_written(
    make_game: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.tick(0.0)

    def busy(*_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(GameLog, "close", busy)
    rig.windows.locked = True
    rig.tick(6.0)
    assert rig.game_flags == [True, False]


def test_input_triggers_are_ignored_and_other_monitors_use_the_idle_rate_in_game_mode(
    make_game: Callable[..., Rig],
) -> None:
    rig = make_game(monitors=[SCREEN, SECOND])
    rig.show(2, "discord.exe", "Discord", SECOND, focus=False)
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.settle(0.0)
    rig.windows.last_input = 1
    rig.tick(2.0)
    rig.tick(2.5)
    rig.drain()
    assert sorted(str(f[1]) for f in frames(rig)) == ["discord.exe", "game.exe"]
    rig.show(2, "discord.exe", "Discord 2", SECOND, focus=False)
    rig.tick(20.0)  # monitor 2 is due again at the idle rate; the input trigger is not
    rig.drain()
    assert [f[1] for f in frames(rig)].count("discord.exe") == 2
    assert {f[0] for f in frames(rig)} == {"game"}


def test_game_ends_after_grace_and_normal_capture_resumes(make_game: Callable[..., Rig]) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.settle(0.0)
    rig.show(1, "notepad.exe", "Notes")
    rig.tick(3.0)  # within the grace period: still gaming, nothing captured
    assert len(rig.pictures()) == 1
    rig.tick(6.0)  # grace over: the game row closes and a normal picture follows
    rig.tick(6.1)
    rig.drain()
    assert games(rig)[0][4] == 1
    assert rig.game_flags == [True, False]
    assert [f[0] for f in frames(rig)][-1] != "game"
    assert len(frames(rig)) == 2


def test_heartbeat_keeps_the_open_row_fresh(make_game: Callable[..., Rig]) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.tick(0.0)
    first_end = games(rig)[0][3]
    rig.tick(5.0)
    assert games(rig)[0][3] == first_end
    rig.tick(11.0)
    assert games(rig)[0][3] > first_end
    assert gaming_now(rig.conn, games(rig)[0][3])
    assert not gaming_now(rig.conn, games(rig)[0][3] + GAME_HEARTBEAT_MAX_AGE_MS + 1)


def test_reset_and_close_end_the_game_row(make_game: Callable[..., Rig]) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.tick(0.0)
    rig.capture.reset()  # pausing
    assert games(rig)[0][4] == 1
    assert not gaming_now(rig.conn, games(rig)[0][3])
    rig.tick(1.0)  # resumed, still in the game: a new row
    assert len(games(rig)) == 2
    rig.capture.close()
    assert [g[4] for g in games(rig)] == [1, 1]
    rig.capture.close()  # idempotent


def test_an_excluded_game_gets_no_frame_but_is_still_logged(make_game: Callable[..., Rig]) -> None:
    rig = make_game(user=UserExclusions(processes=("*game*",)))
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.settle(0.0)
    rig.drain()
    assert frames(rig) == []
    assert rig.last_note() == "hidden"
    assert len(games(rig)) == 1


def test_a_locked_session_logs_no_game_and_stores_no_frame(make_game: Callable[..., Rig]) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.windows.locked = True
    rig.settle(0.0)
    rig.drain()
    assert games(rig) == []
    assert frames(rig) == []
    assert rig.facts.reads == 0


def test_black_exclusive_fullscreen_frame_drops_only_the_image(
    make_game: Callable[..., Rig],
) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", image=blank("black"), has_frame=False)
    rig.settle(0.0)
    rig.drain()
    assert frames(rig) == [("game", "game.exe", None, 0)]
    assert job_keys(rig) == []
    assert rig.conn.execute("SELECT count(capture_path) FROM frame_windows").fetchone() == (0,)


def test_a_game_whose_capture_cannot_start_is_still_logged_and_the_failure_reported(
    make_game: Callable[..., Rig],
) -> None:
    rig = make_game()
    rig.captures.refuse.add(7)
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.tick(0.0)
    assert rig.writer.failures[-1] == ("m1", 1)
    assert len(games(rig)) == 1


def test_stale_game_rows_are_closed_at_startup(make_game: Callable[..., Rig]) -> None:
    rig = make_game()
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
    add_frame(c, ts_ms, None, title, process=process, url=url, focused=focused)


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


def test_a_frame_without_a_focused_window_row_is_not_a_session(conn: Connection) -> None:
    add_monitor(conn)
    conn.execute("INSERT INTO frames (ts_ms, monitor_id, trigger) VALUES (1000, 0, 'idle')")
    add(conn, 2000)
    derive_app_sessions(conn, 3000)
    assert sessions(conn) == [("a.exe", "T", 2000, 2000, 1, 0)]


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


def test_unfocused_monitor_frames_never_start_close_or_extend_a_session(conn: Connection) -> None:
    add_frame(conn, 1000, None, "T", process="a.exe", monitor_id=0)
    add_frame(conn, 2000, None, "C", process="chat.exe", monitor_id=1, focused=False)
    add_frame(conn, 9000, None, "C", process="chat.exe", monitor_id=1, focused=False)
    derive_app_sessions(conn, 10_000)
    assert sessions(conn) == [("a.exe", "T", 1000, 1000, 1, 0)]


def test_same_key_frames_at_one_moment_count_once(conn: Connection) -> None:
    add_frame(conn, 1000, None, "T", process="a.exe", monitor_id=0)
    add_frame(conn, 1000, None, "T", process="a.exe", monitor_id=1)
    assert derive_app_sessions(conn, 2000) == 2
    assert sessions(conn) == [("a.exe", "T", 1000, 1000, 1, 0)]


def test_a_stale_same_key_frame_keeps_end_and_count_but_advances_the_watermark(
    conn: Connection,
) -> None:
    add_frame(conn, 5000, None, "T", process="a.exe", monitor_id=0)
    derive_app_sessions(conn, 6000)
    add_frame(conn, 3000, None, "T", process="a.exe", monitor_id=1)
    assert derive_app_sessions(conn, 6000) == 1
    assert sessions(conn) == [("a.exe", "T", 5000, 5000, 1, 0)]
    assert derive_app_sessions(conn, 6000) == 0
    assert conn.execute("SELECT last_frame_id FROM app_sessions").fetchone()[0] == 2


def test_the_return_value_counts_focused_frames_only(conn: Connection) -> None:
    add(conn, 1000)
    add(conn, 2000, focused=False)
    assert derive_app_sessions(conn, 3000) == 1


# --- app bands ----------------------------------------------------------------------------------


def seed_session(  # noqa: PLR0913 - a session row's fields
    c: Connection,
    start: int,
    end: int,
    process: str | None = "a.exe",
    title: str | None = "T",
    *,
    closed: int = 1,
) -> None:
    c.execute(
        "INSERT INTO app_sessions (process, window_title, start_ms, end_ms, frame_count,"
        " last_frame_id, closed) VALUES (?, ?, ?, ?, 1, 0, ?)",
        (process, title, start, end, closed),
    )


def test_app_bands_returns_overlapping_sessions_unclipped_in_order(conn: Connection) -> None:
    seed_session(conn, 5000, 6000, "b.exe", "B")
    seed_session(conn, 1000, 2000, "a.exe", "A")
    seed_session(conn, 1000, 1500, "c.exe", None, closed=0)
    seed_session(conn, 100, 900)  # ends before the range
    seed_session(conn, 9000, 9500)  # starts after the range
    assert app_bands(conn, 1500, 9000, None) == [
        AppBand(1000, 2000, "a.exe", "A"),
        AppBand(1000, 1500, "c.exe", None),
        AppBand(5000, 6000, "b.exe", "B"),
    ]


def test_app_bands_range_edges_and_single_frame_session(conn: Connection) -> None:
    seed_session(conn, 500, 1000)  # end == range start: included
    seed_session(conn, 2000, 2000)  # start == range end: excluded
    seed_session(conn, 1500, 1500)  # single frame inside
    assert [(b.start_ms, b.end_ms) for b in app_bands(conn, 1000, 2000, None)] == [
        (500, 1000),
        (1500, 1500),
    ]


def test_app_bands_empty_or_inverted_range_is_empty(conn: Connection) -> None:
    seed_session(conn, 1000, 2000)
    assert app_bands(conn, 1500, 1500, None) == []
    assert app_bands(conn, 2000, 1000, None) == []


def test_app_bands_process_filter_ignores_case(conn: Connection) -> None:
    seed_session(conn, 1000, 2000, "Chrome.EXE", "X")
    seed_session(conn, 3000, 4000, "other.exe", "Y")
    assert app_bands(conn, 0, 5000, "chrome.exe") == [AppBand(1000, 2000, "Chrome.EXE", "X")]
    assert len(app_bands(conn, 0, 5000, None)) == 2


def test_app_bands_propagates_sqlite_errors() -> None:
    bad = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.Error):
        app_bands(bad, 0, 10, None)


# --- game mode: windows on the other monitors ----------------------------------------------


def side_rig(make_game: Callable[..., Rig], **kw: object) -> Rig:
    rig = make_game(monitors=[SCREEN, SECOND], **kw)
    rig.show(2, "discord.exe", "Discord", SECOND, focus=False, image=blank("blue"))
    rig.show(7, "game.exe", "The Game", image=blank("gray"), has_frame=False)
    return rig


def side_frames(rig: Rig) -> list[tuple[object, ...]]:
    return rig.conn.execute(
        "SELECT f.trigger, f.monitor_id, w.focused, w.process, w.title, w.url, w.text,"
        " f.image_path IS NOT NULL FROM frames f JOIN frame_windows w ON w.frame_id = f.id"
        " WHERE w.process != 'game.exe' ORDER BY f.id"
    ).fetchall()


def side_id(rig: Rig) -> int:
    """The id of the Discord window row, which carries the capture and its job."""
    return rig.conn.execute(
        "SELECT id FROM frame_windows WHERE process = 'discord.exe'"
    ).fetchone()[0]


def test_side_window_is_stored_as_a_game_frame_without_text_and_queues_ocr(
    make_game: Callable[..., Rig],
) -> None:
    rig = side_rig(make_game)
    rig.uia.texts[2] = ["x" * 80]
    rig.settle(0.0)
    rig.drain()
    assert side_frames(rig) == [("game", 2, 0, "discord.exe", "Discord", None, None, 1)]
    assert job_keys(rig).count(f"ocr_window:{side_id(rig)}") == 1
    assert rig.uia.text_reads == []
    assert rig.ocr.calls == 0


def test_side_window_is_searchable_after_the_worker_ran(make_game: Callable[..., Rig]) -> None:
    from akashic.pipeline.ocr_windows import OcrWindowHandler  # noqa: PLC0415
    from akashic.storage.jobs import Job  # noqa: PLC0415

    rig = side_rig(make_game)
    rig.settle(0.0)
    rig.drain()
    wid = side_id(rig)
    handler = OcrWindowHandler(rig.store, lambda: FakeOcr(text="zebrafish party"))
    handler.run(Job(1, "ocr_window", f"ocr_window:{wid}", {"window_id": wid}, 0, 1))
    assert rig.conn.execute(
        "SELECT rowid FROM frame_windows_fts WHERE frame_windows_fts MATCH 'zebrafish'"
    ).fetchall() == [(wid,)]


def test_side_captures_follow_the_15_second_interval_and_the_game_shot_the_60(
    make_game: Callable[..., Rig],
) -> None:
    rig = side_rig(make_game)
    rig.settle(0.0)
    rig.show(2, "discord.exe", "Discord 2", SECOND, focus=False)
    rig.tick(14.0)
    rig.drain()
    assert len(side_frames(rig)) == 1
    rig.tick(15.2)
    rig.drain()
    assert len(side_frames(rig)) == 2
    assert [f[1] for f in frames(rig)].count("game.exe") == 1
    rig.repaint(7, with_text_block(blank()))
    rig.tick(60.2)
    rig.drain()
    assert [f[1] for f in frames(rig)].count("game.exe") == 2


def test_unchanged_side_window_writes_nothing(make_game: Callable[..., Rig]) -> None:
    rig = side_rig(make_game)
    rig.settle(0.0)
    rig.tick(16.0)
    rig.drain()
    assert len(side_frames(rig)) == 1
    assert rig.last_note("m2") == "shown"


def test_black_side_frame_stores_no_image_and_no_job(make_game: Callable[..., Rig]) -> None:
    rig = side_rig(make_game)
    rig.show(2, "discord.exe", "Discord", SECOND, focus=False, image=blank("black"))
    rig.settle(0.0)
    rig.drain()
    rows = side_frames(rig)
    assert len(rows) == 1
    assert rows[0][7] == 0
    assert len(job_keys(rig)) == 1  # the game screenshot only


def test_a_game_spanning_monitors_is_stored_on_both_and_only_its_own_is_not_side(
    make_game: Callable[..., Rig],
) -> None:
    rig = make_game(monitors=[SCREEN, SECOND])
    rig.show(7, "game.exe", "The Game", Rect(0, 0, 1598, 600), has_frame=False)
    rig.settle(0.0)
    sides = {p.monitor_key: p.side for p in rig.writer.pictures}
    assert sides == {"m1": False, "m2": True}
    assert {p.trigger for p in rig.writer.pictures} == {"game"}


@pytest.mark.parametrize("focus", ["none", "off every monitor"])
def test_without_a_focus_monitor_in_the_grace_period_every_monitor_runs_at_15_s(
    make_game: Callable[..., Rig], focus: str
) -> None:
    rig = side_rig(make_game, end_grace_s=60.0)
    rig.settle(0.0)
    rig.windows.focused = (
        None if focus == "none" else game(rect=Rect(5000, 5000, 5800, 5600))  # centre off the desk
    )
    before = (rig.count("m1"), rig.count("m2"))
    rig.tick(14.0)
    assert (rig.count("m1"), rig.count("m2")) == before
    rig.tick(15.1)
    assert (rig.count("m1"), rig.count("m2")) == (before[0] + 1, before[1] + 1)
    assert rig.game_flags == [True]  # still the same game


def test_single_monitor_game_mode_takes_one_picture_a_minute(make_game: Callable[..., Rig]) -> None:
    rig = make_game()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.tick(0.0)
    rig.tick(0.1)
    for t in (20.0, 40.0):
        rig.tick(t)
    assert rig.count("m1") == 1
    assert "loop_error" not in rig.capture.stats_snapshot()


def test_side_browser_runs_readers_and_stores_the_url(make_game: Callable[..., Rig]) -> None:
    rig = side_rig(make_game)
    rig.show(
        2, "chrome.exe", "Video - Google Chrome", SECOND, focus=False, url="youtube.com/watch?v=abc"
    )
    rig.settle(0.0)
    rig.drain()
    rows = side_frames(rig)
    assert len(rows) == 1
    assert rows[0][5] == "https://youtube.com/watch?v=abc"
    assert rig.uia.text_reads == []


def test_excluded_side_browser_writes_nothing(make_game: Callable[..., Rig]) -> None:
    rig = side_rig(make_game, user=UserExclusions(urls=("*mybank.example*",)))
    rig.show(
        2, "chrome.exe", "secret - Google Chrome", SECOND, focus=False,
        url="https://mybank.example/compte",
    )  # fmt: skip
    rig.settle(0.0)
    rig.drain()
    assert side_frames(rig) == []
    assert rig.last_note("m2") == "hidden"


@pytest.mark.parametrize("process", ["", "applicationframehost.exe"])
def test_a_window_with_no_known_process_is_never_a_game_and_is_not_stored(
    make_game: Callable[..., Rig], process: str
) -> None:
    rig = make_game()
    rig.show(7, process, "The Game", has_frame=False)
    rig.settle(0.0)
    rig.drain()
    assert frames(rig) == []
    assert rig.last_note() == "hidden"
    assert games(rig) == []


def test_hung_side_window_keeps_a_metadata_only_row(make_game: Callable[..., Rig]) -> None:
    rig = side_rig(make_game)
    del rig.captures.images[2]  # the window never delivers a frame
    for step in range(6):
        rig.tick(step / 10)
    rig.drain()
    assert side_frames(rig) == [("game", 2, 0, "discord.exe", "Discord", None, None, 0)]
    assert not [k for k in job_keys(rig) if k == f"ocr_window:{side_id(rig)}"]


def test_triggers_noted_during_a_game_are_not_replayed_after_it(
    make_game: Callable[..., Rig],
) -> None:
    rig = side_rig(make_game)
    rig.settle(0.0)
    rig.windows.last_input = 1
    rig.events.push(2, "moved", 1.0)
    rig.show(2, "discord.exe", "Discord 2", SECOND, focus=False)
    rig.tick(2.0)
    rig.show(1, "notepad.exe", "Notes")
    rig.tick(3.0)
    rig.tick(6.0)  # grace over: normal mode
    rig.tick(7.0)
    assert {"input", "focus", "title", "window"}.isdisjoint(p.trigger for p in rig.writer.pictures)


def test_a_failing_side_capture_does_not_stop_the_others_or_the_game_shot(
    make_game: Callable[..., Rig],
) -> None:
    third = Rect(1600, 0, 2400, 600)
    rig = make_game(monitors=[SCREEN, SECOND, third])
    rig.show(2, "discord.exe", "Discord", SECOND, focus=False)
    rig.show(3, "obs.exe", "OBS", third, focus=False)
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.captures.refuse.add(2)
    rig.settle(0.0)
    rig.drain()
    procs = [f[1] for f in frames(rig)]
    assert "obs.exe" in procs
    assert "game.exe" in procs
    assert ("m2", 1) in rig.writer.failures
    assert len(games(rig)) == 1


def test_failed_side_captures_count_toward_the_health_problem_and_a_good_one_clears_them(
    make_game: Callable[..., Rig],
) -> None:
    rig = side_rig(make_game)
    rig.captures.refuse.add(2)
    for t in (0.0, 16.0, 32.0):
        rig.tick(t)
    assert rig.writer.problems(0.0) == [f"{SCREEN_IMAGES_FAILING}@Monitor 2"]
    rig.captures.refuse.clear()
    rig.tick(48.0)
    rig.tick(48.1)
    assert rig.writer.problems(0.0) == []


def test_every_other_monitor_gets_one_attempt(make_game: Callable[..., Rig]) -> None:
    rects = [Rect(800 * i, 0, 800 * (i + 1), 600) for i in range(4)]
    rig = make_game(monitors=rects)
    for i in (0, 2, 3):
        rig.show(10 + i, f"app{i}.exe", "App", rects[i], focus=False)
    rig.show(7, "game.exe", "The Game", rects[1], has_frame=False)
    rig.settle(0.0)
    submitted = [e.process for p in rig.writer.pictures for e in p.entries if p.side]
    assert sorted(str(process) for process in submitted) == ["app0.exe", "app2.exe", "app3.exe"]


# --- D3D exclusive fullscreen (#150) ---------------------------------------------------------

NEW_NOT_GAMES = (
    "mpc-be64.exe",
    "mpc-be.exe",
    "potplayer64.exe",
    "potplayer.exe",
    "potplayermini.exe",
    "potplayermini64.exe",
    "mpvnet.exe",
    "smplayer.exe",
    "kmplayer.exe",
    "kmplayer64.exe",
    "microsoft.media.player.exe",
    "video.ui.exe",
    "ms-teams.exe",
    "mstsc.exe",
    "msrdc.exe",
    "vmconnect.exe",
)


WINDOWED = Rect(900, 50, 1300, 400)  # inside the second monitor


def d3d(process: str = "game.exe", rect: Rect = SCREEN, *, has_frame: bool = False) -> WindowInfo:
    return WindowInfo(7, process, "T", rect, has_frame=has_frame, exclusive_fullscreen=True)


@pytest.mark.parametrize("process", NEW_NOT_GAMES)
def test_players_meeting_and_remote_clients_are_not_borderless_games(process: str) -> None:
    assert not is_game_window(game(process=process), [SCREEN], GameSettings())


@pytest.mark.parametrize("process", ["mpv.exe", "chrome.exe", "game.exe", "discord.exe"])
@pytest.mark.parametrize("has_frame", [True, False])
def test_d3d_evidence_on_a_covered_monitor_is_a_game(process: str, has_frame: bool) -> None:  # noqa: FBT001
    assert is_game_window(d3d(process, has_frame=has_frame), [SCREEN], GameSettings())


@pytest.mark.parametrize("process", ["discord.exe", "game.exe"])
def test_d3d_evidence_on_a_window_covering_no_monitor_is_not_a_game(process: str) -> None:
    assert not is_game_window(d3d(process, WINDOWED), [SCREEN, SECOND], GameSettings())
    assert not is_game_window(d3d(process, Rect(0, 0, 800, 560)), [SCREEN], GameSettings())


def test_a_windowed_d3d_flag_neither_ends_nor_switches_the_session() -> None:
    mode = GameMode(GameSettings(end_grace_s=5))
    mode.observe(game(process="a.exe"), [SCREEN, SECOND], 0)
    for t in (1, 2, 3):
        change = mode.observe(d3d("b.exe", WINDOWED), [SCREEN, SECOND], t)
        assert change.started is None
        assert change.ended is None
    assert mode.active
    assert mode.observe(d3d("b.exe", WINDOWED), [SCREEN, SECOND], 6.5).ended is not None


def test_precedence_empty_process_and_coverage_come_first() -> None:
    settings = GameSettings(games=("game.exe",))
    assert not is_game_window(d3d(""), [SCREEN], settings)
    assert not is_game_window(d3d("game.exe", Rect(0, 0, 10, 10)), [SCREEN], settings)


def test_user_not_games_beats_d3d_and_user_games_beats_the_default_list() -> None:
    settings = GameSettings(games=("mpv.exe",), not_games=("odd.exe",))
    assert not is_game_window(d3d("odd.exe"), [SCREEN], settings)
    assert is_game_window(game(process="mpv.exe"), [SCREEN], settings)
    both = GameSettings(games=("x.exe",), not_games=("x.exe",))
    assert is_game_window(d3d("x.exe"), [SCREEN], both)  # games is checked first


def test_window_identity_ignores_the_d3d_flag() -> None:
    plain = WindowInfo(7, "game.exe", "T", SCREEN, has_frame=False)
    flagged = d3d()
    assert plain == flagged
    assert hash(plain) == hash(flagged)
    assert len({plain, flagged}) == 1
