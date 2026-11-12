"""The user's pause survives Lock, Quit and Delete all (audit #163): supervisor, retention, app."""

from __future__ import annotations

import io
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest
import sqlcipher3

from akashic import app
from akashic.storage import state
from akashic.storage.crypto import DataKey
from akashic.storage.retention import delete_all_recorded_data
from akashic.supervisor import ChildSpec, Supervisor

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.storage.media import MediaStore

SPECS = (ChildSpec("capture", "x"), ChildSpec("worker", "y"))


@dataclass
class FakeChild:
    pid: int = 1
    alive: bool = True
    returncode: int | None = None
    stdin: io.BytesIO = field(default_factory=io.BytesIO)
    stdout: io.BytesIO = field(default_factory=io.BytesIO)

    def poll(self) -> int | None:
        return None if self.alive else 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.alive = False
        return 0

    def kill(self) -> None:
        self.alive = False


class Fleet:
    def __init__(self) -> None:
        self.specs: list[ChildSpec] = []

    def spawn(self, spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del data_dir
        self.specs.append(spec)
        return cast("subprocess.Popen[bytes]", FakeChild())


def make(tmp_path: Path, fleet: Fleet, remember: Callable[[bool], None]) -> Supervisor:
    return Supervisor(
        tmp_path, DataKey.generate(), SPECS, spawn=fleet.spawn, remember_paused=remember
    )


# --- supervisor ------------------------------------------------------------------------------


def test_pause_and_resume_are_remembered_in_order(tmp_path: Path) -> None:
    calls: list[bool] = []
    sup = make(tmp_path, Fleet(), calls.append)
    sup.start()
    sup.send("pause")
    sup.send("resume")
    sup.send("pause")
    assert calls == [True, False, True]


def test_other_commands_are_not_remembered(tmp_path: Path) -> None:
    calls: list[bool] = []
    sup = make(tmp_path, Fleet(), calls.append)
    sup.start(paused=True)
    sup.send("stop")
    assert calls == []


def test_the_write_happens_under_the_restart_lock(tmp_path: Path) -> None:
    seen: list[bool] = []
    holder: list[Supervisor] = []

    def remember(paused: bool) -> None:  # noqa: FBT001
        del paused
        got: list[bool] = []

        def probe() -> None:
            lock = holder[0]._restart_lock
            ok = lock.acquire(timeout=0.2)
            if ok:
                lock.release()
            got.append(ok)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        seen.append(got[0])

    sup = make(tmp_path, Fleet(), remember)
    holder.append(sup)
    sup.start()
    sup.send("pause")
    assert seen == [False]  # another thread could not take the lock meanwhile


def test_racing_pause_and_resume_leave_stored_equal_to_live(tmp_path: Path) -> None:
    for _ in range(30):
        stored: list[bool] = []
        sup = make(tmp_path, Fleet(), stored.append)
        sup.start()
        barrier = threading.Barrier(2)

        def go(cmd: str, sup: Supervisor = sup, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            for _ in range(20):
                sup.send(cmd)  # type: ignore[arg-type]

        threads = [threading.Thread(target=go, args=(c,)) for c in ("pause", "resume")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert stored[-1] is sup._paused
        sup.stop()


def test_while_stopped_and_restart_keep_pause_and_do_not_rewrite_it(tmp_path: Path) -> None:
    calls: list[bool] = []
    fleet = Fleet()
    sup = make(tmp_path, fleet, calls.append)
    sup.start(paused=True)
    sup.while_stopped(lambda: None)
    sup.restart()
    assert calls == []
    assert [s.start_paused for s in fleet.specs] == [True] * 6


def test_a_failed_while_stopped_action_still_restores_pause(tmp_path: Path) -> None:
    fleet = Fleet()
    sup = make(tmp_path, fleet, lambda _p: None)
    sup.start(paused=True)

    def boom() -> None:
        raise OSError

    with pytest.raises(OSError):  # noqa: PT011
        sup.while_stopped(boom)
    assert [s.start_paused for s in fleet.specs] == [True] * 4


def test_start_paused_reaches_both_children(tmp_path: Path) -> None:
    fleet = Fleet()
    make(tmp_path, fleet, lambda _p: None).start(paused=True)
    assert [(s.name, s.start_paused) for s in fleet.specs] == [("capture", True), ("worker", True)]


# --- delete all ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1])
def test_delete_all_keeps_the_paused_row_and_drops_other_state(
    conn: Connection, media: MediaStore, value: int
) -> None:
    state.set_int(conn, state.CAPTURE_PAUSED, value)
    state.set_int(conn, "conversations_since_ms", 5)
    state.set_int(conn, state.LEVELS_WANTED_UNTIL, 9)
    delete_all_recorded_data(conn, media)
    assert conn.execute("SELECT key, value FROM state").fetchall() == [
        (state.CAPTURE_PAUSED, value)
    ]


def test_delete_all_without_a_row_leaves_none(conn: Connection, media: MediaStore) -> None:
    state.set_int(conn, "conversations_since_ms", 5)
    delete_all_recorded_data(conn, media)
    assert conn.execute("SELECT count(*) FROM state").fetchone() == (0,)
    assert state.get_int(conn, state.CAPTURE_PAUSED, 0) == 0


def test_state_holds_only_the_integer(conn: Connection) -> None:
    state.set_int(conn, state.CAPTURE_PAUSED, int(True))
    state.set_int(conn, state.CAPTURE_PAUSED, int(False))
    rows = conn.execute(
        "SELECT value, typeof(value) FROM state WHERE key = ?", (state.CAPTURE_PAUSED,)
    ).fetchall()
    assert rows == [(0, "integer")]


# --- write failures --------------------------------------------------------------------------


class FakeServices:
    @contextmanager
    def db(self) -> Iterator[object]:
        yield object()


@pytest.mark.parametrize("error", [sqlcipher3.OperationalError, sqlcipher3.ProgrammingError])
def test_remember_failure_is_logged_by_type_and_never_raised(
    error: type[Exception], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(conn: object, key: str, value: int) -> None:
        del conn, key, value
        msg = "secret captured text"
        raise error(msg)

    monkeypatch.setattr(app.state, "set_int", fail)
    with caplog.at_level(logging.WARNING):
        app._remember_paused(FakeServices(), True)  # type: ignore[arg-type]  # noqa: FBT003
    assert any(error.__name__ in r.getMessage() for r in caplog.records)
    assert "secret captured text" not in caplog.text


def test_remember_before_services_exist_is_a_no_op() -> None:
    app._remember_paused(None, True)  # noqa: FBT003
