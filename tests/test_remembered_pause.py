"""The user's pause survives Lock, Quit and Delete all (audit #163): supervisor, retention, app."""

from __future__ import annotations

import http.client
import io
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest
import sqlcipher3

from akashic import app
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.storage import state
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.retention import delete_all_recorded_data
from akashic.supervisor import ChildSpec, Supervisor

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Generator
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
        self.children: list[FakeChild] = []

    def spawn(self, spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del data_dir
        self.specs.append(spec)
        child = FakeChild()
        self.children.append(child)
        return cast("subprocess.Popen[bytes]", child)


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


def test_the_write_runs_outside_the_restart_lock_and_under_the_remember_lock(
    tmp_path: Path,
) -> None:
    seen: list[tuple[bool, bool]] = []
    holder: list[Supervisor] = []

    def free(lock: threading.Lock | threading.RLock) -> bool:
        got: list[bool] = []

        def probe() -> None:
            ok = lock.acquire(timeout=0.2)
            if ok:
                lock.release()
            got.append(ok)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return got[0]

    def remember(paused: bool) -> None:  # noqa: FBT001
        del paused
        seen.append((free(holder[0]._restart_lock), free(holder[0]._remember_lock)))

    sup = make(tmp_path, Fleet(), remember)
    holder.append(sup)
    sup.start()
    sup.send("pause")
    assert seen == [(True, False)]


class Blocked:
    """A remember callback that waits on an Event, standing in for a busy database."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[bool] = []

    def __call__(self, paused: bool) -> None:  # noqa: FBT001
        self.calls.append(paused)
        self.entered.set()
        assert self.release.wait(10)


def _returns(fn: Callable[[], object]) -> bool:
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(2)
    return not t.is_alive()


def test_a_blocked_write_never_delays_other_supervisor_calls(tmp_path: Path) -> None:
    blocked = Blocked()
    sup = make(tmp_path, Fleet(), blocked)
    sup.start()
    writer = threading.Thread(target=lambda: sup.send("pause"), daemon=True)
    writer.start()
    try:
        assert blocked.entered.wait(5)
        calls: dict[str, Callable[[], object]] = {
            "poll": sup.poll,
            "restart": sup.restart,
            "set_ui_open": lambda: sup.set_ui_open(True),
            "while_stopped": lambda: sup.while_stopped(lambda: None),
            "send stop": lambda: sup.send("stop"),
            "stop": sup.stop,
            "shutdown": sup.shutdown,
        }
        for name, fn in calls.items():
            assert _returns(fn), name
        assert blocked.calls == [True]
    finally:
        blocked.release.set()
        writer.join(5)


def test_a_second_command_reaches_children_at_once_and_only_its_write_waits(
    tmp_path: Path,
) -> None:
    blocked = Blocked()
    fleet = Fleet()
    sup = make(tmp_path, fleet, blocked)
    sup.start()
    first = threading.Thread(target=lambda: sup.send("pause"), daemon=True)
    first.start()
    second = threading.Thread(target=lambda: sup.send("resume"), daemon=True)
    try:
        assert blocked.entered.wait(5)
        second.start()
        deadline = time.monotonic() + 5
        while sup._paused and time.monotonic() < deadline:
            time.sleep(0.01)
        assert sup._paused is False
        for child in fleet.children:
            assert child.stdin.getvalue().endswith(b'"resume"}\n')
        assert second.is_alive()  # its write still waits
        assert blocked.calls == [True]
    finally:
        blocked.release.set()
        first.join(5)
        second.join(5)
    assert blocked.calls == [True, False]  # the second write stores the live value


def test_the_callback_gets_the_live_value_not_the_one_this_send_set(tmp_path: Path) -> None:
    blocked = Blocked()
    sup = make(tmp_path, Fleet(), blocked)
    sup.start()
    first = threading.Thread(target=lambda: sup.send("pause"), daemon=True)
    first.start()
    assert blocked.entered.wait(5)
    second = threading.Thread(target=lambda: sup.send("resume"), daemon=True)
    third = threading.Thread(target=lambda: sup.send("pause"), daemon=True)
    second.start()
    third.start()
    time.sleep(0.2)
    blocked.release.set()
    for t in (first, second, third):
        t.join(5)
    assert blocked.calls[0] is True
    assert blocked.calls[-1] is sup._paused


def test_stop_and_ui_commands_never_call_the_callback(tmp_path: Path) -> None:
    calls: list[bool] = []
    sup = make(tmp_path, Fleet(), calls.append)
    sup.start()
    sup.set_ui_open(True)
    sup.set_ui_open(False)
    sup.send("ui_open")
    sup.send("ui_closed")
    sup.stop()
    assert calls == []


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
    def db(self) -> Generator[object]:
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


# --- over loopback HTTP ----------------------------------------------------------------------


def test_http_pause_reaches_capture_and_is_stored(
    tmp_path: Path, conn: Connection, media: MediaStore
) -> None:
    fleet = Fleet()
    holder: list[Services] = []
    sup = make(tmp_path, fleet, lambda p: app._remember_paused(holder[0], p))
    controls = Controls(sup.states, lambda: sup.send("pause"), lambda: sup.send("resume"))
    token = new_token()
    services = Services(conn, FrameStore(conn, media), token, controls)
    holder.append(services)
    server = ApiServer(services)
    server.start()
    sup.start()
    capture = fleet.children[0]

    def post(paused: str) -> tuple[int, object]:
        http_conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        http_conn.request(
            "POST", f"/api/pause?paused={paused}", headers={"Cookie": f"akashic_session={token}"}
        )
        resp = http_conn.getresponse()
        body = json.loads(resp.read())
        http_conn.close()
        return resp.status, body

    try:
        assert post("true") == (200, {"paused": True})
        assert capture.stdin.getvalue().endswith(b'"pause"}\n')
        assert state.get_int(conn, state.CAPTURE_PAUSED, 0) == 1
        assert post("false") == (200, {"paused": False})
        assert capture.stdin.getvalue().endswith(b'"resume"}\n')
        assert state.get_int(conn, state.CAPTURE_PAUSED, 0) == 0
    finally:
        server.stop()


def test_remember_with_a_closed_connection_logs_and_raises_nothing(
    conn: Connection, media: MediaStore, caplog: pytest.LogCaptureFixture
) -> None:
    services = Services(
        conn, FrameStore(conn, media), "t", Controls(dict, lambda: None, lambda: None)
    )
    conn.close()
    with caplog.at_level(logging.WARNING):
        app._remember_paused(services, True)  # noqa: FBT003
    assert any(
        r.levelno == logging.WARNING
        and r.getMessage() == "could not remember pause: ProgrammingError"
        for r in caplog.records
    )
