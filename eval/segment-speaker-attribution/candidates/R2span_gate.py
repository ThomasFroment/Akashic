r"""QA-R2-C2 pieces shared by the gate fit (R2span_C2fit.py) and the candidate (R2span_C2.py).

The proposal step of the block is dropped (gate (b) failed: out-of-fold recall 0.42 at <= 8
proposals per segment, out\R2span_gates\gate_b.json), so C2 = C1's groups and span statistics
+ one logistic gate replacing the hand thresholds. Gate features per segment and k*:
C2 fusion sum, margin, min turn LLR(k*), min LLR(k*) in two-group windows, max unmerged
same-voice pair LLR, ln(1 + overlap ms), max box E, max box D (segmentation residual mass, owned
frames), max -z and max Delta over all spans, ln embedded speech seconds.
"""

from __future__ import annotations

import numpy as np

from C2_lib import PER_FRAME
from R2span_core import logit, pair_feats

BOX = 18
_PS = ((), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2))
_M = np.zeros((7, 3))
for _c, _mem in enumerate(_PS):
    for _j in _mem:
        _M[_c, _j] = 1
FEATS = ["sum", "margin", "min_llr", "cmin", "lsame", "ln_ovl", "maxE", "maxD", "max_negz", "max_delta", "ln_speech"]


def ed_max(logits) -> tuple[float, float]:
    """logits: list of (valid samples, (F, 7) logits, start, owned mask)."""
    me = md = 0.0
    k = np.ones(BOX) / BOX
    for valid, lg, _st, own in logits:
        lg = np.asarray(lg, np.float32)
        real = min(len(lg), int(valid / PER_FRAME))
        lg = lg[:real]
        p = np.exp(lg - lg.max(1, keepdims=True))
        p /= p.sum(1, keepdims=True)
        best = lg.argmax(1)
        solo = (best >= 1) & (best <= 3) & own[:real]
        m = np.clip(best - 1, 0, 2)
        a = p @ _M
        a[np.arange(real), m] = -1
        E = np.where(solo, a.max(1), 0.0)
        D = np.where(solo, 1 - p[np.arange(real), best] - p[:, 0], 0.0)
        if real >= BOX:
            me = max(me, float(np.convolve(E, k, "valid").max()))
            md = max(md, float(np.convolve(D, k, "valid").max()))
    return me, md


def lsame_max(chans, groups, w_pair) -> float:
    """Max same-voice LLR over embedded channel pairs of one window left in different groups."""
    gid = {i: gi for gi, g in enumerate(groups) for i in g}
    best = -20.0
    for p in range(len(chans)):
        for q in range(p + 1, len(chans)):
            a, b = chans[p], chans[q]
            if a["win"] != b["win"] or a["emb"] is None or b["emb"] is None or gid[p] == gid[q]:
                continue
            x = pair_feats(float(np.asarray(a["emb"], np.float64) @ np.asarray(b["emb"], np.float64)), a["ms"], b["ms"])
            best = max(best, float(logit(x, w_pair)))
    return best


def vector(info: dict, k: int, turns, ovl, lsame: float, me: float, md: float, s1: float, s2: float) -> np.ndarray:
    llr = info["llr"][:, k]
    pos = {i: j for j, i in enumerate(info["emb_idx"])}
    by: dict[int, list[int]] = {}
    for i, t in enumerate(turns):
        if t[2] >= 300:
            by.setdefault(t[0], []).append(i)
    cmin = min((llr[pos[i]] for ix in by.values() if len(ix) >= 2 for i in ix if i in pos), default=20.0)
    speech = sum(t[2] for t in turns if t[3] is not None) / 1000
    s1 = s1 if np.isfinite(s1) else -10.0
    s2 = s2 if np.isfinite(s2) else -10.0
    v = [info["sum"], min(info["margin"], 40.0), float(llr.min()), cmin, lsame, np.log1p(max(ovl, default=0.0)),
         me, md, s1, s2, np.log(max(speech, 0.3))]  # fmt: skip
    v = np.array(v, float)
    v[~np.isfinite(v)] = 0.0
    return np.clip(v, -40, 40)


def score(v: np.ndarray, g: dict) -> float:
    z = (v - np.array(g["mu"])) / np.array(g["sd"])
    w = np.array(g["w"])
    return float(z @ w[:-1] + w[-1])
