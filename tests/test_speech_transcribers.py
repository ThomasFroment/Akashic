"""Speech-model selection and the Transformers adapter (Qwen3-ASR, Canary), against stand-in
``torch``/``transformers`` modules: no GPU, no model files, no network."""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import sys
import types
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pytest

from akashic.config import WHISPER_LARGE_V3_MODEL, WHISPER_TURBO_MODEL
from akashic.pipeline import speech
from akashic.pipeline.models import CANARY_V2, QWEN3_ASR, SPEECH_CANDIDATES, ModelFile
from akashic.pipeline.speech import GPU_FRACTION, MAX_TOKENS, TransformerTranscriber
from akashic.pipeline.whisper import WhisperTranscriber

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path


AUDIO = np.zeros(16_000, np.float32)
HF_LOGGERS = ("transformers", "huggingface_hub")
SMALL_FILES = (ModelFile("config.json", "0" * 64, 3), ModelFile("model.safetensors", "0" * 64, 5))


# --- stand-ins -----------------------------------------------------------------------------


class FakeCuda:
    def __init__(self) -> None:
        self.available = True
        self.fraction: float | None = None
        self.emptied = 0

    def is_available(self) -> bool:
        return self.available

    def set_per_process_memory_fraction(self, fraction: float) -> None:
        self.fraction = fraction

    def empty_cache(self) -> None:
        self.emptied += 1


class FakeTorch(types.ModuleType):
    bfloat16 = "bfloat16"
    float32 = "float32"

    def __init__(self) -> None:
        super().__init__("torch")
        self.cuda = FakeCuda()
        self.threads: int | None = None
        self.inference_depth = 0
        self.inference_calls = 0

    def set_num_threads(self, threads: int) -> None:
        self.threads = threads

    @contextlib.contextmanager
    def inference_mode(self) -> Generator[None]:
        self.inference_calls += 1
        self.inference_depth += 1
        try:
            yield
        finally:
            self.inference_depth -= 1


class FakeInputs(dict[str, object]):
    def __init__(self, torch: FakeTorch, **values: object) -> None:
        super().__init__(values)
        self.torch = torch
        self.moved_to: tuple[object, ...] = ()

    def to(self, *args: object) -> FakeInputs:
        self.moved_to = args
        return self


@dataclasses.dataclass
class FakeOutput:
    sequences: str
    scores: str


class FakeScores:
    def __init__(self, value: float) -> None:
        self.value = value

    def float(self) -> FakeScores:
        return self

    def mean(self) -> FakeScores:
        return self

    def item(self) -> float:
        return self.value


PROMPT_TOKENS = 3


class FakeProcessor:
    def __init__(self, torch: FakeTorch) -> None:
        self.torch = torch
        self.requests: list[dict[str, object]] = []
        self.decoded: list[object] = []
        # Qwen: what the parsed output says; Canary: the text decoded per source language.
        self.parsed: dict[str, object] = {"transcription": " Salut Minlo ", "language": "French"}
        self.texts: dict[str, str] = {"fr": " Salut Minlo ", "en": " Salute me low "}

    def apply_transcription_request(self, **request: object) -> FakeInputs:
        assert self.torch.inference_depth > 0, "inference must run under inference_mode()"
        self.requests.append(request)
        if "source_language" in request:
            return FakeInputs(self.torch, language=request["source_language"])
        return FakeInputs(self.torch, input_ids=np.zeros((1, PROMPT_TOKENS), np.int64))

    def decode(self, tokens: object, **kwargs: object) -> list[object]:
        self.decoded.append(tokens)
        if kwargs.get("return_format") == "parsed":
            return [self.parsed]
        assert kwargs == {"skip_special_tokens": True}
        assert isinstance(tokens, str)
        return [self.texts[tokens]]


class FakeModel:
    def __init__(self, torch: FakeTorch, dtype: object) -> None:
        self.torch = torch
        self.dtype = dtype
        self.device = "cuda:0"
        self.moved_to: str | None = None
        self.evaluated = False
        self.generate_calls: list[dict[str, object]] = []
        self.scores = {"fr": -0.2, "en": -0.9}

    def to(self, device: str) -> FakeModel:
        self.moved_to = device
        return self

    def eval(self) -> FakeModel:
        self.evaluated = True
        return self

    def generate(self, **kwargs: object) -> object:
        self.generate_calls.append(kwargs)
        if "language" in kwargs:  # Canary
            language = str(kwargs["language"])
            return FakeOutput(sequences=language, scores=language)
        # Qwen: the prompt tokens followed by four generated ones
        return np.arange(PROMPT_TOKENS + 4).reshape(1, -1)

    def compute_transition_scores(
        self, sequences: str, scores: str, *, normalize_logits: bool
    ) -> FakeScores:
        assert normalize_logits
        assert sequences == scores
        return FakeScores(self.scores[sequences])


class FakeAuto:
    """One ``Auto*`` class: records ``from_pretrained`` and builds the stand-in."""

    def __init__(self, torch: FakeTorch, *, processor: bool = False) -> None:
        self.torch = torch
        self.processor = processor
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.built: list[FakeModel | FakeProcessor] = []

    def from_pretrained(self, path: str, **kwargs: object) -> object:
        self.calls.append((path, kwargs))
        built = (
            FakeProcessor(self.torch)
            if self.processor
            else FakeModel(self.torch, kwargs.get("dtype"))
        )
        self.built.append(built)
        return built


class FakeHfLogging:
    def __init__(self) -> None:
        self.progress_disabled = 0

    def disable_progress_bar(self) -> None:
        self.progress_disabled += 1


class FakeHfUtils:
    def __init__(self) -> None:
        self.logging = FakeHfLogging()


class FakeTransformers(types.ModuleType):
    def __init__(self, torch: FakeTorch) -> None:
        super().__init__("transformers")
        self.utils = FakeHfUtils()
        self.AutoProcessor = FakeAuto(torch, processor=True)
        self.AutoModelForMultimodalLM = FakeAuto(torch)
        self.AutoModelForSpeechSeq2Seq = FakeAuto(torch)


@dataclasses.dataclass
class Libs:
    torch: FakeTorch
    hf: FakeTransformers

    def models(self) -> list[FakeModel]:
        return [
            m
            for auto in (self.hf.AutoModelForMultimodalLM, self.hf.AutoModelForSpeechSeq2Seq)
            for m in auto.built
            if isinstance(m, FakeModel)
        ]

    def processor(self) -> FakeProcessor:
        built = self.hf.AutoProcessor.built[-1]
        assert isinstance(built, FakeProcessor)
        return built


@pytest.fixture(autouse=True)
def _restore_hf_loggers() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction] - autouse
    """Loading silences these process-wide loggers; give the next test the originals back."""
    saved = {
        n: (list(logging.getLogger(n).handlers), logging.getLogger(n).propagate) for n in HF_LOGGERS
    }
    yield
    for name, (handlers, propagate) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate


@pytest.fixture
def small_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real model keys with two tiny files each, so availability needs no gigabytes."""
    specs = dict(SPEECH_CANDIDATES)
    for spec in (QWEN3_ASR, CANARY_V2):
        specs[spec.key] = dataclasses.replace(spec, files=SMALL_FILES)
    monkeypatch.setattr(speech, "SPEECH_CANDIDATES", specs)


@pytest.fixture
def libs(monkeypatch: pytest.MonkeyPatch, small_specs: None) -> Libs:
    del small_specs
    torch = FakeTorch()
    hf = FakeTransformers(torch)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", hf)
    return Libs(torch, hf)


def model_folder(tmp_path: Path, key: str, files: tuple[ModelFile, ...] = SMALL_FILES) -> Path:
    folder = tmp_path / key
    folder.mkdir()
    for file in files:
        (folder / file.name).write_bytes(b"x" * file.size)
    return folder


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# --- model selection -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (QWEN3_ASR.key, TransformerTranscriber),
        (CANARY_V2.key, TransformerTranscriber),
        (WHISPER_LARGE_V3_MODEL, WhisperTranscriber),
        (WHISPER_TURBO_MODEL, WhisperTranscriber),
        ("some-unknown-folder", WhisperTranscriber),
    ],
)
def test_the_folder_name_selects_the_backend_without_importing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str, expected: type
) -> None:
    for heavy in ("torch", "transformers", "faster_whisper"):
        monkeypatch.setitem(sys.modules, heavy, None)  # importing any of them now raises
    transcriber = speech.make_transcriber(tmp_path / key, 60.0)
    assert type(transcriber) is expected
    assert not transcriber.available()  # nothing downloaded


def test_availability_needs_every_file_at_its_pinned_size(
    small_specs: None, tmp_path: Path
) -> None:
    del small_specs
    folder = tmp_path / QWEN3_ASR.key
    transcriber = TransformerTranscriber(folder, 60.0)
    assert not transcriber.available()  # no folder
    folder.mkdir()
    (folder / "config.json").write_bytes(b"xxx")
    assert not transcriber.available()  # one file missing
    (folder / "model.safetensors").write_bytes(b"x" * 4)
    assert not transcriber.available()  # truncated download
    (folder / "model.safetensors").write_bytes(b"x" * 6)
    assert not transcriber.available()  # wrong size the other way
    (folder / "model.safetensors").write_bytes(b"x" * 5)
    assert transcriber.available()


def test_the_real_specs_are_used_for_the_real_keys(tmp_path: Path) -> None:
    folder = model_folder(tmp_path, CANARY_V2.key)  # tiny files: not the pinned sizes
    assert not TransformerTranscriber(folder, 60.0).available()


def test_missing_files_fail_before_any_heavy_import(
    small_specs: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    del small_specs
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setitem(sys.modules, "transformers", None)
    transcriber = TransformerTranscriber(tmp_path / QWEN3_ASR.key, 60.0)
    with pytest.raises(RuntimeError, match="missing"):
        transcriber.transcribe(AUDIO, None)


# --- loading -------------------------------------------------------------------------------


def test_qwen_loads_local_safetensors_in_bfloat16_on_the_gpu(libs: Libs, tmp_path: Path) -> None:
    folder = model_folder(tmp_path, QWEN3_ASR.key)
    TransformerTranscriber(folder, 60.0).transcribe(AUDIO, None)
    assert libs.hf.AutoProcessor.calls == [
        (str(folder), {"local_files_only": True, "trust_remote_code": False})
    ]
    assert libs.hf.AutoModelForSpeechSeq2Seq.calls == []
    assert libs.hf.AutoModelForMultimodalLM.calls == [
        (
            str(folder),
            {
                "dtype": "bfloat16",
                "local_files_only": True,
                "trust_remote_code": False,
                "use_safetensors": True,
            },
        )
    ]
    (model,) = libs.models()
    assert (model.moved_to, model.evaluated) == ("cuda", True)
    assert libs.torch.threads == 2
    assert libs.torch.cuda.fraction == GPU_FRACTION
    assert libs.hf.utils.logging.progress_disabled == 1


def test_canary_loads_as_a_float32_speech_to_text_model(libs: Libs, tmp_path: Path) -> None:
    folder = model_folder(tmp_path, CANARY_V2.key)
    TransformerTranscriber(folder, 60.0).transcribe(AUDIO, None)
    assert libs.hf.AutoModelForMultimodalLM.calls == []
    ((path, kwargs),) = libs.hf.AutoModelForSpeechSeq2Seq.calls
    assert path == str(folder)
    assert kwargs["dtype"] == "float32"
    assert kwargs["local_files_only"] is True
    assert kwargs["trust_remote_code"] is False
    assert kwargs["use_safetensors"] is True


def test_the_model_stays_loaded_between_clips(libs: Libs, tmp_path: Path) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 60.0)
    for _ in range(3):
        transcriber.transcribe(AUDIO, None)
    assert len(libs.models()) == 1
    assert libs.torch.inference_calls == 3


def test_without_cuda_nothing_is_loaded(libs: Libs, tmp_path: Path) -> None:
    libs.torch.cuda.available = False
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 60.0)
    with pytest.raises(RuntimeError, match="CUDA"):
        transcriber.transcribe(AUDIO, None)
    assert libs.models() == []
    assert libs.hf.AutoProcessor.calls == []


def test_library_logs_never_reach_our_log(
    libs: Libs, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rule 3: Transformers warnings can quote prompts or transcripts; they must go nowhere."""
    del libs
    caplog.set_level(logging.DEBUG)
    TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 60.0).transcribe(AUDIO, "Minlo")
    for name in HF_LOGGERS:
        logger = logging.getLogger(name)
        assert logger.propagate is False
        assert [type(h) for h in logger.handlers] == [logging.NullHandler]
    logging.getLogger("transformers.generation.utils").warning("prompt was: Minlo secret")
    logging.getLogger("huggingface_hub.file_download").error("transcript: Minlo secret")
    assert "secret" not in caplog.text


def test_library_logs_are_silenced_even_when_loading_fails(libs: Libs, tmp_path: Path) -> None:
    libs.torch.cuda.available = False
    transcriber = TransformerTranscriber(model_folder(tmp_path, CANARY_V2.key), 60.0)
    with pytest.raises(RuntimeError):
        transcriber.transcribe(AUDIO, None)
    assert all(logging.getLogger(n).propagate is False for n in HF_LOGGERS)


# --- Qwen3-ASR -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "language"),
    [("French", "fr"), ("english", "en"), ("FRENCH", "fr"), ("ENGLISH", "en")],
)
def test_qwen_reports_french_or_english(
    libs: Libs, tmp_path: Path, reported: str, language: str
) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 60.0)
    transcriber.transcribe(AUDIO, None)  # load, then say what the model "heard"
    libs.processor().parsed = {"transcription": " Salut Minlo ", "language": reported}
    result = transcriber.transcribe(AUDIO, "Minlo")
    assert result is not None
    assert (result.text, result.language) == ("Salut Minlo", language)


@pytest.mark.parametrize("reported", ["German", "", None])
def test_qwen_never_labels_another_or_unknown_language_as_french_or_english(
    libs: Libs, tmp_path: Path, reported: str | None
) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 60.0)
    transcriber.transcribe(AUDIO, None)
    libs.processor().parsed = {"transcription": "Hallo", "language": reported}
    result = transcriber.transcribe(AUDIO, None)
    assert result is not None
    assert result.language not in {"fr", "en"}


def test_qwen_passes_the_prompt_and_decodes_only_new_tokens(libs: Libs, tmp_path: Path) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 60.0)
    transcriber.transcribe(AUDIO, "Minlo, Akashic")
    processor = libs.processor()
    ((request),) = processor.requests
    assert request["prompt"] == "Minlo, Akashic"
    assert request["audio"] is AUDIO
    (model,) = libs.models()
    ((kwargs),) = model.generate_calls
    assert kwargs["max_new_tokens"] == MAX_TOKENS
    assert kwargs["do_sample"] is False
    (decoded,) = processor.decoded
    assert np.array_equal(cast("np.ndarray[Any, Any]", decoded), [[3, 4, 5, 6]])  # no prompt


@pytest.mark.parametrize("transcription", ["", "   ", None])
def test_qwen_silence_gives_no_transcript(
    libs: Libs, tmp_path: Path, transcription: str | None
) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 60.0)
    transcriber.transcribe(AUDIO, None)
    libs.processor().parsed = {"transcription": transcription, "language": "French"}
    assert transcriber.transcribe(AUDIO, None) is None


# --- Canary --------------------------------------------------------------------------------


def test_canary_decodes_french_and_english_and_keeps_the_likelier(
    libs: Libs, tmp_path: Path
) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, CANARY_V2.key), 60.0)
    result = transcriber.transcribe(AUDIO, "ignored by Canary")
    assert result is not None
    assert (result.text, result.language) == ("Salut Minlo", "fr")
    requests = libs.processor().requests
    assert [(r["source_language"], r["target_language"]) for r in requests] == [
        ("fr", "fr"),
        ("en", "en"),
    ]  # never a translation
    assert all("prompt" not in r for r in requests)
    (model,) = libs.models()
    assert all(
        c["return_dict_in_generate"] is True and c["output_scores"] is True
        for c in model.generate_calls
    )


def test_canary_picks_english_when_it_scores_higher(libs: Libs, tmp_path: Path) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, CANARY_V2.key), 60.0)
    transcriber.transcribe(AUDIO, None)
    (model,) = libs.models()
    model.scores = {"fr": -1.5, "en": -0.1}
    libs.processor().texts = {"fr": "Ce jeu", "en": "The game"}
    result = transcriber.transcribe(AUDIO, None)
    assert result is not None
    assert (result.text, result.language) == ("The game", "en")


def test_canary_ignores_an_empty_decode_even_when_it_scores_higher(
    libs: Libs, tmp_path: Path
) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, CANARY_V2.key), 60.0)
    transcriber.transcribe(AUDIO, None)
    (model,) = libs.models()
    model.scores = {"fr": -0.01, "en": -2.0}
    libs.processor().texts = {"fr": "  ", "en": "Hello"}
    result = transcriber.transcribe(AUDIO, None)
    assert result is not None
    assert (result.text, result.language) == ("Hello", "en")


def test_canary_silence_gives_no_transcript(libs: Libs, tmp_path: Path) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, CANARY_V2.key), 60.0)
    transcriber.transcribe(AUDIO, None)
    libs.processor().texts = {"fr": "", "en": " "}
    assert transcriber.transcribe(AUDIO, None) is None


# --- unloading -----------------------------------------------------------------------------


def test_model_is_unloaded_after_the_idle_time_and_reloads_on_demand(
    libs: Libs, tmp_path: Path
) -> None:
    clock = Clock()
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 600.0, clock)
    transcriber.unload_if_idle()  # nothing loaded: nothing to do
    transcriber.unload_if_idle(force=True)
    assert libs.torch.cuda.emptied == 0
    clock.now = 100.0
    transcriber.transcribe(AUDIO, None)
    clock.now = 699.0
    transcriber.unload_if_idle()
    assert libs.torch.cuda.emptied == 0
    assert len(libs.models()) == 1
    clock.now = 700.0
    transcriber.unload_if_idle()
    assert libs.torch.cuda.emptied == 1
    transcriber.transcribe(AUDIO, None)  # loads again
    assert len(libs.models()) == 2


def test_a_game_forces_the_unload_at_once(libs: Libs, tmp_path: Path) -> None:
    transcriber = TransformerTranscriber(model_folder(tmp_path, CANARY_V2.key), 600.0, Clock())
    transcriber.transcribe(AUDIO, None)
    transcriber.unload_if_idle(force=True)
    assert libs.torch.cuda.emptied == 1
    transcriber.transcribe(AUDIO, None)
    assert len(libs.models()) == 2


def test_idle_time_counts_from_the_end_of_the_last_clip(libs: Libs, tmp_path: Path) -> None:
    clock = Clock()
    transcriber = TransformerTranscriber(model_folder(tmp_path, QWEN3_ASR.key), 600.0, clock)
    transcriber.transcribe(AUDIO, None)
    model = libs.models()[0]
    real_generate = model.generate

    def slow_generate(**kwargs: object) -> object:
        clock.now += 500.0  # a long clip
        return real_generate(**kwargs)

    model.generate = slow_generate  # type: ignore[method-assign]  # test double
    transcriber.transcribe(AUDIO, None)  # 0 -> 500 s
    clock.now = 900.0  # 400 s after the clip ended, 900 s after it started
    transcriber.unload_if_idle()
    assert libs.torch.cuda.emptied == 0
