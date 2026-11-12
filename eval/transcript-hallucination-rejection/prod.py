"""The production audio path, built from the repository's own classes (no copies of logic).

capture  : akashic.capture.audio.Segmenter fed exactly like StreamRecorder._consume
           (src/akashic/capture/audio.py:473-497): 512-sample windows stamped from 0 ms, Silero
           probability only for windows with a non-zero sample (else 0.0, line 487), one detector
           reset per stream (line 429). Settings: capture/main.py:46-57 applied to config.Audio().
storage  : SegmentWriter encodes Ogg/Opus 24 kbps (audio.py:332, storage/opus.py) and the
           transcribe job decodes it (storage/audio.py:199-203).
whisper  : TranscribeHandler.run (pipeline/transcribe.py:189-215): previous tail + vocabulary
           through build_prompt, normalize_loudness, then WhisperTranscriber.transcribe
           (pipeline/whisper.py:100-128) with the same options; the model is loaded by
           WhisperTranscriber._load (float16, CUDA, local files only).
today    : empty or is_hallucination -> "reject" (transcribe.py:209-211); else unclear when
           search.hybrid.is_unclear(confidence) (confidence < UNCLEAR_BELOW), else "keep".
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(REPO / "src")]
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np  # noqa: E402

from akashic.capture.audio import WINDOW_MS, AudioSettings, Segmenter, Utterance  # noqa: E402
from akashic.capture.vad import WINDOW, SileroVad  # noqa: E402
from akashic.config import Audio  # noqa: E402
from akashic.pipeline import transcribe as repo_transcribe  # noqa: E402
from akashic.pipeline import whisper as repo_whisper  # noqa: E402
from akashic.pipeline.transcribe import (  # noqa: E402
    LANGUAGES,
    build_prompt,
    is_hallucination,
    normalize,
    normalize_loudness,
)
from akashic.pipeline.whisper import (  # noqa: E402
    COMPRESSION_RATIO_THRESHOLD,
    LOG_PROB_THRESHOLD,
    NO_SPEECH_THRESHOLD,
    WhisperTranscriber,
    confidence,
)
from akashic.search.hybrid import is_unclear  # noqa: E402
from akashic.storage.opus import SAMPLE_RATE, decode_opus, encode_opus  # noqa: E402

MODEL_DIR = HERE / "data" / "models" / "faster-whisper-large-v3"
SR = SAMPLE_RATE


def audio_settings() -> AudioSettings:
    """capture/main.py:46-57, applied to the shipped defaults of config.Audio()."""
    cfg = Audio()
    return AudioSettings(
        energy_floor=cfg.energy_floor,
        mic_gain=cfg.mic_gain,
        vad_start=cfg.vad_threshold,
        vad_continue=cfg.vad_threshold * 0.7,
        min_speech_ms=cfg.min_speech_ms,
        hangover_ms=cfg.silence_ms,
        mic_device=cfg.mic_device,
        system_device=cfg.system_device,
    )


class Capture:
    def __init__(self) -> None:
        self.vad = SileroVad()
        self.settings = audio_settings()

    def run(self, clip: np.ndarray) -> tuple[np.ndarray, list[Utterance], list[dict]]:
        """Per-window speech probabilities, the utterances production would store, the drops."""
        self.vad.reset()
        seg = Segmenter(self.settings)
        probs = np.zeros(len(clip) // WINDOW, np.float32)
        out: list[Utterance] = []
        for i in range(len(probs)):
            window = clip[i * WINDOW : (i + 1) * WINDOW]
            p = self.vad.probability(window) if window.any() else 0.0
            probs[i] = p
            out.extend(seg.feed(i * WINDOW_MS, window, p))
        out.extend(seg.flush(len(probs) * WINDOW_MS + 60_000))
        drops = [{"ts_ms": d.ts_ms, "reason": d.reason, "rms": d.rms, "speech_ms": d.speech_ms}
                 for d in seg.drops]
        return probs, out, drops


def silero_probs(samples: np.ndarray, vad: SileroVad | None = None) -> np.ndarray:
    """Fresh-state Silero probability per 32 ms window (zero windows -> 0.0, as in capture)."""
    vad = vad or SileroVad()
    vad.reset()
    n = len(samples) // WINDOW
    return np.array([vad.probability(samples[i * WINDOW : (i + 1) * WINDOW])
                     if samples[i * WINDOW : (i + 1) * WINDOW].any() else 0.0 for i in range(n)],
                    np.float32)


def stored_samples(utterance_samples: np.ndarray) -> np.ndarray:
    """What the transcribe job reads back: Ogg/Opus 24 kbps round trip."""
    return decode_opus(encode_opus(utterance_samples))


def part_dict(part) -> dict:  # noqa: ANN001 - faster_whisper Segment
    return {
        "id": part.id, "seek": part.seek, "start": part.start, "end": part.end, "text": part.text,
        "tokens": list(part.tokens), "avg_logprob": part.avg_logprob,
        "no_speech_prob": part.no_speech_prob, "compression_ratio": part.compression_ratio,
        "temperature": part.temperature,
        "words": [{"start": w.start, "end": w.end, "word": w.word, "probability": w.probability}
                  for w in part.words] if part.words else None,
    }


class Whisper:
    """The repository's WhisperTranscriber, opened up so every Segment field is kept."""

    def __init__(self, model_dir: Path = MODEL_DIR) -> None:
        self.transcriber = WhisperTranscriber(model_dir, idle_unload_s=1e12)

    @property
    def model(self):  # noqa: ANN201 - faster_whisper.WhisperModel
        return self.transcriber._load()  # noqa: SLF001 - the repo's loader (cuda, float16, offline)

    def detect(self, samples: np.ndarray) -> tuple[str, dict[str, float]]:
        """WhisperTranscriber._detect (whisper.py:123-128), also returning the probabilities."""
        _best, _prob, probabilities = self.model.detect_language(samples)
        scores = {lang: prob for lang, prob in probabilities if lang in LANGUAGES}
        top = dict(probabilities[:5])
        top.update({lang: scores.get(lang, 0.0) for lang in LANGUAGES})
        return max(LANGUAGES, key=lambda lang: scores.get(lang, 0.0)), top

    def decode(self, samples: np.ndarray, prompt: str | None, language: str | None = None,
               **overrides) -> list:
        """model.transcribe with the production options of whisper.py:104-116 (overridable)."""
        options = {
            "language": language, "task": "transcribe", "beam_size": 5,
            "condition_on_previous_text": False, "initial_prompt": prompt,
            "no_speech_threshold": NO_SPEECH_THRESHOLD, "log_prob_threshold": LOG_PROB_THRESHOLD,
            "compression_ratio_threshold": COMPRESSION_RATIO_THRESHOLD, "suppress_blank": True,
            "vad_filter": False,
        }
        options.update(overrides)
        segments, _info = self.model.transcribe(samples, **options)
        return list(segments)

    def production(self, samples: np.ndarray, prompt: str | None) -> dict:
        """One production transcription of Whisper-ready samples, with every field kept."""
        t0 = time.perf_counter()
        language, probs = self.detect(samples)
        parts = self.decode(samples, prompt, language)
        ms = (time.perf_counter() - t0) * 1000
        text = " ".join(part.text.strip() for part in parts).strip()
        return {"language": language, "lang_probs": probs, "parts": [part_dict(p) for p in parts],
                "text": text, "confidence": confidence(parts), "decode_ms": ms, "_raw": parts}

    def align_words(self, samples: np.ndarray, parts: list[dict], language: str) -> list[dict]:
        """Word timings for already decoded parts, without changing the decode: one encoder pass
        per 30 s window plus faster-whisper's own cross-attention alignment."""
        from faster_whisper.audio import pad_or_trim
        from faster_whisper.tokenizer import Tokenizer

        model = self.model
        if not parts:
            return []
        features = model.feature_extractor(samples)
        content_frames = features.shape[-1] - 1
        tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task="transcribe",
                              language=language)
        by_seek: dict[int, list[dict]] = {}
        for p in parts:
            by_seek.setdefault(p["seek"], []).append(
                {"seek": p["seek"], "start": p["start"], "end": p["end"], "tokens": p["tokens"]})
        words: list[dict] = []
        for seek, group in sorted(by_seek.items()):
            size = min(model.feature_extractor.nb_max_frames, content_frames - seek)
            encoder_output = model.encode(pad_or_trim(features[:, seek : seek + size]))
            model.add_word_timestamps([group], tokenizer, encoder_output, size,
                                      "\"'“¿([{-", "\"'.。,，!！?？:：”)]}、", 0.0)
            for g in group:
                words.extend(g.get("words") or [])
        return [{k: (float(v) if k != "word" else v) for k, v in w.items()} for w in words]


def today_status(text: str, conf: float | None) -> str:
    """transcribe.py:209-211 then search/hybrid.py:436-437."""
    if not text or is_hallucination(text):
        return "reject"
    return "unclear" if is_unclear(conf) else "keep"


__all__ = [
    "SR", "Capture", "Whisper", "audio_settings", "build_prompt", "confidence", "is_hallucination",
    "is_unclear", "normalize", "normalize_loudness", "repo_transcribe", "repo_whisper",
    "silero_probs", "stored_samples", "today_status",
]
