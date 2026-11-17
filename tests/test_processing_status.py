"""Processing status: queue health, the stall clock, the worker row, /api/status, the tray and the
sidebar line."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
import sqlcipher3
from api_client import Client

from akashic import app, tray
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import Scheduler as SchedulerSettings
from akashic.pipeline import main as worker
from akashic.pipeline.runner import JobRunner
from akashic.pipeline.scheduler import Scheduler
from akashic.storage.crypto import DataKey
from akashic.storage.debuginfo import (
    SIDEBAR_CAPTURE_RUNS,
    TRAY_CAPTURE_RUNS,
    ProcessInfo,
    Processing,
    processing_attention,
    processing_status,
)
from akashic.storage.frames import FrameStore
from akashic.storage.jobs import DeferJob, Job, JobQueue, NewJob
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.pipeline.runner import Handler

ROOT = Path(__file__).resolve().parent.parent
NOW = 10_000_000
HOUR = 3_600_000


# --- queue health ------------------------------------------------------------------------------


def enqueue(queue: JobQueue, kind: str, key: str, created: int) -> None:
    queue.enqueue(NewJob(kind, key, created), created)


def test_health_counts_failed_and_ages_pending_per_kind(conn: Connection) -> None:
    queue = JobQueue(conn, max_attempts=1)
    enqueue(queue, "transcribe", "a", 1_000)
    enqueue(queue, "transcribe", "b", 4_000)
    enqueue(queue, "embed", "c", 2_000)
    enqueue(queue, "speakers", "d", 500)
    enqueue(queue, "speakers", "e", 600)
    enqueue(queue, "ocr_window", "done", 100)
    job = queue.claim(["speakers"], 700)
    assert job is not None
    queue.fail(job, "Boom", 800)  # one attempt allowed: failed for good
    job = queue.claim(["ocr_window"], 900)
    assert job is not None
    queue.complete(job.id, 950)
    health = queue.health(10_000)
    assert health.failed == {"speakers": 1}
    assert health.oldest_pending_ms == {"transcribe": 9_000, "embed": 8_000, "speakers": 9_400}


def test_health_counts_pending_jobs_waiting_out_a_backoff_or_a_deferral(conn: Connection) -> None:
    queue = JobQueue(conn)
    enqueue(queue, "transcribe", "retry", 1_000)
    enqueue(queue, "embed", "later", 2_000)
    retried = queue.claim(["transcribe"], 3_000)
    deferred = queue.claim(["embed"], 3_000)
    assert retried is not None
    assert deferred is not None
    assert queue.fail(retried, "Boom", 3_000)
    queue.defer(deferred, 3_000, 60_000)
    health = queue.health(5_000)
    assert health.failed == {}
    assert health.oldest_pending_ms == {"transcribe": 4_000, "embed": 3_000}


def test_health_of_an_empty_queue_is_empty(conn: Connection) -> None:
    health = JobQueue(conn).health(NOW)
    assert (health.failed, health.oldest_pending_ms) == ({}, {})


def test_health_raises_when_the_database_is_busy() -> None:
    class Busy:
        def execute(self, *_args: object) -> None:
            raise sqlcipher3.OperationalError("database is locked")

    queue = JobQueue(cast("Any", Busy()))
    with pytest.raises(sqlcipher3.OperationalError):
        queue.health(NOW)


# --- the transcribe stall clock ----------------------------------------------------------------


@dataclass
class Stub:
    kind: ClassVar[str] = "transcribe"
    heavy: ClassVar[bool] = True
    block: str | None = None
    raises: Exception | None = None

    def blocked(self) -> str | None:
        return self.block

    def run(self, _job: Job) -> None:
        if self.raises is not None:
            raise self.raises

    def give_up(self, job: Job) -> None:
        pass

    def idle(self, *, force: bool = False) -> None:
        pass


class Light(Stub):
    kind: ClassVar[str] = "light"
    heavy: ClassVar[bool] = False


class IdleLoad:
    def cpu_percent(self) -> float:
        return 5.0

    def gpu_percent(self) -> float | None:
        return 5.0


class Rig:
    def __init__(self, conn: Connection, *handlers: Stub) -> None:
        self.conn = conn
        self.clock = [NOW]
        self.queue = JobQueue(conn)
        self.runner = JobRunner(
            self.queue,
            Scheduler(conn, IdleLoad(), SchedulerSettings()),
            cast("list[Handler]", list(handlers)),
            lambda: self.clock[0],
        )

    def advance(self, ms: int, **step: Any) -> None:  # noqa: ANN401 - forwarded to step
        self.clock[0] += ms
        self.runner.step(**step)

    @property
    def stalled(self) -> int:
        return cast("int", self.runner.status()["transcribe_stalled_ms"])

    def add(self, kind: str = "transcribe", key: str = "j") -> None:
        enqueue(self.queue, kind, key, NOW)


def test_the_stall_clock_is_zero_on_a_fresh_runner(conn: Connection) -> None:
    assert Rig(conn, Stub()).stalled == 0


def test_the_stall_grows_while_transcribe_could_run_but_makes_no_progress(
    conn: Connection,
) -> None:
    rig = Rig(conn, Stub(raises=ValueError()))
    rig.add()
    rig.advance(0)  # fails; retried only after the backoff
    rig.advance(1_000)
    rig.advance(2_000)
    assert rig.stalled == 3_000


def test_the_stall_grows_through_deferrals(conn: Connection) -> None:
    rig = Rig(conn, Stub(raises=DeferJob("later")))
    rig.add()
    rig.advance(0)
    rig.advance(1_000)
    rig.advance(1_000)
    assert rig.stalled == 2_000


def test_any_completion_resets_the_stall_and_restarts_the_count(conn: Connection) -> None:
    rig = Rig(conn, Stub(raises=ValueError()), Light())
    rig.add()
    rig.advance(0)
    rig.advance(5_000)
    assert rig.stalled == 5_000
    rig.add("light", "l")
    rig.advance(1_000)  # the light job completes in this step
    assert rig.stalled == 0
    rig.advance(700)
    assert rig.stalled == 700


def test_the_stall_does_not_grow_while_the_handler_is_blocked(conn: Connection) -> None:
    stub = Stub(block="model missing")
    rig = Rig(conn, stub)
    rig.add()
    rig.advance(0)
    rig.advance(10_000)
    assert rig.stalled == 0
    stub.block = None
    stub.raises = ValueError()
    rig.advance(1_000)  # only the time since the last step counts
    assert rig.stalled == 1_000


def test_the_stall_does_not_grow_during_a_game(conn: Connection) -> None:
    rig = Rig(conn, Stub(raises=ValueError()))
    rig.add()
    GameLog(conn).open(process="game.exe", window_title="G", ts_ms=NOW - 1)
    rig.advance(0)
    rig.advance(10_000)
    assert rig.stalled == 0


def test_the_stall_does_not_grow_during_pause(conn: Connection) -> None:
    rig = Rig(conn, Stub(raises=ValueError()), Light())
    rig.add()
    rig.advance(0, only=["light"])
    rig.advance(10_000, only=["light"])
    assert rig.stalled == 0


def test_the_stall_does_not_grow_without_a_pending_transcribe_job(conn: Connection) -> None:
    rig = Rig(conn, Stub(), Light(block="waiting"))
    rig.add("light", "l")  # another kind waits; transcribe has nothing
    rig.advance(0)
    rig.advance(10_000)
    assert rig.stalled == 0


# --- the worker row ----------------------------------------------------------------------------


def heartbeat(conn: Connection, runner: JobRunner, queue: JobQueue) -> Any:  # noqa: ANN401 - private class
    return worker._Heartbeat(cast("Any", SimpleNamespace(conn=conn)), runner, queue)  # pyright: ignore[reportPrivateUsage]


def test_the_worker_row_carries_the_processing_numbers_and_the_old_keys(conn: Connection) -> None:
    rig = Rig(conn, Stub(raises=ValueError()))
    rig.add()
    queue = JobQueue(conn, max_attempts=1)
    enqueue(queue, "speakers", "dead", 1)
    job = queue.claim(["speakers"], 2)
    assert job is not None
    queue.fail(job, "Boom", 3)
    rig.advance(0)
    rig.advance(5_000)
    heartbeat(conn, rig.runner, rig.queue).beat("ready", "idle", NOW + 5_000, 100.0)
    row = ProcessInfo(conn).read("worker")
    assert row is not None
    assert {"state", "last_outcome", "pending_jobs", "deferred_reason", "blocked", "scheduler"} <= (
        row.keys()
    )
    assert row["transcribe_stalled_ms"] == 5_000
    assert row["failed"] == {"speakers": 1}
    assert row["oldest_pending_ms"] == {"transcribe": 5_000}
    assert isinstance(row["horizon_ms"], int)


def test_a_failure_while_publishing_the_row_is_logged_by_type_name_only(
    conn: Connection, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(conn, Stub())

    def broken(_self: JobQueue, _now_ms: int) -> None:
        raise ZeroDivisionError("secret captured text")

    monkeypatch.setattr(JobQueue, "health", broken)
    with caplog.at_level("WARNING"):
        heartbeat(conn, rig.runner, rig.queue).beat("ready", "idle", NOW, 100.0)
    assert "publishing debug info failed: ZeroDivisionError" in caplog.text
    assert "secret" not in caplog.text
    assert ProcessInfo(conn).read("worker") is None


# --- /api/status processing --------------------------------------------------------------------


def good_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "state": "ready",
        "blocked": {"transcribe": "model missing", "embed": "model missing", "ask": "no model"},
        "failed": {"transcribe": 2},
        "oldest_pending_ms": {"transcribe": 90_000, "embed": 5},
        "horizon_ms": 123,
        "transcribe_stalled_ms": 4_000,
    }
    return {**row, **over}


MISTYPED: list[tuple[str, object]] = [
    ("blocked", []),
    ("failed", "x"),
    ("failed", {"transcribe": "2"}),
    ("oldest_pending_ms", None),
    ("oldest_pending_ms", {"embed": 1.5}),
    ("horizon_ms", "1"),
    ("transcribe_stalled_ms", None),
]


def publish_raw(conn: Connection, data: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO process_info (name, updated_ms, data) VALUES ('worker', 1, ?)",
        (data,),
    )
    conn.commit()


class StatusApi:
    def __init__(self, client: Client, states: dict[str, str]) -> None:
        self.client, self.states = client, states

    def processing(self) -> dict[str, Any] | None:
        code, body = self.client.call("/api/status")
        assert code == 200
        return cast("dict[str, Any] | None", body["processing"])


@pytest.fixture
def status_api(paths: Paths, conn: Connection) -> Iterator[StatusApi]:
    k = DataKey.generate()
    frames = FrameStore(conn, MediaStore(paths.media_dir, k.subkey("media")))
    states = {"capture": "ready", "worker": "ready"}
    token = new_token()
    controls = Controls(lambda: dict(states), lambda: None, lambda: None)
    server = ApiServer(Services(conn, frames, token, controls))
    server.start()
    yield StatusApi(Client(server, token, conn), states)
    server.stop()


def test_processing_is_null_without_a_worker_row(status_api: StatusApi) -> None:
    assert status_api.processing() is None


@pytest.mark.parametrize(
    "data",
    [
        "{not json",
        "[]",
        json.dumps({"state": "ready"}),
        *[json.dumps(good_row(**{key: bad})) for key, bad in MISTYPED],
    ],
)
def test_processing_is_null_for_an_unusable_row(
    status_api: StatusApi, conn: Connection, data: str
) -> None:
    publish_raw(conn, data)
    assert status_api.processing() is None


@pytest.mark.parametrize("missing", ["blocked", "failed", "oldest_pending_ms", "horizon_ms"])
def test_processing_is_null_when_a_needed_key_is_missing(
    status_api: StatusApi, conn: Connection, missing: str
) -> None:
    row = good_row()
    del row[missing]
    publish_raw(conn, json.dumps(row))
    assert status_api.processing() is None


def test_processing_lists_only_blocked_kinds_that_have_pending_jobs(
    status_api: StatusApi, conn: Connection
) -> None:
    publish_raw(conn, json.dumps(good_row()))
    assert status_api.processing() == {
        "state": "ready",
        "blocked": ["embed", "transcribe"],  # ask is blocked but has nothing waiting
        "failed": {"transcribe": 2},
        "oldest_pending_ms": {"transcribe": 90_000, "embed": 5},
        "horizon_ms": 123,
        "transcribe_stalled_ms": 4_000,
    }


def test_processing_state_is_the_supervisors_not_the_rows(
    status_api: StatusApi, conn: Connection
) -> None:
    publish_raw(conn, json.dumps(good_row(state="backlog")))
    status_api.states["worker"] = "crashed"
    got = status_api.processing()
    assert got is not None
    assert got["state"] == "crashed"
    del status_api.states["worker"]
    got = status_api.processing()
    assert got is not None
    assert got["state"] == "stopped"


def test_processing_reads_the_row_without_scanning_other_tables(conn: Connection) -> None:
    publish_raw(conn, json.dumps(good_row()))
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    assert processing_status(conn, "ready") is not None
    conn.set_trace_callback(None)
    assert seen
    assert not any(table in sql for sql in seen for table in ("jobs", "audio", "frames"))


# --- the tray word -----------------------------------------------------------------------------


class Sup:
    def __init__(self, **states: str) -> None:
        self._states = states

    def states(self) -> dict[str, str]:
        return dict(self._states)


def proc(**over: Any) -> Processing:  # noqa: ANN401 - field overrides
    fields: dict[str, Any] = {
        "state": "ready",
        "blocked": [],
        "failed": {},
        "oldest_pending_ms": {},
        "horizon_ms": 0,
        "transcribe_stalled_ms": 0,
    }
    return Processing(**{**fields, **over})


def word(processing: Processing | None, **states: str) -> str:
    return app._overall_state(cast("Any", Sup(**states)), processing)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("capture", ["ready", "gaming", "degraded"])
@pytest.mark.parametrize("worker", ["crashed", "stopped"])
@pytest.mark.parametrize("processing", [None, proc(blocked=["transcribe"])])
def test_a_down_worker_while_recording_is_processing_stopped(
    capture: str, worker: str, processing: Processing | None
) -> None:
    assert word(processing, capture=capture, worker=worker) == "processing_stopped"


@pytest.mark.parametrize("capture", ["ready", "gaming", "degraded"])
@pytest.mark.parametrize("worker", ["ready", "backlog"])
def test_blocked_kinds_or_an_hour_stall_need_attention_over_everything_else(
    capture: str, worker: str
) -> None:
    assert word(proc(blocked=["embed"]), capture=capture, worker=worker) == "needs_attention"
    stalled = proc(transcribe_stalled_ms=HOUR)
    assert word(stalled, capture=capture, worker=worker) == "needs_attention"


def test_a_stall_just_under_an_hour_is_not_a_problem() -> None:
    assert (
        word(proc(transcribe_stalled_ms=HOUR - 1), capture="ready", worker="ready") == "recording"
    )


def test_failed_transcriptions_alone_do_not_need_attention() -> None:
    assert word(proc(failed={"transcribe": 4}), capture="ready", worker="ready") == "recording"


def test_other_states_ignore_processing_problems() -> None:
    bad = proc(blocked=["transcribe"], transcribe_stalled_ms=HOUR)
    assert word(bad, capture="paused", worker="crashed") == "paused"
    assert word(bad, capture="ui_paused", worker="stopped") == "ui_open"
    assert word(bad, capture="starting", worker="stopped") == "starting"
    assert word(bad, capture="crashed", worker="crashed") == "restarting"
    assert word(bad, capture="stopped", worker="stopped") == "stopped"
    assert word(bad) == "stopped"


def test_without_processing_the_tray_follows_capture() -> None:
    assert word(None, capture="ready", worker="ready") == "recording"
    assert word(None, capture="ready", worker="backlog") == "backlog"
    assert word(None, capture="gaming", worker="ready") == "gaming"
    assert word(None, capture="degraded", worker="ready") == "degraded"


def test_backlog_shows_when_there_is_no_problem() -> None:
    assert word(proc(), capture="ready", worker="backlog") == "backlog"


def test_the_tray_detail_names_failed_transcriptions() -> None:
    detail = app._tray_detail  # pyright: ignore[reportPrivateUsage]
    assert detail(None) == ""
    assert detail(proc(failed={"embed": 3})) == ""
    assert detail(proc(failed={"transcribe": 1})) == "1 recording could not be transcribed"
    assert detail(proc(failed={"transcribe": 3})) == "3 recordings could not be transcribed"


def test_a_busy_database_gives_no_processing_and_does_not_raise() -> None:
    def busy(*_args: object) -> None:
        raise sqlcipher3.OperationalError("database is locked")

    conn = cast("Any", SimpleNamespace(execute=busy))
    assert app._read_processing(conn, "ready") is None  # pyright: ignore[reportPrivateUsage]


# --- the tray icon and tooltip -----------------------------------------------------------------


def test_the_tray_names_the_processing_states() -> None:
    assert tray.LABELS["processing_stopped"] == "Recording, but processing stopped"
    assert tray.LABELS["needs_attention"] == "Recording, but processing needs attention"


@pytest.mark.parametrize("state", ["processing_stopped", "needs_attention"])
def test_the_processing_icons_are_full_colour(state: str) -> None:
    def top(name: str) -> int:
        return cast("int", tray.make_icon(name).getchannel("A").getextrema()[1])

    assert top(state) == top("recording")
    assert top(state) > top("restarting")


def make_tray(state: list[str], detail: list[str]) -> tray.Tray:
    return tray.Tray(
        state=lambda: state[0],
        detail=lambda: detail[0],
        open_ui=lambda: None,
        toggle_pause=lambda: None,
        quit_app=lambda: None,
    )


def test_the_tooltip_adds_the_failed_count_whatever_the_state() -> None:
    t = make_tray(["needs_attention"], ["2 recordings could not be transcribed"])
    assert t._icon.title == (  # pyright: ignore[reportPrivateUsage]
        "Akashic: Recording, but processing needs attention · 2 recordings could not be transcribed"
    )
    t = make_tray(["paused"], ["1 recording could not be transcribed"])
    assert t._icon.title == "Akashic: Paused · 1 recording could not be transcribed"  # pyright: ignore[reportPrivateUsage]
    assert make_tray(["recording"], [""])._icon.title == "Akashic: Recording"  # pyright: ignore[reportPrivateUsage]


def test_the_tooltip_follows_a_change_of_state_or_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    state, detail = ["recording"], [""]
    t = make_tray(state, detail)
    icon = cast("Any", t._icon)  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(icon, "update_menu", lambda: None)
    detail[0] = "1 recording could not be transcribed"
    t.refresh()  # the count changed, the state did not
    assert icon.title == "Akashic: Recording · 1 recording could not be transcribed"
    state[0], detail[0] = "processing_stopped", ""
    t.refresh()
    assert icon.title == "Akashic: Recording, but processing stopped"


TRAY_RUNS = TRAY_CAPTURE_RUNS


@pytest.mark.parametrize(
    ("worker_state", "processing", "expected"),
    [
        ("crashed", None, "processing_stopped"),
        ("stopped", proc(blocked=["embed"]), "processing_stopped"),
        (None, None, "processing_stopped"),
        ("ready", proc(blocked=["embed"]), "needs_attention"),
        ("backlog", proc(transcribe_stalled_ms=HOUR), "needs_attention"),
        ("ready", proc(transcribe_stalled_ms=HOUR - 1), None),
        ("ready", proc(failed={"transcribe": 2}), None),
        ("ready", None, None),
    ],
)
def test_processing_attention_gives_the_one_verdict(
    worker_state: str | None, processing: Processing | None, expected: str | None
) -> None:
    for capture in TRAY_RUNS:
        states = {"capture": capture} | ({} if worker_state is None else {"worker": worker_state})
        assert processing_attention(states, processing, TRAY_RUNS) == expected


def test_processing_attention_is_none_unless_capture_is_in_the_given_runs() -> None:
    bad = proc(blocked=["embed"], transcribe_stalled_ms=HOUR)
    for capture in ("paused", "ui_paused", "starting", "crashed", "stopped"):
        states = {"capture": capture, "worker": "crashed"}
        assert processing_attention(states, bad, TRAY_RUNS) is None
    assert processing_attention({"worker": "crashed"}, bad, TRAY_RUNS) is None


def test_the_sidebar_set_also_covers_a_ui_paused_capture() -> None:
    assert TRAY_CAPTURE_RUNS | {"ui_paused"} == SIDEBAR_CAPTURE_RUNS
    states = {"capture": "ui_paused", "worker": "crashed"}
    assert processing_attention(states, None, SIDEBAR_CAPTURE_RUNS) == "processing_stopped"
    assert processing_attention(states, None, TRAY_CAPTURE_RUNS) is None


@pytest.mark.parametrize("capture", ["ready", "gaming", "degraded", "ui_paused", "paused"])
@pytest.mark.parametrize("worker_state", ["ready", "backlog", "crashed", "stopped"])
@pytest.mark.parametrize("problem", ["none", "blocked", "stalled"])
def test_status_attention_agrees_with_the_tray(
    status_api: StatusApi, conn: Connection, capture: str, worker_state: str, problem: str
) -> None:
    row = {
        "none": good_row(blocked={}),
        "blocked": good_row(),
        "stalled": good_row(blocked={}, transcribe_stalled_ms=HOUR),
    }[problem]
    publish_raw(conn, json.dumps(row))
    status_api.states.update(capture=capture, worker=worker_state)
    code, body = status_api.client.call("/api/status")
    assert code == 200
    processing = processing_status(conn, worker_state)
    tray_word = word(processing, capture=capture, worker=worker_state)
    tray_verdict = tray_word if tray_word in {"processing_stopped", "needs_attention"} else None
    if capture == "paused":
        assert body["attention"] is None
    elif capture == "ui_paused":  # the sidebar keeps its line while the tray shows the UI open
        assert tray_verdict is None
        assert body["attention"] == processing_attention(
            status_api.states, processing, SIDEBAR_CAPTURE_RUNS
        )
    else:
        assert body["attention"] == tray_verdict


# --- the sidebar line --------------------------------------------------------------------------


def test_app_js_has_no_stall_threshold_of_its_own() -> None:
    source = (ROOT / "src/akashic/ui/app.js").read_text("utf-8")
    assert "STALL_MS" not in source
    assert "PROCESSING_CAPTURE" not in source


def test_the_sidebar_has_a_separate_processing_line_with_a_warn_dot() -> None:
    html = (ROOT / "src/akashic/ui/index.html").read_text("utf-8")
    css = (ROOT / "src/akashic/ui/app.css").read_text("utf-8")
    assert 'id="status-text"' in html
    assert 'id="processing-line"' in html
    assert 'id="processing-text"' in html
    line = html[html.index('id="processing-line"') :].split("</div>", 1)[0]
    assert "hidden" in line.split(">", 1)[0]
    assert 'class="dot warn"' in line
    assert ".status-line .status-text" in css


def test_the_sidebar_processing_line_logic() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the local JavaScript check")
    result = subprocess.run(  # noqa: S603 - fixed local harness and source, no network
        [node, str(ROOT / "tests/processing_line_check.js"), str(ROOT / "src/akashic/ui/app.js")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "processing line verified" in result.stdout


# --- end to end: runner, worker row, API reader, tray word -------------------------------------


class Flow:
    """A worker with a transcribe job, publishing to a real database, read like the app does."""

    def __init__(self, conn: Connection, *handlers: Stub) -> None:
        self.rig = Rig(conn, *handlers)
        self.conn = conn
        self.beat = heartbeat(conn, self.rig.runner, self.rig.queue)
        self.published = 0.0

    def publish(self, **step: Any) -> str:  # noqa: ANN401 - forwarded to step
        self.rig.advance(0, **step)
        self.published += 10.0
        self.beat.beat("ready", "idle", self.rig.clock[0], self.published)
        processing = processing_status(self.conn, "ready")
        return word(processing, capture="ready", worker="ready")


def test_e2e_missing_models_with_pending_transcribe_jobs_need_attention(conn: Connection) -> None:
    flow = Flow(conn, Stub(block="Whisper model missing"))
    flow.rig.add()
    assert flow.publish() == "needs_attention"


def test_e2e_a_game_or_pause_never_needs_attention(conn: Connection) -> None:
    flow = Flow(conn, Stub(raises=ValueError()), Light())
    flow.rig.add()
    assert flow.publish(only=[]) == "recording"  # paused
    flow.rig.clock[0] += 2 * HOUR
    assert flow.publish(only=[]) == "recording"
    games = GameLog(conn)
    game = games.open(process="game.exe", window_title="G", ts_ms=flow.rig.clock[0])
    for _ in range(2 * HOUR // 20_000):  # the heartbeat keeps the game fresh
        flow.rig.clock[0] += 20_000
        games.heartbeat(game, flow.rig.clock[0])
        flow.rig.runner.step()
    assert flow.publish() == "recording"


def test_e2e_an_hour_without_a_completion_needs_attention_until_a_job_completes(
    conn: Connection,
) -> None:
    flow = Flow(conn, Stub(raises=ValueError()), Light())
    flow.rig.add()
    assert flow.publish() == "recording"
    flow.rig.clock[0] += HOUR - 1_000
    assert flow.publish() == "recording"
    flow.rig.clock[0] += 1_000
    assert flow.publish() == "needs_attention"
    flow.rig.add("light", "l")
    assert flow.publish() == "recording"
