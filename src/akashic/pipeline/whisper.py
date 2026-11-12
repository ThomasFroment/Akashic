"""faster-whisper (CTranslate2) behind the ``Transcriber`` protocol.

This is the only module that imports ``faster_whisper``, and only when the first segment is
transcribed: the model loads on demand and is dropped after the configured idle time. The runner
also releases STT before Ask/embedding jobs (docs/SPEECH.md); model file size is not peak VRAM.
Offline by construction: the model is loaded from a local folder with ``local_files_only``;
nothing here can download.

Settings follow AGENTS.md and docs/SPEECH.md: language restricted to French or
English and detected per segment; ``no_speech_threshold`` 0.6, ``log_prob_threshold`` -2.0,
the compression-ratio guard, blank and non-speech tokens suppressed; no conditioning on previous
text (it is how Whisper falls into repetition loops); our own voice detector already ran, so
Whisper's is off.

Needs a model at ``models/<name>/`` (``scripts/fetch_models.py``); verified by hand with
``tests/manual/CHECKLIST.md`` because it needs the GPU and the model files.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import math
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from akashic.pipeline.models import SPEECH_CANDIDATES, is_complete
from akashic.pipeline.transcribe import LANGUAGES, Transcript

if TYPE_CHECKING:
    from collections.abc import Callable

    import numpy as np
    from numpy.typing import NDArray

log = logging.getLogger(__name__)

NO_SPEECH_THRESHOLD: Final = 0.6
LOG_PROB_THRESHOLD: Final = -2.0
COMPRESSION_RATIO_THRESHOLD: Final = 2.4


def add_cuda_dll_directories() -> None:
    """Make the CUDA libraries from the ``nvidia-*`` wheels findable. Python 3.8+ on Windows
    ignores PATH for its own DLL loads, but CTranslate2 loads cuBLAS with a plain LoadLibrary,
    which does not see ``add_dll_directory`` folders; so the folders go on both. Harmless when
    there are none."""
    spec = importlib.util.find_spec("nvidia")
    folders: list[str] = []
    for root in spec.submodule_search_locations or [] if spec else []:
        folders.extend(sorted({str(dll.parent) for dll in Path(root).rglob("*.dll")}))
    for folder in folders:
        try:
            os.add_dll_directory(folder)
        except OSError:
            log.debug("could not add a CUDA DLL directory")
    if folders:
        os.environ["PATH"] = os.pathsep.join([*folders, os.environ.get("PATH", "")])


def confidence(parts: list[Any]) -> float | None:
    """exp of the mean token log-probability, weighted by each piece's duration (0-1)."""
    total = weighted = 0.0
    for part in parts:
        duration = max(float(part.end) - float(part.start), 0.01)
        total += duration
        weighted += float(part.avg_logprob) * duration
    return math.exp(weighted / total) if total else None


class WhisperTranscriber:
    def __init__(
        self,
        model_dir: Path,
        idle_unload_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dir = model_dir
        self._idle_s = idle_unload_s
        self._clock = clock
        self._model: Any = None
        self._last_used = 0.0

    def available(self) -> bool:
        """The pinned model files are in place (fetch_models.py has run)."""
        spec = SPEECH_CANDIDATES.get(self._dir.name)
        return spec is not None and is_complete(self._dir.parent, spec)

    def _load(self) -> Any:  # noqa: ANN401 - faster_whisper.WhisperModel, imported lazily
        if self._model is None:
            add_cuda_dll_directories()
            fw: Any = importlib.import_module("faster_whisper")
            started = self._clock()
            self._model = fw.WhisperModel(
                str(self._dir), device="cuda", compute_type="float16", local_files_only=True
            )
            log.info("whisper model loaded in %.1f s", self._clock() - started)
        return self._model

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript | None:
        model = self._load()
        self._last_used = self._clock()
        language = self._detect(model, samples)
        segments, _info = model.transcribe(
            samples,
            language=language,
            task="transcribe",
            beam_size=5,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            no_speech_threshold=NO_SPEECH_THRESHOLD,
            log_prob_threshold=LOG_PROB_THRESHOLD,
            compression_ratio_threshold=COMPRESSION_RATIO_THRESHOLD,
            suppress_blank=True,
            vad_filter=False,
        )
        parts = list(segments)
        text = " ".join(part.text.strip() for part in parts).strip()
        self._last_used = self._clock()
        return Transcript(text, language, confidence(parts)) if text else None

    @staticmethod
    def _detect(model: Any, samples: NDArray[np.float32]) -> str:  # noqa: ANN401
        """The likelier of French and English, whatever Whisper thinks of its other 97."""
        _best, _prob, probabilities = model.detect_language(samples)
        scores = {lang: prob for lang, prob in probabilities if lang in LANGUAGES}
        return max(LANGUAGES, key=lambda lang: scores.get(lang, 0.0))

    def unload_if_idle(self, *, force: bool = False) -> None:
        if self._model is not None and (force or self._clock() - self._last_used >= self._idle_s):
            self._model = None
            log.info("whisper model unloaded")
