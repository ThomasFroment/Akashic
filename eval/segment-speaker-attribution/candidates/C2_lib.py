"""C2 helpers: windowing with an end-aligned last window, turn extraction that keeps every
local channel (for the segmentation veto), duration buckets, IRLS logistic calibration, Cllr.

Shared by the offline calibration (C2_fit.py) and the runtime candidate (C2_calib.py), so the
trials are built with exactly the turn construction the candidate uses.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SR = 16_000
WINDOW = 10 * SR
MIN_TAIL = int(0.3 * SR)  # an end-aligned extra window only when >= 300 ms is still uncovered
MIN_EMBED_MS = 300  # channels with less solo speech are counted for the veto only
FRAMES = 589  # segmentation-3.0 frames per 10 s window
PER_FRAME = WINDOW / FRAMES  # samples
_POWERSET = ((), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2))

# Duration buckets (seconds of solo speech in a turn): 0.3-1, 1-2, 2-4, 4-8, >= 8.
EDGES = (0.3, 1.0, 2.0, 4.0, 8.0)


def bucket(speech_ms: float) -> int:
    s = speech_ms / 1000
    b = 0
    for i, e in enumerate(EDGES):
        if s >= e:
            b = i
    return b


def window_starts(n: int) -> list[int]:
    """Fixed 10 s windows from 0; the last one is end-aligned (a full 10 s ending at the end
    of the audio) instead of a short zero-padded tail. A tail under 300 ms is skipped."""
    if n <= WINDOW:
        return [0]
    starts = list(range(0, n - WINDOW + 1, WINDOW))
    covered = starts[-1] + WINDOW
    if n - covered >= MIN_TAIL:
        starts.append(n - WINDOW)
    return starts


def decode(logits: np.ndarray) -> np.ndarray:
    best = np.argmax(logits, axis=-1)
    active = np.zeros((len(best), 3), dtype=np.bool_)
    for cls, members in enumerate(_POWERSET):
        for spk in members:
            active[best == cls, spk] = True
    return active


@dataclass(slots=True)
class Turn:
    win: int  # window index
    ch: int  # local channel in that window
    start_ms: int  # offsets in the body (after the lead)
    end_ms: int
    speech_ms: int  # solo speech of this channel in this window
    samples: np.ndarray | None  # joined solo speech (None when under MIN_EMBED_MS)


def runs(idx: np.ndarray) -> list[tuple[int, int]]:
    br = np.flatnonzero(np.diff(idx) > 1)
    firsts = np.concatenate([[0], br + 1])
    lasts = np.concatenate([br, [len(idx) - 1]])
    return [(int(idx[a]), int(idx[b])) for a, b in zip(firsts, lasts, strict=True)]


def window_turns(win: np.ndarray, logits: np.ndarray, valid: int, wi: int, off: int,
                 min_ms: int = 100) -> list[Turn]:  # fmt: skip
    """Every local channel with >= ``min_ms`` of solo speech in this window: all frames of the
    window count (an end-aligned window is not cut to its new part)."""
    active = decode(logits)
    real = min(len(active), int(valid / PER_FRAME))
    alone = active.sum(axis=1) == 1
    out: list[Turn] = []
    for ch in range(3):
        mine = active[:real, ch] & alone[:real]
        speech_ms = int(mine.sum() * PER_FRAME * 1000 / SR)
        if speech_ms < min_ms:
            continue
        where = np.flatnonzero(mine)
        samples = None
        if speech_ms >= MIN_EMBED_MS:
            samples = np.concatenate(
                [win[int(a * PER_FRAME) : int((b + 1) * PER_FRAME)] for a, b in runs(where)]
            )
        start = off + int(where[0] * PER_FRAME * 1000 / SR)
        end = off + int((where[-1] + 1) * PER_FRAME * 1000 / SR)
        out.append(Turn(wi, ch, start, end, speech_ms, samples))
    return out


def overlap_ms(logits: np.ndarray, valid: int) -> float:
    """Speech the segmentation labels as two voices at once (argmax is a pair class)."""
    real = min(len(logits), int(valid / PER_FRAME))
    return float((np.argmax(logits[:real], axis=-1) >= 4).sum() * PER_FRAME * 1000 / SR)


def segment_turns(models, body: np.ndarray, keep_logits: bool = False):
    """Run segmentation on the body's windows; return (turns, overlap ms per window) and the
    logits if asked."""
    turns: list[Turn] = []
    ovl: list[float] = []
    all_logits = []
    for wi, st in enumerate(window_starts(len(body))):
        chunk = body[st : st + WINDOW]
        padded = np.zeros(WINDOW, np.float32)
        padded[: len(chunk)] = chunk
        logits = models.segment_logits(padded)
        if keep_logits:
            all_logits.append(logits)
        turns.extend(window_turns(padded, logits, len(chunk), wi, st * 1000 // SR))
        ovl.append(overlap_ms(logits, len(chunk)))
    return (turns, ovl, all_logits) if keep_logits else (turns, ovl)


# --- calibration --------------------------------------------------------------------------


def irls_logistic(s: np.ndarray, y: np.ndarray, w: np.ndarray, iters: int = 50,
                  ridge: float = 1e-6) -> tuple[float, float]:  # fmt: skip
    """Weighted logistic regression y ~ sigmoid(a*s + b) by IRLS (Newton). With target and
    non-target weights each summing to 1/2 the fitted a*s + b is an LLR (prior 0.5)."""
    X = np.stack([s, np.ones_like(s)], axis=1)
    beta = np.zeros(2)
    for _ in range(iters):
        z = X @ beta
        p = 1 / (1 + np.exp(-z))
        g = X.T @ (w * (y - p)) - ridge * beta
        H = (X * (w * p * (1 - p))[:, None]).T @ X + ridge * np.eye(2)
        step = np.linalg.solve(H, g)
        beta += step
        if np.abs(step).max() < 1e-9:
            break
    return float(beta[0]), float(beta[1])


def balanced_weights(y: np.ndarray) -> np.ndarray:
    nt, nn = max(1, int(y.sum())), max(1, int((1 - y).sum()))
    return np.where(y == 1, 0.5 / nt, 0.5 / nn)


def cllr(llr_tar: np.ndarray, llr_non: np.ndarray) -> float:
    """Brummer's Cllr in bits (natural-log LLRs)."""
    c_t = np.mean(np.logaddexp(0, -llr_tar)) / np.log(2)
    c_n = np.mean(np.logaddexp(0, llr_non)) / np.log(2)
    return float(0.5 * (c_t + c_n))


def min_cllr(scores_tar: np.ndarray, scores_non: np.ndarray) -> float:
    """Cllr after the optimal monotone (PAV) calibration: discrimination only."""
    s = np.concatenate([scores_tar, scores_non])
    y = np.concatenate([np.ones(len(scores_tar)), np.zeros(len(scores_non))])
    order = np.argsort(s, kind="mergesort")
    yv = y[order].astype(float)
    # PAV on posterior P(target | score)
    vals, wts, lens = [], [], []
    for v in yv:
        vals.append(v)
        wts.append(1.0)
        lens.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            v2, w2, l2 = vals.pop(), wts.pop(), lens.pop()
            vals[-1] = (vals[-1] * wts[-1] + v2 * w2) / (wts[-1] + w2)
            wts[-1] += w2
            lens[-1] += l2
    post = np.repeat(vals, lens)
    eps = 1e-6
    post = np.clip(post, eps, 1 - eps)
    prior_odds = len(scores_tar) / len(scores_non)
    llr = np.log(post / (1 - post)) - np.log(prior_odds)
    llr_sorted = np.empty_like(llr)
    llr_sorted[order] = llr
    return cllr(llr_sorted[y == 1], llr_sorted[y == 0])


def eer(scores_tar: np.ndarray, scores_non: np.ndarray) -> float:
    th = np.sort(np.concatenate([scores_tar, scores_non]))
    fr = np.searchsorted(np.sort(scores_tar), th, side="left") / len(scores_tar)
    fa = 1 - np.searchsorted(np.sort(scores_non), th, side="left") / len(scores_non)
    i = int(np.argmin(np.abs(fr - fa)))
    return float((fr[i] + fa[i]) / 2)
