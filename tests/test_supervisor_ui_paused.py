"""A child launched while a page is open reports ui_paused from launch, not starting."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from test_supervisor_shutdown import Clock, FakeChild

from akashic.storage.crypto import DataKey
from akashic.supervisor import ChildSpec, Supervisor

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

SPECS = (ChildSpec("capture", "x", pauses_for_ui=True), ChildSpec("worker", "y"))


class Launches:
    def __init__(self) -> None:
        self.children: list[FakeChild] = []
        self.specs: list[ChildSpec] = []

    def spawn(self, spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del data_dir
        self.specs.append(spec)
        self.children.append(FakeChild())
        return cast("subprocess.Popen[bytes]", self.children[-1])


@pytest.fixture
def launches() -> Launches:
    return Launches()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sup(launches: Launches, clock: Clock, tmp_path: Path) -> Supervisor:
    return Supervisor(tmp_path, DataKey.generate(), SPECS, spawn=launches.spawn, clock=clock)


def test_a_child_without_ui_open_is_starting(sup: Supervisor, launches: Launches) -> None:
    sup.start()
    assert sup.states() == {"capture": "starting", "worker": "starting"}
    assert [s.ui_open for s in launches.specs] == [False, False]
    sup.shutdown()


def test_start_with_the_ui_open_launches_capture_ui_paused(
    sup: Supervisor, launches: Launches
) -> None:
    sup.set_ui_open(True)
    sup.start()
    assert sup.states() == {"capture": "ui_paused", "worker": "starting"}
    assert {s.name: s.ui_open for s in launches.specs} == {"capture": True, "worker": False}
    sup.shutdown()


def test_restart_with_the_ui_open_launches_capture_ui_paused(
    sup: Supervisor, launches: Launches
) -> None:
    sup.start()
    sup.set_ui_open(True)
    sup.restart()
    assert sup.states() == {"capture": "ui_paused", "worker": "starting"}
    assert launches.specs[-2].ui_open is True
    sup.shutdown()


def test_while_stopped_relaunches_capture_ui_paused(sup: Supervisor, launches: Launches) -> None:
    sup.start()
    sup.set_ui_open(True)
    seen: list[dict[str, str]] = []
    sup.while_stopped(lambda: seen.append(sup.states()))
    assert seen == [{"capture": "stopped", "worker": "stopped"}]
    assert sup.states() == {"capture": "ui_paused", "worker": "starting"}
    assert launches.specs[-2].ui_open is True
    sup.shutdown()


def test_a_crash_relaunch_with_the_ui_open_is_ui_paused(
    sup: Supervisor, launches: Launches, clock: Clock
) -> None:
    sup.start()
    sup.set_ui_open(True)
    launches.children[0].alive = False
    sup.poll()
    assert sup.states()["capture"] == "crashed"
    clock.now += 1000
    sup.poll()
    assert sup.states()["capture"] == "ui_paused"
    assert launches.specs[-1].ui_open is True
    sup.shutdown()
