"""Typed configuration loaded from ``config.toml`` in the data directory.

Unknown keys are errors, so a typo never silently falls back to a default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from akashic.jsonish import as_dict, as_list

RETENTION_TYPES: Final = (
    "screenshots",
    "raw_audio",
    "frame_text",
    "transcripts",
    "embeddings",
    "episodes",
    "media_sessions",
    "app_sessions",
)
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR")
THEMES: Final = ("system", "light", "dark")
DATA_DIR_ENV: Final = "AKASHIC_DATA_DIR"
WHISPER_TURBO_MODEL: Final = "faster-whisper-large-v3-turbo"
WHISPER_LARGE_V3_MODEL: Final = "faster-whisper-large-v3"
# large-v3 hears quiet and mumbled French better than turbo. It is about twice as slow, which
# does not matter: transcription runs in the background and may lag behind.
DEFAULT_WHISPER_MODEL: Final = WHISPER_LARGE_V3_MODEL
DEFAULT_EMBEDDING_MODEL: Final = "qwen3-embedding-0.6b-q8"  # folder inside models/ holding a GGUF
DEFAULT_CHAT_MODEL: Final = "gemma-4-12b-qat-q4"  # the model that plans and answers questions
# Built-in Whisper baselines; full speech choices live in pipeline.models.SPEECH_CANDIDATES.
WHISPER_MODELS: Final = (WHISPER_LARGE_V3_MODEL, WHISPER_TURBO_MODEL)


class ConfigError(Exception):
    """config.toml is invalid. The message names the offending key."""


@dataclass(frozen=True, slots=True)
class Paths:
    root: Path

    @property
    def db_dir(self) -> Path:
        return self.root / "db"

    @property
    def db_file(self) -> Path:
        return self.db_dir / "akashic.db"

    @property
    def keyfile(self) -> Path:
        return self.db_dir / "keyfile"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def runtime_dir(self) -> Path:
        """Pinned third-party runtime binaries (llama.cpp), from the one-time download."""
        return self.root / "runtime"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def config_file(self) -> Path:
        return self.root / "config.toml"

    def ensure(self) -> None:
        for d in (self.db_dir, self.media_dir, self.models_dir, self.runtime_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class Exclusions:
    """User additions; built-in defaults (password managers, private windows, banking) live
    with the exclusion logic, not here."""

    processes: tuple[str, ...] = ()
    window_titles: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Gaming:
    """Fullscreen-game mode: one screenshot per interval, no UI Automation walks."""

    screenshot_interval_s: int = 60
    games: tuple[str, ...] = ()  # executables always treated as games when fullscreen
    not_games: tuple[str, ...] = ()  # fullscreen programs that are not games (added to built-ins)


@dataclass(frozen=True, slots=True)
class Audio:
    """Recording and transcription. Devices are matched by a fragment of their name."""

    enabled: bool = True
    mic_device: str = ""  # empty: the default microphone
    system_device: str = ""  # empty: the default playback device, recorded through loopback
    energy_floor: float = 0.015  # speech quieter than this (RMS) is treated as noise
    mic_gain: float = 1.0  # microphone volume multiplier, applied before everything else
    vad_threshold: float = 0.5  # voice detector: how sure it must be that someone speaks
    min_speech_ms: int = 400  # shorter bursts of speech (a cough, a click) are dropped
    silence_ms: int = 1_000  # silence that ends a segment
    whisper_model: str = DEFAULT_WHISPER_MODEL  # folder inside models/
    # Names and terms Whisper should spell correctly: friends, games, projects.
    vocabulary: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Speakers:
    """Telling voices apart."""

    # Cosine similarity needed to call a voice one already known. Higher: more new speakers,
    # fewer mix-ups. Lower: the opposite.
    match_threshold: float = 0.5


@dataclass(frozen=True, slots=True)
class Search:
    """Semantic search. Changing the model makes new vectors; the old ones stay unused."""

    embedding_model: str = DEFAULT_EMBEDDING_MODEL  # folder inside models/
    chat_model: str = DEFAULT_CHAT_MODEL  # folder inside models/


@dataclass(frozen=True, slots=True)
class Scheduler:
    """When heavy jobs (transcription, ...) may run. Games always defer them."""

    gpu_busy_percent: int = 75
    cpu_busy_percent: int = 85


@dataclass(frozen=True, slots=True)
class Config:
    log_level: str = "INFO"
    theme: str = "system"  # web interface: system, light or dark
    ocr_language: str = "fr"
    # Days to keep each data type; None means forever (the default for every type).
    retention_days: dict[str, int | None] = field(
        default_factory=lambda: dict.fromkeys(RETENTION_TYPES)
    )
    exclusions: Exclusions = Exclusions()
    gaming: Gaming = Gaming()
    audio: Audio = Audio()
    speakers: Speakers = Speakers()
    search: Search = Search()
    scheduler: Scheduler = Scheduler()
    disk_warning_free_gb: int = 50
    model_idle_unload_minutes: int = 5


DEFAULT_CONFIG_TOML: Final = """\
# Akashic configuration. Restart the app after editing.

log_level = "INFO"          # DEBUG, INFO, WARNING, ERROR (logs never contain captured content)
theme = "system"          # web interface: system, light or dark
ocr_language = "fr"         # Windows OCR recognizer; French also reads English well
disk_warning_free_gb = 50   # warn when free space on the data drive drops below this
model_idle_unload_minutes = 5

[retention]
# Days to keep each type, or "forever".
screenshots = "forever"
raw_audio = "forever"
frame_text = "forever"
transcripts = "forever"
embeddings = "forever"
episodes = "forever"
media_sessions = "forever"
app_sessions = "forever"

[gaming]
# While a fullscreen game is in front: one screenshot per interval, no text reading.
# Fullscreen browsers, media players and the desktop are never treated as games.
screenshot_interval_s = 60
games = []       # executables always treated as games when fullscreen, e.g. ["eldenring.exe"]
not_games = []   # fullscreen programs to ignore, added to the built-in list

[audio]
# Microphone and system sound (through loopback), kept only while someone speaks.
enabled = true
mic_device = ""      # part of a device name, e.g. "Wave:3"; empty means the default microphone
system_device = ""   # part of a playback device name; empty means the default playback device
energy_floor = 0.015     # speech quieter than this (RMS of the spoken part) is dropped as noise
mic_gain = 1.0           # microphone volume multiplier, e.g. 2.0 doubles the level
vad_threshold = 0.5      # voice detector sensitivity: lower hears quieter speech, more false alarms
min_speech_ms = 400      # shorter bursts of speech are dropped
silence_ms = 1000        # this much silence ends a segment
whisper_model = "faster-whisper-large-v3"   # legacy key for speech model folder; Settings > Models
# Names and terms Whisper should spell correctly. Whole entries only; about 800 characters fit.
vocabulary = []      # e.g. ["Minlo", "Elden Ring"]

[speakers]
# Voices are matched by similarity (0 to 1). Higher: more new speakers, fewer mix-ups.
match_threshold = 0.5

[search]
embedding_model = "qwen3-embedding-0.6b-q8"   # folder inside models/ holding a GGUF model
chat_model = "gemma-4-12b-qat-q4"             # folder inside models/: plans and answers questions

[scheduler]
# Heavy jobs wait while a game is running or the GPU/CPU is busier than this.
gpu_busy_percent = 75
cpu_busy_percent = 85

[exclusions]
# Added to the built-in lists. Globs are case-insensitive, e.g. "*bank*".
processes = []
window_titles = []
urls = []
"""


def default_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        msg = "LOCALAPPDATA is not set"
        raise ConfigError(msg)
    return Path(local) / "Akashic"


def _expect_keys(table: dict[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        msg = f"unknown key(s) in {where}: {', '.join(unknown)}"
        raise ConfigError(msg)


def _str(table: dict[str, object], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        msg = f"{key} must be a string"
        raise ConfigError(msg)
    return value


def _positive_int(table: dict[str, object], key: str, default: int) -> int:
    value = table.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        msg = f"{key} must be a positive integer"
        raise ConfigError(msg)
    return value


def _bool(table: dict[str, object], key: str, *, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        msg = f"{key} must be true or false"
        raise ConfigError(msg)
    return value


def _fraction(table: dict[str, object], key: str, default: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1:
        msg = f"{key} must be a number from 0 up to (not including) 1"
        raise ConfigError(msg)
    return float(value)


def _number(table: dict[str, object], key: str, default: float, low: float, high: float) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        msg = f"{key} must be a number from {low:g} to {high:g}"
        raise ConfigError(msg)
    return float(value)


def _percent(table: dict[str, object], key: str, default: int) -> int:
    value = _positive_int(table, key, default)
    if value > 100:  # noqa: PLR2004 - a percentage
        msg = f"{key} must be between 1 and 100"
        raise ConfigError(msg)
    return value


def _folder_name(table: dict[str, object], key: str, default: str) -> str:
    """A single folder name inside models/: never a path, so config can't point elsewhere."""
    value = _str(table, key, default)
    if not value or value in {".", ".."} or any(c in value for c in "/\\:"):
        msg = f"{key} must be a folder name inside models/, not a path"
        raise ConfigError(msg)
    return value


def _table(doc: dict[str, object], key: str) -> dict[str, object]:
    value = as_dict(doc.get(key, {}))
    if value is None:
        msg = f"[{key}] must be a table"
        raise ConfigError(msg)
    return value


def _str_tuple(table: dict[str, object], key: str, where: str) -> tuple[str, ...]:
    items = as_list(table.get(key, []))
    if items is None or not all(isinstance(v, str) for v in items):
        msg = f"{where}.{key} must be a list of strings"
        raise ConfigError(msg)
    return tuple(str(v) for v in items)


def _retention(table: dict[str, object]) -> dict[str, int | None]:
    _expect_keys(table, set(RETENTION_TYPES), "[retention]")
    result: dict[str, int | None] = {}
    for kind in RETENTION_TYPES:
        value = table.get(kind, "forever")
        if value == "forever":
            result[kind] = None
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            result[kind] = value
        else:
            msg = f'retention.{kind} must be a positive number of days or "forever"'
            raise ConfigError(msg)
    return result


def parse_config(text: str) -> Config:
    try:
        doc: dict[str, object] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"config.toml is not valid TOML: {exc}"
        raise ConfigError(msg) from exc
    _expect_keys(
        doc,
        {
            "log_level",
            "theme",
            "ocr_language",
            "disk_warning_free_gb",
            "model_idle_unload_minutes",
            "retention",
            "exclusions",
            "gaming",
            "audio",
            "speakers",
            "search",
            "scheduler",
        },
        "config.toml",
    )
    log_level = _str(doc, "log_level", "INFO").upper()
    if log_level not in LOG_LEVELS:
        msg = f"log_level must be one of {', '.join(LOG_LEVELS)}"
        raise ConfigError(msg)
    theme = _str(doc, "theme", "system")
    if theme not in THEMES:
        msg = f"theme must be one of {', '.join(THEMES)}"
        raise ConfigError(msg)
    excl = _table(doc, "exclusions")
    _expect_keys(excl, {"processes", "window_titles", "urls"}, "[exclusions]")
    game = _table(doc, "gaming")
    _expect_keys(game, {"screenshot_interval_s", "games", "not_games"}, "[gaming]")
    audio = _table(doc, "audio")
    _expect_keys(
        audio,
        {
            "enabled",
            "mic_device",
            "system_device",
            "energy_floor",
            "mic_gain",
            "vad_threshold",
            "min_speech_ms",
            "silence_ms",
            "whisper_model",
            "vocabulary",
        },
        "[audio]",
    )
    spk = _table(doc, "speakers")
    _expect_keys(spk, {"match_threshold"}, "[speakers]")
    srch = _table(doc, "search")
    _expect_keys(srch, {"embedding_model", "chat_model"}, "[search]")
    sched = _table(doc, "scheduler")
    _expect_keys(sched, {"gpu_busy_percent", "cpu_busy_percent"}, "[scheduler]")
    return Config(
        log_level=log_level,
        theme=theme,
        ocr_language=_str(doc, "ocr_language", "fr"),
        retention_days=_retention(_table(doc, "retention")),
        exclusions=Exclusions(
            processes=_str_tuple(excl, "processes", "exclusions"),
            window_titles=_str_tuple(excl, "window_titles", "exclusions"),
            urls=_str_tuple(excl, "urls", "exclusions"),
        ),
        gaming=Gaming(
            screenshot_interval_s=_positive_int(game, "screenshot_interval_s", 60),
            games=tuple(g.casefold() for g in _str_tuple(game, "games", "gaming")),
            not_games=tuple(g.casefold() for g in _str_tuple(game, "not_games", "gaming")),
        ),
        audio=Audio(
            enabled=_bool(audio, "enabled", default=True),
            mic_device=_str(audio, "mic_device", ""),
            system_device=_str(audio, "system_device", ""),
            energy_floor=_fraction(audio, "energy_floor", 0.015),
            mic_gain=_number(audio, "mic_gain", 1.0, 0.1, 30.0),
            vad_threshold=_number(audio, "vad_threshold", 0.5, 0.1, 0.95),
            min_speech_ms=_positive_int(audio, "min_speech_ms", 400),
            silence_ms=_positive_int(audio, "silence_ms", 1_000),
            whisper_model=_folder_name(audio, "whisper_model", DEFAULT_WHISPER_MODEL),
            vocabulary=tuple(
                v.strip() for v in _str_tuple(audio, "vocabulary", "audio") if v.strip()
            ),
        ),
        speakers=Speakers(match_threshold=_number(spk, "match_threshold", 0.5, 0.1, 0.95)),
        search=Search(
            embedding_model=_folder_name(srch, "embedding_model", DEFAULT_EMBEDDING_MODEL),
            chat_model=_folder_name(srch, "chat_model", DEFAULT_CHAT_MODEL),
        ),
        scheduler=Scheduler(
            gpu_busy_percent=_percent(sched, "gpu_busy_percent", 75),
            cpu_busy_percent=_percent(sched, "cpu_busy_percent", 85),
        ),
        disk_warning_free_gb=_positive_int(doc, "disk_warning_free_gb", 50),
        model_idle_unload_minutes=_positive_int(doc, "model_idle_unload_minutes", 5),
    )


def load_config(paths: Paths) -> Config:
    """Read config.toml, writing the commented default first if it doesn't exist."""
    if not paths.config_file.exists():
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return parse_config(paths.config_file.read_text(encoding="utf-8"))
