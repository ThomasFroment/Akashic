"""Win32WindowEvents with the hook, pump and ancestor seams replaced (no real hook is installed)."""

from __future__ import annotations

import ctypes
import logging
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from akashic.capture.ports import WindowChanges, WindowEvent
from akashic.platform.win import events as ev

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

N_RANGES = len(ev._RANGES)  # pyright: ignore[reportPrivateUsage]
MAX_EVENTS = ev._MAX_EVENTS  # pyright: ignore[reportPrivateUsage]
REAL_IS_CHILD = ev._is_child  # pyright: ignore[reportPrivateUsage]
HWND = 0xDEAD1234


def _not_child(_hwnd: int) -> bool:
    return False


def _ancestor(root: int | None) -> Callable[[int, int], int | None]:
    return lambda _hwnd, _flag: root


class Seams:
    def __init__(self) -> None:
        self.hook_calls = 0
        self.attempts = 0
        self.fail_at: int | None = None  # 0-based range index that fails in every attempt
        self.fail_error = 5
        self.raise_in_hook = False
        self.raise_in_pump = False
        self.hold_install: threading.Event | None = None
        self.next_handle = 100
        self.on_hook: Callable[[], None] | None = None
        self.unhooked: list[int] = []
        self.quits: list[int] = []
        self.pump_stop = threading.Event()
        self.pump_started = threading.Event()

    def set_hook(self, event_range: tuple[int, int], proc: object) -> int:
        del proc
        if event_range == ev._RANGES[0]:  # pyright: ignore[reportPrivateUsage]
            self.attempts += 1
            self.pump_stop = threading.Event()
        self.hook_calls += 1
        if self.on_hook is not None:
            self.on_hook()
        if self.hold_install is not None:
            self.hold_install.wait(5)
        if self.raise_in_hook:
            raise OSError("hook layer failed")
        if self.fail_at is not None and ev._RANGES.index(event_range) == self.fail_at:  # pyright: ignore[reportPrivateUsage]
            ctypes.set_last_error(self.fail_error)
            return 0
        self.next_handle += 1
        return self.next_handle

    def unhook(self, handles: list[int]) -> None:
        self.unhooked.extend(handles)

    def pump(self) -> None:
        self.pump_started.set()
        if self.raise_in_pump:
            raise OSError("pump failed")
        self.pump_stop.wait(5)

    def quit(self, thread_id: int) -> None:
        self.quits.append(thread_id)
        self.pump_stop.set()


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seams:
    s = Seams()
    monkeypatch.setattr(ev, "_set_hook", s.set_hook)
    monkeypatch.setattr(ev, "_unhook", s.unhook)
    monkeypatch.setattr(ev, "_pump", s.pump)
    monkeypatch.setattr(ev, "_quit", s.quit)
    monkeypatch.setattr(ev, "_is_child", _not_child)
    return s


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(ev, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def events(seams: Seams) -> Iterator[ev.Win32WindowEvents]:
    w = ev.Win32WindowEvents()
    yield w
    seams.pump_stop.set()
    w.close()


def settle(w: ev.Win32WindowEvents) -> None:
    thread = w._thread  # pyright: ignore[reportPrivateUsage]
    if thread is not None:
        thread.join(2)


def fire(
    w: ev.Win32WindowEvents, event: int, hwnd: int | None = HWND, obj: int = 0, child: int = 0
) -> None:
    w._on_event(0, event, hwnd, obj, child, 0, 0)  # pyright: ignore[reportPrivateUsage]


def window_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "window-events")


CHANGE_CASES = [
    (0x8002, "shown"),
    (0x8018, "shown"),
    (0x0017, "shown"),
    (0x8003, "hidden"),
    (0x8017, "hidden"),
    (0x0016, "hidden"),
    (0x8001, "destroyed"),
    (0x800B, "moved"),
    (0x8004, "moved"),
    (0x800C, "title"),
    (0x0003, "focus"),
]


@pytest.mark.parametrize(("event", "change"), CHANGE_CASES)
def test_event_ids_map_to_changes(
    events: ev.Win32WindowEvents, monkeypatch: pytest.MonkeyPatch, event: int, change: str
) -> None:
    monkeypatch.setattr(ev, "time", SimpleNamespace(monotonic=lambda: 42.5))
    fire(events, event)
    assert events.drain().events == (WindowEvent(HWND, change, 42.5),)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("event", "obj", "child", "hwnd"),
    [
        (0x8000, 0, 0, HWND),  # CREATE
        (0x8005, 0, 0, HWND),  # FOCUS
        (0x800E, 0, 0, HWND),  # VALUECHANGE
        (0x800B, -9, 0, HWND),  # OBJID_CURSOR
        (0x800B, -8, 0, HWND),  # OBJID_CARET
        (0x800B, -1, 0, HWND),
        (0x800B, 1, 0, HWND),
        (0x800C, 0, 1, HWND),
        (0x800C, 0, -1, HWND),
        (0x8002, 0, 0, None),
        (0x8002, 0, 0, 0),
    ],
)
def test_dropped_callbacks_queue_nothing(
    events: ev.Win32WindowEvents, event: int, obj: int, child: int, hwnd: int | None
) -> None:
    fire(events, event, hwnd, obj, child)
    assert events.drain().events == ()


def test_client_area_name_change_is_a_title_event(
    events: ev.Win32WindowEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ev, "time", SimpleNamespace(monotonic=lambda: 7.0))
    fire(events, 0x800C, obj=-4)
    assert events.drain().events == (WindowEvent(HWND, "title", 7.0),)


@pytest.mark.parametrize(("event", "child"), [(0x800B, 0), (0x8002, 0), (0x800C, 1)])
def test_client_object_only_counts_for_a_name_change_on_the_window_itself(
    events: ev.Win32WindowEvents, event: int, child: int
) -> None:
    fire(events, event, obj=-4, child=child)
    assert events.drain().events == ()


def test_live_child_window_is_dropped(
    events: ev.Win32WindowEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ev, "_is_child", REAL_IS_CHILD)
    monkeypatch.setattr(ev.user32, "GetAncestor", _ancestor(0x1111))
    fire(events, 0x8002)
    assert events.drain().events == ()


@pytest.mark.parametrize("root", [0, None, HWND])
def test_gone_or_top_level_window_is_kept(
    events: ev.Win32WindowEvents, monkeypatch: pytest.MonkeyPatch, root: int | None
) -> None:
    monkeypatch.setattr(ev, "_is_child", REAL_IS_CHILD)
    monkeypatch.setattr(ev.user32, "GetAncestor", _ancestor(root))
    fire(events, 0x8002)
    assert [e.hwnd for e in events.drain().events] == [HWND]


def test_drain_is_oldest_first_and_empties(events: ev.Win32WindowEvents) -> None:
    for hwnd in (1, 2, 3):
        fire(events, 0x8002, hwnd)
    assert [e.hwnd for e in events.drain().events] == [1, 2, 3]
    assert events.drain().events == ()


def test_health_first_drain_false_then_true(events: ev.Win32WindowEvents) -> None:
    events.start()
    assert events.drain().healthy is False
    assert events.drain().healthy is True
    assert events.drain().healthy is True


def test_overflow_keeps_newest_and_reports_unhealthy_once(
    events: ev.Win32WindowEvents, caplog: pytest.LogCaptureFixture
) -> None:
    events.start()
    events.drain()
    events.drain()
    for hwnd in range(1, MAX_EVENTS + 6):
        fire(events, 0x800B, hwnd)
    with caplog.at_level(logging.DEBUG):
        first = events.drain()
    assert len(first.events) == MAX_EVENTS
    assert first.events[0].hwnd == 6
    assert first.events[-1].hwnd == MAX_EVENTS + 5
    assert first.healthy is False
    assert [r.levelno for r in caplog.records].count(logging.WARNING) == 1
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert events.drain().healthy is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_failed_install_removes_hooks_and_records_nothing(
    events: ev.Win32WindowEvents, seams: Seams
) -> None:
    seams.fail_at = 3
    seams.on_hook = lambda: fire(events, 0x8002)  # an early range delivers before the failure
    events.start()
    settle(events)
    assert len(seams.unhooked) == 3
    assert events.drain() == WindowChanges((), healthy=False)


def test_failed_install_retries_with_a_doubling_delay_capped_at_a_minute(
    events: ev.Win32WindowEvents, seams: Seams, clock: list[float]
) -> None:
    seams.fail_at = 2
    events.start()
    settle(events)
    assert seams.attempts == 1
    delay = 1.0
    for expected_attempts in range(2, 10):
        clock[0] += delay - 0.01
        assert events.drain().healthy is False
        assert seams.attempts == expected_attempts - 1  # too early: no thread
        clock[0] += 0.01
        assert events.drain().healthy is False
        settle(events)
        assert seams.attempts == expected_attempts
        delay = min(delay * 2, 60.0)
    assert delay == 60.0


def test_a_hook_that_installs_and_dies_still_counts_as_an_attempt(
    events: ev.Win32WindowEvents, seams: Seams, clock: list[float]
) -> None:
    events.start()
    seams.pump_stop.set()
    settle(events)
    for delay in (1.0, 2.0, 4.0):
        assert events.drain().healthy is False  # too early, or the reinstall that follows
        clock[0] += delay
        assert events.drain().healthy is False
        seams.pump_stop.set()
        settle(events)
    assert seams.attempts == 4
    clock[0] += 7.9
    attempts = seams.attempts
    events.drain()
    assert seams.attempts == attempts  # the next delay is 8 s


def test_a_healthy_drain_resets_the_backoff(
    events: ev.Win32WindowEvents, seams: Seams, clock: list[float]
) -> None:
    seams.fail_at = 1
    events.start()
    settle(events)
    for _ in range(3):
        clock[0] += 100
        events.drain()
        settle(events)
    seams.fail_at = None
    clock[0] += 100
    events.drain()
    settle(events)
    assert events.drain().healthy is False  # first drain after the install
    assert events.drain().healthy is True
    seams.pump_stop.set()
    settle(events)
    attempts = seams.attempts
    assert events.drain().healthy is False
    assert seams.attempts == attempts + 1  # a working hook that dies reinstalls at once
    settle(events)
    seams.pump_stop.set()
    settle(events)
    clock[0] += 1.0  # the delay is back to 1 s
    events.drain()
    assert seams.attempts == attempts + 2


def test_reinstall_success_needs_two_drains_to_be_healthy(
    events: ev.Win32WindowEvents, seams: Seams, clock: list[float]
) -> None:
    seams.fail_at = 1
    events.start()
    settle(events)
    assert events.drain().healthy is False
    settle(events)
    seams.fail_at = None
    clock[0] += 1.0
    assert events.drain().healthy is False  # triggers the working reinstall
    assert events.drain().healthy is False
    assert events.drain().healthy is True


def test_hook_thread_death_marks_down_and_reinstalls(
    events: ev.Win32WindowEvents, seams: Seams
) -> None:
    events.start()
    events.drain()
    assert events.drain().healthy is True
    seams.pump_stop.set()
    settle(events)
    assert len(seams.unhooked) == N_RANGES
    attempts = seams.attempts
    assert events.drain().healthy is False
    assert seams.attempts == attempts + 1


def test_start_twice_installs_once(events: ev.Win32WindowEvents, seams: Seams) -> None:
    events.start()
    events.start()
    assert seams.attempts == 1
    assert window_threads() == 1


def test_no_reinstall_while_previous_thread_alive(
    events: ev.Win32WindowEvents, seams: Seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ev, "_INSTALL_WAIT_S", 0.05)
    seams.hold_install = threading.Event()
    events.start()
    assert events.drain().healthy is False
    assert events.drain().healthy is False
    assert seams.attempts == 1
    assert window_threads() == 1
    seams.hold_install.set()


def test_close_quits_unhooks_and_joins(events: ev.Win32WindowEvents, seams: Seams) -> None:
    events.start()
    thread = events._thread  # pyright: ignore[reportPrivateUsage]
    assert thread is not None
    events.close()
    assert seams.quits == [thread.native_id]
    assert len(seams.unhooked) == N_RANGES
    assert not thread.is_alive()


def test_close_twice_and_before_start(events: ev.Win32WindowEvents) -> None:
    events.close()
    events.close()
    assert events.drain() == WindowChanges((), healthy=False)


def test_close_twice_after_start(events: ev.Win32WindowEvents) -> None:
    events.start()
    events.close()
    events.close()


def test_after_close_no_reinstall_and_callbacks_dropped(
    events: ev.Win32WindowEvents, seams: Seams
) -> None:
    events.start()
    events.close()
    attempts = seams.attempts
    fire(events, 0x8002)
    assert events.drain() == WindowChanges((), healthy=False)
    assert events.drain() == WindowChanges((), healthy=False)
    assert seams.attempts == attempts
    events.start()
    assert seams.attempts == attempts


def test_hook_layer_error_does_not_raise(events: ev.Win32WindowEvents, seams: Seams) -> None:
    seams.raise_in_hook = True
    events.start()
    settle(events)
    assert events.drain().healthy is False
    events.close()


def test_pump_error_does_not_raise(events: ev.Win32WindowEvents, seams: Seams) -> None:
    seams.raise_in_pump = True
    events.start()
    settle(events)
    assert events.drain().healthy is False
    events.close()


def test_thread_start_failure_does_not_raise(
    events: ev.Win32WindowEvents, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", boom)
    events.start()
    assert events.drain() == WindowChanges((), healthy=False)
    events.close()


def test_failed_install_warns_once_with_error_code(
    events: ev.Win32WindowEvents, seams: Seams, caplog: pytest.LogCaptureFixture, clock: list[float]
) -> None:
    seams.fail_at = 0
    seams.fail_error = 1407
    with caplog.at_level(logging.DEBUG):
        events.start()
        settle(events)
        for _ in range(3):
            clock[0] += 100
            events.drain()
            settle(events)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1407" in warnings[0].getMessage()
    assert [r for r in caplog.records if r.levelno == logging.DEBUG and "1407" in r.getMessage()]


def test_warns_again_on_change_from_working_to_failed(
    events: ev.Win32WindowEvents, seams: Seams, caplog: pytest.LogCaptureFixture, clock: list[float]
) -> None:
    with caplog.at_level(logging.DEBUG):
        events.start()
        seams.pump_stop.set()
        settle(events)
        seams.fail_at = 0
        events.drain()
        settle(events)
        clock[0] += 100
        events.drain()
        settle(events)
    assert [r.levelno for r in caplog.records].count(logging.WARNING) == 1


def test_logs_never_contain_hwnd_text(
    events: ev.Win32WindowEvents, seams: Seams, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        events.start()
        events.drain()
        for hwnd in range(HWND, HWND + MAX_EVENTS + 2):
            fire(events, 0x800C, hwnd)
        events.drain()
        seams.fail_at = 0
        seams.pump_stop.set()
        settle(events)
        events.drain()
        settle(events)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert str(HWND) not in text
    assert f"{HWND:x}" not in text.lower()
