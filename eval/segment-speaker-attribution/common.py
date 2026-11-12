"""Shared pieces of the speaker-attribution fixture and harness.

- paths of the downloaded data, the models and the fixture;
- the repo's own code on ``sys.path`` (``src/akashic``), imported, never modified;
- ``SharedModels``: the app's ``OnnxVoiceModels`` (pipeline/speakers.py) on the CPU provider,
  with every ONNX forward timed and counted, so a candidate can reuse the app's windowing
  (``turns``) and embedding (``embed``) or call the raw forwards (``segment_logits``) itself;
- the candidate interface (``Segment``, ``Attribution``) the harness calls.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # the repository root
SRC = REPO / "src"
DATA = HERE / "data"
MODELS = DATA / "models"
FIXTURE = DATA / "fixture"
OUT = HERE / "out"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from akashic.pipeline import speakers as pipe  # noqa: E402  (the app's code, read-only)
from akashic.pipeline.speakers import OnnxVoiceModels, model_paths  # noqa: E402
from akashic.storage import opus as app_opus  # noqa: E402

SR = 16_000
CAPTURE_WINDOW_MS = 32  # capture/audio.py: one VAD window
MAX_SEGMENT_MS = 938 * CAPTURE_WINDOW_MS  # 30_016: capture/audio.py _cut() at >= 30_000 ms
LEAD_MS = (2_000 // CAPTURE_WINDOW_MS) * CAPTURE_WINDOW_MS  # 1_984: overlap kept by _cut()


# --- candidate interface -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrevSegment:
    """What the app knows about the segment this one continues (audio_segments.continues_id)."""

    segment_id: int
    author: int | None  # what the candidate itself attributed it to
    samples: np.ndarray  # its audio (the app can read it back from storage)


@dataclass(frozen=True, slots=True)
class Segment:
    segment_id: int
    samples: np.ndarray  # mono float32, 16 kHz, as decoded from the app's Opus storage
    sr: int
    lead_ms: int  # 0, or LEAD_MS when this segment starts with the previous one's last audio
    prev: PrevSegment | None  # set when lead_ms > 0


@dataclass(slots=True)
class Attribution:
    author: int | None  # profile id, or None (no single author)
    turns: list[tuple[int, int, int | None]] = field(default_factory=list)  # start, end, id
    # Profiles this segment changed: (profile_id, "create" | "update", start_ms, end_ms) with
    # the span of the turn that caused it (the harness maps created profiles to a true speaker).
    updates: list[tuple[int, str, int, int]] = field(default_factory=list)


# A candidate is a module exposing:
#   NAME: str
#   make_store() -> store
#   enroll(store, models: SharedModels, clips: list[np.ndarray]) -> int   (new profile id)
#   attribute(store, models: SharedModels, seg: Segment) -> Attribution
#   centroid(store, profile_id) -> np.ndarray | None  (unit, WeSpeaker space; None if n/a)


# --- models --------------------------------------------------------------------------------


class _Timed:
    def __init__(self, session: Any, counter: dict[str, float], key: str) -> None:
        self._session = session
        self._counter = counter
        self._key = key

    def run(self, *args: Any, **kwargs: Any) -> Any:
        t = time.perf_counter()
        out = self._session.run(*args, **kwargs)
        self._counter[self._key + "_s"] += time.perf_counter() - t
        self._counter[self._key + "_n"] += 1
        return out

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class SharedModels(OnnxVoiceModels):
    """The app's ONNX adapter (CPUExecutionProvider, default session options), with timed
    forwards. ``turns`` (fixed 10 s windows) and ``embed`` (Kaldi fbank + ResNet34) are the
    app's methods, unchanged."""

    def __init__(self) -> None:
        seg, emb = model_paths(MODELS)
        super().__init__(seg, emb, idle_unload_s=1e9)
        self.counter: dict[str, float] = {"seg_s": 0.0, "seg_n": 0, "emb_s": 0.0, "emb_n": 0}

    def _load(self) -> tuple[Any, Any]:
        if self._sessions is None:
            seg, emb = super()._load()
            self._sessions = (_Timed(seg, self.counter, "seg"), _Timed(emb, self.counter, "emb"))
        return super()._load()

    def segment_logits(self, window: np.ndarray) -> np.ndarray:
        """Raw powerset logits (frames, 7) of one 10 s window (160_000 samples, zero-padded)."""
        segmentation, _ = self._load()
        padded = np.zeros(pipe.WINDOW, dtype=np.float32)
        padded[: len(window)] = window[: pipe.WINDOW]
        return np.asarray(
            segmentation.run(None, {"input_values": padded[None, None, :]})[0][0], np.float32
        )

    def reset_counter(self) -> None:
        for k in self.counter:
            self.counter[k] = 0


# --- audio helpers -------------------------------------------------------------------------


def unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v if n == 0 else (v / n).astype(np.float32)


def app_storage_roundtrip(x: np.ndarray) -> np.ndarray:
    """The app stores each segment as 24 kbps Ogg/Opus (storage/opus.py) and the speakers job
    decodes it back: run the very same functions."""
    y = app_opus.decode_opus(app_opus.encode_opus(np.clip(x, -1.0, 1.0).astype(np.float32)))
    out = np.zeros(len(x), dtype=np.float32)
    out[: min(len(x), len(y))] = y[: len(x)]
    return out
