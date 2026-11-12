"""Media session logging and app-name lookups against a fake OS source and a temporary database."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from capture_fakes import AUMID, FakeMedia, window

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.media import (
    NAME_RETRY_MAX_MS,
    NAME_RETRY_MS,
    PAUSE_CLOSE_MS,
    MediaTracker,
    process_for_app,
)
from akashic.capture.ports import MediaState, WindowInfo
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.sessions import MediaLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.config import Paths


@dataclass
class Rig:
    tracker: MediaTracker
    source: FakeMedia
    conn: Connection
    clock: list[int]
    focus: list[WindowInfo | None]
    browsers: BrowserState

    def focus_browser(self, win: WindowInfo, reason: str | None = None) -> None:
        """Focus a browser window the way the screen loop leaves it: with its verdict published."""
        self.focus[0] = win
        self.browsers.publish(win, reason)

    def at(self, seconds: float) -> None:
        self.clock[0] = int(seconds * 1000)
        self.tracker.poll()

    def rows(self) -> list[tuple[str, str, str | None, int, int, int, int]]:
        return self.conn.execute(
            "SELECT app, title, artist, start_ms, end_ms, played_ms, closed"
            " FROM media_sessions ORDER BY id"
        ).fetchall()

    def events(self) -> list[tuple[int, str]]:
        return self.conn.execute("SELECT ts_ms, state FROM media_events ORDER BY id").fetchall()


def playing(title: str = "Video A", artist: str = "Chan", app_id: str = "Z1") -> MediaState:
    return MediaState(app_id, title, artist, "playing")


@pytest.fixture
def make_rig(paths: Paths) -> Callable[..., Rig]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)

    def build(user: UserExclusions | None = None) -> Rig:
        clock = [0]
        focus: list[WindowInfo | None] = [None]
        source = FakeMedia(names={"Z1": "Zen", "C1": "Google Chrome"})
        browsers = BrowserState()
        tracker = MediaTracker(
            source=source,
            log_store=MediaLog(conn),
            exclusions=Exclusions(user or UserExclusions()),
            focused_window=lambda: focus[0],
            now_ms=lambda: clock[0],
            interval_s=0.01,
            browsers=browsers,
        )
        return Rig(tracker, source, conn, clock, focus, browsers)

    return build


@pytest.mark.parametrize(
    ("app", "process"),
    [
        ("Google Chrome", "chrome.exe"),
        ("Zen", "zen.exe"),
        ("Zen Private Browsing", "zen.exe"),
        ("Microsoft Edge", "msedge.exe"),
        ("Spotify", "spotify.exe"),
        ("VLC.exe", "vlc.exe"),
    ],
)
def test_process_for_app(app: str, process: str) -> None:
    assert process_for_app(app) == process


def test_playing_session_is_logged_and_closed_when_it_disappears(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.source.states = [playing()]
    rig.at(1)
    rig.at(3)
    rig.at(5)
    rig.source.states = []
    rig.at(7)
    assert rig.rows() == [("Zen", "Video A", "Chan", 1000, 5000, 4000, 1)]
    assert rig.events() == [(1000, "playing"), (5000, "stopped")]


def test_pause_stops_the_played_clock_but_keeps_the_row_open(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing()]
    rig.at(0)
    rig.at(2)
    rig.source.states = [MediaState("Z1", "Video A", "Chan", "paused")]
    rig.at(4)  # the 2 s up to here were still playing
    rig.at(6)
    rig.at(8)
    rig.source.states = [playing()]
    rig.at(10)
    rig.at(12)
    assert rig.rows()[0][6] == 0  # still open: pausing does not end it
    rig.tracker.close_all()
    # 0-4 s and 10-12 s played; the paused stretch is not counted
    assert rig.rows()[0][5] == 4000 + 2000
    assert [s for _, s in rig.events()] == ["playing", "paused", "playing", "stopped"]


def test_a_long_pause_closes_the_session(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing()]
    rig.at(0)
    rig.source.states = [MediaState("Z1", "Video A", "Chan", "paused")]
    rig.at(2)
    rig.at(2 + PAUSE_CLOSE_MS / 1000)
    assert rig.rows()[0][6] == 1


def test_new_title_starts_a_new_row(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing("Video A")]
    rig.at(0)
    rig.at(2)
    rig.source.states = [playing("Video B")]
    rig.at(4)
    rows = rig.rows()
    assert [(r[1], r[3], r[4], r[6]) for r in rows] == [
        ("Video A", 0, 4000, 1),
        ("Video B", 4000, 4000, 0),
    ]


def test_session_never_seen_playing_is_ignored(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [
        MediaState("Z1", "Old tab", "x", "paused"),
        MediaState("Z2", "", "", "playing"),
    ]
    rig.at(0)
    assert rig.rows() == []


def test_two_apps_are_tracked_independently(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing("A", app_id="Z1"), playing("B", app_id="C1")]
    rig.at(0)
    rig.source.states = [playing("B", app_id="C1")]
    rig.at(2)
    assert [(r[0], r[1], r[6]) for r in rig.rows()] == [("Zen", "A", 1), ("Google Chrome", "B", 0)]


def test_unknown_app_falls_back_to_its_id(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing(app_id="Weird.App")]
    rig.at(0)
    assert rig.rows()[0][0] == "Weird.App"


def test_stalled_polling_is_not_counted_as_playing_time(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing()]
    rig.at(0)
    rig.at(3600)  # the PC slept
    rig.tracker.close_all()
    assert rig.rows()[0][5] == 10_000


def test_private_browsing_sources_are_never_logged(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing(app_id="Z1;PrivateBrowsingAUMID")]
    rig.at(0)
    assert rig.rows() == []
    assert rig.tracker.stats["excluded:private_window"] == 1


def test_browser_media_is_dropped_while_that_browser_shows_a_private_window(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.focus[0] = window(process="chrome.exe", title="Video - YouTube - Google Chrome (Incognito)")
    rig.source.states = [playing(app_id="C1")]
    rig.at(0)
    assert rig.rows() == []
    rig.focus_browser(window(process="chrome.exe", title="Video - YouTube - Google Chrome"))
    rig.at(2)
    assert len(rig.rows()) == 1


def test_a_private_window_of_another_browser_does_not_matter(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.focus[0] = window(process="firefox.exe", title="x - Private Browsing")
    rig.source.states = [playing(app_id="C1")]
    rig.at(0)
    assert len(rig.rows()) == 1


def test_user_and_built_in_exclusions_apply_to_the_media_title(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(UserExclusions(window_titles=("*secret*",), processes=("zen.exe",)))
    rig.source.states = [playing("My Secret video")]
    rig.at(0)
    rig.source.states = [playing("Fine title")]  # but the user excluded zen.exe altogether
    rig.at(2)
    assert rig.rows() == []
    rig2 = make_rig(UserExclusions(window_titles=("*secret*",)))
    # D2: a video named like a password manager is a video; the user's own title rule still wins.
    rig2.source.states = [playing("My Secret thing", app_id="C1"), playing("Bitwarden vault tour")]
    rig2.at(0)
    assert [r[1] for r in rig2.rows()] == ["Bitwarden vault tour"]


def test_excluded_title_change_closes_the_open_row(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(UserExclusions(window_titles=("*secret*",)))
    rig.source.states = [playing("Fine")]
    rig.at(0)
    rig.source.states = [playing("secret now")]
    rig.at(2)
    assert [(r[1], r[6]) for r in rig.rows()] == [("Fine", 1)]


def test_url_is_matched_from_recent_browser_frames(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.conn.execute(
        "INSERT INTO frames (ts_ms, monitor, process, window_title, url, trigger)"
        " VALUES (500, 0, 'zen.exe', 'Video A - YouTube',"
        " 'https://youtube.com/watch?v=abc', 'focus')"
    )
    rig.source.states = [playing("Video A")]
    rig.at(1)
    assert rig.conn.execute("SELECT url FROM media_sessions").fetchone() == (
        "https://youtube.com/watch?v=abc",
    )


def test_url_appearing_later_is_picked_up_then_search_stops(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing("Video A")]
    rig.at(1)
    assert rig.conn.execute("SELECT url FROM media_sessions").fetchone() == (None,)
    rig.conn.execute(
        "INSERT INTO frames (ts_ms, monitor, process, window_title, url, trigger)"
        " VALUES (3000, 0, 'zen.exe', 'Video A - YouTube',"
        " 'https://youtube.com/watch?v=abc', 'idle')"
    )
    rig.at(7)
    assert rig.conn.execute("SELECT url FROM media_sessions").fetchone() == (
        "https://youtube.com/watch?v=abc",
    )


# Pins the unread media_sessions_fts table: delete or rewrite with #90.
def test_media_titles_are_searchable(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing("Café de Paris", "Chaîne")]
    rig.at(0)
    hits = rig.conn.execute(
        "SELECT rowid FROM media_sessions_fts WHERE media_sessions_fts MATCH 'cafe'"
    ).fetchall()
    assert hits == [(1,)]


PASSES_BEFORE_STOP = 3


class StopAfterPasses(threading.Event):
    """A ``paused`` flag that stops the loop after N passes, so the run ends by itself."""

    def __init__(self, stop: threading.Event, passes: int) -> None:
        super().__init__()
        self._stop_run = stop
        self._passes_left = passes

    def is_set(self) -> bool:
        self._passes_left -= 1
        if self._passes_left <= 0:
            self._stop_run.set()
        return super().is_set()


def test_run_survives_failures_closes_on_pause_and_on_stop(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing()]
    rig.at(0)
    stop = threading.Event()
    paused = StopAfterPasses(stop, PASSES_BEFORE_STOP)
    paused.set()
    rig.tracker.run(stop, paused, lambda: False)
    assert rig.rows()[0][6] == 1  # paused: the open row was closed

    stop.clear()
    rig.source.fail = True
    polls: list[int] = []

    def locked() -> bool:
        polls.append(1)
        if len(polls) == PASSES_BEFORE_STOP:
            stop.set()
        return False

    not_paused = threading.Event()
    # every poll raises OSError: logged, never fatal
    rig.tracker.run(stop, not_paused, locked)
    assert len(polls) == PASSES_BEFORE_STOP


def test_close_stale_closes_rows_left_by_a_crash(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.source.states = [playing()]
    rig.at(0)
    assert MediaLog(rig.conn).close_stale() == 1
    assert rig.rows()[0][6] == 1


# --- a name lookup that missed is retried with a capped backoff and never remembered ---

T0 = 1_700_000_000_000


class _CountingNames(FakeMedia):
    def __init__(self) -> None:
        super().__init__()
        self.lookups: list[int] = []
        self.now = 0

    def app_name(self, app_id: str) -> str | None:
        self.lookups.append(self.now)
        return super().app_name(app_id)


def tracker_for(conn: Connection, source: FakeMedia, clock: list[int]) -> MediaTracker:
    return MediaTracker(
        source=source,
        log_store=MediaLog(conn),
        exclusions=Exclusions(UserExclusions()),
        focused_window=lambda: None,
        now_ms=lambda: clock[0],
        browsers=BrowserState(),
    )


def test_a_miss_is_retried_with_doubling_backoff_and_then_resolved(conn: Connection) -> None:
    source = _CountingNames()
    source.states = [MediaState(AUMID, "A video", "A channel", "playing")]
    clock = [T0]
    tracker = tracker_for(conn, source, clock)
    delays: list[int] = []
    for _ in range(9):
        source.now = clock[0]
        tracker.poll()
        delays = [b - a for a, b in zip(source.lookups, source.lookups[1:], strict=False)]
        clock[0] += 1_000
    assert delays[:3] == [NAME_RETRY_MS, 2 * NAME_RETRY_MS, 4 * NAME_RETRY_MS][: len(delays)]
    assert len(source.lookups) < 9  # not asked on every poll
    clock[0] += NAME_RETRY_MAX_MS * 4
    source.names[AUMID] = "Firefox"
    source.now = clock[0]
    tracker.poll()
    before = len(source.lookups)
    for _ in range(3):
        clock[0] += 1_000
        tracker.poll()
    assert len(source.lookups) == before  # a resolved name is remembered


def test_the_backoff_is_capped(conn: Connection) -> None:
    source = _CountingNames()
    source.states = [MediaState(AUMID, "A video", "A channel", "playing")]
    clock = [T0]
    tracker = tracker_for(conn, source, clock)
    for _ in range(300):
        if len(source.lookups) >= 9:
            break
        source.now = clock[0]
        tracker.poll()
        clock[0] += NAME_RETRY_MS
    assert len(source.lookups) >= 9
    delays = [b - a for a, b in zip(source.lookups, source.lookups[1:], strict=False)]
    assert delays[:6] == [5_000, 10_000, 20_000, 40_000, 80_000, 160_000]
    assert delays[6] == delays[7] == 300_000
    last = source.lookups[-1]
    lookups_before = len(source.lookups)
    clock[0] = source.now = last + 299_999
    tracker.poll()
    assert len(source.lookups) == lookups_before
    clock[0] = source.now = last + 300_000
    tracker.poll()
    assert len(source.lookups) == lookups_before + 1
