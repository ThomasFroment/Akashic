"""Audio inside the capture process, the real voice detector, and the small pieces around them."""

from __future__ import annotations

import io
import threading
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pytest
from audio_fakes import FakeDetector, FakeDevices, FakeSessions, speech
from capture_fakes import (
    LONG_TEXT,
    FakeMedia,
    FakeOcr,
    FakeScreen,
    FakeUia,
    FakeWindows,
    window,
)

from akashic import child, ipc
from akashic.capture import main as capture_main
from akashic.capture import vad
from akashic.capture.audio import AudioSettings
from akashic.capture.exclusions import Exclusions
from akashic.capture.ports import AudioPlatform, CapturePlatform
from akashic.config import Audio, Config
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.debuginfo import ProcessInfo

if TYPE_CHECKING:
    from pathlib import Path

    from akashic.capture.vad import SpeechDetector
    from akashic.config import Paths


def _platform() -> CapturePlatform:
    return CapturePlatform(
        screen=FakeScreen(),
        windows=FakeWindows(focused=window()),
        uia=FakeUia(texts={1: LONG_TEXT}),
        ocr=FakeOcr(),
        media=FakeMedia(),
    )


def _run_capture(paths: Paths, key: DataKey, **kw: object) -> int:
    """Start the capture child paused (so no screen frames), run ``body`` briefly, stop."""

    def body(ctx: child.ChildContext) -> None:
        threading.Timer(0.7, ctx.stop.set).start()
        capture_main.run_capture(ctx, _platform(), **kw)  # type: ignore[arg-type]

    buf = io.BytesIO()
    ipc.send_command(buf, "pause")
    return child.run_child(
        "capture",
        ["--data-dir", str(paths.root)],
        body,
        io.BytesIO(key.raw() + buf.getvalue()),
        io.BytesIO(),
    )


@pytest.fixture
def key(paths: Paths) -> DataKey:
    k = DataKey.generate()
    conn = db.connect(paths.db_file, k.subkey("db"))
    db.migrate(conn)
    conn.close()
    return k


def test_capture_starts_audio_and_pause_keeps_the_microphone_closed(
    paths: Paths, key: DataKey
) -> None:
    devices = FakeDevices()
    devices.scripts["mic"].append([speech(40)])
    code = _run_capture(
        paths,
        key,
        audio_platform=AudioPlatform(devices, FakeSessions()),
        detector_factory=FakeDetector,
    )
    assert code == child.EXIT_OK
    assert devices.opened == []  # started paused: no device was even opened
    log = (paths.logs_dir / "capture.log").read_text("utf-8")
    assert "audio recording running" in log
    assert "audio stats" in log
    conn = db.connect(paths.db_file, key.subkey("db"))
    (info,) = [i for i in ProcessInfo(conn).read_all() if i.name == "capture"]
    conn.close()
    audio = cast("dict[str, Any]", info.data["audio"])
    assert set(audio["streams"]) == {"mic", "system"}
    assert audio["energy_floor"] == AudioSettings().energy_floor
    assert audio["streams"]["mic"]["device"] is None  # started paused: no device open


def test_audio_that_cannot_start_does_not_stop_screen_capture(paths: Paths, key: DataKey) -> None:
    def broken() -> SpeechDetector:
        raise FileNotFoundError

    code = _run_capture(
        paths,
        key,
        audio_platform=AudioPlatform(FakeDevices(), FakeSessions()),
        detector_factory=broken,
    )
    assert code == child.EXIT_OK
    log = (paths.logs_dir / "capture.log").read_text("utf-8")
    assert "audio recording unavailable: FileNotFoundError" in log
    assert "capture stats" in log  # the screen side still ran and shut down normally


def test_audio_can_be_switched_off_in_the_config(paths: Paths, key: DataKey) -> None:
    (paths.root / "config.toml").write_text("[audio]\nenabled = false\n", encoding="utf-8")
    devices = FakeDevices()
    code = _run_capture(
        paths,
        key,
        audio_platform=AudioPlatform(devices, FakeSessions()),
        detector_factory=FakeDetector,
    )
    assert code == child.EXIT_OK
    assert "audio recording running" not in (paths.logs_dir / "capture.log").read_text("utf-8")
    assert not Config(audio=Audio(enabled=False)).audio.enabled


# --- the real Silero model -----------------------------------------------------------------


def test_silero_finds_no_speech_in_silence_and_noise() -> None:
    detector = vad.SileroVad()
    rng = np.random.default_rng(1)
    assert detector.probability(np.zeros(vad.WINDOW, dtype=np.float32)) < 0.2
    noise = (rng.standard_normal(vad.WINDOW) * 0.01).astype(np.float32)
    assert detector.probability(noise) < 0.5


def test_silero_state_is_reset_and_input_is_checked() -> None:
    detector = vad.SileroVad()
    window = (np.random.default_rng(2).standard_normal(vad.WINDOW) * 0.05).astype(np.float32)
    first = detector.probability(window)
    detector.probability(window)
    detector.reset()
    assert detector.probability(window) == pytest.approx(first)
    with pytest.raises(ValueError, match="window"):
        detector.probability(np.zeros(100, dtype=np.float32))


def test_silero_model_is_found_inside_the_faster_whisper_package() -> None:
    path = vad.silero_model_path()
    assert path.name == "silero_vad_v6.onnx"
    assert path.is_file()


def test_missing_model_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def nothing_installed(name: str) -> None:
        del name

    class Spec:
        origin = str(tmp_path / "faster_whisper" / "__init__.py")

    def installed_without_the_file(name: str) -> Spec:
        del name
        return Spec()

    monkeypatch.setattr(vad.importlib.util, "find_spec", nothing_installed)
    with pytest.raises(FileNotFoundError, match="faster-whisper"):
        vad.silero_model_path()
    monkeypatch.setattr(vad.importlib.util, "find_spec", installed_without_the_file)
    with pytest.raises(FileNotFoundError, match="not found"):
        vad.silero_model_path()


# --- exclusions by process -----------------------------------------------------------------


def test_check_process_covers_password_managers_and_user_rules() -> None:
    rules = Exclusions(UserExclusions(processes=("spotify*",)))
    assert rules.check_process("KeePassXC.exe") == "password_manager"
    assert rules.check_process("consent.exe") == "system_secret"
    assert rules.check_process("Spotify.exe") == "user_process"
    assert rules.check_process("discord.exe") is None
    assert rules.check_process("") is None


def test_capture_process_never_loads_cuda_libraries_for_audio() -> None:
    """The voice detector runs on onnxruntime's CPU provider: no CUDA DLL may get loaded."""
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import textwrap  # noqa: PLC0415

    code = textwrap.dedent(
        """
        import ctypes, os, sys
        from ctypes import wintypes
        import numpy as np
        from akashic.capture.vad import SileroVad, WINDOW
        v = SileroVad()
        v.probability(np.zeros(WINDOW, np.float32))
        psapi, k32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
        ptr, dword = ctypes.c_void_p, wintypes.DWORD
        k32.GetCurrentProcess.restype = ptr
        psapi.EnumProcessModules.argtypes = [ptr, ptr, dword, ctypes.POINTER(dword)]
        psapi.GetModuleFileNameExW.argtypes = [ptr, ptr, wintypes.LPWSTR, dword]
        mods, need = (ptr * 2048)(), dword()
        me = k32.GetCurrentProcess()
        psapi.EnumProcessModules(me, mods, ctypes.sizeof(mods), ctypes.byref(need))
        buf = ctypes.create_unicode_buffer(260)
        names = []
        for m in mods[: need.value // ctypes.sizeof(ptr)]:
            psapi.GetModuleFileNameExW(me, m, buf, 260)
            names.append(os.path.basename(buf.value).lower())
        cuda = ("cuda", "cublas", "cudnn", "nvrtc", "cufft", "ctranslate")
        bad = [n for n in names if any(t in n for t in cuda)]
        banned = {"ctranslate2", "faster_whisper", "torch"} & set(sys.modules)
        print("BAD", bad, sorted(banned))
        """
    )
    result = subprocess.run(  # noqa: S603 - fixed interpreter and inline code
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip().endswith("BAD [] []"), result.stdout + result.stderr
