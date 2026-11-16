"""The screen loop while halted (UI open, user Pause) and the own-address failsafe."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from capture_fakes import top_window

from akashic.capture.ports import Rect

if TYPE_CHECKING:
    from collections.abc import Callable

    from screen_rig import Rig

OWN = "http://127.0.0.1:5123/"
OWN_ORIGIN = "http://127.0.0.1:5123"
PAGE = "https://example.org/page"
LEFT, RIGHT = Rect(0, 0, 400, 600), Rect(400, 0, 800, 600)


class Script(threading.Event):
    """A stop that never waits: each pass moves the clock on, runs the action planned for it,
    and ends the loop after ``limit`` passes."""

    def __init__(self, rig: Rig, limit: int, step: float = 0.5) -> None:
        super().__init__()
        self.rig, self.limit, self.step = rig, limit, step
        self.passes = 0
        self.actions: dict[int, Callable[[], None]] = {}

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.passes += 1
        self.rig.clock[0] += self.step
        action = self.actions.get(self.passes)
        if action is not None:
            action()
        if self.passes >= self.limit:
            self.set()
        return self.is_set()


def browser_rig(make_rig: Callable[..., Rig], url: str = OWN, **kw: object) -> Rig:
    rig = make_rig(api_origin=OWN_ORIGIN, shown=False, **kw)
    rig.show(1, "chrome.exe", "Page - Google Chrome", url=url)
    return rig


def run(rig: Rig, stop: Script, halt: threading.Event | None = None) -> threading.Event:
    paused = threading.Event()
    rig.capture.run(stop, halt or threading.Event(), paused)
    return paused


# --- the failsafe trigger ----------------------------------------------------------------------


def test_a_visible_browser_on_the_own_address_stores_nothing_and_halts(
    make_rig: Callable[..., Rig],
) -> None:
    rig = browser_rig(make_rig)
    rig.settle(0.0)
    assert rig.own_flags
    assert all(rig.own_flags)
    assert rig.writer.pictures == []
    assert rig.writer.notes == []
    assert rig.captures.intervals == {}  # every window capture stopped
    assert rig.ocr.calls == 0
    rig.drain()
    assert rig.frames() == []


@pytest.mark.parametrize(
    "url", ["127.0.0.1:5123", "localhost:5123/open?code=x", "http://[::1]:5123/", OWN]
)
def test_the_own_address_counts_with_or_without_a_scheme(
    make_rig: Callable[..., Rig], url: str
) -> None:
    rig = browser_rig(make_rig, url)
    rig.settle(0.0)
    assert rig.own_flags
    assert rig.writer.pictures == []


def test_another_loopback_port_is_the_users_own_server(make_rig: Callable[..., Rig]) -> None:
    rig = browser_rig(make_rig, "http://127.0.0.1:8000/")
    rig.settle(0.0)
    assert rig.own_flags == []
    assert rig.writer.pictures


def test_without_a_known_port_any_loopback_port_trips(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(shown=False)  # no api_origin
    rig.show(1, "chrome.exe", "Page - Google Chrome", url="http://127.0.0.1:8000/")
    rig.settle(0.0)
    assert rig.own_flags


def test_a_private_window_on_the_own_address_still_trips(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig(api_origin=OWN_ORIGIN, shown=False)
    rig.show(1, "chrome.exe", "Page - Google Chrome (Incognito)", url=OWN)
    rig.settle(0.0)
    assert rig.own_flags
    assert rig.writer.pictures == []


def test_a_window_with_unknown_children_on_the_own_address_still_trips(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(api_origin=OWN_ORIGIN, shown=False)
    rig.show(1, "chrome.exe", "Page - Google Chrome", url=OWN, foreign_children=None)
    rig.settle(0.0)
    assert rig.own_flags
    assert rig.writer.pictures == []


def test_an_unreadable_caption_on_the_own_address_still_trips(
    make_rig: Callable[..., Rig],
) -> None:
    rig = browser_rig(make_rig)
    rig.uia.captions[1] = None
    rig.settle(0.0)
    assert rig.own_flags
    assert rig.writer.pictures == []


def test_the_own_address_on_a_side_monitor_trips_and_keeps_the_focused_page_out(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(api_origin=OWN_ORIGIN, monitors=[LEFT, RIGHT], shown=False)
    rig.show(2, "chrome.exe", "Akashic", rect=RIGHT, url=OWN, focus=False)
    rig.show(1, "notepad.exe", "Notes", rect=LEFT)
    rig.settle(0.0)
    assert rig.own_flags
    assert rig.writer.pictures == []
    assert rig.writer.notes == []


# --- the failsafe watch ------------------------------------------------------------------------


def watch_rig(make_rig: Callable[..., Rig]) -> tuple[Rig, Script]:
    rig = browser_rig(make_rig)
    rig.tick(0.0)
    assert rig.own_flags == [True]
    return rig, Script(rig, limit=20)


def test_the_watch_reads_only_addresses_at_most_once_a_second(
    make_rig: Callable[..., Rig],
) -> None:
    rig, stop = watch_rig(make_rig)
    reads, starts = rig.uia.url_reads, list(rig.captures.starts)
    run(rig, stop, threading.Event())
    assert 0 < rig.uia.url_reads - reads <= 10  # 20 passes of 0.5 s: one read a second at most
    assert rig.uia.text_reads == []
    assert rig.ocr.calls == 0
    assert rig.captures.starts == starts  # no grab
    assert rig.writer.pictures == []
    assert rig.own_flags == [True]  # the address is still shown


def test_the_watch_ends_and_capture_resumes_when_the_address_is_gone(
    make_rig: Callable[..., Rig],
) -> None:
    rig, stop = watch_rig(make_rig)
    stop.actions[3] = lambda: rig.uia.urls.__setitem__(1, PAGE)
    run(rig, stop)
    assert rig.own_flags == [True, False]
    assert rig.writer.pictures  # capture checked the page again


def test_an_unreadable_address_counts_as_gone(make_rig: Callable[..., Rig]) -> None:
    rig, stop = watch_rig(make_rig)
    stop.actions[3] = lambda: rig.uia.urls.__setitem__(1, None)
    run(rig, stop)
    assert rig.own_flags[-1] is False


def test_an_owned_or_hidden_window_does_not_hold_the_failsafe(
    make_rig: Callable[..., Rig],
) -> None:
    rig, stop = watch_rig(make_rig)

    def move_own_address_away() -> None:
        rig.uia.urls[1] = PAGE
        rig.uia.urls[2] = OWN
        rig.facts.window_list = [
            top_window(hwnd=1, process="chrome.exe", title="Page - Google Chrome"),
            top_window(hwnd=2, process="chrome.exe", title="Dialog", owner=1),
        ]

    stop.actions[3] = move_own_address_away
    run(rig, stop)
    assert rig.own_flags[-1] is False


def test_the_user_pause_ends_the_watch(make_rig: Callable[..., Rig]) -> None:
    rig, stop = watch_rig(make_rig)
    reads = rig.uia.url_reads
    paused = threading.Event()
    paused.set()
    rig.capture.run(stop, paused, paused)
    assert rig.own_flags == [True, False]
    assert rig.uia.url_reads == reads


def test_a_locked_session_ends_the_watch(make_rig: Callable[..., Rig]) -> None:
    rig, stop = watch_rig(make_rig)
    stop.actions[2] = lambda: setattr(rig.windows, "locked", True)
    run(rig, stop)
    assert rig.own_flags == [True, False]


def test_the_watch_goes_on_while_a_lease_also_halts(make_rig: Callable[..., Rig]) -> None:
    rig, stop = watch_rig(make_rig)
    halt = threading.Event()
    halt.set()  # the UI is open too
    stop.actions[3] = lambda: rig.uia.urls.__setitem__(1, PAGE)
    run(rig, stop, halt)
    assert rig.own_flags == [True, False]  # the watch ended; halt itself belongs to the context
    assert rig.writer.pictures == []  # still halted by the UI: nothing is stored


# --- halted for the UI -------------------------------------------------------------------------


def test_halted_the_loop_grabs_reads_and_stores_nothing(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.show(1, "notepad.exe", "Notes")
    halt = threading.Event()
    halt.set()
    run(rig, Script(rig, limit=15), halt)
    assert rig.facts.reads == 0
    assert rig.displays.reads == 0
    assert rig.captures.starts == []
    assert rig.uia.text_reads == []
    assert rig.uia.url_reads == 0
    assert rig.ocr.calls == 0
    assert rig.writer.pictures == []
    assert rig.writer.notes == []
    assert rig.own_flags == []


def test_the_loop_checks_again_when_the_halt_ends(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    rig.show(1, "notepad.exe", "Notes")
    halt = threading.Event()
    halt.set()
    stop = Script(rig, limit=30, step=0.2)
    stop.actions[5] = halt.clear
    run(rig, stop, halt)
    assert rig.writer.pictures
