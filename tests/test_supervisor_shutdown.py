"""Supervisor.shutdown(): terminal stop, and the races with restart, while_stopped and poll."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest
from waits import wait_for

from akashic.storage.crypto import DataKey
from akashic.supervisor import ChildSpec, Supervisor

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

SPECS = (ChildSpec("capture", "x"), ChildSpec("worker", "y"))


@dataclass
class FakeChild:
    pid: int = 1
    alive: bool = True
    returncode: int | None = None
    gate: threading.Event | None = None  # when set, wait() blocks until it is released
    entered: threading.Event = field(default_factory=threading.Event)
    stdin: io.BytesIO = field(default_factory=io.BytesIO)
    stdout: io.BytesIO = field(default_factory=io.BytesIO)

    def poll(self) -> int | None:
        return None if self.alive else 1

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(10)
        self.alive = False
        return 0

    def kill(self) -> None:
        self.alive = False


class Fleet:
    def __init__(self) -> None:
        self.children: list[FakeChild] = []
        self.gate: threading.Event | None = None

    def spawn(self, spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del spec, data_dir
        self.children.append(FakeChild(gate=self.gate))
        return cast("subprocess.Popen[bytes]", self.children[-1])

    def alive(self) -> int:
        return sum(c.alive for c in self.children)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fleet() -> Fleet:
    return Fleet()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sup(fleet: Fleet, clock: Clock, tmp_path: Path) -> Supervisor:
    return Supervisor(tmp_path, DataKey.generate(), SPECS, spawn=fleet.spawn, clock=clock)


def all_stopped(sup: Supervisor) -> bool:
    return set(sup.states().values()) == {"stopped"}


def test_shutdown_stops_every_child_and_closes_pipes(sup: Supervisor, fleet: Fleet) -> None:
    sup.start()
    assert fleet.alive() == 2
    sup.shutdown()
    assert fleet.alive() == 0
    assert all_stopped(sup)
    assert all(c.stdin.closed and c.stdout.closed for c in fleet.children)


def test_shutdown_kills_stragglers(sup: Supervisor, fleet: Fleet) -> None:
    import subprocess as sp  # noqa: PLC0415

    sup.start()
    stubborn = fleet.children[0]
    killed: list[bool] = []

    def wait(timeout: float | None = None) -> int:
        if stubborn.alive and not killed:
            raise sp.TimeoutExpired("x", timeout or 0)
        stubborn.alive = False
        return 0

    def kill() -> None:
        killed.append(True)

    stubborn.wait = wait  # type: ignore[method-assign]
    stubborn.kill = kill  # type: ignore[method-assign]
    sup.shutdown(timeout=0.01)
    assert killed == [True]
    assert all_stopped(sup)


def test_shutdown_without_start_is_harmless_and_idempotent(sup: Supervisor, fleet: Fleet) -> None:
    sup.shutdown()
    sup.shutdown()
    assert fleet.children == []
    assert all_stopped(sup)


def test_start_restart_poll_never_spawn_after_shutdown(sup: Supervisor, fleet: Fleet) -> None:
    sup.start()
    sup.shutdown()
    count = len(fleet.children)
    sup.start()
    sup.start(paused=True)
    sup.restart()
    sup.poll()
    assert len(fleet.children) == count
    assert fleet.alive() == 0
    assert all_stopped(sup)


def test_start_on_a_fresh_instance_after_shutdown_of_another_works(
    sup: Supervisor, fleet: Fleet, clock: Clock, tmp_path: Path
) -> None:
    sup.start()
    sup.shutdown()
    again = Supervisor(tmp_path, DataKey.generate(), SPECS, spawn=fleet.spawn, clock=clock)
    again.start()
    assert fleet.alive() == 2
    again.shutdown()
    assert fleet.alive() == 0


def test_poll_after_shutdown_ignores_crashed_child_with_elapsed_backoff(
    sup: Supervisor, fleet: Fleet, clock: Clock
) -> None:
    sup.start()
    fleet.children[0].alive = False  # crashed
    sup.poll()  # schedules the restart
    clock.now += 1000
    sup.shutdown()
    count = len(fleet.children)
    clock.now += 1000
    sup.poll()
    assert len(fleet.children) == count
    assert fleet.alive() == 0
    assert all_stopped(sup)


def test_stop_alone_is_not_terminal(sup: Supervisor, fleet: Fleet) -> None:
    sup.start(paused=True)
    sup.stop()
    assert fleet.alive() == 0
    sup.start()
    assert fleet.alive() == 2
    sup.shutdown()


@pytest.mark.parametrize("paused", [False, True])
def test_restart_without_shutdown_restarts_with_the_same_paused_flag(
    sup: Supervisor, fleet: Fleet, *, paused: bool
) -> None:
    specs: list[bool] = []
    real = sup._spawn

    def spy(spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        specs.append(spec.start_paused)
        return real(spec, data_dir)

    sup._spawn = spy
    sup.start(paused=paused)
    sup.restart()
    assert specs == [paused] * 4
    assert fleet.alive() == 2
    sup.shutdown()


def test_while_stopped_without_shutdown_restores_state(sup: Supervisor, fleet: Fleet) -> None:
    sup.start()
    ran: list[int] = []
    sup.while_stopped(lambda: ran.append(fleet.alive()))
    assert ran == [0]
    assert fleet.alive() == 2
    sup.shutdown()


def test_while_stopped_after_shutdown_raises_oserror_without_running(
    sup: Supervisor, fleet: Fleet
) -> None:
    sup.start()
    sup.shutdown()
    ran: list[bool] = []
    with pytest.raises(OSError):  # noqa: PT011 - the type is the contract
        sup.while_stopped(lambda: ran.append(True))
    assert ran == []
    assert fleet.alive() == 0


def test_while_stopped_runs_action_then_does_not_relaunch_when_shutdown_arrives(
    sup: Supervisor, fleet: Fleet
) -> None:
    sup.start()
    in_action, release = threading.Event(), threading.Event()
    done: list[str] = []

    def action() -> None:
        in_action.set()
        release.wait(10)
        done.append("action")

    worker = threading.Thread(target=sup.while_stopped, args=(action,))
    worker.start()
    assert in_action.wait(10)
    count = len(fleet.children)
    closer = threading.Thread(target=sup.shutdown)
    closer.start()
    release.set()
    worker.join(10)
    closer.join(10)
    assert not worker.is_alive()
    assert not closer.is_alive()
    assert done == ["action"]
    assert len(fleet.children) == count
    assert fleet.alive() == 0
    assert all_stopped(sup)


def test_restart_waiting_on_the_lock_does_not_relaunch(sup: Supervisor, fleet: Fleet) -> None:
    sup.start()
    in_action, release = threading.Event(), threading.Event()

    def action() -> None:
        in_action.set()
        release.wait(10)

    holder = threading.Thread(target=sup.while_stopped, args=(action,))
    holder.start()
    assert in_action.wait(10)
    count = len(fleet.children)
    waiting = threading.Thread(target=sup.restart, name="restart")
    waiting.start()
    closer = threading.Thread(target=sup.shutdown)
    closer.start()
    assert wait_for(sup._shut_down.is_set)
    release.set()
    for t in (holder, waiting, closer):
        t.join(10)
        assert not t.is_alive()
    assert len(fleet.children) == count
    assert fleet.alive() == 0
    assert all_stopped(sup)


def test_restart_already_running_ends_with_zero_children(sup: Supervisor, fleet: Fleet) -> None:
    fleet.gate = threading.Event()
    sup.start()
    restarting = threading.Thread(target=sup.restart, name="restart")
    restarting.start()
    assert fleet.children[0].entered.wait(10)  # restart is inside its stop
    fleet.gate = None
    closer = threading.Thread(target=sup.shutdown)
    closer.start()
    assert wait_for(sup._shut_down.is_set)
    first = fleet.children[:2]
    for c in first:
        assert c.gate is not None
        c.gate.set()
    restarting.join(10)
    closer.join(10)
    assert not restarting.is_alive()
    assert not closer.is_alive()
    assert fleet.alive() == 0
    assert all_stopped(sup)


def test_shutdown_hammered_against_restarts_and_polls(sup: Supervisor, fleet: Fleet) -> None:
    sup.start()
    stop = threading.Event()

    def churn() -> None:
        while not stop.is_set():
            sup.restart()
            sup.poll()

    threads = [threading.Thread(target=churn, name="restart") for _ in range(4)]
    for t in threads:
        t.start()
    assert wait_for(lambda: len(fleet.children) > 8)
    sup.shutdown()
    stop.set()
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    assert fleet.alive() == 0
    assert all_stopped(sup)
