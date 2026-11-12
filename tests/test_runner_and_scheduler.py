"""Scheduler verdicts, the job runner, backlog reporting and the worker loop."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

import pytest
from waits import wait_for

from akashic.child import ChildContext
from akashic.config import Config
from akashic.config import Scheduler as SchedulerSettings
from akashic.pipeline import main as worker
from akashic.pipeline.jobs import Job, JobQueue, NewJob, PermanentJobError
from akashic.pipeline.runner import BACKLOG_OFF, BACKLOG_ON, JobRunner
from akashic.pipeline.scheduler import Scheduler
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Paths

NOW = 1_000_000


class FakeLoad:
    def __init__(self, cpu: float = 5.0, gpu: float | None = 5.0) -> None:
        self.cpu, self.gpu = cpu, gpu

    def cpu_percent(self) -> float:
        return self.cpu

    def gpu_percent(self) -> float | None:
        return self.gpu


# --- scheduler -----------------------------------------------------------------------------


def test_heavy_jobs_are_allowed_when_idle(conn: Connection) -> None:
    verdict = Scheduler(conn, FakeLoad(), SchedulerSettings()).heavy_allowed(NOW)
    assert verdict.allowed
    assert verdict.reason == ""


def test_a_running_game_defers_heavy_jobs(conn: Connection) -> None:
    scheduler = Scheduler(conn, FakeLoad(), SchedulerSettings())
    game_id = GameLog(conn).open(process="game.exe", window_title="G", ts_ms=NOW - 1_000)
    verdict = scheduler.heavy_allowed(NOW)
    assert (verdict.allowed, verdict.reason) == (False, "game")
    GameLog(conn).close(game_id, NOW)
    assert scheduler.heavy_allowed(NOW).allowed


@pytest.mark.parametrize(
    ("cpu", "gpu", "reason"),
    [(10.0, 90.0, "gpu busy"), (95.0, 10.0, "cpu busy"), (10.0, 75.0, "gpu busy")],
)
def test_busy_hardware_defers_heavy_jobs(
    conn: Connection, cpu: float, gpu: float, reason: str
) -> None:
    verdict = Scheduler(conn, FakeLoad(cpu, gpu), SchedulerSettings()).heavy_allowed(NOW)
    assert (verdict.allowed, verdict.reason) == (False, reason)


def test_unmeasurable_gpu_is_not_a_reason_to_wait(conn: Connection) -> None:
    assert Scheduler(conn, FakeLoad(10.0, None), SchedulerSettings()).heavy_allowed(NOW).allowed


# --- runner --------------------------------------------------------------------------------


@dataclass
class Handler:
    kind: ClassVar[str] = "work"
    heavy: ClassVar[bool] = True
    block: str | None = None
    failures: list[Exception] = None  # type: ignore[assignment]
    ran: list[str] = None  # type: ignore[assignment]
    gave_up: list[str] = None  # type: ignore[assignment]
    idled: int = 0
    forced: int = 0

    def __post_init__(self) -> None:
        self.failures, self.ran, self.gave_up = [], [], []

    def blocked(self) -> str | None:
        return self.block

    def run(self, job: Job) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.ran.append(job.key)

    def give_up(self, job: Job) -> None:
        self.gave_up.append(job.key)

    def idle(self, *, force: bool = False) -> None:
        self.idled += 1
        self.forced += force


class LightHandler(Handler):
    kind: ClassVar[str] = "light"
    heavy: ClassVar[bool] = False


class AskHandler(LightHandler):
    kind: ClassVar[str] = "ask"


class QueryHandler(LightHandler):
    kind: ClassVar[str] = "embed_query"


class TranscriptionHandler(Handler):
    kind: ClassVar[str] = "transcribe"


class ComparisonHandler(Handler):
    kind: ClassVar[str] = "stt_compare"


def test_speech_and_llm_jobs_release_other_models(conn: Connection) -> None:
    speech, ask, query, comparison = (
        TranscriptionHandler(),
        AskHandler(),
        QueryHandler(),
        ComparisonHandler(),
    )
    runner, queue = make_runner(conn, FakeLoad(), speech, ask, query, comparison)
    for kind in ("transcribe", "ask", "embed_query", "stt_compare"):
        queue.enqueue(NewJob(kind, kind, NOW), NOW)
        assert runner.step() == "ran"
        if kind == "transcribe":
            assert (speech.forced, ask.forced, query.forced) == (0, 1, 1)
        elif kind == "ask":
            assert speech.forced == 1
        elif kind == "embed_query":
            assert speech.forced == 2
        else:
            assert (speech.forced, ask.forced, query.forced) == (3, 2, 2)
    assert comparison.ran == ["stt_compare"]


def make_runner(
    conn: Connection, load: FakeLoad, *handlers: Handler, clock: list[int] | None = None
) -> tuple[JobRunner, JobQueue]:
    queue = JobQueue(conn, max_attempts=2)
    now = clock if clock is not None else [NOW]
    runner = JobRunner(
        queue, Scheduler(conn, load, SchedulerSettings()), list(handlers), lambda: now[0]
    )
    return runner, queue


def test_runs_jobs_oldest_first_until_idle(conn: Connection) -> None:
    handler = Handler()
    runner, queue = make_runner(conn, FakeLoad(), handler)
    queue.enqueue(NewJob("work", "late", 20), NOW)
    queue.enqueue(NewJob("work", "early", 10), NOW)
    assert [runner.step(), runner.step(), runner.step()] == ["ran", "ran", "idle"]
    assert handler.ran == ["early", "late"]
    assert queue.pending_count() == 0


def test_heavy_jobs_wait_during_a_game_but_light_ones_run(conn: Connection) -> None:
    heavy, light = Handler(), LightHandler()
    runner, queue = make_runner(conn, FakeLoad(), heavy, light)
    queue.enqueue(NewJob("work", "h", 1), NOW)
    queue.enqueue(NewJob("light", "l", 2), NOW)
    game_id = GameLog(conn).open(process="g.exe", window_title=None, ts_ms=NOW)
    assert runner.step() == "ran"  # the light job, although it is newer
    assert runner.step() == "deferred"
    assert runner.deferred_reason == "game"
    assert (heavy.ran, light.ran) == ([], ["l"])
    GameLog(conn).close(game_id, NOW)
    assert runner.step() == "ran"
    assert heavy.ran == ["h"]
    assert runner.step() == "idle"
    assert runner.deferred_reason == ""


def test_a_blocked_handler_defers_its_jobs(conn: Connection) -> None:
    handler = Handler(block="model missing")
    runner, queue = make_runner(conn, FakeLoad(), handler)
    queue.enqueue(NewJob("work", "k", 1), NOW)
    assert runner.step() == "deferred"
    assert runner.deferred_reason == "model missing"
    handler.block = None
    assert runner.step() == "ran"


def test_busy_gpu_defers(conn: Connection) -> None:
    load = FakeLoad(gpu=99)
    runner, queue = make_runner(conn, load, Handler())
    queue.enqueue(NewJob("work", "k", 1), NOW)
    assert runner.step() == "deferred"
    load.gpu = 10
    assert runner.step() == "ran"


def test_failing_job_is_retried_then_given_up(conn: Connection) -> None:
    clock = [NOW]
    handler = Handler()
    handler.failures = [RuntimeError("boom"), RuntimeError("boom")]
    runner, queue = make_runner(conn, FakeLoad(), handler, clock=clock)
    queue.enqueue(NewJob("work", "k", 1), NOW)
    assert runner.step() == "ran"  # attempt 1 fails, retry scheduled
    assert handler.gave_up == []
    assert runner.step() == "idle"  # waiting out the backoff
    clock[0] += 10**9
    assert runner.step() == "ran"  # attempt 2 fails, out of attempts
    assert handler.gave_up == ["k"]
    assert conn.execute("SELECT state, error FROM jobs").fetchone() == ("failed", "RuntimeError")


def test_permanent_errors_give_up_immediately(conn: Connection) -> None:
    handler = Handler()
    handler.failures = [PermanentJobError("gone")]
    runner, queue = make_runner(conn, FakeLoad(), handler)
    queue.enqueue(NewJob("work", "k", 1), NOW)
    assert runner.step() == "ran"
    assert handler.gave_up == ["k"]
    assert conn.execute("SELECT state, attempts FROM jobs").fetchone() == ("failed", 1)


def test_backlog_has_hysteresis(conn: Connection) -> None:
    runner, queue = make_runner(conn, FakeLoad(), Handler(block="model missing"))
    assert not runner.backlog_active()
    for i in range(BACKLOG_ON):
        queue.enqueue(NewJob("work", f"k{i}", i), NOW)
    assert runner.backlog_active()
    conn.execute("DELETE FROM jobs WHERE id > ?", (BACKLOG_OFF + 3,))
    assert runner.backlog_active()  # between the two thresholds: unchanged
    conn.execute("DELETE FROM jobs WHERE id > ?", (BACKLOG_OFF,))
    assert not runner.backlog_active()


def test_idle_reaches_every_handler(conn: Connection) -> None:
    heavy, light = Handler(), LightHandler()
    runner, _ = make_runner(conn, FakeLoad(), heavy, light)
    runner.idle()
    assert (heavy.idled, light.idled) == (1, 1)
    runner.idle(force=True)
    assert (heavy.forced, light.forced) == (1, 1)


# --- the worker loop -----------------------------------------------------------------------


class RecordingContext(ChildContext):
    def __init__(self, paths: Paths, conn: Connection, key: DataKey) -> None:
        super().__init__("worker", paths, Config(), key, conn)
        self.states: list[str] = []

    def report(self, state: str) -> None:  # type: ignore[override]
        self.states.append(state)


def test_worker_loop_derives_sessions_runs_jobs_and_reports_backlog(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "IDLE_WAIT_S", 0.01)
    key = DataKey.generate()
    with closing(db.connect(paths.db_file, key.subkey("db"))) as conn:
        db.migrate(conn)
        FrameStore(conn, None).add(  # pyright: ignore[reportArgumentType] - no image is written
            NewFrame(ts_ms=1000, monitor=0, trigger="focus", process="a.exe", window_title="T")
        )
        handler = Handler()
        runner, queue = make_runner(conn, FakeLoad(), handler)
        for i in range(BACKLOG_ON):
            queue.enqueue(NewJob("work", f"k{i}", i), NOW)
        ctx = RecordingContext(paths, conn, key)
        thread = threading.Thread(target=worker.run_worker, args=(ctx, runner, queue, lambda: 2000))
        thread.start()
        try:
            # Drained, reported "ready" and back to idling: the loop has gone past the last job.
            assert wait_for(
                lambda: (
                    len(handler.ran) == BACKLOG_ON
                    and ctx.states[-1:] == ["ready"]
                    and handler.idled > 0
                ),
                15,
            )
            assert thread.is_alive()
        finally:
            ctx.stop.set()
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert handler.ran == [f"k{i}" for i in range(BACKLOG_ON)]
        assert conn.execute("SELECT process, frame_count FROM app_sessions").fetchall() == [
            ("a.exe", 1)
        ]
        assert ctx.states[0] == "backlog"  # ten jobs waiting when it started
        assert ctx.states[-1] == "ready"  # and drained
        assert handler.idled > 0


@pytest.mark.parametrize("gaming", [True, False])
def test_worker_loop_frees_models_while_a_game_runs(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, *, gaming: bool
) -> None:
    monkeypatch.setattr(worker, "IDLE_WAIT_S", 0.01)
    key = DataKey.generate()
    with closing(db.connect(paths.db_file, key.subkey("db"))) as conn:
        db.migrate(conn)
        if gaming:
            GameLog(conn).open(process="game.exe", window_title="Game", ts_ms=2000)
        handler = Handler()
        runner, queue = make_runner(conn, FakeLoad(), handler)
        ctx = RecordingContext(paths, conn, key)
        thread = threading.Thread(target=worker.run_worker, args=(ctx, runner, queue, lambda: 2000))
        thread.start()
        try:
            # Several idle passes: enough for a running game to have forced an unload on each.
            assert wait_for(lambda: handler.idled >= 3 and (handler.forced >= 3 or not gaming), 15)
            assert thread.is_alive()
        finally:
            ctx.stop.set()
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert (handler.forced > 0) is gaming


def test_worker_loop_waits_while_paused_and_survives_errors(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "IDLE_WAIT_S", 0.01)
    key = DataKey.generate()
    with closing(db.connect(paths.db_file, key.subkey("db"))) as conn:
        db.migrate(conn)
        failing = threading.Event()
        failing.set()
        housekeeping_calls: list[int] = []
        loop_errors: list[int] = []
        real_gaming_now = worker.gaming_now

        def broken_housekeeping(*_args: object) -> None:
            housekeeping_calls.append(1)
            if failing.is_set():
                raise sqlite3.OperationalError("database is locked")

        def flaky_gaming_now(c: Connection, now_ms: int) -> bool:
            if failing.is_set():
                loop_errors.append(1)
                raise sqlite3.OperationalError("database is locked")
            return real_gaming_now(c, now_ms)

        monkeypatch.setattr(worker, "housekeeping", broken_housekeeping)
        monkeypatch.setattr(worker, "gaming_now", flaky_gaming_now)
        handler = Handler()
        runner, queue = make_runner(conn, FakeLoad(), handler)
        queue.enqueue(NewJob("work", "after-resume", 1), NOW)
        ctx = RecordingContext(paths, conn, key)
        ctx.paused.set()
        thread = threading.Thread(target=worker.run_worker, args=(ctx, runner, queue))
        thread.start()
        try:
            assert wait_for(lambda: len(loop_errors) >= 3, 15)  # paused passes, each failing
            assert ctx.states == ["paused"]
            assert housekeeping_calls == []  # housekeeping waits for the resume
            assert handler.ran == []
            ctx.paused.clear()
            assert wait_for(
                lambda: housekeeping_calls != [], 15
            )  # resumed: housekeeping tried (and failed)
            errors = len(loop_errors)
            assert wait_for(
                lambda: len(loop_errors) >= errors + 3, 15
            )  # errors logged, the loop keeps going
            assert thread.is_alive()
            failing.clear()  # the database recovers
            assert wait_for(
                lambda: handler.ran == ["after-resume"] and ctx.states[-1:] == ["ready"], 15
            )
            assert thread.is_alive()
        finally:
            ctx.stop.set()
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert ctx.states == ["paused", "ready"]


def test_paused_worker_answers_questions_without_running_background_jobs(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "IDLE_WAIT_S", 0.01)
    key = DataKey.generate()
    with closing(db.connect(paths.db_file, key.subkey("db"))) as conn:
        db.migrate(conn)
        heavy, light, ask, query = Handler(), LightHandler(), AskHandler(), QueryHandler()
        runner, queue = make_runner(conn, FakeLoad(), heavy, light, ask, query)
        for kind in ("work", "light", "ask", "embed_query"):
            queue.enqueue(NewJob(kind, kind, 0), NOW)
        ctx = RecordingContext(paths, conn, key)
        ctx.paused.set()

        def no_housekeeping(*_args: object) -> None:
            pytest.fail("housekeeping must stay paused")

        monkeypatch.setattr(worker, "housekeeping", no_housekeeping)
        thread = threading.Thread(target=worker.run_worker, args=(ctx, runner, queue))
        thread.start()
        try:
            # Both questions answered, then several idle passes in which background jobs stay put.
            assert wait_for(lambda: query.ran != [] and ask.ran != [] and ask.idled >= 3, 15)
            assert ask.ran == ["ask"]
            assert query.ran == ["embed_query"]
            assert heavy.ran == light.ran == []
            assert queue.pending_count() == 2
            assert ctx.states == ["paused"]
            assert ask.idled > 0  # idle models can still be freed while paused
        finally:
            ctx.stop.set()
            thread.join(timeout=10)


def test_released_jobs_after_a_crash_run_again(conn: Connection) -> None:
    handler = Handler()
    runner, queue = make_runner(conn, FakeLoad(), handler)
    queue.enqueue(NewJob("work", "k", 1), NOW)
    assert queue.claim(["work"], NOW) is not None  # the previous worker died holding it
    assert runner.step() == "idle"
    queue.release_running()
    assert runner.step() == "ran"
    assert handler.ran == ["k"]
