"""Starts the capture and worker processes, hands them the data key, restarts them on crash.

Restarts back off exponentially (1 s, 2 s, 4 s ... up to a cap) and the backoff resets once a
child has stayed up for a while, so a crash loop never spins the CPU.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import IO, TYPE_CHECKING, Final

from akashic import ipc
from akashic.offline import apply_offline_env

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from akashic.storage.crypto import DataKey

log = logging.getLogger("supervisor")

STABLE_AFTER_S: Final = 300.0
MAX_BACKOFF_S: Final = 60.0
STOP_TIMEOUT_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class ChildSpec:
    name: str
    module: str
    start_paused: bool = False
    api_port: int | None = None  # the app's local server, so capture can exclude its pages


CHILDREN: Final = (
    ChildSpec("capture", "akashic.capture.main"),
    ChildSpec("worker", "akashic.pipeline.main"),
)

type Spawn = Callable[[ChildSpec, Path], subprocess.Popen[bytes]]


def spawn_module(spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
    """Run ``python -m <module> --data-dir <dir>`` with pipes for the key and messages."""
    env = dict(os.environ)
    apply_offline_env(env)
    return subprocess.Popen(  # noqa: S603 - fixed argv: our own interpreter and module
        [sys.executable, "-m", spec.module, "--data-dir", str(data_dir)]
        + (["--paused"] if spec.start_paused else [])
        + (["--api-port", str(spec.api_port)] if spec.api_port is not None else []),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class _Child:
    def __init__(self, spec: ChildSpec) -> None:
        self.spec = spec
        self.proc: subprocess.Popen[bytes] | None = None
        self.state: str = "stopped"
        self.started_at = 0.0
        self.backoff = 1.0
        self.restart_at: float | None = None


class Supervisor:
    def __init__(  # noqa: PLR0913 - injectable collaborators, all keyword-only after the specs
        self,
        data_dir: Path,
        key: DataKey,
        specs: tuple[ChildSpec, ...] = CHILDREN,
        *,
        spawn: Spawn = spawn_module,
        clock: Callable[[], float] = time.monotonic,
        remember_paused: Callable[[bool], None] = lambda _paused: None,
    ) -> None:
        self._data_dir = data_dir
        self._key = key
        self._spawn = spawn
        self._clock = clock
        self._remember_paused = remember_paused
        self._children = {s.name: _Child(s) for s in specs}
        self._lock = threading.Lock()
        self._restart_lock = threading.RLock()
        self._stopping = False
        self._shut_down = threading.Event()
        self._paused = False
        self._api_port: int | None = None

    def set_api_port(self, port: int) -> None:
        """Tell children where the local server listens (before ``start``; kept across restarts)."""
        self._api_port = port

    def start(self, *, paused: bool = False) -> None:
        with self._restart_lock:
            if self._shut_down.is_set():
                return
            self._stopping = False
            self._paused = paused
            for child in self._children.values():
                self._launch(child, paused=paused)

    def _launch(self, child: _Child, *, paused: bool = False) -> None:
        spec = replace(child.spec, start_paused=paused, api_port=self._api_port)
        proc = self._spawn(spec, self._data_dir)
        child.proc = proc
        child.state = "starting"
        child.started_at = self._clock()
        child.restart_at = None
        stdin, stdout = proc.stdin, proc.stdout
        if stdin is None or stdout is None:
            msg = "child must be spawned with stdin and stdout pipes"
            raise RuntimeError(msg)
        try:
            ipc.send_key(stdin, self._key)
        except OSError:
            log.warning("%s exited before receiving the key", child.spec.name)
        threading.Thread(
            target=self._read_status,
            args=(child, proc, stdout),
            name=f"status-{child.spec.name}",
            daemon=True,
        ).start()
        log.info("started %s (pid %d)", child.spec.name, proc.pid)

    def _read_status(self, child: _Child, proc: subprocess.Popen[bytes], stdout: IO[bytes]) -> None:
        try:
            for state in ipc.read_statuses(stdout):
                with self._lock:
                    if child.proc is proc:
                        child.state = state
        except (ipc.ProtocolError, OSError, ValueError) as exc:
            log.warning("%s status channel failed: %s", child.spec.name, type(exc).__name__)

    def poll(self) -> None:
        """Restart crashed children when their backoff has elapsed. Call about once a second.
        Does nothing after ``shutdown``."""
        with self._restart_lock:
            self._poll()

    def _poll(self) -> None:
        if self._stopping or self._shut_down.is_set():
            return
        now = self._clock()
        for child in self._children.values():
            proc = child.proc
            if proc is None or proc.poll() is None:
                continue
            if child.restart_at is None:
                if now - child.started_at >= STABLE_AFTER_S:
                    child.backoff = 1.0
                child.restart_at = now + child.backoff
                with self._lock:
                    child.state = "crashed"
                log.warning(
                    "%s exited with code %s; restarting in %.0f s",
                    child.spec.name,
                    proc.returncode,
                    child.backoff,
                )
                child.backoff = min(child.backoff * 2, MAX_BACKOFF_S)
            elif now >= child.restart_at:
                self._close_pipes(proc)
                self._launch(child, paused=self._paused)

    def restart(self) -> None:
        """Stop both children and start them again, so they re-read config.toml. Blocks for as
        long as the stop takes (audio in flight is written first): call it from a thread.
        After ``shutdown`` the children are stopped and not started again."""
        with self._restart_lock:
            was_paused = self._paused
            self.stop()
            self.start(paused=was_paused)

    def while_stopped(self, action: Callable[[], None]) -> None:
        """Run deletion with both writers gone; serialize against restarts and watchdog polling.

        Restore the previous running/paused state afterward, including on a failed deletion.
        Initial pause is a startup flag, so capture cannot run before a late pause command.
        The remembered pause choice is unaffected.
        Raises ``OSError`` without running the action after ``shutdown``.
        """
        with self._restart_lock:
            if self._shut_down.is_set():
                msg = "supervisor is shut down"
                raise OSError(msg)
            was_running = any(c.proc is not None for c in self._children.values())
            was_paused = self._paused
            self.stop()
            try:
                action()
            finally:
                if was_running:
                    self.start(paused=was_paused)

    def send(self, command: ipc.Command) -> None:
        with self._restart_lock:
            self._send(command)

    def _send(self, command: ipc.Command) -> None:
        if command in {"pause", "resume"}:
            self._paused = command == "pause"
        for child in self._children.values():
            proc = child.proc
            if proc is not None and proc.poll() is None and proc.stdin is not None:
                try:
                    ipc.send_command(proc.stdin, command)
                except OSError:
                    log.warning("could not send %s to %s", command, child.spec.name)
        if command in {"pause", "resume"}:
            self._remember_paused(self._paused)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Ask every child to stop, wait, and kill any that don't exit in time."""
        with self._restart_lock:
            self._stop(timeout)

    def shutdown(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Stop every child for good, on Lock and Quit. Terminal: later ``start``, ``restart``,
        ``poll`` and ``while_stopped`` calls never launch a child again."""
        self._shut_down.set()
        with self._restart_lock:
            self._stop(timeout)

    def _stop(self, timeout: float) -> None:
        self._stopping = True
        self.send("stop")
        for child in self._children.values():
            proc = child.proc
            if proc is None:
                continue
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                log.warning("%s did not stop in %.0f s; killing it", child.spec.name, timeout)
                proc.kill()
                proc.wait()
            self._close_pipes(proc)
            child.proc = None
            with self._lock:
                child.state = "stopped"

    @staticmethod
    def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()

    def states(self) -> dict[str, str]:
        with self._lock:
            return {name: c.state for name, c in self._children.items()}
