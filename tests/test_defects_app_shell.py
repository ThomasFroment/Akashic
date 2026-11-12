"""Known defects of the app shell: shutdown order, tray, crash visibility.

``app._run`` is driven end to end with fakes for the tray, the API server and the child
processes, so shutdown races can be reproduced deterministically: no window, browser, socket or
real child process is involved there.
"""

from __future__ import annotations

import io
import logging
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from defects import known_defect
from waits import wait_for

from akashic import app, child, supervisor
from akashic.api.models import Downloads
from akashic.config import ConfigError
from akashic.log import setup_logging
from akashic.pipeline import models
from akashic.pipeline.models import ModelFile, ModelSpec
from akashic.storage import db

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.api.server import Controls, Services
    from akashic.config import Paths
    from akashic.storage.crypto import DataKey

ROOT = Path(__file__).resolve().parents[1]


# --- a fake shell around app._run ------------------------------------------------------------


@dataclass
class FakeChild:
    """A child process that exits as soon as it is waited on (it obeys "stop")."""

    pid: int = 4242
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


class Shell:
    """Records what _run builds and lets a test act at the moments that matter."""

    def __init__(self) -> None:
        self.children: list[FakeChild] = []
        self.services: Services | None = None
        self.on_tray_run: Callable[[FakeTray], None] = lambda tray: tray.lock_app()
        self.on_api_stop: Callable[[], None] = lambda: None
        self.on_close: Callable[[], None] = lambda: None

    def spawn(self, spec: supervisor.ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del spec, data_dir
        self.children.append(FakeChild())
        return cast("subprocess.Popen[bytes]", self.children[-1])

    def running(self) -> int:
        return sum(c.alive for c in self.children)

    @property
    def controls(self) -> Controls:
        if self.services is None:
            msg = "the API server was never built"
            raise RuntimeError(msg)
        return self.services.controls


class FakeTray:
    def __init__(  # noqa: PLR0913 - the real Tray's keyword arguments
        self,
        shell: Shell,
        *,
        state: Callable[[], str],
        open_ui: Callable[[], None],
        toggle_pause: Callable[[], None],
        quit_app: Callable[[], None],
        lock_app: Callable[[], None],
    ) -> None:
        self.shell = shell
        self.state = state
        self.open_ui = open_ui
        self.toggle_pause = toggle_pause
        self.quit_app = quit_app
        self.lock_app = lock_app

    def run(self) -> None:
        self.shell.on_tray_run(self)

    def stop(self) -> None:
        pass

    def refresh(self) -> None:
        pass


class FakeApi:
    port = 1  # where the real server would listen; the app hands it to the children

    def __init__(self, shell: Shell, services: Services) -> None:
        self.shell = shell
        shell.services = services

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.shell.on_api_stop()

    def open_url(self) -> str:
        return "http://127.0.0.1:1/open?code=fake"


class ClosingConnection:
    """The real connection, with ``close`` observable."""

    def __init__(self, real: Connection, shell: Shell) -> None:
        self._real = real
        self._shell = shell

    def close(self) -> None:
        self._shell.on_close()
        self._real.close()

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - delegates to the sqlite connection
        return getattr(self._real, name)


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch) -> Shell:
    built = Shell()
    real_connect = db.connect

    def connect(path: Path, key: bytes) -> Any:  # noqa: ANN401 - a proxied Connection
        return ClosingConnection(real_connect(path, key), built)

    def make_supervisor(root: Path, key: DataKey) -> supervisor.Supervisor:
        return supervisor.Supervisor(root, key, spawn=built.spawn)

    def make_api(services: Services) -> FakeApi:
        return FakeApi(built, services)

    def make_tray(**callbacks: Callable[[], Any]) -> FakeTray:
        return FakeTray(built, **callbacks)

    monkeypatch.setattr(app, "Supervisor", make_supervisor)
    monkeypatch.setattr(app, "ApiServer", make_api)
    monkeypatch.setattr(app, "Tray", make_tray)
    monkeypatch.setattr(app.webbrowser, "open", bool)  # never open a real browser
    monkeypatch.setattr(app.db, "connect", connect)
    return built


def restart_threads_done() -> bool:
    return not any(t.name == "restart" and t.is_alive() for t in threading.enumerate())


def test_lock_stops_the_children(shell: Shell, paths: Paths, key: DataKey) -> None:
    assert app._run(paths, key) == "lock"
    assert len(shell.children) == 2
    assert shell.running() == 0


@known_defect(60, "a restart request that lands during Lock/Quit relaunches both children")
def test_a_restart_request_during_lock_never_leaves_children_recording(
    shell: Shell, paths: Paths, key: DataKey
) -> None:
    def restart_request() -> None:  # a POST /api/settings/restart served during shutdown
        shell.controls.restart()

    shell.on_api_stop = restart_request
    assert app._run(paths, key) == "lock"
    assert wait_for(restart_threads_done)
    assert shell.running() == 0


@known_defect(69, "tray Pause/Resume blocks on the restart lock, freezing the tray")
def test_tray_pause_returns_while_a_deletion_holds_the_children(
    shell: Shell, paths: Paths, key: DataKey
) -> None:
    returned: list[bool] = []

    def during_tray(tray: FakeTray) -> None:
        entered, release = threading.Event(), threading.Event()

        def deletion() -> None:
            entered.set()
            release.wait(30)

        stopped = shell.controls.while_stopped
        if stopped is None:
            msg = "no while_stopped control"
            raise RuntimeError(msg)
        holder = threading.Thread(target=stopped, args=(deletion,), daemon=True)
        holder.start()
        try:
            if not entered.wait(10):
                msg = "the deletion never started"
                raise RuntimeError(msg)
            clicker = threading.Thread(target=tray.toggle_pause, daemon=True)
            clicker.start()
            clicker.join(timeout=2)
            returned.append(not clicker.is_alive())
        finally:
            release.set()
            holder.join(timeout=30)
        tray.lock_app()

    shell.on_tray_run = during_tray
    app._run(paths, key)
    assert returned == [True]


@known_defect(72, "after the API join timeout the connection is closed without the Services lock")
def test_the_connection_is_not_closed_under_a_running_request(
    shell: Shell, paths: Paths, key: DataKey
) -> None:
    inside, release, finished = threading.Event(), threading.Event(), threading.Event()
    closed_while_busy: list[bool] = []

    def slow_request() -> None:
        services = shell.services
        if services is None:
            return
        with services.db():
            inside.set()
            release.wait(30)
            finished.set()

    def during_tray(tray: FakeTray) -> None:
        threading.Thread(target=slow_request, daemon=True).start()
        if not inside.wait(10):
            msg = "the slow request never started"
            raise RuntimeError(msg)
        tray.lock_app()

    def api_stop() -> None:  # the server gave up waiting; the request finishes a little later
        threading.Timer(0.5, release.set).start()

    shell.on_tray_run = during_tray
    shell.on_api_stop = api_stop
    shell.on_close = lambda: closed_while_busy.append(not finished.is_set())
    try:
        app._run(paths, key)
    finally:
        release.set()
    assert closed_while_busy == [False]


# --- #157: the tray never shows a stopped worker ---------------------------------------------


@dataclass
class States:
    values: dict[str, str]

    def states(self) -> dict[str, str]:
        return self.values


@known_defect(157, "the tray says 'recording' while nothing is being processed")
@pytest.mark.parametrize("worker", ["crashed", "stopped"])
def test_tray_does_not_say_recording_when_the_worker_is_down(worker: str) -> None:
    sup = cast("supervisor.Supervisor", States({"capture": "ready", "worker": worker}))
    assert app._overall_state(sup) != "recording"


# --- #74: interpreters must start isolated --------------------------------------------------


@known_defect(74, "child processes are spawned without -I")
def test_children_are_spawned_with_an_isolated_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def popen(args: list[str], **kwargs: object) -> object:
        del kwargs
        seen.append(args)
        return object()

    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    supervisor.spawn_module(supervisor.CHILDREN[0], tmp_path)
    assert seen
    assert seen[0][0] == sys.executable
    assert "-I" in seen[0][1 : seen[0].index("-m")]


@known_defect(74, "the autostart command runs python -m without -I")
def test_autostart_command_uses_an_isolated_interpreter() -> None:
    assert " -I -m akashic.app" in app.autostart_command()


@known_defect(74, "packaging/Akashic.cmd runs python -m without -I")
def test_the_packaged_launcher_uses_an_isolated_interpreter() -> None:
    assert " -I -m akashic.app" in (ROOT / "packaging" / "Akashic.cmd").read_text("utf-8")


# --- #67: thread crashes reach the log ------------------------------------------------------


@known_defect(67, "no threading.excepthook: a crashed thread logs nothing")
def test_an_uncaught_thread_exception_is_logged_by_type_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    before = threading.excepthook
    monkeypatch.setattr(threading, "excepthook", before)  # restored after the test
    setup_logging(tmp_path, "app", "INFO")
    hook = threading.excepthook
    assert hook is not before
    error = ValueError("secret window title")
    worker = threading.Thread(target=lambda: None, name="monitor")
    hook(threading.ExceptHookArgs((ValueError, error, error.__traceback__, worker)))
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / "app.log").read_text("utf-8")
    assert "ValueError" in text
    assert "secret window title" not in text


# --- #68: an invalid config.toml must not crash-loop the children silently --------------------


@known_defect(68, "an invalid config.toml makes a child exit before logging is set up")
def test_a_child_with_an_invalid_config_reports_it_and_exits_cleanly(
    paths: Paths, key: DataKey
) -> None:
    paths.config_file.write_text("[audio\nthis is not toml", encoding="utf-8")
    ran: list[bool] = []
    try:
        code: int | None = child.run_child(
            "worker",
            ["--data-dir", str(paths.root)],
            lambda ctx: ran.append(bool(ctx)),
            stdin=io.BufferedReader(io.BytesIO(key.raw())),
            stdout=io.BytesIO(),
        )
    except ConfigError:
        code = None
    assert ran == []
    assert code is not None
    assert code not in {child.EXIT_OK, child.EXIT_BAD_KEY, child.EXIT_PROTOCOL}
    log = paths.logs_dir / "worker.log"
    assert log.is_file()
    assert "config" in log.read_text("utf-8").casefold()


# --- #66: a download thread that dies leaves buttons stuck -----------------------------------


def _spec(key: str) -> ModelSpec:
    return ModelSpec(
        key=key,
        description=f"{key} test model",
        license="MIT",
        base_url=f"https://huggingface.co/someone/{key}/resolve/abc123",
        files=(ModelFile("model.bin", "0" * 64, 10),),
    )


@known_defect(66, "only ModelDownloadError is caught; an OSError leaves specs stuck")
def test_a_disk_error_during_download_marks_every_queued_model_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wanted = [models.Needed(_spec("first"), "Testing"), models.Needed(_spec("second"), "Testing")]

    def disk_full(*args: object, **kwargs: object) -> None:
        del args, kwargs
        msg = "There is not enough space on the disk"
        raise OSError(28, msg)

    def run_like_a_thread(work: Callable[[], None]) -> None:
        with suppress(Exception):  # a thread that dies takes its exception with it
            work()

    monkeypatch.setattr(models, "fetch", disk_full)
    downloads = Downloads(tmp_path / "m", tmp_path / "rt", lambda: wanted, None, run_like_a_thread)
    assert downloads.start(["first", "second"])
    assert [r["status"] for r in downloads.rows()] == ["failed", "failed"]
    assert downloads.start(["first"])  # the queue is usable again
