"""The manual smoke tests that can run themselves: real devices, real OCR, real GPU, real Whisper.

Not part of the normal run (they play a sentence through your speakers and use the GPU):

    uv run pytest -m hardware --no-cov -s

Each test skips itself when its prerequisite is missing (no microphone, model not downloaded...).
Nothing here needs you to speak or click. The only thing a person is still needed for is the
microphone with a real voice, which no software can stand in for (tests/manual/CHECKLIST.md,
"Audio and transcription").
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from akashic.capture.audio import AudioCapture, AudioSettings
from akashic.capture.exclusions import Exclusions
from akashic.capture.vad import SileroVad
from akashic.config import DEFAULT_WHISPER_MODEL, Paths, default_data_dir
from akashic.config import Exclusions as UserExclusions
from akashic.config import Scheduler as SchedulerSettings
from akashic.pipeline import models
from akashic.pipeline.jobs import Job, JobQueue
from akashic.pipeline.scheduler import Scheduler
from akashic.pipeline.speakers import OnnxVoiceModels, model_paths
from akashic.pipeline.transcribe import TranscribeHandler
from akashic.pipeline.whisper import WhisperTranscriber
from akashic.platform.win import build_audio_platform
from akashic.platform.win.load import WindowsLoad
from akashic.storage import db
from akashic.storage.audio import AudioStore
from akashic.storage.crypto import DataKey
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection

pytestmark = pytest.mark.hardware

SENTENCE = "Hello Minlo. This is a short test of the audio recorder."


def speak(text: str, volume: int = 70) -> None:
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Volume = {volume}; $s.Speak('{text}')"
    )
    subprocess.run(  # noqa: S603 - fixed program, text is ours
        ["powershell", "-NoProfile", "-Command", script],  # noqa: S607
        check=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def whisper_dir() -> Paths:
    """The data dir, once the model the transcription test loads (the default) is complete."""
    paths = Paths(default_data_dir())
    if not models.is_complete(paths.models_dir, models.SPEECH_CANDIDATES[DEFAULT_WHISPER_MODEL]):
        pytest.skip(f"{DEFAULT_WHISPER_MODEL} not downloaded (Settings > Models)")
    return paths


# --- devices, sessions, load -----------------------------------------------------------------


def test_default_devices_open_and_deliver_16khz_float_audio() -> None:
    platform = build_audio_platform()
    for kind in ("mic", "system"):
        device = platform.devices.find(kind, "")  # type: ignore[arg-type]
        if device is None:
            pytest.skip(f"no {kind} device on this machine")
        stream = platform.devices.open(kind, device)  # type: ignore[arg-type]
        try:
            total = 0
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                block = stream.read(0.1)
                if block is not None:
                    assert block.dtype == np.float32
                    assert block.ndim == 1
                    assert float(np.abs(block).max()) <= 1.0
                    total += len(block)
            # loopback may deliver nothing while silent; a microphone must deliver ~1 s
            assert total > 0 or kind == "system"
            if kind == "mic":
                assert 0.8 * 16_000 <= total <= 1.3 * 16_000
        finally:
            stream.close()
    assert isinstance(platform.sessions.active_processes(), frozenset)


def test_load_source_reports_sane_numbers() -> None:
    load = WindowsLoad()
    load.cpu_percent()
    time.sleep(0.5)
    assert 0.0 <= load.cpu_percent() <= 100.0
    gpu = load.gpu_percent()
    assert gpu is not None, "NVML should see the NVIDIA GPU"
    assert 0.0 <= gpu <= 100.0


def test_scheduler_uses_real_load_and_the_database(tmp_path: Path) -> None:
    """The verdict follows the real readings it reports, and a game row overrides them."""
    paths = Paths(tmp_path)
    paths.ensure()
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    try:
        settings = SchedulerSettings()
        scheduler = Scheduler(conn, WindowsLoad(), settings)
        now = int(time.time() * 1000)
        verdict = scheduler.heavy_allowed(now)
        assert verdict.gpu is not None, "NVML should see the NVIDIA GPU"
        assert verdict.cpu is not None
        assert 0.0 <= verdict.cpu <= 100.0
        assert 0.0 <= verdict.gpu <= 100.0
        expected = (
            "gpu busy"
            if verdict.gpu >= settings.gpu_busy_percent
            else "cpu busy"
            if verdict.cpu >= settings.cpu_busy_percent
            else ""
        )
        assert (verdict.allowed, verdict.reason) == (expected == "", expected)
        GameLog(conn).open(process="game.exe", window_title="G", ts_ms=now)
        assert scheduler.heavy_allowed(now + 1).reason == "game"
    finally:
        conn.close()


def test_windows_ocr_reads_rendered_text() -> None:
    from akashic.platform.win.ocr import WindowsOcr  # noqa: PLC0415

    image = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 40), "Victory Royale 2026", fill="black", font=ImageFont.load_default(size=64))
    text = WindowsOcr("fr").recognize(image)
    assert "victory" in text.lower()
    assert "royale" in text.lower()


def test_windows_ocr_reads_rendered_french_text() -> None:
    from akashic.capture.exclusions import fold  # noqa: PLC0415
    from akashic.platform.win.ocr import WindowsOcr, available_languages  # noqa: PLC0415

    if not any(tag.casefold().startswith("fr") for tag in available_languages()):
        pytest.skip("no French Windows OCR language pack installed")
    image = Image.new("RGB", (1400, 200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=64)
    draw.text((20, 40), "Réunion très tôt à côté", fill="black", font=font)
    ocr = WindowsOcr("fr")
    assert ocr.language.casefold().startswith("fr")
    text = fold(ocr.recognize(image))
    assert "reunion" in text
    assert "cote" in text


def test_french_sapi_voice_and_default_whisper(whisper_dir: Paths, tmp_path: Path) -> None:
    """Generated French speech tests installed native plumbing, not real-call accuracy."""
    voice_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$v = $s.GetInstalledVoices() | Where-Object {$_.Enabled -and "
        "$_.VoiceInfo.Culture.Name -like 'fr-*'} | Select-Object -First 1; "
        "if ($v) {$v.VoiceInfo.Name}; $s.Dispose()"
    )
    available = subprocess.run(  # noqa: S603 - fixed native voice inventory
        ["powershell", "-NoProfile", "-Command", voice_script],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    if not available:
        pytest.skip("no enabled French SAPI voice installed")
    wave = tmp_path / "invented-french.wav"
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SelectVoice('{available.replace(chr(39), chr(39) * 2)}'); "
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, 16, 1); "
        f"$s.SetOutputToWaveFile('{wave}',$f); "
        "$s.Speak('Le nouveau jeu arrive demain. Je ne veux pas partir.'); $s.Dispose()"
    )
    subprocess.run(  # noqa: S603 - generated speech and a disposable fixture path
        ["powershell", "-NoProfile", "-Command", script],  # noqa: S607
        check=True,
        timeout=60,
    )
    import wave as wav  # noqa: PLC0415

    with wav.open(str(wave)) as stream:
        assert stream.getframerate() == 16_000
        samples = np.frombuffer(stream.readframes(stream.getnframes()), np.int16).astype(
            np.float32
        ) / np.float32(32768)
    transcriber = WhisperTranscriber(whisper_dir.models_dir / DEFAULT_WHISPER_MODEL, 0.0)
    try:
        result = transcriber.transcribe(samples, None)
        assert result is not None
        assert result.language == "fr"
        assert "jeu" in result.text.lower()
        assert "pas" in result.text.lower()
    finally:
        transcriber.unload_if_idle(force=True)


def test_selected_window_pixels_survive_verified_occlusion() -> None:
    """Two disposable native windows: screenshot only the synthetic red target, behind blue."""
    from akashic.platform.win.screen import GdiScreen, set_dpi_aware  # noqa: PLC0415
    from akashic.platform.win.windows import Win32Windows  # noqa: PLC0415

    set_dpi_aware()  # match build_capture_platform before comparing physical DWM/Win32 bounds
    script = (
        "import tkinter as t, ctypes, sys; r=t.Tk(); r.title('Akashic synthetic hardware test'); "
        "r.geometry('500x300+120+120'); r.configure(bg=sys.argv[1]); "
        "r.attributes('-topmost',True); r.update(); "
        "u=ctypes.windll.user32; u.GetAncestor.restype=ctypes.c_void_p; "
        "u.GetAncestor.argtypes=[ctypes.c_void_p,ctypes.c_uint]; "
        "print(u.GetAncestor(r.winfo_id(),2),flush=True); r.after(20000,r.destroy); r.mainloop()"
    )
    processes: list[subprocess.Popen[str]] = []
    target = None
    try:
        for color in ("red", "blue"):
            process = subprocess.Popen(  # noqa: S603 - owned synthetic windows only
                [sys.executable, "-c", script, color],
                stdout=subprocess.PIPE,
                text=True,
            )
            processes.append(process)
            assert process.stdout is not None
            hwnd = int(process.stdout.readline().strip())
            window = Win32Windows().inspect_window(hwnd)
            assert window is not None
            if color == "red":
                target = window
        at = Win32Windows().window_at(350, 250)
        assert at is not None
        assert at.hwnd == hwnd, "blue window must actually cover the selected red target"
        assert target is not None
        image = GdiScreen().grab_window(target)
        assert image is not None
        red, green, blue = np.asarray(image)[image.height // 2, image.width // 2, :3]
        assert red > 200
        assert green < 30
        assert blue < 30
    finally:
        for process in reversed(processes):
            process.terminate()
            process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()


# --- the whole audio chain -------------------------------------------------------------------


def record_a_spoken_sentence(conn: Connection, paths: Paths, key: DataKey) -> AudioStore:
    store = AudioStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    platform = build_audio_platform()
    capture = AudioCapture(
        devices=platform.devices,
        sessions=platform.sessions,
        detector_factory=SileroVad,
        store=store,
        exclusions=Exclusions(UserExclusions()),
        settings=AudioSettings(),
    )
    stop, paused = threading.Event(), threading.Event()
    thread = threading.Thread(target=capture.run, args=(stop, paused))
    thread.start()
    time.sleep(2)
    speak(SENTENCE)
    time.sleep(3)  # lets the end-of-speech silence close the segment
    stop.set()
    thread.join(timeout=30)
    return store


def test_spoken_sentence_is_recorded_from_loopback_and_transcribed_by_whisper(
    whisper_dir: Paths, tmp_path: object
) -> None:
    """Windows' own voice speaks; loopback + VAD + Opus + DB + Whisper on the GPU turn it back
    into text. Covers the device and transcription parts of tests/manual/CHECKLIST.md "Audio and
    transcription" without a human."""
    paths = Paths(tmp_path)  # type: ignore[arg-type]
    paths.ensure()
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    store = record_a_spoken_sentence(conn, paths, key)

    rows = conn.execute(
        "SELECT id FROM audio_segments WHERE stream = 'system' ORDER BY start_ms"
    ).fetchall()
    if not rows:
        pytest.skip("nothing reached the default playback device's loopback (muted output?)")
    segments = [store.get(r[0]) for r in rows]
    assert any("powershell.exe" in s.sources for s in segments if s), "speaker not attributed"

    transcriber = WhisperTranscriber(
        whisper_dir.models_dir / DEFAULT_WHISPER_MODEL, idle_unload_s=60.0
    )
    handler = TranscribeHandler(store, transcriber, ("Minlo",), "test-model")
    started = time.monotonic()
    queue = JobQueue(conn)
    while job := queue.claim([handler.kind], 2**62):
        assert isinstance(job, Job)
        handler.run(job)
        queue.complete(job.id, 0)
    print(f"\ntranscribed {len(segments)} segment(s) in {time.monotonic() - started:.1f} s")  # noqa: T201
    texts = [s.transcript or "" for s in (store.get(r[0]) for r in rows) if s]
    joined = " ".join(texts).lower()
    # The real endpoint may mix unrelated audio with SAPI: never log its decoded content,
    # including through pytest's assertion-value formatting on a failed native check.
    recognized = "recorder" in joined and "short test" in joined
    assert recognized, "expected synthetic speech was not recognized"
    languages = {s.language for s in (store.get(r[0]) for r in rows) if s and s.transcript}
    assert languages == {"en"}
    conn.close()


def test_speaker_models_tell_two_windows_voices_apart(tmp_path: Path) -> None:
    """Two voices read the same text to wav files; their embeddings must separate cleanly and a
    recording holding both must come out as turns of both. Needs the two ONNX models."""
    paths = Paths(default_data_dir())
    seg, emb = model_paths(paths.models_dir)
    voices = OnnxVoiceModels(seg, emb, idle_unload_s=60.0)
    if not voices.available():
        pytest.skip("speaker models not downloaded (scripts/fetch_models.py)")
    texts = (
        "Yesterday afternoon we played a long game together and then talked about the update.",
        "I think the second boss is much harder than the first one, especially without healing.",
    )
    clips: dict[str, list[np.ndarray]] = {}
    for voice in ("Microsoft David Desktop", "Microsoft Zira Desktop"):
        clips[voice] = []
        for i, text in enumerate(texts):
            wav = tmp_path / f"{voice.split()[1]}_{i}.wav"
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.SelectVoice('{voice}'); "
                "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, 16, 1); "
                f"$s.SetOutputToWaveFile('{wav}', $f); $s.Speak('{text}'); $s.Dispose()"
            )
            subprocess.run(  # noqa: S603 - fixed program, text is ours
                ["powershell", "-NoProfile", "-Command", script],  # noqa: S607
                check=True,
                timeout=60,
            )
            pcm = np.frombuffer(wav.read_bytes()[44:], dtype=np.int16)
            clips[voice].append(pcm.astype(np.float32) / 32768)
    david, zira = clips.values()
    d0, d1, z0 = (voices.embed(c) for c in (david[0], david[1], zira[0]))
    assert float(d0 @ d1) > 0.7
    assert float(d0 @ z0) < 0.4
    gap = np.zeros(8_000, dtype=np.float32)
    turns = voices.turns(np.concatenate([david[0], gap, zira[0]]))
    long = [t for t in turns if t.speech_ms >= 1_000]
    assert len(long) >= 2
    first, last = voices.embed(long[0].samples), voices.embed(long[-1].samples)
    assert float(first @ d0) > 0.7
    assert float(last @ z0) > 0.7


def test_llama_server_embeds_french_and_english_close_together() -> None:
    """The pinned llama-server starts through the real launcher and the embedding model puts a
    French and an English sentence about the same thing closer than an unrelated one."""
    from akashic.pipeline.llm import EMBEDDING_FLAGS, LlamaEmbedder, LlamaServer  # noqa: PLC0415
    from akashic.platform.win.process import launch  # noqa: PLC0415

    paths = Paths(default_data_dir())
    program = models.runtime_program(paths.runtime_dir, models.LLAMA_RUNTIME)
    if not program.is_file() or not models.is_complete(paths.models_dir, models.QWEN3_EMBEDDING):
        pytest.skip("llama.cpp runtime or embedding model not downloaded")
    server = LlamaServer(
        program,
        paths.models_dir,
        launch,
        presets={models.EMBEDDING_TEXT_MODEL: EMBEDDING_FLAGS},
        preset_file=paths.runtime_dir / "models.ini",
    )
    embedder = LlamaEmbedder(server, models.EMBEDDING_TEXT_MODEL)
    try:
        french, english, unrelated = embedder.embed_documents(
            [
                "Minlo m'a parlé d'un nouveau jeu de survie.",
                "Minlo told me about a new survival game.",
                "La recette de la tarte aux pommes demande une heure de cuisson.",
            ]
        )
        query = embedder.embed_query("something about a game")
        assert french.shape == (1024,)
        assert float(french @ english) > float(french @ unrelated) + 0.2
        assert float(query @ english) > float(query @ unrelated)
    finally:
        server.stop()


def test_llama_server_plans_and_answers_with_the_chat_model(tmp_path: Path) -> None:
    """The chat model named in the config loads next to the embedding model (per-model flags from
    the preset file), fills a JSON schema, streams an answer, and the embedder still works after
    the router swapped models."""
    import json  # noqa: PLC0415

    from akashic.config import DEFAULT_CHAT_MODEL  # noqa: PLC0415
    from akashic.pipeline.llm import (  # noqa: PLC0415
        CHAT_FLAGS,
        EMBEDDING_FLAGS,
        LlamaChat,
        LlamaEmbedder,
        LlamaServer,
    )
    from akashic.platform.win.process import launch  # noqa: PLC0415
    from akashic.search.planner import PLAN_SCHEMA, plan_prompt  # noqa: PLC0415

    paths = Paths(default_data_dir())
    program = models.runtime_program(paths.runtime_dir, models.LLAMA_RUNTIME)
    chat_dir = paths.models_dir / DEFAULT_CHAT_MODEL
    if not program.is_file() or not any(chat_dir.glob("*.gguf")):
        pytest.skip("llama.cpp runtime or chat model not downloaded")
    server = LlamaServer(
        program,
        paths.models_dir,
        launch,
        presets={models.EMBEDDING_TEXT_MODEL: EMBEDDING_FLAGS, DEFAULT_CHAT_MODEL: CHAT_FLAGS},
        preset_file=tmp_path / "models.ini",
    )
    chat = LlamaChat(server, DEFAULT_CHAT_MODEL)
    try:
        system, user = plan_prompt(
            "What did Minlo say yesterday afternoon about a game?", ["Minlo"]
        )
        plan = json.loads(chat.complete_json(system, user, PLAN_SCHEMA))
        assert plan["people"] == ["Minlo"]
        assert "yesterday" in plan["time_text"].lower()
        text = "".join(
            chat.stream("Answer in one short sentence.", "What is the capital of France?")
        )
        assert "paris" in text.lower()
        vector = LlamaEmbedder(server, models.EMBEDDING_TEXT_MODEL).embed_query("a game")
        assert vector.shape == (1024,)
    finally:
        server.stop()
