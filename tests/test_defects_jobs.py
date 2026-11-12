"""Known defects of phase 1 (processing liveness): jobs that stall, loop or vanish silently.

Each ``known_defect`` test asserts what the audit asks for and fails today. Fault injection is
done with fakes and monkeypatching only: no model, GPU or llama-server is involved. Preconditions
that are not the defect itself raise ``RuntimeError``, so a broken setup is reported as a failure
instead of being counted as the expected one.
"""

from __future__ import annotations

import http.client
import sqlite3
import sys
import types
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
import pytest
from ask_fakes import FakeChat, FakeEmbedder
from clock_fakes import ManualClock
from defects import known_defect
from memory_data import HOUR, MINUTE, NOW, add_episode, add_game, add_job, add_speaker
from memory_data import add_segment as add_segment_row
from whisper_fakes import stage_whisper_model

from akashic.api.debug import DebugSources
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.child import ChildContext
from akashic.config import Config
from akashic.config import Scheduler as SchedulerSettings
from akashic.pipeline import embed, llm, speech, whisper
from akashic.pipeline import main as worker
from akashic.pipeline.ask import AskHandler, ask_job
from akashic.pipeline.episodes import build_episodes
from akashic.pipeline.jobs import MAX_ATTEMPTS, Job, JobQueue, NewJob
from akashic.pipeline.llm import LlamaError, LlamaServer
from akashic.pipeline.runner import Handler, JobRunner
from akashic.pipeline.scheduler import Scheduler
from akashic.pipeline.speakers import SpeakersHandler
from akashic.pipeline.transcribe import TranscribeHandler, Transcript
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.frames import FrameStore
from akashic.storage.opus import encode_opus
from akashic.storage.retention import expire_raw_audio
from akashic.storage.speakers import SpeakerStore

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from numpy.typing import NDArray

    from akashic.config import Paths
    from akashic.pipeline.speakers import LocalTurn
    from akashic.storage.crypto import DataKey
    from akashic.storage.media import MediaStore

DAY = 24 * HOUR


@pytest.fixture
def audio(conn: sqlite3.Connection, media: MediaStore) -> AudioStore:
    return AudioStore(conn, media)


class FakeLoad:
    def cpu_percent(self) -> float:
        return 1.0

    def gpu_percent(self) -> float | None:
        return 1.0


def make_runner(
    conn: sqlite3.Connection, handlers: Sequence[Handler], clock: ManualClock
) -> tuple[JobRunner, JobQueue]:
    queue = JobQueue(conn)
    scheduler = Scheduler(conn, FakeLoad(), SchedulerSettings())
    return JobRunner(queue, scheduler, list(handlers), clock), queue


def job_state(conn: sqlite3.Connection, kind: str) -> list[tuple[str, int]]:
    return [
        (str(r[0]), int(r[1]))
        for r in conn.execute(
            "SELECT state, attempts FROM jobs WHERE kind = ? ORDER BY id", (kind,)
        )
    ]


def speech_opus(seconds: float = 1.0) -> bytes:
    t = np.arange(int(16_000 * seconds), dtype=np.float32) / 16_000
    return encode_opus((0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))


# --- #30: missing speaker models keep raw audio forever ---------------------------------------


class AbsentVoiceModels:
    def available(self) -> bool:
        return False

    def turns(self, samples: NDArray[np.float32]) -> list[LocalTurn]:
        del samples
        msg = "speaker models are not downloaded"
        raise RuntimeError(msg)

    def embed(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        del samples
        msg = "speaker models are not downloaded"
        raise RuntimeError(msg)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


@known_defect(30, "mic speakers jobs wait forever for absent models and block raw-audio retention")
def test_microphone_audio_expires_even_without_speaker_models(
    conn: sqlite3.Connection, audio: AudioStore, media: MediaStore
) -> None:
    start = NOW - 3 * DAY
    segment = audio.add(NewSegment("mic", start, start + 3_000, 2_500), speech_opus(), start)
    conn.execute("UPDATE jobs SET state = 'done' WHERE kind = 'transcribe'")
    audio.set_transcript(segment, "done", "fr", "on se retrouve demain")  # queues 'speakers'
    clock = ManualClock()
    runner, _ = make_runner(
        conn, [SpeakersHandler(audio, SpeakerStore(conn), AbsentVoiceModels())], clock
    )
    for _ in range(5):
        runner.step()
        clock.now += HOUR
    assert expire_raw_audio(conn, media, NOW - DAY) == 1


# --- #31: one blocked speech job stops every episode builder ---------------------------------


def test_games_become_episodes_when_no_speech_job_is_waiting(conn: sqlite3.Connection) -> None:
    add_game(conn, NOW - 3 * HOUR, NOW - 2 * HOUR)
    assert build_episodes(conn, NOW)["games"] == 1


@known_defect(31, "the transcript horizon also holds back games and app runs, which read no speech")
@pytest.mark.parametrize("kind", ["speakers", "transcribe"])
def test_games_become_episodes_while_an_older_speech_job_waits(
    conn: sqlite3.Connection, kind: str
) -> None:
    add_job(conn, kind, f"{kind}:1", NOW - 2 * DAY)  # e.g. blocked: models missing
    add_game(conn, NOW - 3 * HOUR, NOW - 2 * HOUR)
    assert build_episodes(conn, NOW)["games"] == 1


# --- #33: one always-failing embed batch blocks all embedding --------------------------------


class PoisonEmbedder:
    model = "emb-test"

    def available(self) -> bool:
        return True

    def embed_documents(self, texts: Sequence[str]) -> list[NDArray[np.float32]]:
        if any("poison" in t for t in texts):
            msg = "the server returned 500 for this input"
            raise LlamaError(msg)
        return [np.ones(4, dtype=np.float32) for _ in texts]

    def embed_query(self, text: str) -> NDArray[np.float32]:
        del text
        return np.ones(4, dtype=np.float32)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


@known_defect(33, "a batch that always fails is re-queued under a new key forever")
def test_one_failing_embed_batch_does_not_block_later_items(conn: sqlite3.Connection) -> None:
    add_episode(conn, "work", NOW - 3 * HOUR, NOW - 2 * HOUR, "Ancien", "poison digest text")
    later = add_episode(conn, "work", NOW - HOUR, NOW - 30 * MINUTE, "Récent", "a normal digest")
    embedder = PoisonEmbedder()
    clock = ManualClock()
    runner, queue = make_runner(conn, [embed.EmbedHandler(conn, embedder, lambda: True)], clock)
    for _ in range(40):  # housekeeping passes, each followed by a worker step
        embed.enqueue_missing(conn, queue, embedder.model, clock.now, batch=1)
        runner.step()
        clock.now += 2 * HOUR  # past every retry delay
    embedded = conn.execute(
        "SELECT count(*) FROM embeddings WHERE owner_kind = 'episode' AND owner_id = ?", (later,)
    ).fetchone()
    assert embedded == (1,)
    failed = conn.execute("SELECT count(*) FROM jobs WHERE kind = 'embed' AND state = 'failed'")
    assert failed.fetchone()[0] <= 1


# --- #35: idle unload must stop once the model is gone ---------------------------------------


class FakeCuda:
    def __init__(self) -> None:
        self.empties = 0

    def empty_cache(self) -> None:
        self.empties += 1


@known_defect(35, "unload_if_idle tests _torch, so it empties the CUDA cache on every idle loop")
def test_transformer_unload_runs_once_after_the_model_is_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transcriber = speech.TransformerTranscriber(tmp_path / speech.QWEN3_ASR.key, idle_s=60.0)
    cuda = FakeCuda()
    monkeypatch.setattr(transcriber, "_torch", types.SimpleNamespace(cuda=cuda))
    monkeypatch.setattr(transcriber, "_model", object())  # as if a model had been used
    collections: list[int] = []
    monkeypatch.setattr(speech.gc, "collect", lambda: collections.append(1) or 0)
    for _ in range(5):  # the idle loop runs every couple of seconds
        transcriber.unload_if_idle(force=True)
    assert cuda.empties == 1
    assert len(collections) == 1


# --- #39: http.client errors escape the llama-server fallbacks -------------------------------


def _raise_http_exception(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise http.client.BadStatusLine("garbled")


@known_defect(
    39, "http.client.HTTPException is not an OSError and escapes", raises=http.client.HTTPException
)
def test_http_protocol_errors_become_llama_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm.urllib.request, "urlopen", _raise_http_exception)
    with pytest.raises(LlamaError):
        llm.http_transport("GET", f"http://{llm.HOST}:1/health", "k", None)


@dataclass
class FakeProcess:
    terminated: bool = False
    killed: bool = False

    def poll(self) -> int | None:
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0


@known_defect(39, "an HTTPException inside _start leaves llama-server running, untracked")
def test_a_failed_start_never_leaves_the_server_process_running(tmp_path: Path) -> None:
    program = tmp_path / "llama-server.exe"
    program.write_bytes(b"fake")
    launched: list[FakeProcess] = []

    def launch(args: Sequence[str], env: Mapping[str, str]) -> Any:  # noqa: ANN401 - Running
        del args, env
        launched.append(FakeProcess())
        return launched[-1]

    def transport(method: str, url: str, key: str, body: dict[str, object] | None) -> object:
        del method, url, key, body
        raise http.client.BadStatusLine("garbled health answer")

    server = LlamaServer(
        program,
        tmp_path,
        launch,
        transport,
        port=lambda: 1,
        sleep=lambda _: None,
        presets={},
        preset_file=tmp_path / "models.ini",
    )
    with suppress(Exception):
        server.request("/v1/models")
    if not launched:
        msg = "llama-server was never launched"
        raise RuntimeError(msg)
    assert all(p.terminated or p.killed for p in launched)


# --- #40: a failed question shows "working" for about 42 minutes ----------------------------


def ask_question(conn: sqlite3.Connection, text: str, clock: ManualClock) -> int:
    with db.transaction(conn):
        cursor = conn.execute("INSERT INTO questions (asked_ms, text) VALUES (?, ?)", (NOW, text))
    if cursor.lastrowid is None:
        msg = "question row was not created"
        raise RuntimeError(msg)
    JobQueue(conn).enqueue(ask_job(cursor.lastrowid), clock.now)
    return cursor.lastrowid


def question_state(conn: sqlite3.Connection, question_id: int) -> str:
    return str(
        conn.execute("SELECT state FROM questions WHERE id = ?", (question_id,)).fetchone()[0]
    )


@known_defect(40, "ask jobs use 5 attempts and up to 32 min of backoff")
def test_a_failing_question_is_reported_failed_within_minutes(conn: sqlite3.Connection) -> None:
    minlo = add_speaker(conn, "Minlo")
    add_episode(
        conn, "conversation", NOW - 3 * HOUR, NOW - 2 * HOUR, "Discord call", "game talk",
        people=(minlo,),
    )  # fmt: skip
    add_segment_row(conn, NOW - 3 * HOUR + MINUTE, text="there is a new game", speaker=minlo)
    clock = ManualClock()
    chat = FakeChat(pieces=(), stream_error=True)
    handler = AskHandler(conn, chat, FakeEmbedder(), lambda: True, lambda: True, clock)
    runner, _ = make_runner(conn, [handler], clock)
    question = ask_question(conn, "what did Minlo say about a game?", clock)
    asked = clock.now
    for _ in range(MAX_ATTEMPTS + 2):
        runner.step()
        if question_state(conn, question) == "failed":
            break
        row = conn.execute("SELECT run_after_ms FROM jobs WHERE kind = 'ask' AND state = 'pending'")
        pending = row.fetchone()
        if pending is None:
            break
        clock.now = max(clock.now, int(pending[0]))  # the worker picks it up as soon as allowed
    if not chat.prompts:
        msg = "the chat model was never asked: the setup has no evidence"
        raise RuntimeError(msg)
    assert question_state(conn, question) == "failed"
    assert clock.now - asked <= 5 * MINUTE


# --- #41: environment failures fail segments for good ----------------------------------------


class CudaMissingModel:
    def __init__(self, path: str, **kwargs: object) -> None:
        del path, kwargs
        msg = "CUDA failed with error CUDA driver version is insufficient"
        raise RuntimeError(msg)


@known_defect(41, "a model load failure fails each job 5 times, then the segments for good")
def test_a_model_load_failure_keeps_segments_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, conn: sqlite3.Connection, audio: AudioStore
) -> None:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = CudaMissingModel  # type: ignore[attr-defined]  # fake module
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(whisper, "add_cuda_dll_directories", lambda: None)
    model_dir = stage_whisper_model(monkeypatch, tmp_path)
    transcriber = whisper.WhisperTranscriber(model_dir, 600.0)
    segment = audio.add(NewSegment("mic", NOW - HOUR, NOW - HOUR + 1_000, 900), speech_opus(), NOW)
    clock = ManualClock()
    handler = TranscribeHandler(audio, transcriber, (), "test-model")
    runner, _ = make_runner(conn, [handler], clock)
    for _ in range(3 * MAX_ATTEMPTS):
        runner.step()
        clock.now += 2 * HOUR
    stored = audio.get(segment)
    assert stored is not None
    assert stored.status == "pending"
    assert handler.blocked() is not None


# --- #154: a job that crashes the worker is retried first, forever ---------------------------


@known_defect(154, "attempts are counted but never checked when crashed jobs are released")
def test_a_job_that_keeps_crashing_the_worker_is_given_up(conn: sqlite3.Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("transcribe", "transcribe:1", NOW - DAY, {"segment_id": 1}), NOW)
    queue.enqueue(NewJob("transcribe", "transcribe:2", NOW, {"segment_id": 2}), NOW)
    claimed: list[str] = []
    for _ in range(MAX_ATTEMPTS + 2):  # each claim is followed by a worker crash and restart
        job = queue.claim(["transcribe"], NOW + DAY)
        if job is None:
            break
        claimed.append(job.key)
        if job.key == "transcribe:2":
            break
        queue.release_running()
    assert "transcribe:2" in claimed
    assert job_state(conn, "transcribe")[0][0] == "failed"


def test_released_jobs_keep_their_attempt_count(conn: sqlite3.Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("work", "work:1", NOW), NOW)
    for _ in range(3):
        assert queue.claim(["work"], NOW) is not None
        queue.release_running()
    assert job_state(conn, "work") == [("pending", 3)]


# --- #155: "Run waiting jobs now" does not run failed transcriptions --------------------------


@dataclass
class WorkingTranscriber:
    calls: int = 0

    def available(self) -> bool:
        return True

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript | None:
        del samples, prompt
        self.calls += 1
        return Transcript("bonjour tout le monde", "fr", 0.9)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


def post(server: ApiServer, token: str, path: str) -> int:
    request = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        request.request("POST", path, headers={"Cookie": f"akashic_session={token}"})
        response = request.getresponse()
        response.read()
        return response.status
    finally:
        request.close()


@known_defect(155, "retrying resets the job but not the failed segment, so nothing is transcribed")
def test_run_waiting_jobs_now_retranscribes_failed_segments(
    conn: sqlite3.Connection, audio: AudioStore, media: MediaStore, paths: Paths
) -> None:
    segment = audio.add(NewSegment("mic", NOW - HOUR, NOW - HOUR + 1_000, 900), speech_opus(), NOW)
    # The state a bad CUDA day leaves behind: five failed runs, then give_up().
    conn.execute("UPDATE jobs SET state = 'failed', attempts = 5, error = 'RuntimeError'")
    audio.set_transcript(segment, "failed", None, None)
    token = new_token()
    services = Services(
        conn,
        FrameStore(conn, media),
        token,
        Controls(dict, lambda: None, lambda: None),
        audio,
        DebugSources(paths.root, paths.logs_dir, dict),
    )
    server = ApiServer(services)
    server.start()
    try:
        status = post(server, token, "/api/debug/jobs/retry")
    finally:
        server.stop()
    if status != 200:
        msg = f"retry endpoint answered {status}"
        raise RuntimeError(msg)
    transcriber = WorkingTranscriber()
    runner, _ = make_runner(
        conn, [TranscribeHandler(audio, transcriber, (), "test-model")], ManualClock()
    )
    runner.step()
    stored = audio.get(segment)
    assert stored is not None
    assert (stored.status, stored.transcript) == ("done", "bonjour tout le monde")


# --- #156: a database error in complete()/fail() leaves the job "running" ---------------------


@dataclass
class QuickHandler:
    kind: ClassVar[str] = "work"
    heavy: ClassVar[bool] = False
    fail: bool = False

    def blocked(self) -> str | None:
        return None

    def run(self, job: Job) -> None:
        del job
        if self.fail:
            msg = "handler failed"
            raise RuntimeError(msg)

    def give_up(self, job: Job) -> None:
        del job

    def idle(self, *, force: bool = False) -> None:
        del force


@known_defect(156, "complete() and fail() are unguarded; 'database is locked' leaves it running")
@pytest.mark.parametrize("step", ["complete", "fail"])
def test_a_locked_database_never_leaves_a_job_running(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    clock = ManualClock()
    runner, queue = make_runner(conn, [QuickHandler(fail=step == "fail")], clock)
    queue.enqueue(NewJob("work", "work:1", NOW), NOW)
    original = getattr(queue, step)
    calls = [0]

    def locked_once(*args: object, **kwargs: object) -> object:
        calls[0] += 1
        if calls[0] == 1:
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)
        return original(*args, **kwargs)

    monkeypatch.setattr(queue, step, locked_once)
    with suppress(Exception):
        runner.step()
    assert calls[0] >= 1
    assert job_state(conn, "work")[0][0] != "running"


# --- #159: one failing housekeeping step stops the others ------------------------------------


@known_defect(
    159, "housekeeping is all-or-nothing; one failing builder stops embedding and retention"
)
def test_a_failing_episode_builder_does_not_stop_the_other_housekeeping_steps(
    conn: sqlite3.Connection, paths: Paths, key: DataKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_episode(conn, "work", NOW - 3 * HOUR, NOW - 2 * HOUR, "Travail", "un résumé assez long")
    config = replace(Config(), retention_days={**Config().retention_days, "screenshots": 30})
    ctx = ChildContext("worker", paths, config, key, conn)

    def broken(*args: object) -> dict[str, int]:
        del args
        msg = "episode builder bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(worker, "build_episodes", broken)
    with suppress(RuntimeError):
        worker.housekeeping(ctx, JobQueue(conn), NOW)
    kinds = {str(r[0]) for r in conn.execute("SELECT kind FROM jobs")}
    assert {"embed", "retention"} <= kinds


def test_housekeeping_queues_embedding_and_retention(
    conn: sqlite3.Connection, paths: Paths, key: DataKey
) -> None:
    add_episode(conn, "work", NOW - 3 * HOUR, NOW - 2 * HOUR, "Travail", "un résumé assez long")
    config = replace(Config(), retention_days={**Config().retention_days, "screenshots": 30})
    worker.housekeeping(ChildContext("worker", paths, config, key, conn), JobQueue(conn), NOW)
    kinds = {str(r[0]) for r in conn.execute("SELECT kind FROM jobs")}
    assert {"embed", "retention"} <= kinds


# --- queue bounds: many jobs, oldest data first -----------------------------------------------


def test_many_jobs_are_claimed_oldest_data_first(conn: sqlite3.Connection) -> None:
    queue = JobQueue(conn)
    rng = np.random.default_rng(7)
    times = [int(t) for t in rng.integers(0, 10**9, size=2_000)]
    for i, t in enumerate(times):
        queue.enqueue(NewJob("work", f"work:{i}", t), NOW)
    seen: list[int] = []
    while (job := queue.claim(["work"], NOW)) is not None:
        seen.append(job.data_ms)
        queue.complete(job.id, NOW)
    assert seen == sorted(times)
    assert queue.pending_count() == 0
