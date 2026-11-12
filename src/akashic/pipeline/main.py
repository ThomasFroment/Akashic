"""Worker process entry point: job queue and all GPU work. Restartable without losing state."""

from __future__ import annotations

import logging
import sys
import time
from typing import TYPE_CHECKING, Final

from akashic.child import ChildContext, run_child
from akashic.pipeline.ask import ASK, AskHandler
from akashic.pipeline.embed import EMBED_QUERY, EmbedHandler, QueryEmbedHandler, enqueue_missing
from akashic.pipeline.episodes import build_episodes
from akashic.pipeline.jobs import JobQueue
from akashic.pipeline.llm import (
    CHAT_FLAGS,
    EMBEDDING_FLAGS,
    LlamaChat,
    LlamaEmbedder,
    LlamaServer,
    Running,
)
from akashic.pipeline.models import LLAMA_RUNTIME, runtime_program
from akashic.pipeline.ocr_frames import OcrFrameHandler
from akashic.pipeline.retention import RetentionHandler, retention_job
from akashic.pipeline.runner import Handler, JobRunner
from akashic.pipeline.scheduler import LoadSource, Scheduler
from akashic.pipeline.sessions import derive_app_sessions
from akashic.pipeline.speakers import OnnxVoiceModels, SpeakersHandler, model_paths
from akashic.pipeline.speech import make_transcriber
from akashic.pipeline.stt_compare import STT_COMPARE, STTCompareHandler
from akashic.pipeline.transcribe import TranscribeHandler
from akashic.storage import embeddings
from akashic.storage.audio import AudioStore
from akashic.storage.debuginfo import ProcessInfo
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.retention import DAY_MS
from akashic.storage.sessions import gaming_now
from akashic.storage.speakers import SpeakerStore

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from akashic.capture.ports import OcrEngine

log = logging.getLogger("worker")

HOUSEKEEPING_INTERVAL_S: Final = 30.0
IDLE_WAIT_S: Final = 2.0
PUBLISH_INTERVAL_S: Final = 5.0
KEEP_DONE_JOBS_MS: Final = 24 * 3_600_000  # finished jobs are only kept for debugging


class _NoLoad:
    """Used when load can't be measured: the scheduler then only watches for games."""

    def cpu_percent(self) -> float:
        return 0.0

    def gpu_percent(self) -> float | None:
        return None


def windows_ocr(language: str) -> Callable[[], OcrEngine]:
    """Builds the Windows OCR engine on first use, inside the job thread that will use it."""

    def make() -> OcrEngine:
        from akashic.platform.win.ocr import WindowsOcr  # noqa: PLC0415 - Windows only

        return WindowsOcr(language)

    return make


def windows_launcher(args: Sequence[str], env: Mapping[str, str]) -> Running:
    """Starts llama-server in a job object (Windows only), on first use."""
    from akashic.platform.win.process import launch  # noqa: PLC0415 - Windows only

    return launch(args, env)


def model_folder_has_gguf(folder: Path) -> bool:
    return folder.is_dir() and any(folder.glob("*.gguf"))


def housekeeping(ctx: ChildContext, queue: JobQueue, now_ms: int) -> None:
    """Cheap periodic derivations and cleanup."""
    derive_app_sessions(ctx.conn, now_ms)
    built = build_episodes(ctx.conn, now_ms)
    if any(built.values()):
        log.info("episodes built: %s", ", ".join(f"{k}={n}" for k, n in built.items()))
    days = ctx.config.retention_days.get("embeddings")
    enqueue_missing(
        ctx.conn,
        queue,
        ctx.config.search.embedding_model,
        now_ms,
        since_ms=0 if days is None else now_ms - days * DAY_MS,
    )
    embeddings.delete_old_queries(ctx.conn, now_ms)
    queue.purge_done(now_ms - KEEP_DONE_JOBS_MS)
    job = retention_job(ctx.config.retention_days, now_ms)
    if job is not None:
        queue.enqueue(job, now_ms)  # no-op once today's pass for these limits exists


class _Heartbeat:
    """Tells the log (once per change) and the Debug page (every few seconds) why jobs wait."""

    def __init__(self, ctx: ChildContext, runner: JobRunner, queue: JobQueue) -> None:
        self._info = ProcessInfo(ctx.conn)
        self._runner = runner
        self._queue = queue
        self._last_published = float("-inf")
        self._last_deferred = ""

    def beat(self, state: str, outcome: str, now_ms: int, clock_s: float) -> None:
        reason = self._runner.deferred_reason
        if reason != self._last_deferred:
            self._last_deferred = reason
            if reason:
                log.info("jobs are waiting: %s", reason)
            else:
                log.info("jobs run again")
        if clock_s - self._last_published < PUBLISH_INTERVAL_S:
            return
        self._last_published = clock_s
        try:
            self._info.publish(
                "worker",
                {
                    "state": state,
                    "last_outcome": outcome,
                    "pending_jobs": self._queue.pending_count(),
                    **self._runner.status(),
                },
                now_ms,
            )
        except Exception as exc:  # noqa: BLE001 - a debugging aid must never stop the worker
            log.warning("publishing debug info failed: %s", type(exc).__name__)


def run_worker(
    ctx: ChildContext,
    runner: JobRunner,
    queue: JobQueue,
    now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Housekeeping every 30 s, a job whenever one can run, otherwise wait a little."""
    last_housekeeping = float("-inf")
    last_state = ""
    heartbeat = _Heartbeat(ctx, runner, queue)
    outcome = "idle"
    while not ctx.stop.is_set():
        paused = ctx.paused.is_set()
        if not paused and clock() - last_housekeeping >= HOUSEKEEPING_INTERVAL_S:
            last_housekeeping = clock()
            try:
                housekeeping(ctx, queue, now_ms())
            except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the worker
                log.warning("housekeeping failed: %s", type(exc).__name__)
        try:
            if gaming_now(ctx.conn, now_ms()):
                runner.idle(force=True)  # a game is in front: give its VRAM back right away
            state = "paused" if paused else "backlog" if runner.backlog_active() else "ready"
            # Pause keeps background processing stopped; explicit questions still run.
            outcome = runner.step(only=(ASK, EMBED_QUERY, STT_COMPARE) if paused else None)
        except Exception as exc:  # noqa: BLE001 - e.g. the database was busy; try again shortly
            log.warning("job loop failed: %s", type(exc).__name__)
            outcome, state = "idle", "paused" if paused else last_state or "ready"
        if state != last_state:
            last_state = state
            ctx.report(
                "paused" if state == "paused" else "backlog" if state == "backlog" else "ready"
            )
        heartbeat.beat(state, outcome, now_ms(), clock())
        if outcome != "ran":
            runner.idle()
            ctx.stop.wait(IDLE_WAIT_S)


def build_runner(
    ctx: ChildContext, load: LoadSource, handlers: list[Handler] | None = None
) -> tuple[JobRunner, JobQueue]:
    queue = JobQueue(ctx.conn)
    if handlers is None:
        audio = AudioStore(ctx.conn, MediaStore(ctx.paths.media_dir, ctx.key.subkey("media")))
        whisper = make_transcriber(
            ctx.paths.models_dir / ctx.config.audio.whisper_model,
            ctx.config.model_idle_unload_minutes * 60.0,
        )
        media = MediaStore(ctx.paths.media_dir, ctx.key.subkey("media"))
        segmentation, embedding = model_paths(ctx.paths.models_dir)
        voices = OnnxVoiceModels(
            segmentation, embedding, idle_unload_s=ctx.config.model_idle_unload_minutes * 60.0
        )
        speakers = SpeakerStore(ctx.conn, ctx.config.speakers.match_threshold)
        embedding_dir = ctx.paths.models_dir / ctx.config.search.embedding_model
        chat_model = ctx.config.search.chat_model
        chat_dir = ctx.paths.models_dir / chat_model
        llama = LlamaServer(
            runtime_program(ctx.paths.runtime_dir, LLAMA_RUNTIME),
            ctx.paths.models_dir,
            windows_launcher,
            presets={
                ctx.config.search.embedding_model: EMBEDDING_FLAGS,
                chat_model: CHAT_FLAGS,
            },
            preset_file=ctx.paths.runtime_dir / "models.ini",
            idle_s=ctx.config.model_idle_unload_minutes * 60.0,
            # Embedder and chat model side by side: a question no longer swaps them twice
            # (about 5 of its 8 s). Both leave on the idle timeout, or at once in a game.
            models_max=2,
        )
        embedder = LlamaEmbedder(llama, ctx.config.search.embedding_model)
        chat = LlamaChat(llama, chat_model)

        def embedding_present() -> bool:
            return model_folder_has_gguf(embedding_dir)

        handlers = [
            TranscribeHandler(
                audio, whisper, ctx.config.audio.vocabulary, ctx.config.audio.whisper_model
            ),
            STTCompareHandler(ctx.conn, audio, ctx.paths.models_dir),
            SpeakersHandler(audio, speakers, voices),
            OcrFrameHandler(FrameStore(ctx.conn, media), windows_ocr(ctx.config.ocr_language)),
            RetentionHandler(ctx.conn, media, ctx.config.retention_days),
            EmbedHandler(ctx.conn, embedder, embedding_present),
            QueryEmbedHandler(ctx.conn, embedder, embedding_present),
            AskHandler(
                ctx.conn, chat, embedder, lambda: model_folder_has_gguf(chat_dir), embedding_present
            ),
        ]
    scheduler = Scheduler(ctx.conn, load, ctx.config.scheduler)
    runner = JobRunner(queue, scheduler, handlers, lambda: time.time_ns() // 1_000_000)
    return runner, queue


def _body(ctx: ChildContext) -> None:
    load: LoadSource
    try:
        from akashic.platform.win.load import WindowsLoad  # noqa: PLC0415 - Windows only

        load = WindowsLoad()
    except Exception as exc:  # noqa: BLE001 - the scheduler still watches for games
        log.warning("load measurement unavailable: %s", type(exc).__name__)
        load = _NoLoad()
    runner, queue = build_runner(ctx, load)
    released = queue.release_running()
    if released:
        log.info("%d interrupted job(s) put back in the queue", released)
    run_worker(ctx, runner, queue)


def main(argv: list[str] | None = None) -> int:
    return run_child("worker", argv, _body)


if __name__ == "__main__":
    sys.exit(main())
