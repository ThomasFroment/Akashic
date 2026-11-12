from __future__ import annotations

from pathlib import Path

import pytest

from akashic import config
from akashic.config import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONFIG_TOML,
    RETENTION_TYPES,
    Config,
    ConfigError,
    Paths,
    default_data_dir,
    load_config,
    parse_config,
)


def test_default_file_matches_defaults() -> None:
    assert parse_config(DEFAULT_CONFIG_TOML) == Config()


def test_empty_file_gives_defaults() -> None:
    cfg = parse_config("")
    assert cfg == Config()
    assert cfg.retention_days == dict.fromkeys(RETENTION_TYPES)


def test_load_writes_default(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "new")
    assert load_config(paths) == Config()
    assert paths.config_file.read_text(encoding="utf-8") == DEFAULT_CONFIG_TOML
    paths.config_file.write_text('log_level = "debug"\n', encoding="utf-8")
    assert load_config(paths).log_level == "DEBUG"


# Pins disk_warning_free_gb: delete or rewrite with M21.2 (also the invalid-value row below).
def test_full_config() -> None:
    cfg = parse_config(
        """
        log_level = "WARNING"
        ocr_language = "en"
        disk_warning_free_gb = 20
        model_idle_unload_minutes = 3
        [retention]
        screenshots = 30
        raw_audio = "forever"
        [exclusions]
        processes = ["Signal.exe"]
        window_titles = ["*Mon compte*"]
        urls = ["*.bank.example/*"]
        """
    )
    assert cfg.log_level == "WARNING"
    assert cfg.ocr_language == "en"
    assert cfg.disk_warning_free_gb == 20
    assert cfg.model_idle_unload_minutes == 3
    assert cfg.retention_days["screenshots"] == 30
    assert cfg.retention_days["raw_audio"] is None
    assert cfg.retention_days["transcripts"] is None
    assert cfg.exclusions.processes == ("Signal.exe",)
    assert cfg.exclusions.window_titles == ("*Mon compte*",)
    assert cfg.exclusions.urls == ("*.bank.example/*",)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("log_levl = 'INFO'", "unknown key"),
        ("log_level = 'LOUD'", "log_level"),
        ("disk_warning_free_gb = 0", "disk_warning_free_gb"),
        ("retention = 5", r"\[retention\]"),
        ("[retention]\nscreenshots = 'never'", "retention.screenshots"),
        ("[exclusions]\nprocesses = 'x'", "exclusions.processes"),
        ("this is = not toml =", "not valid TOML"),
    ],
)
def test_invalid(text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_config(text)


def test_paths() -> None:
    p = Paths(Path("X"))
    assert p.db_file == Path("X/db/akashic.db")
    assert p.keyfile == Path("X/db/keyfile")
    assert p.media_dir == Path("X/media")
    assert p.models_dir == Path("X/models")
    assert p.runtime_dir == Path("X/runtime")
    assert p.logs_dir == Path("X/logs")


def test_data_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    assert default_data_dir() == tmp_path


def test_data_dir_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_data_dir() == tmp_path / "Akashic"
    monkeypatch.delenv("LOCALAPPDATA")
    with pytest.raises(ConfigError):
        default_data_dir()


def test_gaming_section() -> None:
    cfg = parse_config(
        """
        [gaming]
        screenshot_interval_s = 30
        games = ["Framed.exe"]
        not_games = ["Odd.EXE"]
        """
    )
    assert cfg.gaming.screenshot_interval_s == 30
    assert cfg.gaming.games == ("framed.exe",)
    assert cfg.gaming.not_games == ("odd.exe",)
    assert parse_config("").gaming.screenshot_interval_s == 60


@pytest.mark.parametrize("text", ["[gaming]\nfoo = 1", "[gaming]\nscreenshot_interval_s = 0"])
def test_invalid_gaming(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(text)


def test_audio_and_scheduler_sections() -> None:
    cfg = parse_config(
        """
        [audio]
        enabled = false
        mic_device = "Wave:3"
        system_device = "Headphones"
        energy_floor = 0.02
        whisper_model = "my-whisper"
        vocabulary = ["Minlo", "  Elden Ring ", ""]
        [scheduler]
        gpu_busy_percent = 60
        cpu_busy_percent = 90
        """
    )
    assert not cfg.audio.enabled
    assert (cfg.audio.mic_device, cfg.audio.system_device) == ("Wave:3", "Headphones")
    assert cfg.audio.energy_floor == 0.02
    assert cfg.audio.whisper_model == "my-whisper"
    assert cfg.audio.vocabulary == ("Minlo", "Elden Ring")
    assert (cfg.scheduler.gpu_busy_percent, cfg.scheduler.cpu_busy_percent) == (60, 90)


def test_audio_defaults() -> None:
    cfg = parse_config("")
    assert cfg.audio.enabled
    assert cfg.audio.energy_floor == 0.015
    assert cfg.audio.vocabulary == ()
    assert (cfg.scheduler.gpu_busy_percent, cfg.scheduler.cpu_busy_percent) == (75, 85)


@pytest.mark.parametrize(
    "text",
    [
        "[audio]\nfoo = 1",
        "[audio]\nenabled = 1",
        "[audio]\nenergy_floor = 1.5",
        "[audio]\nvocabulary = 'Minlo'",
        "[audio]\nwhisper_model = '../elsewhere'",
        "[scheduler]\ngpu_busy_percent = 0",
    ],
)
def test_invalid_audio_and_scheduler(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(text)


def test_search_section() -> None:
    assert parse_config("").search.embedding_model == "qwen3-embedding-0.6b-q8"
    cfg = parse_config('[search]\nembedding_model = "bge-m3"')
    assert cfg.search.embedding_model == "bge-m3"
    assert parse_config("").search.chat_model == DEFAULT_CHAT_MODEL
    cfg = parse_config('[search]\nchat_model = "qwen3-14b-q4km"')
    assert cfg.search.chat_model == "qwen3-14b-q4km"
    assert cfg.search.embedding_model == "qwen3-embedding-0.6b-q8"


@pytest.mark.parametrize(
    "text",
    [
        "[search]\nfoo = 1",
        "[search]\nembedding_model = '../elsewhere'",
        "[search]\nembedding_model = 3",
    ],
)
def test_invalid_search(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(text)
