"""Round-2 QA helpers: closed-form span embeddings from the WeSpeaker pooling input.

- ``FrameEmbedder``: the derived ResNet34-LM (data\\models_derived\\resnet34_LM_framemap.onnx, same
  weights, one extra output: the pre-TSTP frame map, (256, 10, T') at 80 ms per step), loaded on
  the CPU provider with default options like the app; its sha256 is pinned.
- ``Stats``: float64 prefix sums (n, sum h, sum h^2) over a turn's steps, so any span's (or any
  union of spans', also across turns) embedding is one Gemm: e = W [mu; sigma] + b - mean_vec,
  sigma = sqrt(unbiased var + eps), exactly as the graph's TSTP + Gemm + Sub.
- windowing with the app's window count, starts spread evenly, frames owned by the nearest
  window centre; turn extraction (owned solo frames per local channel) with the join positions
  of the concatenated runs, so sub-spans can skip steps that straddle a join.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from C2_lib import FRAMES, MIN_EMBED_MS, PER_FRAME, SR, WINDOW, decode, runs, window_starts

HERE = Path(__file__).resolve().parents[1]
DERIVED = HERE / "data" / "models_derived" / "resnet34_LM_framemap.onnx"
HEAD = HERE / "data" / "models_derived" / "resnet34_LM_head.npz"
DERIVED_SHA = "941b02cc3c57143810fed6b679815283892a091fd17c4bfe585c38d8a673d2a0"
STEP_S = 0.08  # one frame-map step: 8 fbank frames of 10 ms
_POWERSET = ((), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2))


def _check_sha(p: Path) -> None:
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    if h != DERIVED_SHA:
        raise RuntimeError(f"derived model checksum mismatch: {h}")


class Head:
    def __init__(self) -> None:
        z = np.load(HEAD)
        self.W = z["W"].astype(np.float64)  # (256, 5120)
        self.b = z["b"].astype(np.float64)
        self.mean_vec = z["mean_vec"].astype(np.float64)
        self.eps = float(z["eps"])
        self.WT = np.ascontiguousarray(self.W.T)

    def embed(self, n: np.ndarray, s1: np.ndarray, s2: np.ndarray) -> np.ndarray:
        """n (J,), s1/s2 (J, 2560) sums -> (J, 256) unit embeddings."""
        n = n[:, None].astype(np.float64)
        mu = s1 / n
        var = np.maximum(s2 - n * mu * mu, 0.0) / np.maximum(n - 1, 1e-9)
        sig = np.sqrt(var + self.eps)
        x = np.concatenate([mu, sig], axis=1) @ self.WT + self.b - self.mean_vec
        return x / np.linalg.norm(x, axis=1, keepdims=True)


@dataclass(slots=True)
class Stats:
    """Prefix sums over a turn's frame-map steps."""

    c1: np.ndarray  # (T'+1, 2560)
    c2: np.ndarray
    valid: np.ndarray  # (T',) step usable inside a sub-span (not next to a run join)

    @property
    def T(self) -> int:
        return len(self.valid)

    def total(self) -> tuple[float, np.ndarray, np.ndarray]:
        return float(self.T), self.c1[-1], self.c2[-1]

    def span(self, a: int, b: int) -> tuple[float, np.ndarray, np.ndarray]:
        return float(b - a), self.c1[b] - self.c1[a], self.c2[b] - self.c2[a]


class FrameEmbedder:
    def __init__(self, counter: dict | None = None) -> None:
        import onnxruntime as ort

        _check_sha(DERIVED)
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(str(DERIVED), opts, providers=["CPUExecutionProvider"])
        self.head = Head()
        self.counter = counter

    def forward(self, samples: np.ndarray, joins_s: list[float] | None = None):
        """-> (unit embedding (256,) as the app's embed(), Stats)."""
        import time

        from akashic.pipeline.speakers import fbank

        feats = fbank(samples)
        t = time.perf_counter()
        emb, fm = self.sess.run(None, {"feats": feats[None]})
        if self.counter is not None:
            self.counter["emb_s"] += time.perf_counter() - t
            self.counter["emb_n"] += 1
        e = emb[0].astype(np.float32)
        e = e / np.linalg.norm(e)
        h = fm[0].reshape(-1, fm.shape[-1]).T.astype(np.float64)  # (T', 2560)
        T = len(h)
        c1 = np.zeros((T + 1, h.shape[1]))
        c2 = np.zeros((T + 1, h.shape[1]))
        np.cumsum(h, axis=0, out=c1[1:])
        np.cumsum(h * h, axis=0, out=c2[1:])
        valid = np.ones(T, bool)
        for j in joins_s or []:
            k = int(j / STEP_S)
            valid[max(0, k - 1) : min(T, k + 2)] = False  # drop the straddling steps
        return e, Stats(c1, c2, valid)


# --- windowing / turns ----------------------------------------------------------------------


def spread_starts(n: int) -> list[int]:
    """The app's window count (C2_lib.window_starts: fixed + end-aligned), starts evenly spread."""
    k = len(window_starts(n))
    if k == 1:
        return [0]
    span = n - WINDOW
    return [int(round(i * span / (k - 1))) for i in range(k)]


@dataclass(slots=True)
class Turn:
    win: int
    ch: int
    start_ms: int  # body offsets
    end_ms: int
    speech_ms: int  # owned solo speech
    samples: np.ndarray | None
    joins_s: list[float] = field(default_factory=list)  # positions in the joined audio
    piece_t: list[tuple[int, int, int]] = field(default_factory=list)  # (joined_start, abs_start, len) samples


@dataclass(slots=True)
class WinInfo:
    start: int
    logits: np.ndarray
    owned: np.ndarray  # (F,) bool
    real: int  # valid frames
    active: np.ndarray  # (F, 3)


def window_turns_owned(padded: np.ndarray, logits: np.ndarray, valid: int, wi: int, off_s: int,
                       owned: np.ndarray, min_ms: int = 100) -> list[Turn]:  # fmt: skip
    active = decode(logits)
    real = min(len(active), int(valid / PER_FRAME))
    alone = active.sum(axis=1) == 1
    out: list[Turn] = []
    for ch in range(3):
        mine = active[:real, ch] & alone[:real] & owned[:real]
        speech_ms = int(mine.sum() * PER_FRAME * 1000 / SR)
        if speech_ms < min_ms:
            continue
        where = np.flatnonzero(mine)
        samples, joins, pieces = None, [], []
        if speech_ms >= MIN_EMBED_MS:
            parts, pos = [], 0
            for a, b in runs(where):
                x = padded[int(a * PER_FRAME) : int((b + 1) * PER_FRAME)]
                if pos:
                    joins.append(pos / SR)
                pieces.append((pos, off_s + int(a * PER_FRAME), len(x)))
                parts.append(x)
                pos += len(x)
            samples = np.concatenate(parts)
        start = (off_s + int(where[0] * PER_FRAME)) * 1000 // SR
        end = (off_s + int((where[-1] + 1) * PER_FRAME)) * 1000 // SR
        out.append(Turn(wi, ch, start, end, speech_ms, samples, joins, pieces))
    return out


def owned_masks(starts: list[int], n: int) -> list[np.ndarray]:
    centres = np.array([s + WINDOW / 2 for s in starts])
    out = []
    for i, s in enumerate(starts):
        ct = s + (np.arange(FRAMES) + 0.5) * PER_FRAME
        if len(starts) == 1:
            out.append(np.ones(FRAMES, bool))
        else:
            out.append(np.argmin(np.abs(ct[:, None] - centres[None, :]), axis=1) == i)
    return out


def pair_overlap_ms(logits: np.ndarray, valid: int, owned: np.ndarray) -> dict[tuple[int, int], float]:
    """Owned frames whose argmax is a pair class, per channel pair."""
    real = min(len(logits), int(valid / PER_FRAME))
    best = np.argmax(logits[:real], axis=-1)
    o = owned[:real]
    out = {}
    for cls in (4, 5, 6):
        out[_POWERSET[cls]] = float(((best == cls) & o).sum() * PER_FRAME * 1000 / SR)
    return out


def segment_windows(models, body: np.ndarray):
    """-> list of (turns, pair-overlap dict, logits, start) per window."""
    starts = spread_starts(len(body))
    masks = owned_masks(starts, len(body))
    out = []
    for wi, (st, own) in enumerate(zip(starts, masks, strict=True)):
        chunk = body[st : st + WINDOW]
        padded = np.zeros(WINDOW, np.float32)
        padded[: len(chunk)] = chunk
        logits = models.segment_logits(padded)
        turns = window_turns_owned(padded, logits, len(chunk), wi, st, own)
        out.append((turns, pair_overlap_ms(logits, len(chunk), own), logits, st, own, len(chunk)))
    return out


def joined_to_abs_ms(turn: Turn, pos_samples: np.ndarray) -> np.ndarray:
    """Map positions in the turn's joined audio (samples) to body-absolute ms."""
    out = np.zeros(len(pos_samples))
    for js, ab, ln in turn.piece_t:
        m = (pos_samples >= js) & (pos_samples < js + ln)
        out[m] = (ab + pos_samples[m] - js) * 1000 / SR
    last = turn.piece_t[-1]
    m = pos_samples >= last[0] + last[2]
    out[m] = (last[1] + last[2]) * 1000 / SR
    return out
