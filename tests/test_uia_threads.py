"""ThreadedUia: one worker thread per process, a short wait, late results kept, fail closed."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import TYPE_CHECKING

import pytest
from capture_fakes import FakeUia, GatedUia, window

from akashic.capture import uia_threads
from akashic.capture.uia_threads import MAX_WORKERS, ThreadedUia, UiaRead
from akashic.log import FaultLog

if TYPE_CHECKING:
    from collections.abc import Callable

    from akashic.capture.ports import BrowserFamily, TextSource, WindowInfo

UNFINISHED: UiaRead[str] = UiaRead(None, None)


def on(pid: int, hwnd: int = 1) -> WindowInfo:
    return dataclasses.replace(window(hwnd=hwnd), pid=pid)


def make(new_uia: Callable[[], TextSource]) -> tuple[ThreadedUia, FaultLog]:
    faults = FaultLog(logging.getLogger(__name__))
    return ThreadedUia(new_uia, faults, lambda: 7.0), faults


@pytest.fixture
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """For the tests that need a read to miss the wait."""
    monkeypatch.setattr(uia_threads, "WAIT_S", 0.05)
    monkeypatch.setattr(uia_threads, "TEXT_WAIT_S", 0.05)


def test_a_read_runs_on_its_pids_own_thread_with_its_own_source() -> None:
    built: list[int] = []

    def new_uia() -> TextSource:
        built.append(threading.get_ident())
        return FakeUia(urls={1: "a.org"})

    threads, _ = make(new_uia)
    first = threads.address(on(10), "chromium")
    assert first.value == "a.org"
    assert first.started_at == 7.0
    threads.address(on(10), "chromium")
    threads.address(on(11), "chromium")
    assert len(built) == 2
    assert threading.get_ident() not in built
    threads.close()


@pytest.mark.usefixtures("short_wait")
def test_a_busy_worker_answers_unfinished_at_once_and_its_late_result_is_kept_once() -> None:
    gated = GatedUia(urls={1: "a.org"})
    threads, _ = make(lambda: gated)
    assert threads.address(on(10), "chromium") == UNFINISHED  # misses the wait, keeps running
    assert threads.caption(on(10, hwnd=2), "chromium") == UNFINISHED  # busy: nothing queued
    gated.release.set()
    while threads.caption(on(10, hwnd=3), "chromium").started_at is None:  # until it is idle
        time.sleep(0.01)
    assert threads.address(on(10), "chromium").value == "a.org"  # the late result, no new call
    assert len(gated.threads) == 1
    assert threads.address(on(10), "chromium").value == "a.org"  # a fresh call now
    assert len(gated.threads) == 2
    threads.close()


@pytest.mark.usefixtures("short_wait")
def test_a_stalled_pid_does_not_block_another() -> None:
    stalled = GatedUia(urls={1: "a.org"})
    healthy = FakeUia(urls={1: "b.org"})
    sources = iter([stalled, healthy])
    threads, _ = make(lambda: next(sources))
    assert threads.address(on(10), "chromium") == UNFINISHED
    assert threads.address(on(11), "chromium").value == "b.org"
    stalled.release.set()
    threads.close()


def test_a_raising_read_finishes_with_none_and_is_counted() -> None:
    class Raising(FakeUia):
        def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
            del window, family
            raise OSError

    threads, faults = make(Raising)
    read = threads.address(on(10), "chromium")
    assert read.value is None
    assert read.started_at == 7.0
    assert faults.counts() == {"uia worker: OSError": 1}
    threads.close()


def test_a_failing_new_uia_leaves_every_read_unfinished() -> None:
    def broken() -> TextSource:
        raise OSError

    threads, faults = make(broken)
    assert threads.address(on(10), "chromium") == UNFINISHED
    assert threads.address(on(10), "chromium") == UNFINISHED
    assert faults.counts() == {"uia worker: OSError": 1}
    threads.close()


@pytest.mark.usefixtures("short_wait")
def test_a_worker_whose_new_uia_raised_is_replaced_at_the_cap() -> None:
    built: list[int] = []

    def new_uia() -> TextSource:
        built.append(1)
        if len(built) == 1:
            raise OSError
        return FakeUia(urls={1: "a.org"})

    threads, faults = make(new_uia)
    assert threads.address(on(1), "chromium") == UNFINISHED  # pid 1 is the broken one
    for pid in range(2, MAX_WORKERS + 2):
        assert threads.address(on(pid), "chromium").value == "a.org"
    assert threads.address(on(1), "chromium").value == "a.org"  # a new worker: it was evicted
    assert faults.counts() == {"uia worker: OSError": 1}
    threads.close()


@pytest.mark.usefixtures("short_wait")
def test_at_the_cap_with_every_worker_busy_the_next_pid_is_unfinished() -> None:
    gated = GatedUia(urls={1: "a.org"})
    threads, faults = make(lambda: gated)
    for pid in range(1, MAX_WORKERS + 1):
        assert threads.address(on(pid), "chromium") == UNFINISHED  # each worker stays busy
    assert threads.address(on(MAX_WORKERS + 1), "chromium") == UNFINISHED
    assert faults.counts() == {"uia worker: cap": 1}
    gated.release.set()
    threads.close()


def test_at_the_cap_a_new_pid_replaces_the_idle_worker_read_least_recently() -> None:
    built: list[int] = []

    def new_uia() -> TextSource:
        built.append(1)
        return FakeUia(urls={1: "a.org"})

    threads, faults = make(new_uia)
    for pid in range(1, MAX_WORKERS + 1):
        assert threads.address(on(pid), "chromium").value == "a.org"
    threads.address(on(1), "chromium")  # pid 2 is now the least recently read
    assert threads.address(on(MAX_WORKERS + 1), "chromium").value == "a.org"
    assert len(built) == MAX_WORKERS + 1
    threads.address(on(1), "chromium")
    assert len(built) == MAX_WORKERS + 1  # pid 1 kept its worker
    threads.address(on(2), "chromium")
    assert len(built) == MAX_WORKERS + 2  # pid 2 lost it
    assert faults.counts() == {}
    threads.close()


def test_retire_ends_workers_of_gone_pids_but_not_the_shared_one() -> None:
    built: list[int] = []

    def new_uia() -> TextSource:
        built.append(1)
        return FakeUia(urls={1: "a.org"})

    threads, _ = make(new_uia)
    threads.address(on(0), "chromium")
    threads.address(on(10), "chromium")
    threads.retire({11})
    threads.address(on(0), "chromium")
    assert len(built) == 2
    threads.address(on(10), "chromium")
    assert len(built) == 3  # pid 10 got a new worker
    threads.close()


def test_the_pid_is_not_part_of_a_window_identity() -> None:
    assert on(10) == on(11)
    assert on(10).key == on(11).key
    assert window().pid == 0


@pytest.mark.usefixtures("short_wait")
def test_close_returns_while_a_worker_is_stuck_in_a_read() -> None:
    gated = GatedUia(urls={1: "a.org"})
    threads, _ = make(lambda: gated)
    assert threads.address(on(10), "chromium") == UNFINISHED
    started = time.monotonic()
    threads.close()
    assert time.monotonic() - started < 1.0  # the gate holds for up to 5 s
    gated.release.set()
