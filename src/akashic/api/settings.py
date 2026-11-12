"""The Settings page's routes: read the settings, change them, restart the recorder.

Settings live in ``config.toml``, the one source of truth the processes read at start. Saving
edits that file in place (comments kept), validates the result with the same parser the
processes use, and only then replaces the file. A saved change takes effect when the recording
and worker processes restart; the page offers that button.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from akashic.config import THEMES, Config, ConfigError, Paths, parse_config
from akashic.config_edit import EditError, set_values
from akashic.jsonish import as_dict
from akashic.pipeline.models import CHAT_CANDIDATES, SPEECH_CANDIDATES
from akashic.storage import state
from akashic.storage.crypto import CryptoError
from akashic.storage.debuginfo import ProcessInfo
from akashic.storage.retention import (
    DAY_MS,
    PURGERS,
    delete_all_recorded_data,
    delete_spent_frames,
    delete_spent_segments,
    unreleased_media_references,
)
from akashic.storage.usage import storage_report

if TYPE_CHECKING:
    from pathlib import Path

    from akashic.api.server import Services

type Json = dict[str, object]  # FastAPI reads return annotations at runtime, so no TYPE_CHECKING
# "days": a number of days, or empty for forever (stored as "forever" in config.toml)
type Kind = Literal["number", "int", "bool", "text", "choice", "lines", "device", "days"]


class SettingError(ValueError):
    """A value the user typed is not acceptable; the message says why."""


@dataclass(frozen=True, slots=True)
class Setting:
    key: str  # "section.name" as in config.toml, or a top-level name
    label: str
    kind: Kind
    group: str
    help: str = ""
    low: float | None = None
    high: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    needs: Literal["none", "recording", "app"] = "recording"  # which restart applies it


LOOK: Final = "Appearance"
MIC: Final = "Microphone and voice"
SYSTEM: Final = "System sound"
SPEECH: Final = "Transcription"
SCREEN: Final = "Screen"
PRIVACY: Final = "Privacy"
KEEP: Final = "Retention"
LOAD: Final = "Background work"
OTHER: Final = "Storage and logs"
MODELS: Final = "Model selection"
LEVELS_WANTED_MS: Final = 10_000  # how long one meter poll keeps capture publishing every second

SETTINGS: Final = (
    Setting(
        "theme",
        "Theme",
        "choice",
        LOOK,
        "System follows Windows. Applies as soon as you save.",
        choices=THEMES,
        needs="none",
    ),
    Setting("audio.enabled", "Record audio", "bool", MIC, "Microphone and system sound."),
    Setting("audio.mic_device", "Microphone", "device", MIC, "Empty: the Windows default."),
    Setting(
        "audio.mic_gain",
        "Microphone gain",
        "number",
        MIC,
        "Multiplies the microphone level before anything else. Raise it if the meter stays low.",
        0.1,
        30.0,
        0.1,
    ),
    Setting(
        "audio.energy_floor",
        "Energy floor",
        "number",
        MIC,
        "Speech quieter than this (average level of the spoken part) is dropped as noise. "
        "Keep it below your voice level on the meter.",
        0.0,
        0.99,
        0.001,
    ),
    Setting(
        "audio.vad_threshold",
        "Voice detection sensitivity",
        "number",
        MIC,
        "How sure the voice detector must be that someone speaks. Lower hears quieter speech "
        "but reacts to more noise.",
        0.1,
        0.95,
        0.05,
    ),
    Setting(
        "audio.min_speech_ms",
        "Shortest speech kept (ms)",
        "int",
        MIC,
        "Shorter bursts (a cough, a click) are dropped.",
        0,
        10000,
        50,
    ),
    Setting(
        "audio.silence_ms",
        "Silence that ends a segment (ms)",
        "int",
        MIC,
        "A pause this long closes the current recording.",
        200,
        10000,
        100,
    ),
    Setting(
        "audio.system_device",
        "Playback device to record",
        "device",
        SYSTEM,
        "Sound from programs is recorded through this output. Empty: the Windows default.",
    ),
    Setting(
        "audio.vocabulary",
        "Names and terms",
        "lines",
        SPEECH,
        "One per line: friends, games, projects. Recognition hints for Whisper and Qwen; "
        "not a guarantee of spelling. Canary does not use vocabulary hints.",
    ),
    Setting(
        "audio.whisper_model",
        "Speech model",
        "choice",
        MODELS,
        "Choose a local model, save, and download it below. Whisper is the established "
        "baseline; Qwen and Canary are experimental. Compare the same recordings before "
        "deciding. Changing models affects unprocessed recordings; "
        "saved transcripts stay as they are.",
        choices=tuple(SPEECH_CANDIDATES),
    ),
    Setting(
        "search.chat_model",
        "Ask model",
        "choice",
        MODELS,
        "Choose a model, save, and download it below. The smaller alternatives are "
        "experimental and have not yet been evaluated on your memory questions.",
        choices=tuple(CHAT_CANDIDATES),
    ),
    Setting(
        "speakers.match_threshold",
        "Voice matching strictness",
        "number",
        SPEECH,
        "How alike two voices must sound to be the same person. Higher makes more new "
        "speakers and fewer mix-ups; lower the opposite. Applies to new transcripts.",
        0.1,
        0.95,
        0.05,
    ),
    Setting(
        "model_idle_unload_minutes",
        "Free the AI models after idle (min)",
        "int",
        MODELS,
        "Speech, speaker and language models leave memory after this long without work. "
        "A detected game releases them between jobs.",
        1,
        600,
        1,
    ),
    Setting(
        "ocr_language",
        "Screen text language",
        "text",
        SCREEN,
        "Windows OCR recognizer, e.g. fr or en-US. French also reads English well.",
    ),
    Setting(
        "gaming.screenshot_interval_s",
        "Game screenshot interval (s)",
        "int",
        SCREEN,
        "During a fullscreen game: one screenshot this often.",
        5,
        3600,
        5,
    ),
    Setting(
        "gaming.games",
        "Always treat as games",
        "lines",
        SCREEN,
        "Executables, one per line, e.g. eldenring.exe.",
    ),
    Setting(
        "gaming.not_games",
        "Never treat as games",
        "lines",
        SCREEN,
        "Fullscreen programs that are not games, one per line.",
    ),
    Setting(
        "exclusions.processes",
        "Never capture these programs",
        "lines",
        PRIVACY,
        "Executable names, one per line (globs allowed), added to the built-in list.",
    ),
    Setting(
        "exclusions.window_titles",
        "Never capture these window titles",
        "lines",
        PRIVACY,
        "Globs, one per line, e.g. *bank*.",
    ),
    Setting(
        "exclusions.urls",
        "Never capture these addresses",
        "lines",
        PRIVACY,
        "Globs, one per line, e.g. *://*.example.com/*.",
    ),
    Setting(
        "retention.screenshots",
        "Keep screenshots (days)",
        "days",
        KEEP,
        "Empty keeps them forever. Older screenshots are deleted for good; the text and window "
        "details of those moments stay.",
        1,
        36500,
        1,
    ),
    Setting(
        "retention.frame_text",
        "Keep text read from the screen (days)",
        "days",
        KEEP,
        "Empty keeps it forever. Older text is deleted and no longer searchable; screenshots "
        "stay. A moment with neither screenshot nor text left disappears.",
        1,
        36500,
        1,
    ),
    Setting(
        "retention.raw_audio",
        "Keep recorded audio (days)",
        "days",
        KEEP,
        "Empty keeps it forever. Older recordings can no longer be played; their transcripts "
        "stay. Audio is only deleted once it has been transcribed.",
        1,
        36500,
        1,
    ),
    Setting(
        "retention.transcripts",
        "Keep transcripts (days)",
        "days",
        KEEP,
        "Empty keeps them forever. Older transcripts are deleted and no longer searchable; "
        "recordings stay. Linked digests are rebuilt and affected saved answers are cleared. "
        "Speaker names are kept.",
        1,
        36500,
        1,
    ),
    Setting(
        "retention.media_sessions",
        "Keep what was playing (days)",
        "days",
        KEEP,
        "Empty keeps it forever. Videos and music from before this are forgotten.",
        1,
        36500,
        1,
    ),
    Setting(
        "retention.app_sessions",
        "Keep app and game history (days)",
        "days",
        KEEP,
        "Empty keeps it forever. Which program was in front, and when, before this is forgotten.",
        1,
        36500,
        1,
    ),
    Setting(
        "retention.episodes",
        "Keep episodes (days)",
        "days",
        KEEP,
        "Empty keeps them forever. Episodes are the videos, calls, games and work sessions found "
        "by search; older ones are forgotten, the recordings and text behind them stay.",
        1,
        36500,
        1,
    ),
    Setting(
        "retention.embeddings",
        "Keep search vectors (days)",
        "days",
        KEEP,
        "Empty keeps them forever. Vectors let search find things by meaning; older ones are "
        "deleted and not made again, so those moments are only found by their words.",
        1,
        36500,
        1,
    ),
    Setting(
        "scheduler.cpu_busy_percent",
        "Pause transcription above CPU use (%)",
        "int",
        LOAD,
        "Heavy jobs wait while the CPU is busier than this.",
        1,
        100,
        1,
    ),
    Setting(
        "scheduler.gpu_busy_percent",
        "Pause transcription above GPU use (%)",
        "int",
        LOAD,
        "Heavy jobs wait while the GPU is busier than this. Games always pause them.",
        1,
        100,
        1,
    ),
    Setting(
        "disk_warning_free_gb",
        "Warn when free disk space is below (GB)",
        "int",
        OTHER,
        "",
        1,
        100000,
        1,
    ),
    Setting(
        "log_level",
        "Log detail",
        "choice",
        OTHER,
        "DEBUG records much more (Debug page, Logs tab). The app's own log needs a full restart.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        needs="app",
    ),
)
BY_KEY: Final = {s.key: s for s in SETTINGS}


class Autostart(BaseModel):
    enabled: bool


class Update(BaseModel):
    values: dict[str, object]


def current_value(config: Config, key: str) -> object:
    section, _, name = key.partition(".")
    if section == "retention":
        return config.retention_days[name]  # None: forever
    value: object = config
    for part in key.split("."):
        value = getattr(value, part)
    if isinstance(value, tuple):
        return list(cast("tuple[object, ...]", value))
    return value


def _number(setting: Setting, raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        msg = "must be a number"
        raise SettingError(msg)
    if setting.low is not None and raw < setting.low:
        msg = f"must be at least {setting.low:g}"
        raise SettingError(msg)
    if setting.high is not None and raw > setting.high:
        msg = f"must be at most {setting.high:g}"
        raise SettingError(msg)
    return float(raw)


def _whole_or_fractional(setting: Setting, raw: object) -> float | int:
    number = _number(setting, raw)
    if setting.kind == "number":
        return number
    if number != int(number):
        msg = "must be a whole number"
        raise SettingError(msg)
    return int(number)


def _clean(setting: Setting, raw: object) -> object:
    """``raw`` as the value this setting stores. Raises SettingError when it is not valid."""
    kind = setting.kind
    if kind == "days" and raw is None:
        return None
    if kind in {"int", "number", "days"}:
        return _whole_or_fractional(setting, raw)
    if kind == "bool":
        if not isinstance(raw, bool):
            msg = "must be on or off"
            raise SettingError(msg)
        return raw
    if kind == "lines":
        items = cast("list[object] | None", raw if isinstance(raw, list) else None)
        if items is None or not all(isinstance(v, str) for v in items):
            msg = "must be a list of lines"
            raise SettingError(msg)
        return [str(v).strip() for v in items if str(v).strip()]
    if not isinstance(raw, str):
        msg = "must be text"
        raise SettingError(msg)
    if kind == "choice" and raw not in setting.choices:
        msg = "must be one of " + ", ".join(setting.choices)
        raise SettingError(msg)
    return raw.strip() if kind == "device" else raw


def theme_of(path: Path | None) -> str:
    """The interface theme from config.toml; "system" when there is none or it is unreadable."""
    if path is None:
        return "system"
    try:
        return parse_config(path.read_text(encoding="utf-8")).theme
    except OSError, ConfigError:
        return "system"


def read_config(path: Path) -> Config:
    try:
        return parse_config(path.read_text(encoding="utf-8"))
    except (OSError, ConfigError) as exc:
        raise HTTPException(409, f"config.toml cannot be read: {exc}") from exc


def _changes(config: Config, values: dict[str, object]) -> dict[str, object]:
    changes: dict[str, object] = {}
    for key, raw in values.items():
        setting = BY_KEY.get(key)
        if setting is None:
            raise HTTPException(422, f"unknown setting: {key}")
        try:
            value = _clean(setting, raw)
        except SettingError as exc:
            raise HTTPException(422, f"{setting.label}: {exc}") from exc
        if value != current_value(config, key):
            changes[key] = value
    return changes


def toml_ready(values: dict[str, object]) -> dict[str, object]:
    """``values`` as config.toml writes them: no limit (None) is the word "forever"."""
    return {k: "forever" if v is None else v for k, v in values.items()}


def save_values(path: Path, values: dict[str, object]) -> list[str]:
    """Apply ``values`` to the file and return the keys whose value really changed."""
    changes = _changes(read_config(path), values)
    if not changes:
        return []
    old_text = path.read_text(encoding="utf-8")
    try:
        new_text = set_values(old_text, toml_ready(changes))
        parse_config(new_text)
    except (ConfigError, EditError) as exc:
        raise HTTPException(422, str(exc)) from exc
    path.with_name(path.name + ".bak").write_text(old_text, encoding="utf-8")
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(new_text, encoding="utf-8")
    temp.replace(path)  # atomic: a crash never leaves half a config
    return list(changes)


def _capture_audio(services: Services) -> tuple[int | None, Json | None]:
    """The capture process's latest audio numbers and how old they are (ms)."""
    with services.db() as conn:
        infos = {i.name: i for i in ProcessInfo(conn).read_all()}
    capture = infos.get("capture")
    if capture is None:
        return None, None
    age_ms = time.time_ns() // 1_000_000 - capture.updated_ms
    return age_ms, as_dict(capture.data.get("audio"))


_FILES_REMAIN: Final = "some files could not be removed; close programs using them and retry"
_INCOMPLETE_DELETE: Final = f"{_FILES_REMAIN}, or use Delete all data"


def _purge_category(services: Services, kind: str, cutoff: int) -> tuple[int, int]:
    """Delete one category; returns (rows removed, expirable media references still held)."""
    media = services.frames.media
    with services.db() as conn:
        try:
            removed = PURGERS[kind](conn, media, cutoff)
        finally:
            if kind in {"screenshots", "frame_text"}:
                services.clear_thumbs()
        if kind in {"screenshots", "frame_text"}:
            removed += delete_spent_frames(conn, cutoff)
        if kind in {"raw_audio", "transcripts"}:
            removed += delete_spent_segments(conn, cutoff)
        kept = sum(unreleased_media_references(conn, media, kind, cutoff).values())
    return removed, kept


def _deletion_routes(services: Services) -> APIRouter:
    """Settings > Retention: delete one kind of recording, or everything, at once."""
    router = APIRouter()

    @router.delete("/storage/{kind}")
    def delete_data(kind: str) -> Json:  # pyright: ignore[reportUnusedFunction]
        """Delete one recording category using the same file-safe cleanup as retention."""
        if kind not in PURGERS:
            raise HTTPException(404, "unknown data category")
        cutoff = time.time_ns() // 1_000_000 + DAY_MS
        try:
            removed, kept = _purge_category(services, kind, cutoff)
        except (OSError, CryptoError) as exc:
            raise HTTPException(409, _INCOMPLETE_DELETE) from exc
        if kept:
            raise HTTPException(409, _INCOMPLETE_DELETE)
        return {"deleted": removed}

    @router.delete("/storage")
    def delete_all_data() -> Json:  # pyright: ignore[reportUnusedFunction]
        """Remove all captured and derived user data while keeping app setup intact."""
        if services.controls.while_stopped is None:
            raise HTTPException(503, "recording controls unavailable; restart Akashic and retry")
        removed: dict[str, int] = {}

        def erase() -> None:
            with services.db() as conn:
                try:
                    removed.update(delete_all_recorded_data(conn, services.frames.media))
                finally:
                    services.clear_thumbs()

        try:
            services.controls.while_stopped(erase)
        except (OSError, CryptoError) as exc:
            raise HTTPException(409, _FILES_REMAIN) from exc
        return {"deleted": sum(removed.values()), "types": removed}

    return router


def settings_routes(services: Services, path: Path) -> APIRouter:
    router = APIRouter(prefix="/settings")
    router.include_router(_deletion_routes(services))

    @router.get("")
    def read() -> Json:  # pyright: ignore[reportUnusedFunction]
        config = read_config(path)
        _, audio = _capture_audio(services)
        return {
            "devices": as_dict(audio.get("available")) if audio else {},
            "fields": [
                {
                    "key": s.key,
                    "label": s.label,
                    "kind": s.kind,
                    "group": s.group,
                    "help": s.help,
                    "min": s.low,
                    "max": s.high,
                    "step": s.step,
                    "choices": list(s.choices),
                    "choice_labels": {
                        key: f"{spec.description} ({spec.total_size / 1_000_000_000:.1f} GB)"
                        for key, spec in CHAT_CANDIDATES.items()
                    }
                    if s.key == "search.chat_model"
                    else {
                        key: f"{spec.description} ({spec.total_size / 1_000_000_000:.1f} GB)"
                        for key, spec in SPEECH_CANDIDATES.items()
                    }
                    if s.key == "audio.whisper_model"
                    else {},
                    "needs": s.needs,
                    "value": current_value(config, s.key),
                }
                for s in SETTINGS
            ],
        }

    @router.put("")
    def save(update: Update) -> Json:  # pyright: ignore[reportUnusedFunction]
        changed = save_values(path, update.values)
        return {
            "changed": changed,
            "restart_recording": any(BY_KEY[k].needs == "recording" for k in changed),
            "restart_app": any(BY_KEY[k].needs == "app" for k in changed),
        }

    @router.get("/storage")
    def storage() -> Json:  # pyright: ignore[reportUnusedFunction]
        """Disk used per kind of recording and how fast it grows, for choosing retention."""
        config = read_config(path)
        now_ms = time.time_ns() // 1_000_000
        with services.db() as conn:
            report = storage_report(conn, Paths(path.parent), now_ms)
        free_gb = None if report.free_bytes is None else report.free_bytes / 1e9
        return {
            "types": [
                {
                    "kind": u.kind,
                    "bytes": u.bytes,
                    "bytes_7d": u.bytes_7d,
                    "days": config.retention_days[u.kind],
                }
                for u in report.usage
            ],
            "database_bytes": report.database_bytes,
            "total_bytes": report.total_bytes,
            "free_bytes": report.free_bytes,
            "warn_below_gb": config.disk_warning_free_gb,
            "low_space": free_gb is not None and free_gb < config.disk_warning_free_gb,
        }

    @router.get("/levels")
    def levels() -> Json:  # pyright: ignore[reportUnusedFunction]
        """What the microphone meter shows: the capture process's latest numbers. Polling this
        asks capture to publish every second for the next ten seconds (otherwise every five)."""
        with services.db() as conn:
            now_ms = time.time_ns() // 1_000_000
            state.set_int(conn, state.LEVELS_WANTED_UNTIL, now_ms + LEVELS_WANTED_MS)
        age_ms, audio = _capture_audio(services)
        return {"age_ms": age_ms, "audio": audio}

    @router.post("/restart")
    def restart() -> Json:  # pyright: ignore[reportUnusedFunction]
        services.controls.restart()
        return {"restarting": True}

    @router.get("/autostart")
    def autostart() -> Json:  # pyright: ignore[reportUnusedFunction]
        """Whether the app starts at login. The registry is the truth, not config.toml."""
        get = services.controls.autostart_get
        return {"available": get is not None, "enabled": get() if get else False}

    @router.put("/autostart")
    def set_autostart(body: Autostart) -> Json:  # pyright: ignore[reportUnusedFunction]
        put, get = services.controls.autostart_set, services.controls.autostart_get
        if put is None or get is None:
            raise HTTPException(404, "not available here")
        put(body.enabled)
        return {"available": True, "enabled": get()}

    return router
