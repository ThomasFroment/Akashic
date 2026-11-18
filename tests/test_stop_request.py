"""The stop request time: recorded once, and the capture stop budget counts from it."""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest
from capture_fakes import FakeOcr
from test_capture_gaps import HeldStdin, make_platform
from test_capture_stop import gap_rows

from akashic import child, ipc
from akashic.capture import main as capture_main
from akashic.capture.screen import ScreenCapture
from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.storage.crypto import DataKey

BUDGET_S = 1.5
LOOP_DELAY_S = 1.0  # the screen loop returns this long after the request


def make_context(out: io.BytesIO | None = None) -> child.ChildContext:
    unused = cast("Any", None)
    return child.ChildContext("test", unused, unused, unused, unused, _status_out=out)


def commands(*names: ipc.Command) -> io.BytesIO:
    buf = io.BytesIO()
    for name in names:
        ipc.send_command(buf, name)
    return io.BytesIO(buf.getvalue())


def listened(ctx: child.ChildContext, stdin: Any) -> tuple[float, float]:  # noqa: ANN401
    """Run ``_listen`` and return (time before it, time after it)."""
    before = time.monotonic()
    child._listen(ctx, stdin, logging.getLogger("test"))
    return before, time.monotonic()


# --- the request ---------------------------------------------------------------------------------


def test_a_request_records_its_time_and_sets_stop() -> None:
    ctx = make_context()
    assert ctx.stop_requested_at is None
    before = time.monotonic()
    ctx.request_stop()
    assert ctx.stop.is_set()
    assert ctx.stop_requested_at is not None
    assert before <= ctx.stop_requested_at <= time.monotonic()


def test_a_second_request_keeps_the_first_time() -> None:
    ctx = make_context()
    ctx.request_stop()
    first = ctx.stop_requested_at
    time.sleep(0.02)
    ctx.request_stop()
    assert ctx.stop_requested_at == first


def test_the_stop_command_requests_the_stop() -> None:
    ctx = make_context()
    before, after = listened(ctx, commands("stop"))
    assert ctx.stop.is_set()
    assert ctx.stop_requested_at is not None
    assert before <= ctx.stop_requested_at <= after


def test_stdin_eof_requests_the_stop() -> None:
    ctx = make_context()
    listened(ctx, io.BytesIO())
    assert ctx.stop.is_set()
    assert ctx.stop_requested_at is not None


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_a_failed_command_channel_requests_the_stop(error: type[Exception]) -> None:
    class Broken:
        def readline(self, size: int = -1, /) -> bytes:
            del size
            raise error

    ctx = make_context()
    listened(ctx, Broken())
    assert ctx.stop.is_set()
    assert ctx.stop_requested_at is not None


def test_a_protocol_error_requests_the_stop() -> None:
    ctx = make_context()
    listened(ctx, io.BytesIO(b"garbage\n"))
    assert ctx.stop.is_set()
    assert ctx.stop_requested_at is not None


class ClosedPipe(io.BytesIO):
    def write(self, _b: object) -> int:  # type: ignore[override]
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


def test_a_status_to_a_gone_parent_requests_the_stop_without_deadlock() -> None:
    ctx = make_context(ClosedPipe())
    done = threading.Event()

    def send() -> None:
        ctx.report("ready")  # takes the status lock, then fails inside _send
        done.set()

    threading.Thread(target=send, daemon=True).start()
    assert done.wait(5)
    assert ctx.stop.is_set()
    assert ctx.stop_requested_at is not None


def test_a_failed_send_after_the_listener_stop_keeps_the_listener_time() -> None:
    ctx = make_context(ClosedPipe())
    listened(ctx, commands("stop"))
    first = ctx.stop_requested_at
    time.sleep(0.05)
    ctx.report("ready")
    assert ctx.stop_requested_at == first


# --- the budget counts from the request ---------------------------------------------------------


def run_requested(  # noqa: PLR0913 - a scenario's knobs
    paths: Paths,
    key: DataKey,
    monkeypatch: pytest.MonkeyPatch,
    *,
    budget: float,
    lock: bool,
    request: bool = True,
    loop_delay: float = LOOP_DELAY_S,
) -> float:
    """Run capture, stop it after 0.7 s and return the seconds from the stop to ``run_capture``'s
    return. The screen loop returns ``loop_delay`` after the stop. ``request`` uses
    ``request_stop``, else ``stop.set``."""
    monkeypatch.setattr(capture_main, "STOP_BUDGET_S", budget)
    original = ScreenCapture.run

    def slow(self: ScreenCapture, *args: Any, **kw: Any) -> None:  # noqa: ANN401
        original(self, *args, **kw)
        time.sleep(loop_delay)

    monkeypatch.setattr(ScreenCapture, "run", slow)
    platform = make_platform(ocr=FakeOcr())
    stdin = HeldStdin(key)
    started = [0.0]
    elapsed: list[float] = []
    holders: list[Connection] = []

    def stop_now(ctx: child.ChildContext) -> None:
        if lock:
            holder = db.connect(paths.db_file, key.subkey("db"))
            holder.execute("BEGIN IMMEDIATE")
            holders.append(holder)
        started[0] = time.monotonic()
        stop: Callable[[], None] = ctx.request_stop if request else ctx.stop.set
        stop()

    def body(ctx: child.ChildContext) -> None:
        threading.Timer(0.7, stop_now, args=(ctx,)).start()
        try:
            capture_main.run_capture(ctx, platform)
            elapsed.append(time.monotonic() - started[0])
        finally:
            while holders:
                holder = holders.pop()
                holder.execute("ROLLBACK")
                holder.close()
            stdin.release.set()

    argv = ["--data-dir", str(paths.root)]
    code = child.run_child("capture", argv, body, cast("Any", stdin), io.BytesIO())
    assert code == child.EXIT_OK
    return elapsed[0]


def test_a_slow_loop_and_a_held_lock_end_within_the_budget_of_the_request(
    paths: Paths, key: DataKey, conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = run_requested(paths, key, monkeypatch, budget=BUDGET_S, lock=True)
    assert BUDGET_S <= elapsed < BUDGET_S + 0.4  # a fresh budget would give 2.5 s
    assert [r for r in gap_rows(conn) if r[1] == "stopped"] == []


def test_a_stop_without_a_request_counts_the_budget_from_the_loop_return(
    paths: Paths, key: DataKey, conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = run_requested(paths, key, monkeypatch, budget=BUDGET_S, lock=True, request=False)
    assert elapsed >= LOOP_DELAY_S + BUDGET_S
    assert [r for r in gap_rows(conn) if r[1] == "stopped"] == []


def test_a_deadline_already_past_abandons_threads_and_drops_gaps_on_a_busy_database(
    paths: Paths, key: DataKey, conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    elapsed = run_requested(paths, key, monkeypatch, budget=0.3, lock=True)
    assert elapsed < LOOP_DELAY_S + 0.8
    assert [r for r in gap_rows(conn) if r[1] == "stopped"] == []
    assert "capture stop not written" in (paths.logs_dir / "capture.log").read_text("utf-8")


def test_a_deadline_already_past_still_writes_the_stop_on_a_free_database(
    paths: Paths, key: DataKey, conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_requested(paths, key, monkeypatch, budget=0.3, lock=False)
    stopped = [r for r in gap_rows(conn) if r[1] == "stopped"]
    assert [(r[0], r[3]) for r in stopped] == [("all", None)]
