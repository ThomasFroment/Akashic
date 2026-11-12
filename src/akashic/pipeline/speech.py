"""Local speech-model selection; imports PyTorch/Transformers only for a selected model.

Whisper keeps its established CTranslate2 backend. The HF models use only pinned local
safetensors and built-in Transformers architectures; no remote code, URLs or auto-downloads.
"""

from __future__ import annotations

import gc
import importlib
import logging
import time
from typing import TYPE_CHECKING, Any, Final

from akashic.pipeline.models import CANARY_V2, QWEN3_ASR, SPEECH_CANDIDATES, is_complete
from akashic.pipeline.transcribe import Transcriber, Transcript
from akashic.pipeline.whisper import WhisperTranscriber

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

MAX_TOKENS: Final = 512
GPU_FRACTION: Final = 0.55  # bound this allocator, retaining headroom for the desktop


def make_transcriber(folder: Path, idle_s: float) -> Transcriber:
    if folder.name in {QWEN3_ASR.key, CANARY_V2.key}:
        return TransformerTranscriber(folder, idle_s)
    return WhisperTranscriber(folder, idle_s)


class TransformerTranscriber:
    def __init__(
        self, folder: Path, idle_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._folder = folder
        self._idle_s = idle_s
        self._clock = clock
        self._used = 0.0
        # Optional libraries expose dynamic processor/model APIs; no Any leaks into our port.
        self._torch: Any = None
        self._processor: Any = None
        self._model: Any = None

    def available(self) -> bool:
        return is_complete(self._folder.parent, SPEECH_CANDIDATES[self._folder.name])

    def _load(self) -> None:
        if self._model is not None:
            return
        if not self.available():
            msg = "speech model files are missing"
            raise RuntimeError(msg)
        torch: Any = importlib.import_module("torch")
        hf: Any = importlib.import_module("transformers")
        self._torch = torch
        # Third-party warnings must not write prompts/transcripts to our captured-content-free log.
        for name in ("transformers", "huggingface_hub"):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
        hf.utils.logging.disable_progress_bar()
        if not torch.cuda.is_available():
            msg = "speech model requires the bundled CUDA runtime"
            raise RuntimeError(msg)
        torch.set_num_threads(2)
        torch.cuda.set_per_process_memory_fraction(GPU_FRACTION)
        qwen = self._folder.name == QWEN3_ASR.key
        dtype = torch.bfloat16 if qwen else torch.float32
        model_class = hf.AutoModelForMultimodalLM if qwen else hf.AutoModelForSpeechSeq2Seq
        processor = hf.AutoProcessor.from_pretrained(
            str(self._folder), local_files_only=True, trust_remote_code=False
        )
        model = (
            model_class.from_pretrained(
                str(self._folder),
                dtype=dtype,
                local_files_only=True,
                trust_remote_code=False,
                use_safetensors=True,
            )
            .to("cuda")
            .eval()
        )
        self._torch, self._processor, self._model = torch, processor, model

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript | None:
        self._load()
        self._used = self._clock()
        try:
            with self._torch.inference_mode():
                if self._folder.name == QWEN3_ASR.key:
                    return self._qwen(samples, prompt)
                return self._canary(samples)
        finally:
            self._used = self._clock()

    def _qwen(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript | None:
        inputs = self._processor.apply_transcription_request(audio=samples, prompt=prompt).to(
            self._model.device, self._model.dtype
        )
        output = self._model.generate(**inputs, max_new_tokens=MAX_TOKENS, do_sample=False)
        generated = output[:, inputs["input_ids"].shape[1] :]
        parsed = self._processor.decode(generated, return_format="parsed")[0]
        text = str(parsed.get("transcription") or "").strip()
        language = {"french": "fr", "english": "en"}.get(
            str(parsed.get("language") or "").casefold(), "und"
        )
        return Transcript(text, language) if text else None

    def _canary(self, samples: NDArray[np.float32]) -> Transcript | None:
        # Canary requires a source language. Compare same-language FR/EN decodes rather than
        # defaulting every clip to English or silently translating French. This is experimental;
        # decoder likelihood is not a calibrated language/word confidence.
        best: tuple[float, str, str] | None = None
        for language in ("fr", "en"):
            inputs = self._processor.apply_transcription_request(
                audio=samples,
                source_language=language,
                target_language=language,
            ).to(self._model.device)
            output = self._model.generate(
                **inputs,
                max_new_tokens=MAX_TOKENS,
                do_sample=False,
                return_dict_in_generate=True,
                output_scores=True,
            )
            text = str(
                self._processor.decode(output.sequences, skip_special_tokens=True)[0]
            ).strip()
            scores = self._model.compute_transition_scores(
                output.sequences, output.scores, normalize_logits=True
            )
            score = float(scores.float().mean().item())
            if text and (best is None or score > best[0]):
                best = score, text, language
            del inputs, output, scores
        return Transcript(best[1], best[2]) if best else None

    def unload_if_idle(self, *, force: bool = False) -> None:
        if self._torch is not None and (force or self._clock() - self._used >= self._idle_s):
            self._model = self._processor = None
            gc.collect()
            self._torch.cuda.empty_cache()
