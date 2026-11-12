"""The faster-whisper adapter (against a stand-in module) and the worker process body."""

from __future__ import annotations

import io
import math
import os
import sys
import types
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from whisper_fakes import small_whisper_spec, stage_whisper_model

from akashic import child
from akashic.pipeline import main as worker
from akashic.pipeline import whisper
from akashic.pipeline.jobs import JobQueue, NewJob
from akashic.pipeline.whisper import WhisperTranscriber
from akashic.storage import db
from akashic.storage.crypto import DataKey

if TYPE_CHECKING:
    from pathlib import Path

    from numpy.typing import NDArray

    from akashic.config import Paths


class Piece:
    def __init__(
        self, text: str, start: float = 0.0, end: float = 1.0, avg_logprob: float = -0.2
    ) -> None:
        self.text, self.start, self.end, self.avg_logprob = text, start, end, avg_logprob


class FakeModel:
    instances: list[FakeModel] = []  # noqa: RUF012 - test double

    def __init__(self, path: str, **kwargs: Any) -> None:  # noqa: ANN401
        self.path, self.kwargs = path, kwargs
        self.transcribe_kwargs: dict[str, Any] = {}
        self.language_probs = [("fr", 0.3), ("de", 0.5), ("en", 0.1)]
        self.pieces = [Piece(" Salut "), Piece("Minlo ")]
        FakeModel.instances.append(self)

    def detect_language(
        self, samples: NDArray[np.float32]
    ) -> tuple[str, float, list[tuple[str, float]]]:
        del samples
        return "de", 0.5, self.language_probs

    def transcribe(
        self,
        samples: NDArray[np.float32],
        **kwargs: Any,  # noqa: ANN401
    ) -> tuple[list[Piece], None]:
        del samples
        self.transcribe_kwargs = kwargs
        return self.pieces, None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


AUDIO = np.zeros(16_000, np.float32)


@pytest.fixture
def fake_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> type[FakeModel]:
    FakeModel.instances = []
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(whisper, "add_cuda_dll_directories", lambda: None)
    return FakeModel


def test_model_availability_follows_the_pinned_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = small_whisper_spec(monkeypatch)
    transcriber = WhisperTranscriber(tmp_path / spec.key, 60.0)
    assert not transcriber.available()
    folder = stage_whisper_model(monkeypatch, tmp_path)
    assert transcriber.available()
    (folder / spec.files[0].name).write_bytes(b"short")
    assert not transcriber.available()
    assert not WhisperTranscriber(tmp_path / "unknown-model", 60.0).available()


def test_french_is_chosen_over_a_likelier_third_language(
    fake_faster_whisper: type[FakeModel], tmp_path: Path
) -> None:
    transcriber = WhisperTranscriber(tmp_path, 60.0)
    result = transcriber.transcribe(AUDIO, "Minlo")
    assert result is not None
    assert (result.text, result.language) == ("Salut Minlo", "fr")
    (model,) = fake_faster_whisper.instances
    assert model.kwargs == {"device": "cuda", "compute_type": "float16", "local_files_only": True}
    kw = model.transcribe_kwargs
    assert kw["language"] == "fr"
    assert kw["initial_prompt"] == "Minlo"
    assert kw["vad_filter"] is False
    assert kw["condition_on_previous_text"] is False
    assert kw["no_speech_threshold"] == 0.6
    assert kw["log_prob_threshold"] == -2.0
    assert kw["suppress_blank"] is True


def test_english_wins_when_it_is_likelier_than_french(
    fake_faster_whisper: type[FakeModel], tmp_path: Path
) -> None:
    transcriber = WhisperTranscriber(tmp_path, 60.0)
    transcriber.transcribe(AUDIO, None)
    fake_faster_whisper.instances[0].language_probs = [("en", 0.6), ("fr", 0.2)]
    result = transcriber.transcribe(AUDIO, None)
    assert result is not None
    assert result.language == "en"
    assert len(fake_faster_whisper.instances) == 1  # the model stays loaded between segments


def test_silence_gives_no_transcript(fake_faster_whisper: type[FakeModel], tmp_path: Path) -> None:
    transcriber = WhisperTranscriber(tmp_path, 60.0)
    transcriber.transcribe(AUDIO, None)
    fake_faster_whisper.instances[0].pieces = [Piece("  ")]
    assert transcriber.transcribe(AUDIO, None) is None


def test_model_is_unloaded_after_the_idle_time(
    fake_faster_whisper: type[FakeModel], tmp_path: Path
) -> None:
    clock = Clock()
    transcriber = WhisperTranscriber(tmp_path, idle_unload_s=600.0, clock=clock)
    transcriber.unload_if_idle()  # nothing loaded: nothing to do
    transcriber.transcribe(AUDIO, None)
    clock.now = 599.0
    transcriber.unload_if_idle()
    assert transcriber._model is not None
    transcriber.unload_if_idle(force=True)  # a game started: no waiting
    assert transcriber._model is None
    transcriber.transcribe(AUDIO, None)
    clock.now = 1200.0
    transcriber.unload_if_idle()
    assert transcriber._model is None
    transcriber.transcribe(AUDIO, None)  # loads again on demand
    assert len(fake_faster_whisper.instances) == 3


def test_cuda_dll_directories_are_added_for_every_nvidia_wheel_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "cu13" / "bin" / "x86_64").mkdir(parents=True)
    (tmp_path / "cu13" / "bin" / "x86_64" / "cublas64_13.dll").write_bytes(b"")
    (tmp_path / "cudnn" / "bin").mkdir(parents=True)
    (tmp_path / "cudnn" / "bin" / "cudnn64_9.dll").write_bytes(b"")
    (tmp_path / "empty").mkdir()
    spec = types.SimpleNamespace(submodule_search_locations=[str(tmp_path)])
    added: list[str] = []

    def find_spec(name: str) -> object:
        del name
        return spec

    def add_dll_directory(path: str) -> None:
        added.append(path)

    monkeypatch.setattr(whisper.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(whisper.os, "add_dll_directory", add_dll_directory)
    monkeypatch.setenv("PATH", "ORIGINAL")
    whisper.add_cuda_dll_directories()
    assert os.environ["PATH"].endswith(os.pathsep + "ORIGINAL")
    assert str(tmp_path / "cudnn" / "bin") in os.environ["PATH"]
    assert sorted(added) == sorted(
        [str(tmp_path / "cu13" / "bin" / "x86_64"), str(tmp_path / "cudnn" / "bin")]
    )

    def find_nothing(name: str) -> None:
        del name

    monkeypatch.setattr(whisper.importlib.util, "find_spec", find_nothing)
    whisper.add_cuda_dll_directories()  # no nvidia wheels at all: fine


# --- the worker process body ---------------------------------------------------------------


def test_no_load_reports_nothing_busy() -> None:
    load = worker._NoLoad()
    assert (load.cpu_percent(), load.gpu_percent()) == (0.0, None)


def run_worker_body(paths: Paths, key: DataKey) -> int:
    return child.run_child(
        "worker",
        ["--data-dir", str(paths.root)],
        worker._body,
        io.BytesIO(key.raw()),  # EOF right after the key: the child stops itself
        io.BytesIO(),
    )


def test_worker_body_puts_interrupted_jobs_back_and_stops(paths: Paths) -> None:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    queue = JobQueue(conn)
    queue.enqueue(NewJob("transcribe", "transcribe:1", 1, {"segment_id": 1}), now_ms=0)
    assert queue.claim(["transcribe"], 0) is not None  # "running" when the previous worker died
    conn.close()
    assert run_worker_body(paths, key) == child.EXIT_OK
    assert "1 interrupted job(s) put back" in (paths.logs_dir / "worker.log").read_text("utf-8")
    reopened = db.connect(paths.db_file, key.subkey("db"))
    try:
        # Back in the queue, not left "running" (which nothing would ever pick up again).
        assert reopened.execute("SELECT state, attempts FROM jobs").fetchall() == [("pending", 1)]
    finally:
        reopened.close()


def test_worker_body_copes_without_load_measurement(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    conn.close()

    def broken(*args: object) -> None:
        del args
        raise OSError

    monkeypatch.setattr("akashic.platform.win.load.WindowsLoad.__init__", broken)
    assert run_worker_body(paths, key) == child.EXIT_OK
    assert "load measurement unavailable" in (paths.logs_dir / "worker.log").read_text("utf-8")


def test_confidence_weighs_each_piece_by_its_duration() -> None:
    pieces = [Piece("a", 0.0, 3.0, -0.1), Piece("b", 3.0, 4.0, -1.3)]
    assert whisper.confidence(pieces) == pytest.approx(math.exp((-0.3 - 1.3) / 4))
    assert whisper.confidence([]) is None
    assert whisper.confidence([Piece("x", 1.0, 1.0, -0.5)]) == pytest.approx(math.exp(-0.5))
