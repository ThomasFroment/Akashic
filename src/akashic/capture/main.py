"""Capture process entry point.

CPU only: this process must never import CUDA-backed libraries (onnxruntime-gpu, llama_cpp,
ctranslate2). A test enforces it.

``AKASHIC_CAPTURE=0`` starts the process but captures nothing (used by the test suite so it never
records the real screen, and handy when debugging the supervisor).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Final

from akashic.capture.audio import VAD_CONTINUE_RATIO, AudioCapture, AudioSettings
from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.gaming import GameSettings
from akashic.capture.gaps import CaptureGaps
from akashic.capture.health import CaptureStatus
from akashic.capture.media import MediaTracker
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.screen import ScreenCapture
from akashic.capture.screen_writer import ScreenWriter
from akashic.capture.vad import SileroVad
from akashic.child import ChildContext, run_child
from akashic.storage import db
from akashic.storage.audio import AudioStore
from akashic.storage.debuginfo import ProcessInfo
from akashic.storage.frames import FrameStore
from akashic.storage.gaps import GapLog
from akashic.storage.media import MediaStore
from akashic.storage.monitors import MonitorLog
from akashic.storage.sessions import GameLog, MediaLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.capture.gaps import GapSink
    from akashic.capture.ports import AudioPlatform, CapturePlatform
    from akashic.capture.vad import SpeechDetector
    from akashic.config import Audio

log = logging.getLogger("capture")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


DISABLE_ENV = "AKASHIC_CAPTURE"


def audio_settings(cfg: Audio) -> AudioSettings:
    """The recorder's settings from the [audio] table of config.toml."""
    return AudioSettings(
        energy_floor=cfg.energy_floor,
        mic_gain=cfg.mic_gain,
        vad_start=cfg.vad_threshold,
        vad_continue=cfg.vad_threshold * VAD_CONTINUE_RATIO,
        min_speech_ms=cfg.min_speech_ms,
        hangover_ms=cfg.silence_ms,
        mic_device=cfg.mic_device,
        system_device=cfg.system_device,
    )


def _start_audio(
    ctx: ChildContext,
    audio_platform: AudioPlatform,
    exclusions: Exclusions,
    detector_factory: Callable[[], SpeechDetector],
    gaps: GapSink,
) -> tuple[AudioCapture, threading.Thread, Connection] | None:
    """Audio is best effort: if it can't start, screen capture carries on without it."""
    cfg = ctx.config.audio
    conn = db.connect(ctx.paths.db_file, ctx.key.subkey("db"))
    try:
        audio = AudioCapture(
            devices=audio_platform.devices,
            sessions=audio_platform.sessions,
            detector_factory=detector_factory,
            store=AudioStore(conn, MediaStore(ctx.paths.media_dir, ctx.key.subkey("media"))),
            exclusions=exclusions,
            settings=audio_settings(cfg),
            gaps=gaps,
        )
    except Exception as exc:  # noqa: BLE001 - e.g. the VAD model is missing
        conn.close()
        log.error("audio recording unavailable: %s", type(exc).__name__)  # noqa: TRY400
        return None
    thread = threading.Thread(target=audio.run, args=(ctx.stop, ctx.halt, ctx.paused), name="audio")
    thread.start()
    log.info("audio recording running")
    return audio, thread, conn


GAP_THREAD_JOIN_S: Final = 2.0  # a tick still running means the database is busy anyway
WRITER_JOIN_S: Final = 10.0  # time to store the few pictures still queued at stop
PUBLISH_INTERVAL_S: Final = 5.0


def _active_problems(
    status: CaptureStatus,
    writer: ScreenWriter,
    audio: AudioCapture | None,
    *,
    halted: bool,
    since: float,
) -> list[str]:
    """Problems now: only the sticky ones while halted, because nothing is read then."""
    problems = list(status.sticky)
    if halted:
        return problems
    if audio is not None:
        problems.extend(audio.problems(time.monotonic(), since))
    problems.extend(writer.problems(since))
    return problems


def _report_problems(status: CaptureStatus, problems: list[str], epoch: int) -> None:
    try:
        status.set_problems(problems, epoch)
    except Exception as exc:  # noqa: BLE001 - health reporting must never disturb capture
        log.warning("reporting capture health failed: %s", type(exc).__name__)


def _collect(  # noqa: PLR0913, PLR0917 - wiring: each argument is a distinct collaborator
    ctx: ChildContext,
    status: CaptureStatus,
    capture: ScreenCapture,
    writer: ScreenWriter,
    audio: AudioCapture | None,
    tracker: MediaTracker,
    gaps: CaptureGaps,
) -> tuple[int, list[str], dict[str, object]] | None:
    """``(pause epoch, problems, debug data)``, or None (logged) when reading health failed."""
    try:
        epoch, halted, since = ctx.health_snapshot()  # before reading any health
        problems = _active_problems(status, writer, audio, halted=halted, since=since)
        data: dict[str, object] = {
            "halted": halted,
            "problems": problems,
            "screen": dict(sorted(capture.stats_snapshot().items())),
            "media": dict(sorted(tracker.stats.items())),
            "audio": audio.snapshot() if audio is not None else None,
            "gaps": dict(sorted(gaps.stats.items())),
        }
    except Exception as exc:  # noqa: BLE001 - health reporting must never disturb capture
        log.warning("collecting capture health failed: %s", type(exc).__name__)
        return None
    return epoch, problems, data


def _publish_info(  # noqa: PLR0913, PLR0917 - wiring: each argument is a distinct collaborator
    ctx: ChildContext,
    capture: ScreenCapture,
    writer: ScreenWriter,
    audio: AudioCapture | None,
    tracker: MediaTracker,
    status: CaptureStatus,
    gaps: CaptureGaps,
) -> None:
    """Tell the Debug page what this process is up to, every few seconds (numbers only), and
    report the capture problems it found."""
    conn = db.connect(ctx.paths.db_file, ctx.key.subkey("db"))
    info = ProcessInfo(conn)
    try:
        while True:
            collected = _collect(ctx, status, capture, writer, audio, tracker, gaps)
            if collected is not None:
                epoch, problems, data = collected
                try:
                    info.publish("capture", data, _now_ms())
                except Exception as exc:  # noqa: BLE001 - debugging aid: never disturb capture
                    log.warning("publishing debug info failed: %s", type(exc).__name__)
                _report_problems(status, problems, epoch)
            if ctx.stop.wait(PUBLISH_INTERVAL_S):
                break
    finally:
        conn.close()


def _publish_sticky_problems(ctx: ChildContext, status: CaptureStatus) -> None:
    """Write the start-up problems before ``degraded`` is sent, so the row explains the state."""
    if not status.sticky:
        return
    try:
        ProcessInfo(ctx.conn).publish(
            "capture",
            {"halted": ctx.halt.is_set(), "problems": list(status.sticky)},
            _now_ms(),
        )
    except Exception as exc:  # noqa: BLE001 - debugging aid: never disturb capture
        log.warning("publishing debug info failed: %s", type(exc).__name__)


def _open_unrecorded_streams(
    gaps: CaptureGaps, *, enabled: bool, running: bool, start_ms: int
) -> None:
    """Both audio streams are gaps for the whole run when audio is off or could not start."""
    if running:
        return
    reason = "audio_unavailable" if enabled else "disabled"
    gaps.open("mic", reason, start_ms)
    gaps.open("system", reason, start_ms)


def _finish_gaps(gaps: CaptureGaps, thread: threading.Thread, stop_ms: int) -> bool:
    """Write the clean stop once the gap thread is done. False when the thread would not end:
    the next start then records a crash from the heartbeat."""
    thread.join(timeout=GAP_THREAD_JOIN_S)
    if thread.is_alive():
        log.warning("capture gap thread did not stop; the stop is not recorded")
        return False
    try:
        gaps.finish(stop_ms)
    except Exception as exc:  # noqa: BLE001 - the next start settles open rows
        log.warning("recording the capture stop failed: %s", type(exc).__name__)
    return True


def _stop_writer(
    writer: ScreenWriter, stop: threading.Event, thread: threading.Thread, conn: Connection
) -> None:
    """Let the writer store what is queued; give up (and drop the rest) after a bounded wait.
    Called once the screen loop has returned, so nothing more is submitted."""
    stop.set()
    thread.join(timeout=WRITER_JOIN_S)
    if thread.is_alive():
        log.warning("screen writer did not stop; queued pictures are dropped")
        writer.close()
    else:
        conn.close()


def _close_capture(
    capture: ScreenCapture,
    writer: ScreenWriter,
    writer_stop: threading.Event,
    thread: threading.Thread,
    conn: Connection,
) -> None:
    try:
        capture.close()
    except Exception as exc:  # noqa: BLE001 - the rest of the stop must still run
        log.warning("closing screen capture failed: %s", type(exc).__name__)
    _stop_writer(writer, writer_stop, thread, conn)


def _flush_late_gaps(gaps: CaptureGaps) -> None:
    try:
        gaps.flush_late()
    except Exception as exc:  # noqa: BLE001 - only late drops are lost
        log.warning("writing late capture gaps failed: %s", type(exc).__name__)


def run_capture(
    ctx: ChildContext,
    platform: CapturePlatform,
    audio_platform: AudioPlatform | None = None,
    detector_factory: Callable[[], SpeechDetector] = SileroVad,
) -> None:
    """Wire the screen loop, the media poller and audio to storage; run until the context stops."""
    # Akashic's own address halts capture (the failsafe). Without the port every loopback counts.
    api_origin = f"http://127.0.0.1:{ctx.api_port}" if ctx.api_port is not None else None
    exclusions = Exclusions(ctx.config.exclusions, api_origin)
    browsers = BrowserState()  # screen verdicts on browser windows, read by the media tracker
    games = GameLog(ctx.conn)
    games.close_stale()  # a crash left rows open; their end is the last heartbeat
    gaming = ctx.config.gaming
    start_ms = _now_ms()
    gap_conn = db.connect(ctx.paths.db_file, ctx.key.subkey("db"))
    gaps = CaptureGaps(GapLog(gap_conn), _now_ms, start_ms)
    if platform.ocr is None:
        gaps.open("screen_text", "ocr_unavailable", start_ms)
    audio = (
        _start_audio(ctx, audio_platform, exclusions, detector_factory, gaps)
        if audio_platform is not None and ctx.config.audio.enabled
        else None
    )
    _open_unrecorded_streams(
        gaps, enabled=ctx.config.audio.enabled, running=audio is not None, start_ms=start_ms
    )
    status = CaptureStatus(
        ctx.set_active,
        lambda: ctx.pause_epoch,
        audio_failed=audio_platform is not None and ctx.config.audio.enabled and audio is None,
    )
    _publish_sticky_problems(ctx, status)
    ctx.set_resume_state("degraded" if status.sticky else "ready")
    # The writer stores pictures in its own thread, so it gets its own connection.
    writer_conn = db.connect(ctx.paths.db_file, ctx.key.subkey("db"))
    writer = ScreenWriter(
        FrameStore(writer_conn, MediaStore(ctx.paths.media_dir, ctx.key.subkey("media"))),
        MonitorLog(writer_conn),
        OcrGate(platform.ocr),
    )
    writer_stop = threading.Event()  # set after the screen loop is done, not with ctx.stop
    writer_thread = threading.Thread(
        target=writer.run, args=(writer_stop,), name="screen-writer", daemon=True
    )
    writer_thread.start()
    capture = ScreenCapture(
        displays=platform.displays,
        windows=platform.windows,
        facts=platform.facts,
        events=platform.events,
        captures=platform.captures,
        uia=platform.uia,
        writer=writer,
        exclusions=exclusions,
        browsers=browsers,
        games=games,
        game_settings=GameSettings(
            interval_s=float(gaming.screenshot_interval_s),
            games=gaming.games,
            not_games=gaming.not_games,
        ),
        on_own_address=lambda seen: ctx.set_own_address(visible=seen),
        on_game=status.set_gaming,
    )
    # The media poller runs in its own thread, so it gets its own connection: a sqlite
    # connection must not interleave transactions from two threads.
    media_conn = db.connect(ctx.paths.db_file, ctx.key.subkey("db"))
    tracker = MediaTracker(
        source=platform.media,
        log_store=MediaLog(media_conn),
        exclusions=exclusions,
        focused_window=platform.windows.focused_window,
        now_ms=_now_ms,
        browsers=browsers,
    )
    media_thread = threading.Thread(
        target=tracker.run,
        args=(ctx.stop, ctx.halt, platform.windows.is_locked),
        name="media",
        daemon=True,
    )
    media_thread.start()
    gap_thread = threading.Thread(
        target=gaps.run,
        args=(ctx.stop, ctx.paused, ctx.halt, platform.windows.is_locked),
        name="gaps",
        daemon=True,
    )
    gap_thread.start()
    threading.Thread(
        target=_publish_info,
        args=(ctx, capture, writer, audio[0] if audio is not None else None, tracker, status, gaps),
        name="debug-info",
        daemon=True,
    ).start()
    log.info("screen capture and media logging running")
    try:
        capture.run(ctx.stop, ctx.halt, ctx.paused)
    finally:
        ctx.stop.set()
        stop_ms = _now_ms()
        _close_capture(capture, writer, writer_stop, writer_thread, writer_conn)
        gaps_done = _finish_gaps(gaps, gap_thread, stop_ms)
        media_thread.join(timeout=10)
        media_conn.close()
        if audio is not None:
            audio[1].join(timeout=60)
            audio[2].close()
            log.info("audio stats: %s", dict(sorted(audio[0].stats.items())))
        if gaps_done:
            _flush_late_gaps(gaps)
            gap_conn.close()
        log.info("capture stats: %s", dict(sorted(capture.stats_snapshot().items())))
        log.info("media stats: %s", dict(sorted(tracker.stats.items())))


def _body(ctx: ChildContext) -> None:
    if os.environ.get(DISABLE_ENV) == "0":
        log.info("capture disabled by %s=0", DISABLE_ENV)
        ctx.stop.wait()
        return
    from akashic.platform.win import (  # noqa: PLC0415 - Windows only
        build_audio_platform,
        build_capture_platform,
    )

    run_capture(ctx, build_capture_platform(ctx.config.ocr_language), build_audio_platform())


def main(argv: list[str] | None = None) -> int:
    return run_child("capture", argv, _body)


if __name__ == "__main__":
    sys.exit(main())
