"""Who spoke in a segment: voice segmentation, voice embeddings and the ``speakers`` job.

Torch-free, as in screenpipe: pyannote segmentation-3.0 says who speaks when inside a 10 s
window (up to three local speakers, overlap included), and a WeSpeaker ResNet34 model turns a
speaker's speech into a 256-number voice embedding. Both are ONNX models on onnxruntime's CPU
provider: they are small, and keeping them off the GPU leaves its memory to Whisper and the LLM.

The pure parts (powerset decoding, the Kaldi-style filter bank the embedding model expects,
cutting a window into turns) are plain numpy and tested without the models. Who a voice *is*
(gates, matching, profiles) lives in ``storage/speakers.py``.

One turn is one local speaker's speech inside one 10 s window; a segment is at most 30 s, so a
segment holds a handful of turns. The transcript is not split by speaker: a segment ends at
every second of silence, so the rare segment with two voices is attributed to the one who
spoke most, and its turns say who else was there.
"""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, Protocol

import numpy as np

from akashic.pipeline.jobs import PermanentJobError
from akashic.pipeline.models import (
    EMBEDDING_FILE,
    EMBEDDING_MODEL,
    SEGMENTATION_FILE,
    SEGMENTATION_MODEL,
)
from akashic.storage.embeddings import unit
from akashic.storage.opus import SAMPLE_RATE
from akashic.storage.speakers import MIN_IDENTIFY_MS, NewTurn

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

    from numpy.typing import NDArray

    from akashic.pipeline.jobs import Job
    from akashic.storage.audio import AudioStore
    from akashic.storage.speakers import SpeakerStore

log = logging.getLogger(__name__)

SPEAKERS: Final = "speakers"
WINDOW_S: Final = 10  # what segmentation-3.0 was trained on
WINDOW: Final = WINDOW_S * SAMPLE_RATE
MIN_TURN_MS: Final = 300  # less speech than this from one voice in a window is dropped
MIN_WINDOW_MS: Final = 500  # a tail shorter than this is not worth a pass
MAX_LOCAL_SPEAKERS: Final = 3

# The model's 7 classes: nobody, each of 3 speakers alone, then each pair.
_POWERSET: Final = ((), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2))

# Kaldi fbank settings the WeSpeaker model was trained with.
_FRAME: Final = 400  # 25 ms
_SHIFT: Final = 160  # 10 ms
_FFT: Final = 512
_MELS: Final = 80
_LOW_HZ: Final = 20.0
_PREEMPHASIS: Final = 0.97
_EPSILON: Final = float(np.finfo(np.float32).eps)


@dataclass(frozen=True, slots=True)
class LocalTurn:
    start_ms: int  # offsets from the start of the audio
    end_ms: int
    speech_ms: int  # time this voice spoke alone (overlap is not counted)
    samples: NDArray[np.float32]  # that speech, joined: what gets embedded


class VoiceModels(Protocol):
    def available(self) -> bool: ...

    def turns(self, samples: NDArray[np.float32]) -> list[LocalTurn]:
        """Who spoke in ``samples``: one turn per voice per 10 s window."""
        ...

    def embed(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        """A unit-length voice embedding of ``samples`` (at least about one second)."""
        ...

    def unload_if_idle(self, *, force: bool = False) -> None: ...


# --- numpy parts ---------------------------------------------------------------------------


def _mel(hz: NDArray[np.float64] | float) -> NDArray[np.float64]:
    return 1127.0 * np.log(1.0 + np.asarray(hz) / 700.0)


def _mel_bank() -> NDArray[np.float64]:
    """Kaldi's triangular mel filters over the first 256 FFT bins -> shape (80, 256)."""
    nyquist = SAMPLE_RATE / 2
    low, high = _mel(_LOW_HZ), _mel(nyquist)
    delta = (high - low) / (_MELS + 1)
    left = low + np.arange(_MELS)[:, None] * delta
    centre = left + delta
    right = centre + delta
    mel = _mel(np.arange(_FFT // 2) * SAMPLE_RATE / _FFT)[None, :]
    up = (mel - left) / (centre - left)
    down = (right - mel) / (right - centre)
    return np.maximum(0.0, np.minimum(up, down))


_BANK: Final = _mel_bank()
# Kaldi's "povey" window: a Hann window raised to 0.85.
_WINDOW_FN: Final = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_FRAME) / (_FRAME - 1))) ** 0.85


def fbank(samples: NDArray[np.float32]) -> NDArray[np.float32]:
    """Log mel filter bank features, (frames, 80), with the mean removed over time: the input of
    the embedding model (Kaldi ``compliance.fbank``, no dither, then cepstral mean normalisation).
    Returns no frames for audio shorter than one 25 ms frame."""
    if len(samples) < _FRAME:
        return np.zeros((0, _MELS), dtype=np.float32)
    wave = samples.astype(np.float64) * 32768.0
    count = 1 + (len(wave) - _FRAME) // _SHIFT
    index = np.arange(_FRAME)[None, :] + _SHIFT * np.arange(count)[:, None]
    frames = wave[index]
    frames -= frames.mean(axis=1, keepdims=True)
    shifted = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames = (frames - _PREEMPHASIS * shifted) * _WINDOW_FN
    power = np.abs(np.fft.rfft(frames, n=_FFT, axis=1)[:, : _FFT // 2]) ** 2
    features = np.log(np.maximum(power @ _BANK.T, _EPSILON))
    features -= features.mean(axis=0, keepdims=True)
    return features.astype(np.float32)


def decode_window(logits: NDArray[np.float32]) -> NDArray[np.bool_]:
    """Powerset logits (frames, 7) -> (frames, 3) booleans: which local speakers are talking."""
    best = np.argmax(logits, axis=-1)
    active = np.zeros((len(best), MAX_LOCAL_SPEAKERS), dtype=np.bool_)
    for cls, members in enumerate(_POWERSET):
        for speaker in members:
            active[best == cls, speaker] = True
    return active


def window_turns(
    window: NDArray[np.float32], active: NDArray[np.bool_], valid_samples: int, offset_ms: int
) -> list[LocalTurn]:
    """Cut one window into turns, one per local speaker. Only frames where that speaker talks
    alone are used (overlap makes bad embeddings), and frames past ``valid_samples`` (padding)
    are ignored."""
    frames = len(active)
    if frames == 0:
        return []
    per_frame = len(window) / frames  # samples
    real = min(frames, int(valid_samples / per_frame))
    alone = active.sum(axis=1) == 1
    turns: list[LocalTurn] = []
    for speaker in range(MAX_LOCAL_SPEAKERS):
        mine = active[:real, speaker] & alone[:real]
        speech_ms = int(mine.sum() * per_frame * 1000 / SAMPLE_RATE)
        if speech_ms < MIN_TURN_MS:
            continue
        where = np.flatnonzero(mine)
        pieces = _runs(where)
        joined = np.concatenate(
            [window[int(a * per_frame) : int((b + 1) * per_frame)] for a, b in pieces]
        )
        start = offset_ms + int(where[0] * per_frame * 1000 / SAMPLE_RATE)
        end = offset_ms + int((where[-1] + 1) * per_frame * 1000 / SAMPLE_RATE)
        turns.append(LocalTurn(start, end, speech_ms, joined))
    return turns


def _runs(indices: NDArray[np.intp]) -> list[tuple[int, int]]:
    """Consecutive runs in a sorted index array as (first, last) pairs."""
    breaks = np.flatnonzero(np.diff(indices) > 1)
    firsts = np.concatenate([[0], breaks + 1])
    lasts = np.concatenate([breaks, [len(indices) - 1]])
    return [(int(indices[a]), int(indices[b])) for a, b in zip(firsts, lasts, strict=True)]


# --- the ONNX adapter ----------------------------------------------------------------------


class OnnxVoiceModels:
    """Both models on onnxruntime's CPU provider, created on first use and dropped after the
    idle time. Offline by construction: it only opens local files."""

    def __init__(
        self,
        segmentation: Path,
        embedding: Path,
        idle_unload_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._segmentation_path = segmentation
        self._embedding_path = embedding
        self._idle_s = idle_unload_s
        self._clock = clock
        self._sessions: tuple[Any, Any] | None = None
        self._last_used = 0.0

    def available(self) -> bool:
        return self._segmentation_path.is_file() and self._embedding_path.is_file()

    def _load(self) -> tuple[Any, Any]:
        if self._sessions is None:
            ort: Any = importlib.import_module("onnxruntime")
            options = ort.SessionOptions()
            options.log_severity_level = 3
            providers = ["CPUExecutionProvider"]
            self._sessions = (
                ort.InferenceSession(str(self._segmentation_path), options, providers=providers),
                ort.InferenceSession(str(self._embedding_path), options, providers=providers),
            )
            log.info("speaker models loaded")
        self._last_used = self._clock()
        return self._sessions

    def turns(self, samples: NDArray[np.float32]) -> list[LocalTurn]:
        segmentation, _ = self._load()
        turns: list[LocalTurn] = []
        for start in range(0, len(samples), WINDOW):
            chunk = samples[start : start + WINDOW]
            if len(chunk) * 1000 < MIN_WINDOW_MS * SAMPLE_RATE:
                continue
            padded = np.zeros(WINDOW, dtype=np.float32)
            padded[: len(chunk)] = chunk
            logits = segmentation.run(None, {"input_values": padded[None, None, :]})[0][0]
            active = decode_window(np.asarray(logits, dtype=np.float32))
            offset_ms = start * 1000 // SAMPLE_RATE
            turns.extend(window_turns(padded, active, len(chunk), offset_ms))
        self._last_used = self._clock()
        return turns

    def embed(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        _, embedding = self._load()
        features = fbank(samples)
        if len(features) == 0:
            msg = "audio too short to embed"
            raise ValueError(msg)
        vector = embedding.run(None, {"feats": features[None, :, :]})[0][0]
        self._last_used = self._clock()
        return unit(np.asarray(vector, dtype=np.float32))

    def unload_if_idle(self, *, force: bool = False) -> None:
        if self._sessions is not None and (
            force or self._clock() - self._last_used >= self._idle_s
        ):
            self._sessions = None
            log.info("speaker models unloaded")


def model_paths(models_dir: Path) -> tuple[Path, Path]:
    return (
        models_dir / SEGMENTATION_MODEL / SEGMENTATION_FILE,
        models_dir / EMBEDDING_MODEL / EMBEDDING_FILE,
    )


# --- the job -------------------------------------------------------------------------------


class SpeakersHandler:
    """Job handler for ``speakers``, queued when a segment's transcript is done."""

    kind: ClassVar[str] = SPEAKERS
    heavy: ClassVar[bool] = True

    def __init__(
        self,
        audio: AudioStore,
        speakers: SpeakerStore,
        models: VoiceModels,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._audio = audio
        self._speakers = speakers
        self._models = models
        self._now_ms = now_ms

    def blocked(self) -> str | None:
        return None if self._models.available() else "speaker models missing"

    def run(self, job: Job) -> None:
        segment_id = job.payload.get("segment_id")
        if not isinstance(segment_id, int):
            msg = "speakers job without a segment id"
            raise PermanentJobError(msg)
        segment = self._audio.get(segment_id)
        if segment is None:
            msg = "segment no longer exists"
            raise PermanentJobError(msg)
        if self._speakers.has_turns(segment_id):
            return  # done already (the worker died before completing the job)
        if segment.stream == "mic":
            length_ms = segment.end_ms - segment.start_ms
            self._speakers.record_self(segment_id, segment.speech_ms, length_ms, self._now_ms())
            return
        samples = self._audio.read_samples(segment)
        if samples is None:
            msg = "raw audio is gone"
            raise PermanentJobError(msg)
        turns = [
            NewTurn(
                t.start_ms,
                t.end_ms,
                t.speech_ms,
                self._models.embed(t.samples) if t.speech_ms >= MIN_IDENTIFY_MS else None,
            )
            for t in self._models.turns(samples)
        ]
        self._speakers.record_turns(segment_id, turns, self._now_ms())

    def give_up(self, job: Job) -> None:
        """Nothing to undo: a segment without turns simply has no speaker."""

    def idle(self, *, force: bool = False) -> None:
        self._models.unload_if_idle(force=force)
