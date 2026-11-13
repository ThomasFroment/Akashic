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
from akashic.capture.health import SCREEN_IMAGES_FAILING, CaptureStatus
from akashic.capture.media import MediaTracker
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.screen import ScreenCapture
from akashic.capture.vad import SileroVad
from akashic.child import ChildContext, run_child
from akashic.storage import db, state
from akashic.storage.audio import AudioStore
from akashic.storage.debuginfo import ProcessInfo
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GameLog, MediaLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.capture.ports import AudioPlatform, CapturePlatform
    from akashic.capture.vad import SpeechDetector
    from akashic.config import Audio

log = logging.getLogger("capture")
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
        )
    except Exception as exc:  # noqa: BLE001 - e.g. the VAD model is missing
        conn.close()
        log.error("audio recording unavailable: %s", type(exc).__name__)  # noqa: TRY400
        return None
    thread = threading.Thread(target=audio.run, args=(ctx.stop, ctx.paused), name="audio")
    thread.start()
    log.info("audio recording running")
    return audio, thread, conn


PUBLISH_INTERVAL_S: Final = 5.0
FAST_PUBLISH_S: Final = 1.0  # while the Settings microphone meter is open


def publish_interval(conn: Connection, now_ms: int) -> float:
    """Seconds until the next publish: fast only while someone watches the meter."""
    wanted = state.get_int(conn, state.LEVELS_WANTED_UNTIL, 0) > now_ms
    return FAST_PUBLISH_S if wanted else PUBLISH_INTERVAL_S


def _active_problems(
    status: CaptureStatus,
    capture: ScreenCapture,
    audio: AudioCapture | None,
    *,
    paused: bool,
    since: float,
) -> list[str]:
    """Problems now: only the sticky ones while paused, because nothing is read then."""
    problems = list(status.sticky)
    if paused:
        return problems
    if audio is not None:
        problems.extend(audio.problems(time.monotonic(), since))
    if capture.images_failing(since):
        problems.append(SCREEN_IMAGES_FAILING)
    return problems


def _report_problems(status: CaptureStatus, problems: list[str], epoch: int) -> None:
    try:
        status.set_problems(problems, epoch)
    except Exception as exc:  # noqa: BLE001 - health reporting must never disturb capture
        log.warning("reporting capture health failed: %s", type(exc).__name__)


def _collect(
    ctx: ChildContext,
    status: CaptureStatus,
    capture: ScreenCapture,
    audio: AudioCapture | None,
    tracker: MediaTracker,
) -> tuple[int, list[str], dict[str, object]] | None:
    """``(pause epoch, problems, debug data)``, or None (logged) when reading health failed."""
    try:
        epoch, paused, since = ctx.health_snapshot()  # before reading any health
        problems = _active_problems(status, capture, audio, paused=paused, since=since)
        data: dict[str, object] = {
            "paused": paused,
            "problems": problems,
            "screen": dict(sorted(capture.stats.items())),
            "media": dict(sorted(tracker.stats.items())),
            "audio": audio.snapshot() if audio is not None else None,
        }
    except Exception as exc:  # noqa: BLE001 - health reporting must never disturb capture
        log.warning("collecting capture health failed: %s", type(exc).__name__)
        return None
    return epoch, problems, data


def _publish_info(
    ctx: ChildContext,
    capture: ScreenCapture,
    audio: AudioCapture | None,
    tracker: MediaTracker,
    status: CaptureStatus,
) -> None:
    """Tell the Debug page what this process is up to, every few seconds (numbers only), and
    report the capture problems it found."""
    conn = db.connect(ctx.paths.db_file, ctx.key.subkey("db"))
    info = ProcessInfo(conn)
    try:
        while True:
            wait = PUBLISH_INTERVAL_S
            collected = _collect(ctx, status, capture, audio, tracker)
            if collected is not None:
                epoch, problems, data = collected
                try:
                    now_ms = time.time_ns() // 1_000_000
                    info.publish("capture", data, now_ms)
                    wait = publish_interval(conn, now_ms)
                except Exception as exc:  # noqa: BLE001 - debugging aid: never disturb capture
                    log.warning("publishing debug info failed: %s", type(exc).__name__)
                _report_problems(status, problems, epoch)
            if ctx.stop.wait(wait):
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
            {"paused": ctx.paused.is_set(), "problems": list(status.sticky)},
            time.time_ns() // 1_000_000,
        )
    except Exception as exc:  # noqa: BLE001 - debugging aid: never disturb capture
        log.warning("publishing debug info failed: %s", type(exc).__name__)


def run_capture(
    ctx: ChildContext,
    platform: CapturePlatform,
    audio_platform: AudioPlatform | None = None,
    detector_factory: Callable[[], SpeechDetector] = SileroVad,
) -> None:
    """Wire the screen loop, the media poller and audio to storage; run until the context stops."""
    store = FrameStore(ctx.conn, MediaStore(ctx.paths.media_dir, ctx.key.subkey("media")))
    # Akashic's own pages are never captured. Without the port every loopback address counts.
    api_origin = f"http://127.0.0.1:{ctx.api_port}" if ctx.api_port is not None else None
    exclusions = Exclusions(ctx.config.exclusions, api_origin)
    browsers = BrowserState()  # screen verdicts on browser windows, read by the media tracker
    games = GameLog(ctx.conn)
    games.close_stale()  # a crash left rows open; their end is the last heartbeat
    gaming = ctx.config.gaming
    audio = (
        _start_audio(ctx, audio_platform, exclusions, detector_factory)
        if audio_platform is not None and ctx.config.audio.enabled
        else None
    )
    status = CaptureStatus(
        ctx.set_active,
        lambda: ctx.pause_epoch,
        audio_failed=audio_platform is not None and ctx.config.audio.enabled and audio is None,
    )
    _publish_sticky_problems(ctx, status)
    ctx.set_resume_state("degraded" if status.sticky else "ready")
    capture = ScreenCapture(
        screen=platform.screen,
        windows=platform.windows,
        uia=platform.uia,
        gate=OcrGate(platform.ocr),
        store=store,
        exclusions=exclusions,
        browsers=browsers,
        games=games,
        game_settings=GameSettings(
            interval_s=float(gaming.screenshot_interval_s),
            games=gaming.games,
            not_games=gaming.not_games,
        ),
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
        now_ms=lambda: time.time_ns() // 1_000_000,
        browsers=browsers,
    )
    media_thread = threading.Thread(
        target=tracker.run,
        args=(ctx.stop, ctx.paused, platform.windows.is_locked),
        name="media",
        daemon=True,
    )
    media_thread.start()
    threading.Thread(
        target=_publish_info,
        args=(ctx, capture, audio[0] if audio is not None else None, tracker, status),
        name="debug-info",
        daemon=True,
    ).start()
    log.info("screen capture and media logging running")
    try:
        capture.run(ctx.stop, ctx.paused)
    finally:
        ctx.stop.set()
        capture.close()
        media_thread.join(timeout=10)
        media_conn.close()
        if audio is not None:
            audio[1].join(timeout=60)
            audio[2].close()
            log.info("audio stats: %s", dict(sorted(audio[0].stats.items())))
        log.info("capture stats: %s", dict(sorted(capture.stats.items())))
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
