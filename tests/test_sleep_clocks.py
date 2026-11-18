"""Sleep is monotonic time without awake time; wall-clock steps and stalls are not sleep.

Covers the sleep split of gaming mode and the media tracker, the awake clock, the browser-state
generation behind the `url_unknown` hold, and the media event retry.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from capture_fakes import SCREEN, FakeMedia, window
from test_gaming_and_sessions import game

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.gaming import Game, GameMode, GameSettings, Transition
from akashic.capture.media import MediaTracker
from akashic.capture.ports import MediaState, slept
from akashic.config import Exclusions as UserExclusions
from akashic.platform.win import windows as win_windows
from akashic.storage.sessions import MediaLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from screen_rig import Rig

    from akashic.capture.ports import WindowInfo

SECRET_TITLE = "Secret Title 4711"  # noqa: S105 - a marker that must never reach a log


# --- the rule and the awake clock ------------------------------------------------------------


def test_sleep_is_a_monotonic_gap_exceeding_the_awake_gap_by_five_seconds() -> None:
    assert slept(5_000, 0)
    assert slept(65_000, 60_000)
    assert not slept(4_999, 0)
    assert not slept(30_000, 30_000)
    assert not slept(2_000, 4_000)  # awake ahead of monotonic never counts


def test_awake_ms_advances_with_real_time() -> None:
    windows = win_windows.Win32Windows()
    first = windows.awake_ms()
    time.sleep(0.1)
    second = windows.awake_ms()
    assert isinstance(first, int)
    assert first > 0
    assert 80 <= second - first < 2_000


def test_awake_ms_converts_100_ns_units_to_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(ref: Any) -> int:  # noqa: ANN401 - a ctypes byref
        ref._obj.value = 123_456_789  # pyright: ignore[reportUnknownMemberType]
        return 1

    monkeypatch.setattr(win_windows, "kernel32", SimpleNamespace(QueryUnbiasedInterruptTime=fake))
    assert win_windows.Win32Windows().awake_ms() == 12_345


# --- gaming mode -----------------------------------------------------------------------------

THE_GAME = Game("game.exe", "The Game")


def watched_game() -> GameMode:
    mode = GameMode(GameSettings(end_grace_s=600))
    mode.observe(game(), [SCREEN], 0, 1_000, 0)
    mode.observe(game(), [SCREEN], 2, 3_000, 2_000)
    return mode


def test_a_wake_with_the_game_still_in_front_ends_it_at_the_last_tick_and_starts_it_again() -> None:
    mode = watched_game()
    change = mode.observe(game(), [SCREEN], 100, 99_000, 2_500)
    assert change == Transition(ended=THE_GAME, ended_ms=3_000, started=THE_GAME)
    assert mode.current == THE_GAME


def test_a_wake_with_another_window_in_front_ends_the_game_even_within_the_grace() -> None:
    mode = watched_game()  # grace is 600 s, the sleep is 10 s
    change = mode.observe(window(), [SCREEN], 12, 13_000, 2_000)
    assert change == Transition(ended=THE_GAME, ended_ms=3_000)
    assert not mode.active


def test_a_stall_while_awake_changes_nothing() -> None:
    mode = watched_game()
    assert mode.observe(game(), [SCREEN], 32, 33_000, 32_000) == Transition()
    assert mode.current == THE_GAME


@pytest.mark.parametrize("wall_ms", [3_600_000, -3_600_000])
def test_a_wall_clock_step_without_sleep_changes_nothing(wall_ms: int) -> None:
    mode = watched_game()
    assert mode.observe(game(), [SCREEN], 4, 3_000 + wall_ms, 4_000) == Transition()
    assert mode.observe(window(), [SCREEN], 5, 4_000 + wall_ms, 5_000) == Transition()  # grace


def game_rows(rig: Rig) -> list[tuple[str, int, int, int]]:
    return rig.conn.execute(
        "SELECT process, start_ms, end_ms, closed FROM game_sessions ORDER BY id"
    ).fetchall()


@pytest.fixture
def clocked_game(make_rig: Callable[..., Rig]) -> Callable[[], tuple[Rig, Callable[..., None]]]:
    def build() -> tuple[Rig, Callable[..., None]]:
        wall = [0]
        awake = [0.0]
        rig = make_rig(
            game_settings=GameSettings(interval_s=60.0, end_grace_s=600.0, heartbeat_s=10),
            shown=False,
            now_ms=lambda: wall[0],
        )
        rig.windows.awake_clock = lambda: awake[0]
        rig.show(7, "game.exe", "The Game", has_frame=False)

        def tick(
            mono_s: float, *, awake_s: float | None = None, wall_ms: int | None = None
        ) -> None:
            awake[0] = mono_s if awake_s is None else awake_s
            wall[0] = int(mono_s * 1000) if wall_ms is None else wall_ms
            rig.tick(mono_s)

        return rig, tick

    return build


def test_a_wake_in_front_of_the_game_closes_its_row_and_opens_a_second_one(
    clocked_game: Callable[[], tuple[Rig, Callable[..., None]]],
) -> None:
    rig, tick = clocked_game()
    tick(0.0)
    tick(2.0)
    tick(100.0, awake_s=3.0)
    assert game_rows(rig) == [("game.exe", 0, 2_000, 1), ("game.exe", 100_000, 100_000, 0)]


def test_a_wake_with_another_window_closes_the_row_at_the_last_tick(
    clocked_game: Callable[[], tuple[Rig, Callable[..., None]]],
) -> None:
    rig, tick = clocked_game()
    tick(0.0)
    tick(2.0)
    rig.show(1, "notepad.exe", "Notes")
    tick(12.0, awake_s=2.0)  # 10 s asleep, well inside the 600 s grace
    assert game_rows(rig) == [("game.exe", 0, 2_000, 1)]


def test_a_stall_of_the_screen_loop_while_awake_keeps_one_row(
    clocked_game: Callable[[], tuple[Rig, Callable[..., None]]],
) -> None:
    rig, tick = clocked_game()
    tick(0.0)
    tick(2.0)
    tick(32.0)
    assert [(r[0], r[3]) for r in game_rows(rig)] == [("game.exe", 0)]


@pytest.mark.parametrize("wall_ms", [3_600_000, 1])
def test_a_wall_clock_step_keeps_one_game_row(
    clocked_game: Callable[[], tuple[Rig, Callable[..., None]]], wall_ms: int
) -> None:
    rig, tick = clocked_game()
    tick(0.0)
    tick(2.0)
    tick(4.0, wall_ms=wall_ms)
    assert [(r[0], r[3]) for r in game_rows(rig)] == [("game.exe", 0)]


# --- browser state generation ----------------------------------------------------------------


def test_every_clear_adds_one_to_the_generation() -> None:
    state = BrowserState()
    assert state.generation == 0
    assert isinstance(state.generation, int)
    state.clear()
    state.clear()
    assert state.generation == 2


# --- the media tracker -----------------------------------------------------------------------


class FlakyLog(MediaLog):
    """add_event fails ``event_failures`` times, then works; every call is recorded."""

    def __init__(self, conn: Connection) -> None:
        super().__init__(conn)
        self.event_failures = 0
        self.calls: list[str] = []

    def add_event(self, session_id: int, ts_ms: int, state: str) -> None:
        self.calls.append(f"event:{session_id}:{state}")
        if self.event_failures:
            self.event_failures -= 1
            msg = f"database is locked {SECRET_TITLE}"
            raise sqlite3.OperationalError(msg)
        super().add_event(session_id, ts_ms, state)

    def touch(self, session_id: int, *, end_ms: int, played_ms: int) -> None:
        self.calls.append(f"touch:{session_id}")
        super().touch(session_id, end_ms=end_ms, played_ms=played_ms)

    def close(self, session_id: int, *, end_ms: int, played_ms: int) -> None:
        self.calls.append(f"close:{session_id}")
        super().close(session_id, end_ms=end_ms, played_ms=played_ms)


class MRig:
    """A tracker whose wall, monotonic and awake clocks move independently."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.wall = [0]
        self.mono = [0]
        self.awake = [0]
        self.focus: list[WindowInfo | None] = [None]
        self.log = FlakyLog(conn)
        self.browsers = BrowserState()
        self.source = FakeMedia(names={"Z1": "Zen", "C1": "Google Chrome", "S1": "Spotify"})
        self.tracker = MediaTracker(
            source=self.source,
            log_store=self.log,
            exclusions=Exclusions(UserExclusions()),
            focused_window=lambda: self.focus[0],
            now_ms=lambda: self.wall[0],
            monotonic_ms=lambda: self.mono[0],
            awake_ms=lambda: self.awake[0],
            interval_s=0.01,
            browsers=self.browsers,
        )

    def move(
        self, mono_s: float, *, awake_s: float | None = None, wall_s: float | None = None
    ) -> None:
        self.mono[0] = int(mono_s * 1000)
        self.awake[0] = self.mono[0] if awake_s is None else int(awake_s * 1000)
        self.wall[0] = self.mono[0] if wall_s is None else int(wall_s * 1000)

    def at(self, mono_s: float, *, locked: bool = False, **clocks: float) -> None:
        self.move(mono_s, **clocks)
        self.tracker.poll(locked=locked)

    def rows(self) -> list[tuple[Any, ...]]:
        """(start_ms, end_ms, played_ms, closed)"""
        return self.conn.execute(
            "SELECT start_ms, end_ms, played_ms, closed FROM media_sessions ORDER BY id"
        ).fetchall()

    def events(self) -> list[tuple[int, int, str]]:
        return self.conn.execute(
            "SELECT media_session_id, ts_ms, state FROM media_events ORDER BY id"
        ).fetchall()


@pytest.fixture
def mrig(conn: Connection) -> MRig:
    return MRig(conn)


def playing(title: str = "Video A", app_id: str = "Z1") -> MediaState:
    return MediaState(app_id, title, "Chan", "playing")


def paused(title: str = "Video A", app_id: str = "Z1") -> MediaState:
    return MediaState(app_id, title, "Chan", "paused")


def test_a_sleep_while_paused_closes_the_row_at_the_pause_point_and_opens_none(
    mrig: MRig,
) -> None:
    mrig.source.states = [playing()]
    mrig.at(0)
    mrig.source.states = [paused()]
    mrig.at(2)
    mrig.at(4)
    mrig.at(100, awake_s=6)
    assert mrig.rows() == [(0, 2_000, 2_000, 1)]
    assert mrig.events()[-1] == (1, 2_000, "stopped")


def test_a_stall_of_the_media_thread_while_awake_splits_nothing(mrig: MRig) -> None:
    mrig.source.states = [playing()]
    mrig.at(0)
    mrig.at(2)
    mrig.at(32)
    assert mrig.rows() == [(0, 32_000, 32_000, 0)]  # heartbeat after 30 s


@pytest.mark.parametrize("wall_s", [3_600, 1])
def test_a_wall_clock_step_without_sleep_splits_nothing_and_played_follows_the_awake_clock(
    mrig: MRig, wall_s: int
) -> None:
    mrig.source.states = [playing()]
    mrig.at(0)
    mrig.at(2)
    mrig.at(4, wall_s=wall_s)
    mrig.tracker.close_all()
    rows = mrig.rows()
    assert len(rows) == 1
    assert rows[0][2:] == (4_000, 1)
    assert rows[0][1] >= rows[0][0]


def test_a_wall_clock_step_back_never_reorders_events_nor_lowers_the_end(mrig: MRig) -> None:
    mrig.source.states = [playing()]
    mrig.at(0)
    mrig.at(2, wall_s=3_600)
    mrig.at(4, wall_s=3_602)
    ends = [mrig.rows()[0][1]]
    mrig.at(12, wall_s=10)  # stepped back an hour; the 10 s heartbeat writes
    ends.append(mrig.rows()[0][1])
    mrig.source.states = [paused()]
    mrig.at(14, wall_s=12)
    mrig.source.states = [playing()]
    mrig.at(16, wall_s=14)
    mrig.source.states = []
    mrig.at(18, wall_s=16)
    ends.append(mrig.rows()[0][1])
    assert ends == sorted(ends)
    stamps = [ts for _, ts, _ in mrig.events()]
    assert stamps == sorted(stamps)
    assert [s for _, _, s in mrig.events()] == ["playing", "paused", "playing", "stopped"]
    start, end, played, closed = mrig.rows()[0]
    assert (start, played, closed) == (0, 14_000, 1)
    assert end >= 3_602_000


def test_a_close_never_puts_the_end_before_the_last_poll(mrig: MRig) -> None:
    mrig.source.states = [playing()]
    mrig.at(0)
    mrig.at(2, wall_s=3_600)
    mrig.move(4, wall_s=5)
    mrig.tracker.close_all()
    assert mrig.rows()[0][1] == 3_600_000
    assert mrig.events()[-1][1] == 3_600_000


# --- url_unknown and the verdict generation --------------------------------------------------

CHROME = "Video A - YouTube - Google Chrome"


def open_chrome_row(mrig: MRig) -> None:
    win = window(process="chrome.exe", title=CHROME)
    mrig.focus[0] = win
    mrig.browsers.publish(win, None)
    mrig.source.states = [playing(app_id="C1")]
    mrig.at(0)
    mrig.at(2)


def test_a_clear_since_the_previous_poll_holds_a_url_unknown_row_for_one_poll(mrig: MRig) -> None:
    open_chrome_row(mrig)
    mrig.browsers.clear()  # a UAC prompt of a second; locked() never saw it
    mrig.at(4)
    assert mrig.rows()[0][3] == 0
    assert "excluded:url_unknown" not in mrig.tracker.stats
    mrig.at(6)  # still unknown one poll after
    assert mrig.rows() == [(0, 4_000, 4_000, 1)]
    assert mrig.tracker.stats["excluded:url_unknown"] == 1


def test_a_clear_in_the_middle_of_a_poll_still_holds(mrig: MRig) -> None:
    open_chrome_row(mrig)
    win = mrig.focus[0]

    def clearing_focus() -> Any:  # noqa: ANN401
        mrig.browsers.clear()
        return win

    mrig.tracker._focused_window = clearing_focus  # pyright: ignore[reportPrivateUsage]
    mrig.at(4)
    assert mrig.rows()[0][3] == 0
    assert "excluded:url_unknown" not in mrig.tracker.stats


def test_a_clear_never_opens_a_row_for_url_unknown(mrig: MRig) -> None:
    mrig.focus[0] = window(process="chrome.exe", title=CHROME)
    mrig.browsers.clear()
    mrig.source.states = [playing(app_id="C1")]
    mrig.at(0)
    assert mrig.rows() == []


def test_an_excluding_verdict_closes_the_row_at_once_after_a_clear(mrig: MRig) -> None:
    open_chrome_row(mrig)
    mrig.browsers.clear()
    mrig.browsers.publish(window(process="chrome.exe", title=CHROME), "finance")
    mrig.at(4)
    assert mrig.rows() == [(0, 2_000, 2_000, 1)]


def test_a_one_second_uac_keeps_a_url_unknown_video_row_open(
    make_rig: Callable[..., Rig], conn: Connection
) -> None:
    screen = make_rig(shown=False)
    screen.show(2, "chrome.exe", CHROME, focus=True)
    screen.settle(0.0)
    rig = MRig(conn)
    rig.browsers = screen.browsers
    rig.tracker = MediaTracker(
        source=rig.source,
        log_store=rig.log,
        exclusions=Exclusions(UserExclusions()),
        focused_window=screen.windows.focused_window,
        now_ms=lambda: rig.wall[0],
        monotonic_ms=lambda: rig.mono[0],
        awake_ms=lambda: rig.awake[0],
        browsers=screen.browsers,
    )
    rig.source.states = [playing(app_id="C1")]
    rig.at(0)
    rig.at(2)
    screen.windows.locked = True  # the UAC prompt shows: the loop clears the verdicts
    screen.tick(3.0)
    screen.windows.locked = False  # gone before the next poll; the loop has not republished
    rig.at(4)
    assert rig.rows()[0][3] == 0
    assert "excluded:url_unknown" not in rig.tracker.stats
    rig.at(6)  # the verdict is still unknown one poll after
    assert rig.rows()[0][3] == 1
    assert rig.rows()[0][1] == 4_000


# --- failed events ---------------------------------------------------------------------------


def test_a_failed_pause_event_is_kept_counted_and_the_poll_goes_on(
    mrig: MRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    mrig.source.states = [
        playing(SECRET_TITLE, "Z1"),
        playing("Other", "C1"),
        playing("Gone", "S1"),
    ]
    mrig.at(0)
    mrig.at(2)
    mrig.source.states = [paused(SECRET_TITLE, "Z1"), playing("Other", "C1")]
    mrig.log.event_failures = 1
    mrig.log.calls.clear()
    mrig.at(12)
    stats = mrig.tracker.stats_snapshot()
    assert stats["event_failed"] == 1
    assert stats["writing a media event: OperationalError"] == 1
    assert SECRET_TITLE not in caplog.text
    assert "touch:1" in mrig.log.calls  # the failing session itself is updated
    assert "touch:2" in mrig.log.calls  # another session is updated
    assert "close:3" in mrig.log.calls  # a gone session is closed
    assert mrig.rows()[2][3] == 1


def test_unsent_events_are_retried_oldest_first_with_their_own_time_and_written_once(
    mrig: MRig,
) -> None:
    mrig.source.states = [playing()]
    mrig.at(0)
    mrig.log.event_failures = 2
    mrig.source.states = [paused()]
    mrig.at(4)  # fails
    mrig.source.states = [playing()]
    mrig.at(6)  # the pause fails again, the new one waits
    mrig.at(8)  # both go out
    mrig.at(10)
    assert mrig.events() == [(1, 0, "playing"), (1, 4_000, "paused"), (1, 6_000, "playing")]
    assert mrig.tracker.stats["event_failed"] == 2


def test_a_close_writes_the_unsent_events_first_and_a_failure_keeps_it_pending(
    mrig: MRig,
) -> None:
    mrig.source.states = [playing()]
    mrig.at(0)
    mrig.at(2)
    mrig.log.event_failures = 2
    mrig.source.states = [paused()]
    mrig.at(4)  # the pause event fails
    mrig.source.states = []
    mrig.at(6)  # gone: the close tries the event first and fails
    assert mrig.rows()[0][3] == 0
    assert mrig.tracker.stats["close_failed"] == 1
    assert "close:1" not in mrig.log.calls
    mrig.at(60)  # retried later, still ended at the first attempt
    assert mrig.rows() == [(0, 4_000, 4_000, 1)]
    assert mrig.events() == [(1, 0, "playing"), (1, 4_000, "paused"), (1, 4_000, "stopped")]


def test_a_failing_event_write_at_stop_costs_one_attempt_per_session(mrig: MRig) -> None:
    mrig.source.states = [playing("A", "Z1"), playing("B", "C1")]
    mrig.at(0)
    mrig.source.states = [paused("A", "Z1"), paused("B", "C1")]
    mrig.log.event_failures = 10**6
    mrig.at(2)  # one failed event each
    mrig.log.calls.clear()
    mrig.tracker.close_all()
    assert [c for c in mrig.log.calls if c.startswith("event")] == [
        "event:1:paused",
        "event:2:paused",
    ]
    assert not [c for c in mrig.log.calls if c.startswith("close")]
