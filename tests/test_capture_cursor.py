"""Input over the window under the cursor (audit #144): the cursor gate, the focused-window skip,
owned popups, and the unfocused-capture consistency checks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import pytest
from capture_fakes import SCREEN, blank, window, with_text_block
from screen_rig import MON_B, Rig
from store_reads import latest_frames

from akashic.capture.health import FAILED_GRABS, GOOD_GRABS
from akashic.capture.ports import Rect, WindowInfo
from akashic.capture.screen import Settings
from akashic.config import Exclusions as UserExclusions

if TYPE_CHECKING:
    from collections.abc import Callable

LEFT = Rect(0, 0, 400, 600)
RIGHT = Rect(400, 0, 800, 600)
NEVER = float("-inf")


def _beside(rig: Rig, *, settle: bool = True) -> WindowInfo:
    """A focused left half, an unfocused Discord pane on the right; the cursor is over Discord."""
    rig.windows.focused = window(rect=LEFT)
    side = window(hwnd=9, process="discord.exe", title="#general", rect=RIGHT)
    rig.windows.at[(600, 300)] = side
    rig.windows.at[(100, 300)] = rig.windows.focused
    rig.windows.cursor = (600, 300)
    if settle:
        rig.capture.tick(0.0)
        rig.capture.tick(3.0)  # the start-up focus trigger has fired
    return side


def _rows(rig: Rig) -> list[tuple[str, int, str, int]]:
    return [
        (str(r[0]), int(r[1]), str(r[2]), int(r[3]))
        for r in rig.conn.execute(
            "SELECT trigger, monitor, process, focused FROM frames ORDER BY id"
        )
    ]


def _discord(rig: Rig) -> list[tuple[str, int, str, int]]:
    return [r for r in _rows(rig) if r[2] == "discord.exe"]


def _next_due(rig: Rig) -> float | None:
    return rig.capture._debouncer.next_due()


def test_input_over_an_unfocused_window_beside_the_focused_one_is_stored_unfocused(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert ("input", 0, "discord.exe", 0) in _rows(rig)
    assert 9 not in rig.uia.text_reads  # OCR only, no UIA walk


def test_the_cursor_frame_has_the_monitor_containing_the_cursor(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(monitors=[SCREEN, MON_B])
    rig.windows.at[MON_B.center] = window(hwnd=7, process="vlc.exe", title="Movie", rect=MON_B)
    rig.windows.at[(900, 300)] = window(
        hwnd=9, process="discord.exe", title="#g", rect=Rect(800, 0, 1100, 600)
    )
    rig.capture.tick(0.0)
    rig.capture.tick(3.0)
    rig.windows.cursor = (900, 300)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    rows = _rows(rig)
    assert ("input", 1, "discord.exe", 0) in rows
    assert not [r for r in rows if r[0] == "input" and r[2] == "vlc.exe"]


def test_input_over_the_focused_window_is_neutral_and_leaves_the_cursor_gate_alone(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.windows.cursor = (100, 300)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert rig.capture.stats["pointed_focused"] == 1
    assert rig.capture._pointed_state.last_attempt == NEVER
    assert not [r for r in _rows(rig) if r[3] == 0]
    rig.windows.cursor = (600, 300)  # the cursor gate is still open: no deferral
    rig.capture.note("input", 5.5)
    rig.capture.tick(6.0)
    assert ("input", 0, "discord.exe", 0) in _rows(rig)


def test_pointed_focused_is_in_neither_grab_set_and_counts_once_per_pop(
    make_rig: Callable[..., Rig],
) -> None:
    assert "pointed_focused" not in FAILED_GRABS
    assert "pointed_focused" not in GOOD_GRABS
    rig = make_rig()
    _beside(rig)
    rig.windows.cursor = (100, 300)
    for step in range(6):
        rig.capture.note("input", 4.0 + step)
        rig.capture.tick(4.5 + step)
    assert rig.capture.stats["pointed_focused"] == 6


def test_a_deferred_input_pop_does_not_count_pointed_focused(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(settings=Settings(min_gap_s=1.0))
    _beside(rig)
    rig.windows.cursor = (100, 300)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.4)  # the focused gate is open: counted once
    assert rig.capture.stats["pointed_focused"] == 1
    rig.capture.note("input", 4.5)
    rig.capture.tick(4.9)  # inside the focused gate: deferred, not counted
    assert rig.capture.stats["pointed_focused"] == 1
    rig.capture.tick(5.5)  # the replay
    assert rig.capture.stats["pointed_focused"] == 2


def test_an_owned_popup_of_the_focused_app_is_never_stored_unfocused(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig, settle=False)
    # window_at resolves the popup to focused
    rig.windows.at[(600, 300)] = cast("WindowInfo", rig.windows.focused)
    rig.capture.tick(0.0)
    rig.capture.tick(3.0)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert rig.capture.stats["pointed_focused"] == 1
    assert not [r for r in _rows(rig) if r[3] == 0]


def test_a_centre_popup_of_the_focused_app_is_focused_and_gets_the_password_check(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(monitors=[SCREEN, MON_B])
    rig.windows.focused = window(rect=MON_B)
    rig.windows.at[SCREEN.center] = rig.windows.focused
    rig.uia.password = True
    rig.capture.tick(0.0)
    assert latest_frames(rig.conn) == []


@pytest.mark.parametrize("cursor", [(-50, -50), (9000, 9000), (800, 600)])
def test_no_cursor_capture_off_every_monitor(
    make_rig: Callable[..., Rig],
    cursor: tuple[int, int],
) -> None:
    rig = make_rig(settings=Settings(min_gap_s=1.0))
    _beside(rig)
    rig.windows.cursor = cursor
    rig.windows.at[cursor] = window(hwnd=9, process="discord.exe", title="x", rect=RIGHT)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert not [r for r in _rows(rig) if r[3] == 0]
    assert rig.capture._pointed_state.last_attempt == NEVER


def test_no_cursor_capture_when_no_window_is_under_the_cursor(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    del rig.windows.at[(600, 300)]
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert not [r for r in _rows(rig) if r[3] == 0]
    assert rig.capture.stats["pointed_focused"] == 0


def test_one_input_defers_once_when_the_focused_gate_is_blocked(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(settings=Settings(min_gap_s=1.0))
    _beside(rig)
    rig.screen.frames[0] = with_text_block(blank())
    rig.capture.tick(10.0)  # an idle focused capture at t0
    before = len(_rows(rig))
    rig.capture.note("input", 10.0)
    rig.capture.tick(10.4)  # due, but the gate is closed: neither target is captured
    assert len(_rows(rig)) == before
    assert _next_due(rig) == pytest.approx(11.0)
    rig.capture.tick(10.7)
    assert len(_rows(rig)) == before
    rig.capture.tick(11.0)
    assert ("input", 0, "discord.exe", 0) in _rows(rig)
    assert _next_due(rig) is None  # it never replays again


def test_the_trigger_defers_to_the_later_of_two_blocked_gates(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(settings=Settings(min_gap_s=1.0))
    _beside(rig)
    rig.capture._states[0].last_attempt = 10.0
    rig.capture._pointed_state.last_attempt = 10.5
    rig.capture.note("input", 10.0)
    rig.capture.tick(10.6)
    assert _next_due(rig) == pytest.approx(11.5)
    rig.capture.tick(11.5)
    assert _next_due(rig) is None


def test_a_blocked_cursor_gate_alone_defers_the_whole_trigger(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(settings=Settings(min_gap_s=1.0))
    _beside(rig)
    before = len(_rows(rig))
    rig.capture._pointed_state.last_attempt = 10.0
    rig.capture.note("input", 10.0)
    rig.capture.tick(10.4)
    assert len(_rows(rig)) == before
    assert _next_due(rig) == pytest.approx(11.0)


def test_cursor_over_the_focused_window_only_the_focused_gate_applies(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(settings=Settings(min_gap_s=1.0))
    _beside(rig)
    rig.windows.cursor = (100, 300)
    rig.capture._states[0].last_attempt = 10.0
    rig.capture._pointed_state.last_attempt = 10.5
    rig.capture.note("input", 10.0)
    rig.capture.tick(10.4)
    assert _next_due(rig) == pytest.approx(11.0)  # today's deferral
    assert rig.capture.stats["pointed_focused"] == 0


def test_repeated_input_over_an_unchanged_cursor_window_is_unchanged(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    unchanged = rig.capture.stats["unchanged"]
    rig.capture.note("input", 6.0)
    rig.capture.tick(6.5)
    assert rig.capture.stats["unchanged"] == unchanged + 2  # the focused and the cursor window
    assert len(_discord(rig)) == 1


def test_reset_clears_the_cursor_state(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    _beside(rig)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert rig.capture._pointed_state.last_attempt == 4.5
    rig.capture.reset()
    assert rig.capture._pointed_state.last_attempt == NEVER
    rig.capture.tick(10.0)
    rig.capture.tick(12.0)  # the focus trigger after the reset has fired
    rig.capture.note("input", 13.0)
    rig.capture.tick(13.5)
    assert len(_discord(rig)) == 2  # stored again: the change state was forgotten


def test_the_cursor_capture_goes_through_exclusions(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(user=UserExclusions(processes=("discord.exe",)))
    _beside(rig)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert not _discord(rig)
    assert 9 not in rig.uia.text_reads


def test_the_cursor_capture_respects_the_lock(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.windows.locked = True
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert not _discord(rig)
    assert rig.capture.stats["locked"] >= 1


def test_a_private_browser_window_under_the_cursor_is_not_stored(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.windows.at[(600, 300)] = window(
        hwnd=9, process="chrome.exe", title="New tab - Google Chrome (Incognito)", rect=RIGHT
    )
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert not [r for r in _rows(rig) if r[2] == "chrome.exe"]


def test_an_excluded_url_under_the_cursor_is_not_stored(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(user=UserExclusions(urls=("*mybank.example*",)))
    _beside(rig)
    rig.windows.at[(600, 300)] = window(
        hwnd=9, process="chrome.exe", title="Bank - Google Chrome", rect=RIGHT
    )
    rig.uia.urls[9] = "https://mybank.example/compte"
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert not [r for r in _rows(rig) if r[2] == "chrome.exe"]


def test_a_cursor_window_that_gains_focus_during_its_capture_is_rejected(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    side = _beside(rig)
    rig.screen.on_grab = lambda: setattr(rig.windows, "focused", side)
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert not _discord(rig)
    assert rig.capture.stats["window_changed"] >= 1


def test_a_centre_window_that_gains_focus_during_its_capture_is_rejected(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(monitors=[SCREEN, MON_B])
    vlc = window(hwnd=7, process="vlc.exe", title="Movie", rect=MON_B)
    rig.windows.at[MON_B.center] = vlc
    grabs: list[int] = []

    def focus_on_second_grab() -> None:
        grabs.append(1)
        if len(grabs) == 2:  # the second monitor's window
            rig.windows.focused = vlc

    rig.screen.on_grab = focus_on_second_grab
    rig.capture.tick(0.0)
    assert not [r for r in _rows(rig) if r[2] == "vlc.exe"]
    assert rig.capture.stats["window_changed"] >= 1


def test_the_side_monitor_idle_capture_wins_the_hwnd_in_the_same_tick(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(monitors=[SCREEN, MON_B])
    vlc = window(hwnd=7, process="vlc.exe", title="Movie", rect=MON_B)
    rig.windows.at[MON_B.center] = vlc
    rig.windows.at[(900, 300)] = vlc
    rig.capture.tick(0.0)
    rig.capture.tick(3.0)
    rig.windows.cursor = (900, 300)
    rig.capture.note("input", 14.0)
    before = len([r for r in _rows(rig) if r[2] == "vlc.exe"])
    rig.capture.tick(16.0)  # the input trigger and the side monitor's idle capture are both due
    assert rig.capture.stats["duplicate_window"] >= 1
    assert len([r for r in _rows(rig) if r[2] == "vlc.exe"]) == before + 1


def test_focus_and_title_triggers_never_capture_the_cursor_window(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.capture.note("title", 4.0)
    rig.capture.note("focus", 4.0)
    rig.capture.tick(5.0)
    assert not _discord(rig)


def test_cursor_capture_logs_stay_content_free(
    make_rig: Callable[..., Rig],
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.windows.at[(600, 300)] = window(
        hwnd=9, process="discord.exe", title="SECRETTITLE", rect=RIGHT
    )
    rig.screen.fail = True
    with caplog.at_level(logging.DEBUG):
        rig.capture.note("input", 4.0)
        rig.capture.tick(4.5)
    assert "SECRETTITLE" not in caplog.text
    assert "600" not in caplog.text


def test_a_failing_cursor_grab_is_counted_and_the_loop_recovers(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    _beside(rig)
    rig.screen.fail = True
    rig.capture.note("input", 4.0)
    rig.capture.tick(4.5)
    assert rig.capture.stats["error"] >= 1
    rig.screen.fail = False
    rig.capture.note("input", 6.0)
    rig.capture.tick(6.5)
    assert ("input", 0, "discord.exe", 0) in _rows(rig)
