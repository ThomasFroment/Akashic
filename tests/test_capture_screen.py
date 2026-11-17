"""The screen loop: per-monitor schedules, triggers, change detection, text, pause and wiring.

Driven by fakes for every port, a manual clock and a real ScreenWriter that the test drains.
Timing notes: a monitor's first check starts the window captures, so its picture arrives with
the next pass 0.1 s later (``Rig.settle``); the start-up focus trigger fires at 0.3 s and its
check waits for the 1 s gap, which ``Rig.quiet`` runs out.
"""

from __future__ import annotations

import io
import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING

import pytest
import sqlcipher3
from capture_fakes import (
    LONG_TEXT,
    SCREEN,
    FakeMedia,
    FakeOcr,
    FakeUia,
    FakeWindowEvents,
    FakeWindowFacts,
    FakeWindows,
    blank,
    monitor,
    top_window,
    window,
    with_text_block,
)
from PIL import Image
from screen_rig import MON_B, ImageCaptures, Rig
from store_reads import latest_frames
from waits import wait_for

from akashic import child, ipc
from akashic.capture import main as capture_main
from akashic.capture.health import HOOK_DOWN_PROBLEM_S, WINDOW_EVENTS_DOWN
from akashic.capture.ports import CapturePlatform, Rect, TextBlock, WindowChanges
from akashic.capture.screen import (
    HOOK_DOWN_RECHECK_S,
    STATS_LOG_INTERVAL_S,
    ScreenCapture,
    Settings,
)
from akashic.capture.screen_writer import Picture, ScreenWriter
from akashic.capture.text import MAX_TEXT_CHARS
from akashic.capture.uia_budget import UiaBudget
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from akashic.capture.ports import MonitorInfo
    from akashic.capture.screen import _Tick  # pyright: ignore[reportPrivateUsage]
    from akashic.config import Paths

TWO = [SCREEN, MON_B]


def two_monitors(make_rig: Callable[..., Rig]) -> Rig:
    """Notepad (focused) on the left monitor m1, a player on the right monitor m2."""
    rig = make_rig(monitors=TWO)
    rig.show(2, "vlc.exe", "Movie", MON_B, focus=False)
    return rig


# --- first pictures and stored rows -------------------------------------------------------------


def test_first_check_waits_for_the_captures_then_stores_a_picture_with_uia_text(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.tick(0.0)
    assert rig.writer.pictures == []  # the capture just started: no pixels yet
    rig.tick(0.1)
    rig.drain()
    frame = latest_frames(rig.conn)[0]
    assert (frame.process, frame.window_title, frame.text_source) == ("notepad.exe", "Notes", "uia")
    assert frame.text is not None
    assert "real window text" in frame.text
    assert frame.image_path is not None
    assert rig.store.image(frame.image_path) is not None
    assert rig.ocr.calls == 0


def test_the_stored_picture_has_the_native_size_of_the_monitor(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(monitors=[Rect(-1000, 100, 0, 900)])
    rig.show(rect=Rect(-1000, 100, 0, 900))
    rig.settle(0.0)
    rig.drain()
    frame = latest_frames(rig.conn)[0]
    assert frame.image_path is not None
    data = rig.store.image(frame.image_path)
    assert data is not None
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (1000, 800)


def test_the_window_rows_of_a_stored_picture(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(shown=False)
    rig.show(5, rect=Rect(100, 80, 500, 380))
    rig.uia.texts[5] = LONG_TEXT
    rig.settle(0.0)
    rig.drain()
    (row,) = rig.conn.execute(
        "SELECT role, z, hwnd, x0, y0, x1, y1, process, title, url, focused, painted, text_source"
        " FROM frame_windows"
    ).fetchall()
    assert tuple(row) == ("app", 0, 5, 100, 80, 500, 380, "notepad.exe", "Notes", None, 1, 1, "uia")


def test_a_browser_address_is_normalized_and_stored(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.show(1, "zen.exe", "Video - YouTube", url="youtube.com/watch?v=abc")
    rig.settle(0.0)
    rig.drain()
    assert latest_frames(rig.conn)[0].url == "https://youtube.com/watch?v=abc"


# --- schedule: rates ----------------------------------------------------------------------------


def test_the_focus_monitor_is_checked_every_5_s_and_the_others_every_15_s(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()  # m1 last checked at 1.1 s, m2 at 0.1 s
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.tick(6.0)
    assert (rig.count("m1"), rig.count("m2")) == (m1, m2)
    rig.tick(6.1)
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 1, m2)
    rig.tick(11.1)
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 2, m2)
    rig.tick(15.0)
    assert rig.count("m2") == m2
    rig.tick(15.1)
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 2, m2 + 1)


@pytest.mark.parametrize("where", ["no window", "off every monitor"])
def test_without_a_focus_monitor_every_monitor_is_checked_every_15_s(
    make_rig: Callable[..., Rig], where: str
) -> None:
    rig = make_rig()
    if where == "no window":
        rig.windows.focused = None
    else:
        rig.windows.focused = window(rect=Rect(5000, 0, 5400, 300))
    rig.settle(0.0)
    done = rig.count()
    rig.tick(5.2)
    rig.tick(14.0)
    assert rig.count() == done
    rig.tick(15.1)
    assert rig.count() == done + 1


def test_unchanged_screens_are_noted_not_stored_again(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    rig.tick(6.1)
    rig.drain()
    assert len(rig.pictures()) == 1
    assert rig.last_note() == "shown"
    assert len(latest_frames(rig.conn)) == 1


# --- schedule: triggers -------------------------------------------------------------------------


def test_a_new_focused_window_triggers_a_check_of_its_monitor_after_300_ms(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    rig.show(3, "code.exe", "main.py")
    done = rig.count("m1")
    rig.tick(2.0)  # the poll sees the new hwnd
    rig.tick(2.2)
    assert rig.count("m1") == done
    rig.tick(2.3)  # fires; the new window's capture has to start first
    rig.tick(2.4)
    assert rig.count("m1") == done + 1
    assert rig.pictures()[-1].trigger == "focus"
    assert rig.pictures()[-1].entries[0].title == "main.py"


def test_a_new_title_on_the_same_window_triggers_a_check_after_500_ms(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.quiet()
    rig.show(1, title="Notes - edited")
    rig.tick(2.0)
    rig.tick(2.4)
    assert len(rig.pictures()) == 1
    rig.tick(2.5)
    assert [p.trigger for p in rig.pictures()] == ["idle", "title"]


def test_input_checks_the_focus_monitor_and_the_one_under_the_cursor(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.windows.cursor = (900, 100)  # on m2
    rig.windows.last_input = 1
    rig.tick(2.0)
    rig.tick(2.3)
    assert (rig.count("m1"), rig.count("m2")) == (m1, m2)
    rig.tick(2.4)  # 400 ms after the input
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 1, m2 + 1)


def test_input_with_the_cursor_off_every_monitor_checks_only_the_focus_monitor(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.windows.cursor = (-50, -50)
    rig.windows.last_input = 1
    rig.tick(2.0)
    rig.tick(2.4)
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 1, m2)


def test_input_trigger_is_reported_on_the_picture(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    rig.repaint(1, with_text_block(blank()))
    rig.windows.last_input = 1
    rig.tick(2.0)
    rig.tick(2.4)
    assert rig.pictures()[-1].trigger == "input"


def test_continuous_input_is_checked_within_two_seconds(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    done = rig.count()
    now = 2.0
    while now < 3.95:  # the mouse never stops moving
        rig.windows.last_input += 1
        rig.tick(now)
        now += 0.1
    assert rig.count() == done
    rig.windows.last_input += 1
    rig.tick(4.0)  # 2 s after the first input at 2.0
    assert rig.count() == done + 1


def test_a_window_event_checks_only_the_monitors_the_window_touches(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.events.push(2, "title", 2.0)
    rig.tick(2.0)
    rig.tick(2.2)
    assert (rig.count("m1"), rig.count("m2")) == (m1, m2)
    rig.tick(2.3)
    assert (rig.count("m1"), rig.count("m2")) == (m1, m2 + 1)


def test_a_window_that_moved_checks_the_monitor_it_left_and_the_one_it_entered(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.show(2, "vlc.exe", "Movie", Rect(100, 100, 300, 300), focus=False)
    rig.events.push(2, "moved", 2.0)
    rig.tick(2.0)
    rig.tick(2.3)
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 1, m2 + 1)


def test_window_events_are_checked_within_two_seconds_of_the_first(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    m2 = rig.count("m2")
    now = 2.0
    while now < 3.95:  # events never stop coming
        rig.events.push(2, "moved", now)
        rig.tick(now)
        now += 0.1
    assert rig.count("m2") == m2
    rig.events.push(2, "moved", 4.0)
    rig.tick(4.0)
    assert rig.count("m2") == m2 + 1


def test_lost_window_events_make_every_monitor_due(make_rig: Callable[..., Rig]) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.events.healthy = False
    rig.tick(2.0)  # m1 waits for the 1 s gap after its check at 1.1
    rig.events.healthy = True
    assert (rig.count("m1"), rig.count("m2")) == (m1, m2 + 1)
    rig.tick(2.1)
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 1, m2 + 1)


class _Seen:
    """What the verdicts and the request collection were handed, per tick."""

    def __init__(self, rig: Rig) -> None:
        self.verdicts: list[WindowChanges] = []
        self.requests: list[WindowChanges] = []
        capture = rig.capture
        note = capture._verdicts.note_events  # pyright: ignore[reportPrivateUsage]
        collect = capture._collect_requests  # pyright: ignore[reportPrivateUsage]

        def note_events(changes: WindowChanges) -> None:
            self.verdicts.append(changes)
            note(changes)

        def collect_requests(
            monitors: Sequence[MonitorInfo], changes: WindowChanges, tick: _Tick
        ) -> bool:
            self.requests.append(changes)
            return collect(monitors, changes, tick)

        capture._verdicts.note_events = note_events  # pyright: ignore[reportPrivateUsage]
        capture._collect_requests = collect_requests  # pyright: ignore[reportPrivateUsage]


def test_the_first_unhealthy_drain_acts_at_once(make_rig: Callable[..., Rig]) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    m2 = rig.count("m2")
    seen = _Seen(rig)
    rig.events.healthy = False
    rig.tick(2.0)
    assert seen.verdicts[-1].healthy is False
    assert rig.count("m2") == m2 + 1


def test_a_drain_soon_after_an_acting_one_is_treated_as_healthy(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    seen = _Seen(rig)
    rig.events.healthy = False
    rig.tick(2.0)
    rig.tick(2.1)
    done = (rig.count("m1"), rig.count("m2"))
    rig.tick(2.0 + HOOK_DOWN_RECHECK_S - 0.5)
    assert seen.verdicts[-1].healthy is True
    assert (rig.count("m1"), rig.count("m2")) == done


def test_events_in_a_bounded_drain_are_still_handled_and_both_consumers_see_one_object(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.show(1, "chrome.exe", "Page", url="https://example.org/one")
    rig.quiet()
    seen = _Seen(rig)
    rig.events.healthy = False
    rig.tick(2.0)
    rig.events.push(1, "title", 3.0)
    rig.tick(3.0)
    bounded = seen.verdicts[-1]
    assert bounded.healthy is True
    assert [e.hwnd for e in bounded.events] == [1]
    assert seen.requests[-1] is bounded
    rig.tick(3.3)
    rig.tick(3.4)
    assert rig.capture.stats_snapshot()["pending"] >= 1  # the title event started a hold


def test_an_unhealthy_drain_after_the_recheck_interval_acts_again(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    seen = _Seen(rig)
    rig.events.healthy = False
    rig.tick(2.0)
    rig.tick(2.0 + HOOK_DOWN_RECHECK_S - 0.5)
    m2 = rig.count("m2")
    rig.tick(2.0 + HOOK_DOWN_RECHECK_S)
    assert seen.verdicts[-1].healthy is False
    assert rig.count("m2") == m2 + 1
    rig.tick(2.0 + HOOK_DOWN_RECHECK_S + 1.0)  # the count started over from the acting drain
    assert seen.verdicts[-1].healthy is True


def test_a_healthy_drain_ends_the_run_so_the_next_unhealthy_one_acts_at_once(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    seen = _Seen(rig)
    rig.events.healthy = False
    rig.tick(2.0)
    rig.events.healthy = True
    rig.tick(2.5)
    rig.events.healthy = False
    rig.tick(3.0)
    assert seen.verdicts[-1].healthy is False


def test_the_drain_is_filtered_before_the_no_monitors_return(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    seen = _Seen(rig)
    rig.events.healthy = False
    rig.tick(2.0)
    rig.displays.monitor_list = []
    rig.tick(6.0)  # the monitor list refreshes every 5 s
    assert seen.verdicts[-1].healthy is True
    assert len(seen.requests) == 1  # nothing collected without monitors


def test_the_window_events_problem_appears_after_thirty_seconds_of_unhealthy_drains(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.quiet()
    assert rig.capture.problems(100.0) == []
    rig.events.healthy = False
    rig.tick(2.0)
    rig.tick(10.0)
    assert rig.capture.problems(2.0 + HOOK_DOWN_PROBLEM_S - 0.1) == []
    assert rig.capture.problems(2.0 + HOOK_DOWN_PROBLEM_S) == [WINDOW_EVENTS_DOWN]
    rig.events.healthy = True
    rig.tick(40.0)
    assert rig.capture.problems(41.0) == []


def test_reset_clears_the_window_events_problem(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    rig.events.healthy = False
    rig.tick(2.0)
    assert rig.capture.problems(100.0) == [WINDOW_EVENTS_DOWN]
    rig.capture.reset()
    assert rig.capture.problems(100.0) == []
    rig.tick(101.0)  # the first drain after a reset acts at once
    assert rig.capture.problems(101.0) == []


def test_a_triggered_check_waits_for_the_gap_after_the_previous_check(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.settle(0.0)  # checked at 0.1; the start-up focus trigger fires at 0.4
    done = rig.count()
    rig.tick(0.4)
    rig.tick(1.0)
    assert rig.count() == done  # moved to 1.1, not dropped
    rig.tick(1.1)
    assert rig.count() == done + 1


# --- schedule: pending monitors -----------------------------------------------------------------


def test_a_monitor_without_a_snapshot_is_checked_again_on_every_poll(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.captures.images.clear()  # the capture starts, but no frame ever arrives
    for step in range(5):
        rig.tick(step / 10)
    assert rig.facts.reads == 5  # one enumeration per poll, though the 1 s gap has not passed
    assert rig.writer.pictures == []
    rig.tick(0.5)
    assert len(rig.pictures()) == 1  # after 0.5 s the window is stored unpainted
    assert rig.pictures()[0].entries[0].painted is False


def test_a_pending_check_keeps_its_trigger(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    rig.show(3, "code.exe", "main.py")
    rig.tick(2.0)
    rig.tick(2.3)  # focus fires, the new capture has no frame yet
    assert len(rig.pictures()) == 1
    rig.tick(2.4)
    assert [p.trigger for p in rig.pictures()] == ["idle", "focus"]


def test_all_due_monitors_share_one_enumeration_and_a_quiet_tick_has_none(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.tick(0.0)
    assert rig.facts.reads == 1  # both monitors due, one windows() call
    rig.tick(0.1)
    assert rig.facts.reads == 2
    rig.tick(0.4)
    rig.tick(1.1)
    reads = rig.facts.reads
    rig.tick(2.0)
    rig.tick(3.0)
    assert rig.facts.reads == reads


# --- monitors coming and going ------------------------------------------------------------------


def test_the_layout_is_written_before_the_first_picture(make_rig: Callable[..., Rig]) -> None:
    rig = two_monitors(make_rig)
    rig.settle(0.0)
    assert [len(monitors) for monitors, _ in rig.writer.layouts] == [2]
    rig.drain()
    keys = rig.conn.execute(
        "SELECT m.key, l.is_main FROM monitors m JOIN layout_monitors l ON l.monitor_id = m.id"
        " ORDER BY m.key"
    ).fetchall()
    assert [tuple(k) for k in keys] == [("m1", 1), ("m2", 0)]
    stored = rig.conn.execute(
        "SELECT m.key FROM frames f JOIN monitors m ON m.id = f.monitor_id ORDER BY f.id"
    ).fetchall()
    assert sorted(tuple(r) for r in stored) == [("m1",), ("m2",)]


def test_the_monitor_list_is_refreshed_every_5_s_and_a_change_is_written_again(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.show(2, "vlc.exe", "Movie", MON_B, focus=False)
    rig.settle(0.0)
    assert rig.displays.reads == 1  # once, not on every pass
    rig.tick(4.9)
    assert rig.displays.reads == 1
    rig.displays.monitor_list = [*(rig.displays.monitor_list or []), monitor(key="m2", rect=MON_B)]
    rig.tick(5.0)
    assert rig.displays.reads == 2
    assert len(rig.writer.layouts) == 2
    rig.drain()
    assert rig.conn.execute("SELECT count(*) FROM monitor_layouts").fetchone() == (2,)


def test_a_new_monitor_is_checked_at_once_and_leaves_the_others_alone(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.show(2, "vlc.exe", "Movie", MON_B, focus=False)
    rig.quiet()
    m1 = rig.count("m1")
    rig.displays.monitor_list = [*(rig.displays.monitor_list or []), monitor(key="m2", rect=MON_B)]
    rig.tick(5.0)  # refresh: m2 appears, its capture starts
    rig.tick(5.1)
    assert rig.count("m2") == 1
    assert rig.count("m1") == m1  # not due: its own 5 s rate runs from 1.1


def test_a_removed_monitor_is_forgotten_and_composed_again_when_it_returns(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    both = rig.displays.monitor_list
    assert both is not None
    rig.displays.monitor_list = both[:1]
    rig.tick(5.0)
    m2 = len(rig.pictures("m2"))
    rig.displays.monitor_list = both
    rig.tick(10.0)  # refresh: m2 is back; the desk saw it before and nothing changed
    rig.tick(10.1)
    assert len(rig.pictures("m2")) == m2 + 1


def test_a_monitor_list_that_cannot_be_read_keeps_the_last_one(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.quiet()
    rig.displays.monitor_list = None
    done = rig.count()
    rig.tick(6.1)
    assert rig.count() == done + 1  # still checked
    assert len(rig.writer.layouts) == 1


def test_with_no_monitors_nothing_is_checked(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    rig.displays.monitor_list = []
    reads = rig.facts.reads
    rig.tick(6.1)
    rig.tick(30.0)
    assert rig.facts.reads == reads
    assert len(rig.pictures()) == 1


# --- change detection ---------------------------------------------------------------------------


def test_a_new_frame_with_the_same_pixels_and_listing_is_only_noted_as_shown(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.quiet()
    rig.repaint(1, blank())  # a new frame, the same pixels
    rig.tick(6.1)
    assert len(rig.pictures()) == 1
    assert rig.last_note() == "shown"


def test_changed_pixels_are_submitted(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    rig.repaint(1, with_text_block(blank()))
    rig.tick(6.1)
    assert len(rig.pictures()) == 2


def test_the_same_pixels_with_a_new_title_are_submitted(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    rig.repaint(1, blank())
    rig.show(1, title="Notes - edited")
    rig.tick(6.1)
    assert [p.entries[0].title for p in rig.pictures()] == ["Notes", "Notes - edited"]


def test_only_excluded_content_is_noted_as_hidden_and_the_next_picture_is_new(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(user=UserExclusions(processes=("secret.exe",)))
    rig.quiet()
    rig.show(2, "secret.exe", "Private", focus=False)
    rig.tick(6.1)
    assert rig.last_note() == "hidden"
    assert len(rig.pictures()) == 1
    rig.hide(2)
    rig.repaint(1, blank())  # the same pixels as the stored picture
    rig.tick(12.0)  # the covered window's capture restarts
    rig.tick(12.1)
    assert len(rig.pictures()) == 2  # the signature was cleared with the hidden note


def test_a_black_composite_is_submitted_without_pixels_but_with_text(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.show(1, image=blank("black"))
    rig.uia.texts[1] = ["Some Show - Netflix"]
    rig.settle(0.0)
    (picture,) = rig.pictures()
    assert picture.image is None
    assert [e.image for e in picture.entries] == [None]
    assert picture.uia_text == "Some Show - Netflix"
    rig.drain()
    frame = latest_frames(rig.conn)[0]
    assert (frame.image_path, frame.text) == (None, "Some Show - Netflix")
    assert rig.ocr.calls == 0


def test_a_lost_monitor_is_submitted_again_even_if_unchanged(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.quiet()
    sample = rig.pictures()[0]
    for _ in range(4):  # more than the queue holds: the oldest are dropped and the monitor is lost
        ScreenWriter.submit(rig.writer, sample)  # not through the recording override
    assert rig.writer.lost("m1")
    rig.tick(6.1)
    assert len(rig.pictures()) == 2


def test_an_enumeration_failure_notes_failed_and_the_next_check_submits_again(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    rig.facts.window_list, saved = None, rig.facts.window_list
    reads = rig.captures.starts[:]
    rig.tick(6.1)
    assert rig.last_note("m1") == "failed"
    assert ("m1", 1) in rig.writer.failures
    assert rig.captures.starts == reads  # no desk check, no verdicts
    rig.facts.window_list = saved
    rig.tick(15.1)  # unchanged, but re-taken
    assert len(rig.pictures("m1")) == 2
    assert len(rig.pictures("m2")) == 1  # m2 was not marked failed at 6.1 (not due)


def test_every_due_monitor_is_noted_failed_when_windows_cannot_be_listed(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.facts.window_list = None
    rig.tick(0.0)
    assert {(n[0], n[1]) for n in rig.writer.notes} == {("m1", "failed"), ("m2", "failed")}
    assert sorted(rig.writer.failures) == [("m1", 1), ("m2", 1)]
    assert rig.writer.pictures == []


def test_a_failed_window_capture_is_reported_and_zero_resets_the_run(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.quiet()
    assert rig.writer.failures[-1] == ("m1", 0)
    rig.captures.failures.add(1)
    rig.tick(6.1)
    assert rig.writer.failures[-1] == ("m1", 1)
    rig.tick(11.2)
    assert rig.writer.failures[-1] == ("m1", 0)


def test_the_picture_has_one_timestamp_per_tick_and_the_right_flags(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.settle(0.0)
    first, second = sorted(rig.writer.pictures, key=lambda p: p.monitor_key)
    assert first.ts_ms == second.ts_ms
    assert (first.trigger, second.trigger) == ("idle", "idle")
    assert (first.side, second.side) == (False, True)
    assert (first.defer_ocr, second.defer_ocr) == (False, False)
    rig.repaint(1, with_text_block(blank()))
    rig.tick(6.1)
    assert rig.pictures()[-1].ts_ms > first.ts_ms


# --- text ---------------------------------------------------------------------------------------


def test_the_focused_text_is_read_only_for_a_picture_that_shows_the_focused_window(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    assert rig.uia.text_reads == [1]  # never the window on the other monitor
    rig.tick(6.1)  # unchanged: no picture, no read
    assert rig.uia.text_reads == [1]


def test_the_text_is_read_once_when_the_focused_window_spans_two_monitors(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(monitors=TWO)
    rig.show(1, rect=Rect(400, 0, 1200, 600))
    rig.settle(0.0)
    assert len(rig.writer.pictures) == 2
    assert rig.uia.text_reads == [1]
    assert [p.uia_text for p in rig.writer.pictures] == [rig.writer.pictures[0].uia_text] * 2


@pytest.mark.parametrize("change", ["focus", "title", "address"])
def test_text_is_dropped_when_the_window_changed_during_the_read(
    make_rig: Callable[..., Rig], change: str
) -> None:
    rig = make_rig()
    rig.show(1, "chrome.exe", "Page", url="https://example.org/one")
    rig.uia.texts[1] = LONG_TEXT

    def switch() -> None:
        if change == "focus":
            rig.windows.focused = window(hwnd=2, process="taskmgr.exe")
        elif change == "title":
            rig.windows.focused = window(process="chrome.exe", title="New tab")
        else:
            rig.uia.urls[1] = "https://example.org/two"

    rig.uia.on_read = switch
    rig.settle(0.0)
    (picture,) = rig.writer.pictures
    assert picture.uia_text is None


def test_text_is_kept_when_the_window_is_still_the_same_after_the_read(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.show(1, "chrome.exe", "Page", url="https://example.org/one")
    rig.settle(0.0)
    (picture,) = rig.writer.pictures
    assert picture.uia_text is not None
    assert "real window text" in picture.uia_text


def test_an_expensive_app_skips_uia_and_the_writer_uses_ocr(make_rig: Callable[..., Rig]) -> None:
    budget = UiaBudget()
    budget.record("notepad.exe", 400, 0.0)
    rig = make_rig(budget=budget)
    rig.repaint(1, with_text_block(blank()))
    rig.settle(1.0)  # 1 s after a critical walk: inside the 20 s backoff
    rig.drain()
    assert rig.uia.text_reads == []
    assert latest_frames(rig.conn)[0].text_source == "ocr"


def test_stored_window_text_is_capped(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    line = "Minlo a parle du nouveau jeu de survie pendant tout l'apres-midi. " * 4
    blocks = [TextBlock(f"{i:05d} {line}", Rect(0, 20 * i, 800, 20 * i + 18)) for i in range(400)]
    assert sum(len(b.text) for b in blocks) > 2 * MAX_TEXT_CHARS
    rig.uia.texts[1] = blocks
    rig.settle(0.0)
    rig.drain()
    frame = latest_frames(rig.conn)[0]
    assert frame.text is not None
    assert frame.text.startswith("00000 ")
    assert len(frame.text) <= MAX_TEXT_CHARS


# --- pause and lock -----------------------------------------------------------------------------


def test_a_locked_session_reads_nothing_and_forgets_everything(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.quiet()
    rig.windows.locked = True
    reads = rig.facts.reads
    rig.events.push(1, "moved", 2.0)
    rig.tick(2.0)
    rig.tick(7.0)
    assert rig.facts.reads == reads
    assert rig.captures.intervals == {}  # every capture was dropped
    assert rig.events.queued == []
    assert len(rig.pictures()) == 1


def test_after_the_lock_every_monitor_is_checked_at_once_and_stores_a_fresh_picture(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.quiet()
    rig.windows.locked = True
    rig.tick(2.0)
    rig.windows.locked = False
    rig.tick(2.1)
    rig.tick(2.2)
    assert len(rig.pictures("m1")) == 2
    assert len(rig.pictures("m2")) == 2


def test_locking_forgets_the_browser_verdicts_and_closes_the_game_row(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.settle(0.0)
    assert rig.conn.execute("SELECT count(*) FROM game_sessions").fetchone() == (1,)
    rig.browsers.publish(top_window(process="chrome.exe"), "finance")
    rig.windows.locked = True
    rig.tick(1.0)
    assert rig.game_flags == [True, False]
    assert rig.browsers.media_reason("chrome.exe", None) is None
    assert rig.conn.execute(
        "SELECT count(*) FROM game_sessions WHERE end_ms IS NULL"
    ).fetchone() == (0,)


# --- verdicts -----------------------------------------------------------------------------------


def test_window_events_reach_the_verdicts_before_they_judge(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.show(1, "chrome.exe", "Page", url="https://example.org/one")
    rig.quiet()
    done = rig.count()
    rig.events.push(1, "title", 2.0)  # the page changed, whatever the title says
    rig.tick(2.0)
    rig.tick(2.3)  # the event's check: the window is held
    rig.tick(2.4)
    assert rig.count() == done
    assert rig.capture.stats_snapshot()["pending"] >= 2
    rig.tick(2.5)  # three reads and half a second later the hold ends
    assert rig.count() == done + 1


def test_events_are_drained_on_every_tick(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.displays.monitor_list = []
    rig.events.push(1, "moved", 0.0)
    rig.tick(0.0)
    assert rig.events.queued == []


def test_verdicts_are_published_for_the_browsers_on_a_checked_monitor_even_if_unchanged(
    make_rig: Callable[..., Rig],
) -> None:
    rig = two_monitors(make_rig)
    rig.show(3, "zen.exe", "Private - Zen Browser", MON_B, focus=False, url="https://example.org/")
    rig.uia.captions[3] = "Private - Zen Browser (Private Browsing)"
    rig.quiet()
    assert rig.browsers.media_reason("zen.exe", None) == "private_window"
    rig.browsers.clear()
    rig.tick(15.1)  # m2 unchanged, but checked
    assert rig.browsers.media_reason("zen.exe", None) == "private_window"
    rig.browsers.clear()
    rig.tick(30.2)
    assert rig.browsers.media_reason("zen.exe", None) == "private_window"


# --- game mode ----------------------------------------------------------------------------------


def game_rig(make_rig: Callable[..., Rig], *, grace: float = 60.0, **kwargs: object) -> Rig:
    from akashic.capture.gaming import GameSettings  # noqa: PLC0415

    rig = make_rig(
        monitors=TWO,
        game_settings=GameSettings(interval_s=30.0, end_grace_s=grace),
        shown=False,
        **kwargs,
    )
    rig.show(2, "notepad.exe", "Notes", MON_B, focus=False)
    rig.show(7, "game.exe", "The Game", SCREEN, has_frame=False)
    return rig


def test_a_game_in_front_is_captured_at_its_interval_and_the_others_every_15_s(
    make_rig: Callable[..., Rig],
) -> None:
    rig = game_rig(make_rig)
    rig.settle(0.0)
    assert {p.trigger for p in rig.writer.pictures} == {"game"}
    assert rig.count("m1") == 1
    assert rig.count("m2") == 1
    rig.tick(15.0)
    assert rig.count("m2") == 1
    rig.tick(15.1)
    assert (rig.count("m1"), rig.count("m2")) == (1, 2)
    rig.tick(29.0)
    assert rig.count("m1") == 1
    rig.tick(30.1)
    assert rig.count("m1") == 2


def test_game_pictures_defer_ocr_read_no_text_and_mark_the_side(
    make_rig: Callable[..., Rig],
) -> None:
    rig = game_rig(make_rig)
    rig.settle(0.0)
    by_key = {p.monitor_key: p for p in rig.writer.pictures}
    assert (by_key["m1"].side, by_key["m2"].side) == (False, True)
    assert (by_key["m1"].defer_ocr, by_key["m2"].defer_ocr) == (True, True)
    assert rig.uia.text_reads == []
    assert [p.uia_text for p in rig.writer.pictures] == [None, None]


def test_triggers_are_ignored_while_a_game_is_in_front(make_rig: Callable[..., Rig]) -> None:
    rig = game_rig(make_rig)
    rig.settle(0.0)
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.windows.last_input = 5
    rig.events.push(2, "moved", 2.0)
    rig.show(2, "notepad.exe", "Notes - edited", MON_B, focus=False)
    for now in (2.0, 2.4, 2.5, 3.0, 5.0):
        rig.tick(now)
    assert (rig.count("m1"), rig.count("m2")) == (m1, m2)


def test_the_first_game_picture_is_taken_at_once_even_if_the_game_row_fails(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(*_args: object, **_kwargs: object) -> int:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(GameLog, "open", failing)
    rig = game_rig(make_rig)
    rig.settle(0.0)
    assert rig.capture.stats_snapshot()["game_log_error"] == 1
    assert rig.count("m1") == 1


def test_after_alt_tab_from_a_game_every_monitor_is_checked_every_15_s_without_triggers(
    make_rig: Callable[..., Rig],
) -> None:
    rig = game_rig(make_rig)
    rig.settle(0.0)
    m1, m2 = rig.count("m1"), rig.count("m2")
    rig.show(1, "notepad.exe", "Notes", SCREEN)  # focus moves to another app on m1
    for now in (2.0, 2.4, 2.5, 5.0, 10.0, 15.0):
        rig.tick(now)
    assert (rig.count("m1"), rig.count("m2")) == (m1, m2)
    rig.tick(15.1)
    rig.tick(15.2)  # the other app's capture had to start
    assert (rig.count("m1"), rig.count("m2")) == (m1 + 1, m2 + 1)


def test_the_game_rows_are_opened_refreshed_and_closed(make_rig: Callable[..., Rig]) -> None:
    rig = game_rig(make_rig, grace=5.0)
    rig.tick(0.0)
    assert rig.conn.execute("SELECT process FROM game_sessions").fetchall() == [("game.exe",)]
    assert rig.game_flags == [True]
    rig.tick(10.0)  # heartbeat
    assert rig.conn.execute("SELECT end_ms FROM game_sessions").fetchone()[0] is not None
    rig.show(1, "notepad.exe", "Notes", SCREEN)
    rig.tick(11.0)
    rig.tick(16.5)  # grace over
    assert rig.game_flags == [True, False]


# --- loop: wiring, faults, stats ----------------------------------------------------------------


def _accept(state: ipc.State, *, epoch: int | None) -> bool:
    del state, epoch
    return True


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


def test_the_loop_waits_a_fixed_poll_every_pass(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(clock=stepping_clock(0.01))
    stop = CountingStop(50)
    rig.capture.run(stop, threading.Event(), threading.Event())
    assert set(stop.timeouts) == {Settings().poll_s} == {0.1}
    assert rig.events.starts == 1  # events start in run


def test_the_loop_checks_pauses_and_resumes_on_its_own_thread(make_rig: Callable[..., Rig]) -> None:
    import time  # noqa: PLC0415

    rig = make_rig(clock=time.monotonic, settings=Settings(poll_s=0.01))
    stop, paused = threading.Event(), threading.Event()
    thread = threading.Thread(target=rig.capture.run, args=(stop, paused, paused), daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: len(rig.writer.pictures) >= 1)
        paused.set()
        assert wait_for(lambda: rig.captures.intervals == {})
        reads, pictures, drains = rig.facts.reads, len(rig.writer.pictures), rig.events.drains
        assert wait_for(lambda: rig.events.drains >= drains + 3)  # passes went by, doing nothing
        assert (rig.facts.reads, len(rig.writer.pictures)) == (reads, pictures)
        paused.clear()
        assert wait_for(lambda: len(rig.writer.pictures) >= pictures + 1)  # fresh, though unchanged
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_a_failing_tick_is_counted_logged_by_type_and_reported_on_every_monitor(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = two_monitors(make_rig)
    rig.clock[0] = 0.0
    real_tick = rig.capture.tick
    calls = [0]

    def flaky(now: float) -> None:
        calls[0] += 1
        if calls[0] == 3:
            msg = "secret title https://private.example/x"
            raise RuntimeError(msg)
        real_tick(now)

    monkeypatch.setattr(rig.capture, "tick", flaky)
    rig.capture._clock = stepping_clock(1.0)
    with caplog.at_level(logging.WARNING, logger="akashic.capture.screen"):
        rig.capture.run(CountingStop(12), threading.Event(), threading.Event())
    assert rig.capture.stats_snapshot()["loop_error"] == 1
    assert calls[0] > 3  # the loop kept going
    assert ("m1", 1) in rig.writer.failures
    assert ("m2", 1) in rig.writer.failures
    warnings = loop_warnings(caplog)
    assert len(warnings) == 1
    assert "screen loop iteration failed: RuntimeError (1 so far)" in warnings[0]
    assert rig.capture.stats_snapshot()["screen loop iteration: RuntimeError"] == 1
    assert "secret" not in caplog.text
    assert "private.example" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_a_monitor_is_submitted_again_after_a_failed_tick_even_if_unchanged(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.quiet()
    real_tick = rig.capture.tick
    calls = [0]

    def flaky(now: float) -> None:
        calls[0] += 1
        if calls[0] == 1:
            msg = "boom"
            raise OSError(msg)
        real_tick(now)

    monkeypatch.setattr(rig.capture, "tick", flaky)
    rig.capture._clock = stepping_clock(2.0)
    rig.capture.run(CountingStop(8), threading.Event(), threading.Event())
    assert len(rig.pictures()) >= 2


def test_a_persistent_fault_neither_spins_nor_floods_the_log(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(clock=stepping_clock(0.01))

    def always(now: float) -> None:
        del now
        msg = "boom"
        raise OSError(msg)

    monkeypatch.setattr(rig.capture, "tick", always)
    stop = CountingStop(300)
    with caplog.at_level(logging.WARNING, logger="akashic.capture.screen"):
        rig.capture.run(stop, threading.Event(), threading.Event())
    assert rig.capture.stats_snapshot()["loop_error"] == 299
    assert set(stop.timeouts) == {0.1}
    assert len(loop_warnings(caplog)) == 1
    assert rig.capture.stats_snapshot()["screen loop iteration: OSError"] == 299


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
        rig.capture.run(CountingStop(40), threading.Event(), threading.Event())
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
        rig.capture.run(CountingStop(30), threading.Event(), threading.Event())
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
        rig.capture.run(CountingStop(5), threading.Event(), threading.Event())
    assert rig.capture.stats_snapshot().get("loop_error", 0) == 0


def test_an_enumeration_error_does_not_end_the_loop(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(clock=stepping_clock(10.0))  # every pass refreshes the monitor list
    real = rig.displays.monitors
    calls = [0]

    def flaky() -> list[object]:
        calls[0] += 1
        if calls[0] == 1:
            msg = "display configuration changed"
            raise OSError(msg)
        return real()  # type: ignore[return-value]

    monkeypatch.setattr(rig.displays, "monitors", flaky)
    rig.capture.run(CountingStop(8), threading.Event(), threading.Event())
    assert rig.capture.stats_snapshot()["loop_error"] == 1
    assert len(rig.pictures()) >= 1


def test_a_failing_game_close_while_paused_does_not_end_the_loop(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(clock=stepping_clock(1.0))
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.tick(0.0)  # a game row opens

    def failing_close(*_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(GameLog, "close", failing_close)
    paused = threading.Event()
    paused.set()
    stop = CountingStop(4)
    rig.capture.run(stop, paused, paused)
    assert len(stop.timeouts) == 4  # the loop ran to the stop, not to its death
    assert rig.game_flags == [
        True,
        False,
    ]  # the end is reported though the row could not be written
    assert rig.capture.stats_snapshot()["game_log_error"] >= 1


def test_a_failed_game_end_row_is_never_closed_by_a_later_end(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.settle(0.0)
    closed: list[int] = []

    def failing_close(*_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(GameLog, "close", failing_close)
    rig.capture.reset()  # pause or lock: the end row cannot be written
    monkeypatch.setattr(GameLog, "open", failing_close)
    rig.settle(10.0)  # the game is back, but its new row cannot be opened either

    def recording_close(_self: GameLog, game_id: int, _ts_ms: int) -> None:
        closed.append(game_id)

    monkeypatch.setattr(GameLog, "close", recording_close)
    rig.capture.close()  # the first row stays open for close_stale, never ended now
    assert closed == []
    assert rig.capture.stats_snapshot()["game_log_error"] == 2


def test_closing_the_capture_ends_the_game_and_closes_captures_and_events_even_if_the_row_fails(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.settle(0.0)

    def failing_close(*_args: object, **_kwargs: object) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(GameLog, "close", failing_close)
    rig.capture.close()
    assert rig.events.closes == 1
    assert rig.captures.intervals == {}
    rig.drain()  # the writer still stores what was queued
    assert len(latest_frames(rig.conn)) == 1


def test_stats_are_logged_every_five_minutes_and_hold_only_counts(
    make_rig: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(clock=stepping_clock(STATS_LOG_INTERVAL_S / 2))  # the loop reads it once a pass
    rig.show(1, "notepad.exe", "secret title https://private.example/x")
    with caplog.at_level(logging.INFO, logger="akashic.capture.screen"):
        rig.capture.run(CountingStop(8), threading.Event(), threading.Event())
    logged = [r.getMessage() for r in caplog.records if "capture stats" in r.getMessage()]
    assert len(logged) == 3  # every second pass
    assert all(isinstance(v, int) for v in rig.capture.stats_snapshot().values())
    assert "secret" not in caplog.text
    assert "private.example" not in caplog.text
    assert not any("secret" in k or "private" in k for k in rig.capture.stats_snapshot())


# --- wiring through run_capture ----------------------------------------------------------------


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


class Fakes:
    """What ``run_capture_child`` ran against, for assertions afterwards."""

    def __init__(self) -> None:
        self.facts = FakeWindowFacts([top_window(hwnd=1)])
        self.events = FakeWindowEvents()
        self.captures = ImageCaptures(images={1: with_text_block(blank())})
        self.windows = FakeWindows(focused=window())

    def platform(self) -> CapturePlatform:
        from capture_fakes import FakeDisplays  # noqa: PLC0415

        return CapturePlatform(
            displays=FakeDisplays(),
            windows=self.windows,
            facts=self.facts,
            events=self.events,
            captures=self.captures,
            uia=FakeUia(texts={1: LONG_TEXT}),
            ocr=FakeOcr(),
            media=FakeMedia(),
        )


def run_capture_child(
    paths: Paths, key: DataKey, *, paused: bool, until: Callable[[], bool]
) -> Fakes:
    """Run the capture child's body over fakes until ``until()`` holds, then stop it."""
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    conn.close()
    fakes = Fakes()
    platform = fakes.platform()
    stdin = OpenStdin(key)
    reached: list[bool] = []

    def watch(ctx: child.ChildContext) -> None:
        try:
            if wait_for(until, 15):
                reached.append(True)
        finally:
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
    return fakes


def frame_rows(paths: Paths, key: DataKey) -> list[tuple[str, str | None, str | None]]:
    reopened = db.connect(paths.db_file, key.subkey("db"))
    try:
        return reopened.execute(
            "SELECT w.process, w.text_source, f.image_path FROM frames f"
            " JOIN frame_windows w ON w.frame_id = f.id"
        ).fetchall()
    finally:
        reopened.close()


def test_run_capture_wires_storage_through_the_screen_writer(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real wiring (writer thread, own connection, SQLCipher, encrypted media) stores it."""
    from akashic.storage.frames import FrameStore  # noqa: PLC0415
    from akashic.storage.monitors import MonitorLog  # noqa: PLC0415

    threads: set[str] = set()
    real_add, real_present = FrameStore.add, MonitorLog.present

    def add(self: FrameStore, frame: object) -> int:
        threads.add(threading.current_thread().name)
        return real_add(self, frame)  # type: ignore[arg-type]

    def present(self: MonitorLog, rows: object, ts_ms: int) -> dict[str, int]:
        threads.add(threading.current_thread().name)
        return real_present(self, rows, ts_ms)  # type: ignore[arg-type]

    monkeypatch.setattr(FrameStore, "add", add)
    monkeypatch.setattr(MonitorLog, "present", present)
    key = DataKey.generate()

    def stored() -> bool:
        return bool(frame_rows(paths, key)) and any(paths.media_dir.rglob("*.enc"))

    fakes = run_capture_child(paths, key, paused=False, until=stored)
    assert threads == {"screen-writer"}
    assert fakes.events.starts == 1
    assert fakes.events.closes == 1  # closed with the capture
    assert fakes.captures.intervals == {}
    rows = frame_rows(paths, key)
    assert rows[0][:2] == ("notepad.exe", "uia")
    image_path = rows[0][2]
    assert image_path is not None
    assert (paths.media_dir / (image_path + ".enc")).is_file()
    assert not (paths.media_dir / image_path).exists()
    data = MediaStore(paths.media_dir, key.subkey("media")).read(image_path)
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
    fakes = run_capture_child(paths, key, paused=True, until=lambda: len(paused_passes) >= 5)
    assert fakes.facts.reads == 0  # started paused: nothing was enumerated or captured
    assert fakes.captures.starts == []
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


# --- shutdown of the writer ----------------------------------------------------------------------


def _deadline(seconds: float = 5.0) -> capture_main.StopDeadline:
    return capture_main.StopDeadline(time.monotonic() + seconds)


class _FailingCapture:
    def close(self) -> None:
        msg = "boom"
        raise RuntimeError(msg)


def test_the_writer_stores_what_is_queued_before_its_connection_closes_even_if_close_fails(
    make_rig: Callable[..., Rig], paths: Paths, key: DataKey, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()
    rig.settle(0.0)  # a picture waits in the writer's queue
    stop = threading.Event()
    thread = threading.Thread(target=rig.writer.run, args=(stop,), name="screen-writer")
    thread.start()
    with caplog.at_level(logging.WARNING, logger="capture"):
        capture_main._close_capture(
            _FailingCapture(),  # type: ignore[arg-type]
            rig.writer,
            stop,
            thread,
            rig.conn,
            _deadline(),
        )
    assert "closing screen capture failed: RuntimeError" in caplog.text
    assert not thread.is_alive()
    with pytest.raises(sqlcipher3.ProgrammingError):
        rig.conn.execute("SELECT 1")  # closed after the writer was done
    reopened = db.connect(paths.db_file, key.subkey("db"))
    try:
        assert len(latest_frames(reopened)) == 1  # the queued picture was stored first
    finally:
        reopened.close()


def test_the_writer_outlives_the_screen_loop_until_its_own_stop_is_set(
    make_rig: Callable[..., Rig], paths: Paths, key: DataKey
) -> None:
    rig = make_rig()
    stop = threading.Event()
    thread = threading.Thread(target=rig.writer.run, args=(stop,), name="screen-writer")
    thread.start()
    thread.join(timeout=0.3)
    assert thread.is_alive()  # idle, and the context's stop is not this event
    rig.settle(0.0)  # the loop's last tick submits after everything else stopped
    capture_main._stop_writer(rig.writer, stop, thread, rig.conn, _deadline())
    reopened = db.connect(paths.db_file, key.subkey("db"))
    try:
        assert len(latest_frames(reopened)) == 1
    finally:
        reopened.close()


def test_a_writer_that_will_not_stop_is_abandoned_with_its_connection_left_open(
    make_rig: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="screen-writer", daemon=True)
    thread.start()
    with caplog.at_level(logging.WARNING, logger="capture"):
        capture_main._stop_writer(rig.writer, threading.Event(), thread, rig.conn, _deadline(0.05))
    release.set()
    thread.join(timeout=5)
    assert "screen-writer still running at the stop deadline; abandoned" in caplog.text
    rig.conn.execute("SELECT 1")  # not closed by the main thread


def test_writer_problems_reach_the_capture_health(make_rig: Callable[..., Rig]) -> None:
    from akashic.capture.health import (  # noqa: PLC0415
        SCREEN_FRAMES_DROPPED,
        SCREEN_IMAGES_FAILING,
        CaptureStatus,
    )

    rig = make_rig()
    status = CaptureStatus(_accept, lambda: 0, audio_failed=False)
    for _ in range(3):
        rig.writer.capture_failures("m1", 1)
    problems = capture_main._active_problems(
        status, rig.writer, None, rig.capture, halted=False, since=0.0
    )
    assert f"{SCREEN_IMAGES_FAILING}@m1" in problems
    assert not any(p.startswith(SCREEN_FRAMES_DROPPED) for p in problems)
    paused = capture_main._active_problems(
        status, rig.writer, None, rig.capture, halted=True, since=0.0
    )
    assert not any(p.startswith(SCREEN_IMAGES_FAILING) for p in paused)


def test_the_debug_screen_data_is_the_capture_stats(make_rig: Callable[..., Rig]) -> None:
    from types import SimpleNamespace  # noqa: PLC0415

    from akashic.capture.health import CaptureStatus  # noqa: PLC0415

    rig = make_rig()
    rig.settle(0.0)
    from akashic.log import FaultLog  # noqa: PLC0415

    ctx = SimpleNamespace(health_snapshot=lambda: (0, False, 0.0))
    status = CaptureStatus(_accept, lambda: 0, audio_failed=False)
    media_faults = FaultLog(logging.getLogger("test.media"))
    media_faults.note("media poll", OSError("x"))
    gaps_faults = FaultLog(logging.getLogger("test.gaps"))
    gaps_faults.note("capture gap tick", ValueError("x"))
    gaps_faults.note("capture gap tick", ValueError("x"))
    collected = capture_main._collect(
        ctx,  # type: ignore[arg-type]
        status,
        rig.capture,
        rig.writer,
        None,
        SimpleNamespace(stats_snapshot=lambda: {"close_failed": 2} | media_faults.counts()),  # type: ignore[arg-type]
        SimpleNamespace(stats_snapshot=lambda: {"tick_failed": 2} | gaps_faults.counts()),  # type: ignore[arg-type]
    )
    assert collected is not None
    assert collected[2]["media"] == {"close_failed": 2, "media poll: OSError": 1}
    assert collected[2]["gaps"] == {"capture gap tick: ValueError": 2, "tick_failed": 2}
    assert collected[2]["writer"] == dict(sorted(rig.writer.stats.items()))
    assert collected[2]["screen"] == dict(sorted(rig.capture.stats_snapshot().items()))
    assert "images_failing" not in str(rig.capture.stats_snapshot())
    assert isinstance(rig.writer.pictures[0], Picture)


def test_the_windows_platform_is_built_from_the_per_monitor_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from akashic.platform import win  # noqa: PLC0415 - Windows only
    from akashic.platform.win import ocr as win_ocr  # noqa: PLC0415

    class Marker:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    kinds = {name: type(name, (Marker,), {}) for name in ("Displays", "Windows", "Events", "Wgc")}
    monkeypatch.setattr(win, "set_dpi_aware", lambda: None)
    monkeypatch.setattr(win, "Win32Displays", kinds["Displays"])
    monkeypatch.setattr(win, "Win32Windows", kinds["Windows"])
    monkeypatch.setattr(win, "Win32WindowEvents", kinds["Events"])
    monkeypatch.setattr(win, "WgcCaptures", kinds["Wgc"])
    monkeypatch.setattr(win, "WindowsUia", Marker)
    monkeypatch.setattr(win, "WindowsMedia", Marker)
    monkeypatch.setattr(win_ocr, "WindowsOcr", Marker)
    platform = win.build_capture_platform("en")
    assert type(platform.displays) is kinds["Displays"]
    assert type(platform.windows) is kinds["Windows"]
    assert platform.facts is platform.windows  # one object is both the window source and facts
    assert type(platform.events) is kinds["Events"]
    assert type(platform.captures) is kinds["Wgc"]
