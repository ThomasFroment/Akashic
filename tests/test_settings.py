"""Settings: the config keys, the in-place TOML editor, the /api/settings routes, the recorder's
gain and voice level, the supervisor's restart."""

from __future__ import annotations

import http.client
import json
import time
import tomllib
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from audio_fakes import RecorderRig, speech
from store_reads import latest_frames
from waits import wait_for

from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.api.settings import BY_KEY, SETTINGS, current_value
from akashic.capture.audio import AudioSettings
from akashic.capture.main import (
    FAST_PUBLISH_S,
    PUBLISH_INTERVAL_S,
    audio_settings,
    publish_interval,
)
from akashic.config import (
    DEFAULT_CONFIG_TOML,
    Audio,
    Config,
    ConfigError,
    parse_config,
)
from akashic.config_edit import EditError, set_value, set_values, toml_value
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.debuginfo import ProcessInfo
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.media import MediaStore
from akashic.supervisor import CHILDREN, Supervisor, spawn_module

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.supervisor import ChildSpec

# --- config keys ---------------------------------------------------------------------------


def test_audio_and_theme_keys_have_defaults_and_bounds() -> None:
    cfg = parse_config("")
    assert (cfg.audio.mic_gain, cfg.audio.vad_threshold) == (1.0, 0.5)
    assert (cfg.audio.min_speech_ms, cfg.audio.silence_ms, cfg.theme) == (400, 1000, "system")
    cfg = parse_config("theme = 'dark'\n[audio]\nmic_gain = 2.5\nvad_threshold = 0.3\n")
    assert (cfg.theme, cfg.audio.mic_gain, cfg.audio.vad_threshold) == ("dark", 2.5, 0.3)
    for bad in (
        "[audio]\nmic_gain = 0\n",
        "[audio]\nmic_gain = 'loud'\n",
        "[audio]\nvad_threshold = 1.5\n",
        "[audio]\nmin_speech_ms = 0\n",
        "theme = 'purple'\n",
    ):
        with pytest.raises(ConfigError):
            parse_config(bad)


def test_the_default_file_documents_the_audio_and_theme_keys() -> None:
    for key in ("mic_gain", "vad_threshold", "min_speech_ms", "silence_ms", "theme"):
        assert key in DEFAULT_CONFIG_TOML
    assert parse_config(DEFAULT_CONFIG_TOML) == Config()


def test_the_recorder_gets_its_settings_from_the_config() -> None:
    s = audio_settings(Audio(mic_gain=2.0, vad_threshold=0.4, min_speech_ms=300, silence_ms=800))
    assert (s.mic_gain, s.vad_start, s.min_speech_ms, s.hangover_ms) == (2.0, 0.4, 300, 800)
    assert s.vad_continue == pytest.approx(0.28)


# --- the TOML editor -----------------------------------------------------------------------


def test_values_as_toml_literals() -> None:
    assert toml_value(True) == "true"  # noqa: FBT003
    assert toml_value(3) == "3"
    assert toml_value(0.015) == "0.015"
    assert toml_value('say "hi"\\') == '"say \\"hi\\"\\\\"'
    assert toml_value(["a", "é"]) == '["a", "é"]'
    assert toml_value([]) == "[]"
    for bad in ([1], None, {"a": 1}):
        with pytest.raises(EditError):
            toml_value(bad)


def test_editing_keeps_comments_and_neighbours() -> None:
    text = (
        '# top comment\nlog_level = "INFO"   # keep me\n\n[audio]\n'
        'energy_floor = 0.015   # the floor\nmic_device = ""\n\n'
        "[scheduler]\ncpu_busy_percent = 85\n"
    )
    out = set_value(text, "audio.energy_floor", 0.02)
    assert "energy_floor = 0.02   # the floor\n" in out
    assert out.replace("0.02   #", "0.015   #") == text
    out = set_value(text, "log_level", "DEBUG")
    assert 'log_level = "DEBUG"   # keep me\n' in out
    assert tomllib.loads(out)["audio"]["mic_device"] == ""


def test_editing_adds_missing_keys_and_sections() -> None:
    text = 'log_level = "INFO"\n\n[audio]\nenabled = true\n\n[scheduler]\ncpu_busy_percent = 85\n'
    out = set_value(text, "audio.mic_gain", 2.0)
    assert out == (
        'log_level = "INFO"\n\n[audio]\nenabled = true\nmic_gain = 2.0\n\n'
        "[scheduler]\ncpu_busy_percent = 85\n"
    )
    out = set_value(text, "theme", "dark")
    assert out.startswith('log_level = "INFO"\ntheme = "dark"\n\n[audio]')
    out = set_value("a = 1\n", "gaming.games", ["x.exe"])
    assert tomllib.loads(out) == {"a": 1, "gaming": {"games": ["x.exe"]}}
    assert tomllib.loads(set_value("", "theme", "light")) == {"theme": "light"}
    assert tomllib.loads(set_value("[audio]\n", "audio.mic_gain", 2)) == {"audio": {"mic_gain": 2}}
    assert tomllib.loads(set_values("x = 1", {"x": 2, "audio.enabled": False})) == {
        "x": 2,
        "audio": {"enabled": False},
    }


# --- the routes ----------------------------------------------------------------------------


class Client:
    def __init__(
        self, server: ApiServer, token: str, conn: Connection, frames: FrameStore | None = None
    ) -> None:
        self.server, self.token, self.conn, self.frames = server, token, conn, frames

    def call(
        self, path: str, method: str = "GET", body: object = None, token: str | None = None
    ) -> tuple[int, Any]:
        c = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        headers = {"Cookie": f"akashic_session={token or self.token}"}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=payload, headers=headers)
        resp = c.getresponse()
        data = resp.read()
        c.close()
        return resp.status, json.loads(data)

    def save(self, values: dict[str, object]) -> tuple[int, Any]:
        return self.call("/api/settings", "PUT", {"values": values})


@pytest.fixture
def restarts() -> list[bool]:
    return []


@pytest.fixture
def client(paths: Paths, restarts: list[bool]) -> Iterator[Client]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    ProcessInfo(conn).publish(
        "capture",
        {"audio": {"available": {"mic": ["Wave:3"], "system": ["DT-770"]}, "streams": {}}},
        1,
    )
    token = new_token()
    controls = Controls(
        dict,
        lambda: None,
        lambda: None,
        lambda: restarts.append(True),
        while_stopped=lambda action: action(),
    )
    frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    server = ApiServer(Services(conn, frames, token, controls, config_file=paths.config_file))
    server.start()
    yield Client(server, token, conn, frames)
    server.stop()
    conn.close()


def test_settings_are_listed_with_their_current_values_and_the_devices(client: Client) -> None:
    status, out = client.call("/api/settings")
    assert status == 200
    assert out["devices"] == {"mic": ["Wave:3"], "system": ["DT-770"]}
    fields = {f["key"]: f for f in out["fields"]}
    assert set(fields) == {s.key for s in SETTINGS}
    assert fields["audio.energy_floor"]["value"] == 0.015
    assert fields["audio.vocabulary"]["value"] == []
    assert fields["log_level"]["choices"] == ["DEBUG", "INFO", "WARNING", "ERROR"]
    assert fields["theme"]["needs"] == "none"
    assert "path" not in out


def test_model_choices_can_be_saved_and_are_described(client: Client, paths: Paths) -> None:
    from akashic.pipeline.models import CHAT_CANDIDATES, QWEN35_4B  # noqa: PLC0415

    _, out = client.call("/api/settings")
    fields = {f["key"]: f for f in out["fields"]}
    chat = fields["search.chat_model"]
    assert chat["group"] == fields["audio.whisper_model"]["group"] == "Model selection"
    assert set(chat["choices"]) == set(CHAT_CANDIDATES)
    assert all(chat["choice_labels"].get(key) for key in chat["choices"])
    status, result = client.save(
        {"search.chat_model": QWEN35_4B.key, "audio.whisper_model": "faster-whisper-large-v3-turbo"}
    )
    assert status == 200
    assert result["restart_recording"] is True
    config = parse_config(paths.config_file.read_text("utf-8"))
    assert config.search.chat_model == QWEN35_4B.key
    assert config.audio.whisper_model == "faster-whisper-large-v3-turbo"
    from akashic.pipeline.models import required  # noqa: PLC0415

    assert QWEN35_4B in [need.spec for need in required(config)]


def test_speech_models_follow_the_same_selection_and_download_flow(
    client: Client, paths: Paths
) -> None:
    from akashic.pipeline.models import QWEN3_ASR, SPEECH_CANDIDATES, required  # noqa: PLC0415

    _, out = client.call("/api/settings")
    fields = {f["key"]: f for f in out["fields"]}
    speech = fields["audio.whisper_model"]
    assert set(speech["choices"]) == set(SPEECH_CANDIDATES)
    assert all(speech["choice_labels"].get(key) for key in speech["choices"])
    assert client.save({"audio.whisper_model": QWEN3_ASR.key})[0] == 200
    config = parse_config(paths.config_file.read_text("utf-8"))
    assert config.audio.whisper_model == QWEN3_ASR.key
    assert QWEN3_ASR in [need.spec for need in required(config)]


def test_saving_edits_the_file_keeps_comments_and_a_backup(client: Client, paths: Paths) -> None:
    status, out = client.save(
        {
            "audio.energy_floor": 0.008,
            "audio.mic_gain": 3,
            "audio.vocabulary": [" Minlo ", "", "Elden Ring"],
            "theme": "dark",
            "audio.enabled": True,  # unchanged: not written, not reported
        }
    )
    assert status == 200
    assert sorted(out["changed"]) == [
        "audio.energy_floor",
        "audio.mic_gain",
        "audio.vocabulary",
        "theme",
    ]
    assert (out["restart_recording"], out["restart_app"]) == (True, False)
    text = paths.config_file.read_text("utf-8")
    assert "energy_floor = 0.008     # speech quieter" in text
    assert 'vocabulary = ["Minlo", "Elden Ring"]      # e.g.' in text
    cfg = parse_config(text)
    assert (cfg.audio.mic_gain, cfg.audio.vocabulary, cfg.theme) == (
        3.0,
        ("Minlo", "Elden Ring"),
        "dark",
    )
    assert paths.config_file.with_name("config.toml.bak").read_text("utf-8") == DEFAULT_CONFIG_TOML
    assert not paths.config_file.with_name("config.toml.tmp").exists()
    assert client.call("/api/status")[1]["theme"] == "dark"


def test_bad_values_are_refused_and_the_file_is_untouched(client: Client, paths: Paths) -> None:
    before = paths.config_file.read_text("utf-8")
    bad: list[tuple[dict[str, object], str]] = [
        ({"audio.mic_gain": 0}, "at least 0.1"),
        ({"audio.mic_gain": 99}, "at most 30"),
        ({"audio.mic_gain": "x"}, "must be a number"),
        ({"audio.mic_gain": True}, "must be a number"),
        ({"audio.min_speech_ms": 100.5}, "whole number"),
        ({"audio.enabled": "yes"}, "on or off"),
        ({"audio.vocabulary": "a"}, "list of lines"),
        ({"audio.vocabulary": [1]}, "list of lines"),
        ({"log_level": "LOUD"}, "one of"),
        ({"ocr_language": 5}, "must be text"),
        ({"nope.key": 1}, "unknown setting"),
    ]
    for values, why in bad:
        status, out = client.save(values)
        assert status == 422, values
        assert why in out["detail"], (values, out)
    assert paths.config_file.read_text("utf-8") == before
    assert not paths.config_file.with_name("config.toml.bak").exists()


def test_a_broken_config_file_is_reported_not_overwritten(client: Client, paths: Paths) -> None:
    paths.config_file.write_text("this is = = not toml", encoding="utf-8")
    assert client.call("/api/settings")[0] == 409
    assert client.save({"theme": "dark"})[0] == 409
    assert paths.config_file.read_text("utf-8") == "this is = = not toml"
    assert client.call("/api/status")[1]["theme"] == "system"


def test_levels_restart_and_auth(client: Client, restarts: list[bool]) -> None:
    status, levels = client.call("/api/settings/levels")
    assert status == 200
    assert levels["audio"]["available"]["mic"] == ["Wave:3"]
    assert levels["age_ms"] > 0
    now_ms = time.time_ns() // 1_000_000
    assert publish_interval(client.conn, now_ms) == FAST_PUBLISH_S  # the meter is open
    assert publish_interval(client.conn, now_ms + 60_000) == PUBLISH_INTERVAL_S  # closed since
    assert client.call("/api/settings/restart", "POST") == (200, {"restarting": True})
    assert restarts == [True]
    for method, path in (("GET", "/api/settings"), ("POST", "/api/settings/restart")):
        assert client.call(path, method, token="wrong")[0] == 401  # noqa: S106
    assert client.call("/api/settings", "PUT", {"values": {}}, token="wrong")[0] == 401  # noqa: S106


def test_start_with_windows_is_read_and_set_through_the_controls(paths: Paths) -> None:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    registry: list[bool] = [False]
    controls = Controls(
        dict,
        lambda: None,
        lambda: None,
        autostart_get=lambda: registry[0],
        autostart_set=lambda on: registry.__setitem__(0, on),
    )
    frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    token = new_token()
    server = ApiServer(Services(conn, frames, token, controls, config_file=paths.config_file))
    server.start()
    try:
        client = Client(server, token, conn)
        assert client.call("/api/settings/autostart") == (
            200,
            {"available": True, "enabled": False},
        )
        status, out = client.call("/api/settings/autostart", "PUT", {"enabled": True})
        assert (status, out, registry) == (200, {"available": True, "enabled": True}, [True])
    finally:
        server.stop()
        conn.close()


def test_start_with_windows_is_not_offered_without_the_platform(client: Client) -> None:
    assert client.call("/api/settings/autostart") == (200, {"available": False, "enabled": False})
    assert client.call("/api/settings/autostart", "PUT", {"enabled": True})[0] == 404


def test_every_setting_is_known_to_the_config_and_its_choices_are_valid() -> None:
    cfg = Config()
    for s in SETTINGS:
        current_value(cfg, s.key)  # raises when the key does not exist in Config
        assert BY_KEY[s.key] is s
        if s.kind == "choice":
            assert current_value(cfg, s.key) in s.choices


# --- gain, voice level, device list ----------------------------------------------------------


def test_mic_gain_lifts_quiet_speech_over_the_energy_floor() -> None:
    quiet = speech(40, amplitude=0.06)  # the fake detector hears it; the floor below does not
    floor = AudioSettings(preroll_ms=96, hangover_ms=320, min_speech_ms=128, energy_floor=0.1)
    rig = RecorderRig("mic", floor)
    rig.deliver(quiet)
    rig.deliver(np.zeros(30 * 512, dtype=np.float32))
    assert rig.out == []
    assert rig.stats["dropped_quiet"] == 1

    amplified = RecorderRig("mic", replace(floor, mic_gain=3.0))
    amplified.deliver(quiet)
    amplified.deliver(np.zeros(30 * 512, dtype=np.float32))
    ((_, utterance),) = amplified.out
    assert float(utterance.samples.max()) == pytest.approx(0.18)


# --- restarting the processes --------------------------------------------------------------


def test_restart_replaces_both_processes_and_keeps_a_pause(paths: Paths) -> None:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    conn.close()
    spawned: list[str] = []

    def counting_spawn(spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        spawned.append(spec.name)
        return spawn_module(spec, data_dir)

    sup = Supervisor(paths.root, key, CHILDREN, spawn=counting_spawn)
    sup.start()
    try:
        assert wait_for(lambda: sup.states() == {"capture": "ready", "worker": "ready"}, 30)
        sup.send("pause")
        assert wait_for(lambda: sup.states() == {"capture": "paused", "worker": "paused"}, 30)
        sup.restart()
        assert spawned == ["capture", "worker", "capture", "worker"]
        assert wait_for(lambda: sup.states() == {"capture": "paused", "worker": "paused"}, 30)
    finally:
        sup.stop()


# --- retention -----------------------------------------------------------------------------


def test_retention_limits_are_settings_that_can_be_set_and_cleared(
    client: Client, paths: Paths
) -> None:
    fields = {f["key"]: f for f in client.call("/api/settings")[1]["fields"]}
    assert fields["retention.screenshots"]["kind"] == "days"
    assert fields["retention.screenshots"]["value"] is None  # forever
    status, out = client.save({"retention.screenshots": 30, "retention.raw_audio": 7})
    assert (status, sorted(out["changed"])) == (
        200,
        ["retention.raw_audio", "retention.screenshots"],
    )
    cfg = parse_config(paths.config_file.read_text("utf-8"))
    assert (cfg.retention_days["screenshots"], cfg.retention_days["raw_audio"]) == (30, 7)
    assert cfg.retention_days["transcripts"] is None
    assert client.save({"retention.screenshots": None})[1]["changed"] == ["retention.screenshots"]
    text = paths.config_file.read_text("utf-8")
    assert 'screenshots = "forever"' in text
    assert parse_config(text).retention_days["screenshots"] is None
    assert client.save({"retention.screenshots": None})[1]["changed"] == []


@pytest.mark.parametrize("bad", [0, 2.5, "30"])
def test_a_retention_limit_must_be_a_whole_number_of_days(client: Client, bad: object) -> None:
    status, out = client.save({"retention.screenshots": bad})
    assert status == 422
    assert "Keep screenshots" in out["detail"]


# Pins disk_warning_free_gb and low_space: delete or rewrite with M21.2.
def test_the_storage_report_lists_sizes_growth_and_free_space(client: Client, paths: Paths) -> None:
    (paths.media_dir / "2026" / "11" / "08").mkdir(parents=True)
    (paths.media_dir / "2026" / "11" / "08" / "a.webp.enc").write_bytes(b"x" * 1500)
    (paths.media_dir / "audio" / "2026" / "11" / "08").mkdir(parents=True)
    (paths.media_dir / "audio" / "2026" / "11" / "08" / "a.opus.enc").write_bytes(b"y" * 700)
    status, out = client.call("/api/settings/storage")
    assert status == 200
    by_kind = {t["kind"]: t for t in out["types"]}
    assert set(by_kind) == {
        "screenshots",
        "raw_audio",
        "frame_text",
        "transcripts",
        "embeddings",
        "episodes",
    }
    assert (by_kind["screenshots"]["bytes"], by_kind["raw_audio"]["bytes"]) == (1500, 700)
    assert by_kind["screenshots"]["bytes_7d"] == 1500  # written just now
    assert by_kind["screenshots"]["days"] is None
    assert out["database_bytes"] > 0
    assert out["total_bytes"] == 1500 + 700 + out["database_bytes"]
    assert out["free_bytes"] > 0
    assert out["low_space"] is False
    client.save({"disk_warning_free_gb": 100000})
    assert client.call("/api/settings/storage")[1]["low_space"] is True


def test_one_kind_of_data_can_be_deleted_with_its_files(client: Client, paths: Paths) -> None:
    assert client.frames is not None
    frame = NewFrame(1_000, 0, "click", "chrome.exe", "Tab", None, "text", "uia", b"webp")
    client.frames.add(frame)
    stored = latest_frames(client.conn, 1)[0]
    assert stored.image_path is not None
    assert (paths.media_dir / (stored.image_path + ".enc")).exists()
    status, out = client.call("/api/settings/storage/screenshots", "DELETE")
    assert status == 200
    assert out["deleted"] >= 1
    assert not any(paths.media_dir.rglob("*.enc"))
    assert client.call("/api/settings/storage/nonsense", "DELETE")[0] == 404


def test_everything_recorded_can_be_deleted_but_the_setup_stays(
    client: Client, paths: Paths
) -> None:
    assert client.frames is not None
    client.frames.add(NewFrame(1_000, 0, "click", "chrome.exe", "Tab", None, "text", "uia", b"x"))
    client.conn.execute("INSERT INTO speakers (name, is_self, created_ms) VALUES ('Minlo', 0, 0)")
    client.conn.execute("INSERT INTO questions (asked_ms, text) VALUES (0, 'what?')")
    status, out = client.call("/api/settings/storage", "DELETE")
    assert status == 200
    assert out["deleted"] >= 1
    assert out["types"]["screenshots"] >= 1
    for table in ("speakers", "questions"):
        assert client.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    assert not any(paths.media_dir.rglob("*.enc"))
    assert paths.config_file.exists()
    assert client.call("/api/settings/storage", "DELETE", token="wrong")[0] == 401  # noqa: S106


def test_media_only_deletion_keeps_searchable_text(client: Client, paths: Paths) -> None:
    assert client.frames is not None
    frame = client.frames.add(NewFrame(1000, 0, "click", text="retained screen", image_webp=b"x"))
    audio = AudioStore(client.conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))
    segment = audio.add(NewSegment("system", 1000, 5000, 4000), b"opus", 0)
    audio.set_transcript(segment, "done", "en", "retained speech")
    client.conn.execute("DELETE FROM jobs")
    assert client.call("/api/settings/storage/screenshots", "DELETE")[0] == 200
    assert client.call("/api/settings/storage/raw_audio", "DELETE")[0] == 200
    assert client.conn.execute(
        "SELECT text, image_path FROM frames WHERE id = ?", (frame,)
    ).fetchone() == ("retained screen", None)
    assert client.conn.execute(
        "SELECT transcript, audio_path FROM audio_segments WHERE id = ?", (segment,)
    ).fetchone() == ("retained speech", None)
    assert client.call("/api/settings/storage/transcripts", "DELETE")[0] == 200
    assert audio.get(segment) is None
