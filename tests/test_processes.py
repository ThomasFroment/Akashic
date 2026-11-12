"""Supervisor + child processes, end to end with real subprocesses, plus in-process child runs."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from waits import wait_for

from akashic import child, ipc
from akashic.capture import main as capture_main
from akashic.pipeline import main as worker_main
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.supervisor import CHILDREN, ChildSpec, Supervisor, spawn_module

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from akashic.config import Paths


@pytest.fixture
def key(paths: Paths) -> DataKey:
    k = DataKey.generate()
    conn = db.connect(paths.db_file, k.subkey("db"))
    db.migrate(conn)
    conn.close()
    return k


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_children_start_pause_stop(paths: Paths, key: DataKey) -> None:
    sup = Supervisor(paths.root, key)
    sup.start()
    try:
        assert wait_for(lambda: sup.states() == {"capture": "ready", "worker": "ready"}, 30)
        sup.send("pause")
        assert wait_for(lambda: set(sup.states().values()) == {"paused"}, 30)
        sup.send("resume")
        assert wait_for(lambda: set(sup.states().values()) == {"ready"}, 30)
    finally:
        sup.stop()
    assert sup.states() == {"capture": "stopped", "worker": "stopped"}
    for spec in CHILDREN:
        log = (paths.logs_dir / f"{spec.name}.log").read_text("utf-8")
        assert f"ready (schema v{len(db.load_migrations())})" in log
        assert "stopped" in log


def test_stopped_action_excludes_watchdog_and_restores_pause_after_failure(
    paths: Paths, key: DataKey
) -> None:
    sup = Supervisor(paths.root, key)
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    sup.start(paused=True)
    try:
        assert wait_for(lambda: set(sup.states().values()) == {"paused"}, 30)

        def action() -> None:
            assert set(sup.states().values()) == {"stopped"}
            entered.set()
            assert release.wait(10)
            assert all(c.proc is None for c in sup._children.values())

        def watchdog() -> None:
            attempted.set()
            sup.poll()

        with ThreadPoolExecutor(max_workers=2) as pool:
            deletion = pool.submit(sup.while_stopped, action)
            try:
                assert entered.wait(10)
                poll = pool.submit(watchdog)
                assert attempted.wait(10)
                assert not poll.done()
            finally:
                release.set()
            deletion.result(timeout=10)
            poll.result(timeout=10)
        assert wait_for(lambda: set(sup.states().values()) == {"paused"}, 30)

        def failed_action() -> None:
            assert all(c.proc is None for c in sup._children.values())
            raise RuntimeError("synthetic deletion failure")

        with pytest.raises(RuntimeError, match="synthetic deletion failure"):
            sup.while_stopped(failed_action)
        assert wait_for(lambda: set(sup.states().values()) == {"paused"}, 30)
    finally:
        release.set()
        sup.stop()


def test_initial_pause_applies_before_child_body(paths: Paths, key: DataKey) -> None:
    out = io.BytesIO()
    observed: list[bool] = []
    result = child.run_child(
        "paused-test",
        ["--data-dir", str(paths.root), "--paused"],
        lambda ctx: observed.append(ctx.paused.is_set()),
        stdin=io.BufferedReader(io.BytesIO(key.raw())),
        stdout=out,
    )
    assert result == 0
    assert observed == [True]
    assert _statuses(out)[0] == "paused"


def test_crashed_child_is_restarted_with_backoff(paths: Paths, key: DataKey) -> None:
    clock = FakeClock()
    spawns: list[str] = []

    def counting_spawn(spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        spawns.append(spec.name)
        return spawn_module(spec, data_dir)

    sup = Supervisor(paths.root, key, (CHILDREN[0],), clock=clock, spawn=counting_spawn)
    sup.start()
    try:
        assert wait_for(lambda: sup.states()["capture"] == "ready", 30)
        proc = sup._children["capture"].proc
        assert proc is not None
        proc.kill()
        proc.wait()

        sup.poll()  # notices the crash, schedules restart in 1 s
        assert sup.states()["capture"] == "crashed"
        clock.now += 0.5
        sup.poll()
        assert len(spawns) == 1
        clock.now += 0.6
        sup.poll()
        assert len(spawns) == 2
        assert wait_for(lambda: sup.states()["capture"] == "ready", 30)
        assert sup._children["capture"].backoff == 2.0

        # A child that stayed up long enough gets its backoff reset.
        clock.now += 1000
        new_proc = sup._children["capture"].proc
        assert new_proc is not None
        new_proc.kill()
        new_proc.wait()
        sup.poll()
        assert sup._children["capture"].restart_at == clock.now + 1.0
    finally:
        sup.stop()


def test_child_with_wrong_key_exits(paths: Paths, key: DataKey) -> None:
    del key
    sup = Supervisor(paths.root, DataKey.generate(), (CHILDREN[1],))
    sup.start()
    proc = sup._children["worker"].proc
    assert proc is not None
    assert proc.wait(timeout=30) == child.EXIT_BAD_KEY
    sup.stop()


def test_stop_kills_unresponsive_child(paths: Paths, key: DataKey) -> None:
    def spawn_sleeper(spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del spec, data_dir
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    sup = Supervisor(paths.root, key, (ChildSpec("sleepy", "x"),), spawn=spawn_sleeper)
    sup.start()
    start = time.monotonic()
    sup.stop(timeout=0.5)
    assert time.monotonic() - start < 10
    assert sup.states() == {"sleepy": "stopped"}
    sup.poll()  # no-op once stopping


def test_spawn_rejects_missing_pipes(paths: Paths, key: DataKey) -> None:
    def spawn_no_pipes(spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del spec, data_dir
        return subprocess.Popen([sys.executable, "-c", "pass"])

    sup = Supervisor(paths.root, key, (ChildSpec("x", "x"),), spawn=spawn_no_pipes)
    with pytest.raises(RuntimeError, match="pipes"):
        sup.start()
    sup.stop()


def test_key_not_passed_in_argv_or_env(paths: Paths, key: DataKey) -> None:
    proc = spawn_module(CHILDREN[0], paths.root)
    try:
        secrets_hex = {key.raw().hex(), key.subkey("db").hex()}
        args = " ".join(str(a) for a in proc.args)  # pyright: ignore[reportGeneralTypeIssues, reportUnknownVariableType, reportUnknownArgumentType] - Popen.args is loosely typed
        assert not any(s in args for s in secrets_hex)
        assert not any(s in v for v in os.environ.values() for s in secrets_hex)
    finally:
        proc.kill()
        proc.wait()
        Supervisor._close_pipes(proc)


def test_capture_never_imports_cuda_libraries() -> None:
    code = (
        "import sys, akashic.capture.main\n"
        "bad = {'onnxruntime', 'llama_cpp', 'ctranslate2', 'faster_whisper', 'torch'}\n"
        "print(sorted(bad & set(sys.modules)))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


# --- in-process child runs (cover child.py without subprocess indirection) ---


def _statuses(out: io.BytesIO) -> list[str]:
    return [json.loads(line)["state"] for line in out.getvalue().splitlines()]


def _commands(*names: ipc.Command) -> bytes:
    buf = io.BytesIO()
    for n in names:
        ipc.send_command(buf, n)
    return buf.getvalue()


@pytest.mark.parametrize("entry", [capture_main.main, worker_main.main])
def test_child_entry_points(
    entry: Callable[[list[str] | None], int],
    paths: Paths,
    key: DataKey,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = io.BytesIO(key.raw() + _commands("stop"))
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(stdin))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(stdout))
    assert entry(["--data-dir", str(paths.root)]) == child.EXIT_OK
    sys.stdout.flush()
    assert _statuses(stdout)[0] == "ready"
    assert _statuses(stdout)[-1] == "stopping"


def test_child_pause_resume_and_eof(paths: Paths, key: DataKey) -> None:
    stdin = io.BytesIO(key.raw() + _commands("pause", "resume"))  # then EOF
    stdout = io.BytesIO()
    seen: list[bool] = []

    def body(ctx: child.ChildContext) -> None:
        assert ctx.stop.wait(10)
        seen.append(ctx.paused.is_set())

    code = child.run_child("test", ["--data-dir", str(paths.root)], body, stdin, stdout)
    assert code == child.EXIT_OK
    assert seen == [False]
    assert sorted(_statuses(stdout)) == sorted(["ready", "paused", "ready", "stopping"])


def test_child_protocol_error_stops(paths: Paths, key: DataKey) -> None:
    stdin = io.BytesIO(key.raw() + b"garbage\n")

    def body(ctx: child.ChildContext) -> None:
        assert ctx.stop.wait(10)

    assert child.run_child("t", ["--data-dir", str(paths.root)], body, stdin, io.BytesIO()) == 0
    assert "ProtocolError" in (paths.logs_dir / "t.log").read_text("utf-8")


def test_child_without_key(paths: Paths) -> None:
    code = child.run_child("t", ["--data-dir", str(paths.root)], lambda _c: None, io.BytesIO())
    assert code == child.EXIT_PROTOCOL


def test_child_status_to_closed_parent(paths: Paths, key: DataKey) -> None:
    class Closed(io.BytesIO):
        def write(self, _b: object) -> int:  # type: ignore[override]  # simulate a broken pipe
            raise BrokenPipeError

    stdin = io.BytesIO(key.raw() + _commands("pause"))

    def body(ctx: child.ChildContext) -> None:
        assert ctx.stop.wait(10)

    assert child.run_child("t", ["--data-dir", str(paths.root)], body, stdin, Closed()) == 0
