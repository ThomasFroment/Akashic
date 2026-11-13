"""Shared startup for the capture and worker processes.

A child receives the data key over stdin, opens the database with it (which also proves the key
is right), reports ``ready``, then runs its body until told to stop or until the parent goes
away (stdin EOF).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Final, cast

from akashic import ipc
from akashic.config import Config, Paths, load_config
from akashic.log import enable_crash_log, setup_logging
from akashic.offline import apply_offline_env
from akashic.storage import db

if TYPE_CHECKING:
    import io
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.storage.crypto import DataKey

EXIT_OK: Final = 0
EXIT_BAD_KEY: Final = 3
EXIT_PROTOCOL: Final = 4


@dataclass
class ChildContext:
    name: str
    paths: Paths
    config: Config
    key: DataKey
    conn: Connection
    api_port: int | None = None  # where the app's local server listens (capture excludes it)
    stop: threading.Event = field(default_factory=threading.Event)
    paused: threading.Event = field(default_factory=threading.Event)
    pause_epoch: int = 0  # counts pause and resume flips; guards status computed before a flip
    resumed_at: float = field(default_factory=time.monotonic)  # last resume, else process start
    resume_state: ipc.State = "ready"  # what _active falls back to when a pause clears the problems
    _active: ipc.State = "ready"  # what to report while not paused
    _sent: ipc.State | None = None
    _status_out: IO[bytes] | None = None
    _status_lock: threading.Lock = field(default_factory=threading.Lock)

    def report(self, state: ipc.State) -> None:
        with self._status_lock:
            self._send(state)

    def _send(self, state: ipc.State) -> None:
        self._sent = state
        if self._status_out is None:
            return
        try:
            ipc.send_status(self._status_out, state)
        except OSError:
            self.stop.set()  # parent is gone

    def health_snapshot(self) -> tuple[int, bool, float]:
        """``(pause_epoch, paused, resumed_at)`` as one consistent reading."""
        with self._status_lock:
            return self.pause_epoch, self.paused.is_set(), self.resumed_at

    def set_active(self, state: ipc.State, *, epoch: int | None = None) -> bool:
        """Set the state to report while running; False when the change was discarded: computed
        under an older ``epoch`` than the current one, or the process is shutting down."""
        with self._status_lock:
            if self.stop.is_set() or (epoch is not None and epoch != self.pause_epoch):
                return False
            self._active = state
            if not self.paused.is_set() and state != self._sent:
                self._send(state)
            return True

    def set_resume_state(self, state: ipc.State) -> None:
        """Set what a pause falls back to. The state to report follows it too: sent now when
        running, kept for the resume when already paused (a ``--paused`` start)."""
        with self._status_lock:
            self.resume_state = state
            self._active = state
            if not self.paused.is_set() and state != self._sent:
                self._send(state)

    def set_paused(self, *, paused: bool) -> None:
        """Pause or resume and report it, as one step, so a late "ready" can't hide a pause."""
        with self._status_lock:
            if paused == self.paused.is_set():
                return
            self.pause_epoch += 1
            if paused:
                self.paused.set()
                self._active = self.resume_state
                self._send("paused")
            else:
                self.resumed_at = time.monotonic()
                self.paused.clear()
                self._send(self._active)

    def announce_ready(self) -> None:
        """The start-up "ready", unless a pause command already arrived (a restart while paused)."""
        with self._status_lock:
            self._send("paused" if self.paused.is_set() else self._active)


def _listen(ctx: ChildContext, stdin: ipc.LineReader, log: logging.Logger) -> None:
    try:
        for command in ipc.read_commands(stdin):
            log.info("command %s", command)
            if command == "stop":
                break
            ctx.set_paused(paused=command == "pause")
    except (ipc.ProtocolError, OSError, ValueError) as exc:
        log.error("command channel failed: %s", type(exc).__name__)  # noqa: TRY400 - no message: may quote data
    ctx.stop.set()


def run_child(
    name: str,
    argv: list[str] | None,
    body: Callable[[ChildContext], None],
    stdin: ipc.ParentStream | None = None,
    stdout: IO[bytes] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog=f"akashic-{name}")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--paused", action="store_true")
    parser.add_argument("--api-port", type=int, default=None)
    args = parser.parse_args(argv)
    paths = Paths(args.data_dir)
    config = load_config(paths)
    apply_offline_env(os.environ)
    setup_logging(paths.logs_dir, name, config.log_level)
    _crash_log = enable_crash_log(paths.logs_dir, name)  # stays open
    log = logging.getLogger(name)
    # sys.stdin.buffer is a BufferedReader at runtime; typeshed only says BinaryIO.
    stdin = stdin if stdin is not None else cast("io.BufferedReader", sys.stdin.buffer)
    stdout = stdout if stdout is not None else sys.stdout.buffer

    try:
        key = ipc.read_key(stdin)
    except ipc.ProtocolError:
        log.error("no data key received")  # noqa: TRY400 - nothing more useful to say
        return EXIT_PROTOCOL
    try:
        try:
            conn = db.connect(paths.db_file, key.subkey("db"))
        except db.DatabaseKeyError:
            log.error("data key does not open the database")  # noqa: TRY400
            return EXIT_BAD_KEY
        ctx = ChildContext(
            name, paths, config, key, conn, api_port=args.api_port, _status_out=stdout
        )
        if args.paused:
            ctx.paused.set()
        try:
            listener = threading.Thread(
                target=_listen, args=(ctx, stdin, log), name="ipc", daemon=True
            )
            listener.start()
            log.info("ready (schema v%d)", db.schema_version(conn))
            ctx.announce_ready()
            body(ctx)
            ctx.report("stopping")
        finally:
            conn.close()
    finally:
        key.wipe()
    log.info("stopped")
    return EXIT_OK
