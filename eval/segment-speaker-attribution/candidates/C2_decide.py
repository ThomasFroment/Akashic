"""C2 decision rule, shared by the threshold tuning (C2_fit.py) and the runtime (C2_calib.py).

Input: the segment's turns (window, channel, solo speech, unit embedding or None) and the
profiles (unit centroids) with each profile's per-bucket LLR constants.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from C2_lib import bucket

VETO_MS = 300  # a second local channel with this much solo speech in one window


@dataclass(frozen=True, slots=True)
class Params:
    tau_a: float  # accept: clipped LLR sum for k*
    margin: float  # accept: sum(k*) - sum(runner-up)
    tau_v: float  # Wald-style veto: every embedded turn has LLR(k*) >= -tau_v
    tau_c: float  # a window with two channels: each channel needs LLR(k*) >= tau_c
    ovl_ms: float = 1e9  # overlap veto: a window with this much two-voice speech -> NULL
    clip: float = 8.0  # per-turn LLR clip


def llr_matrix(embs: np.ndarray, ms: np.ndarray, cents: np.ndarray, consts: np.ndarray):
    """embs (T, D) unit, cents (K, D) unit, consts (K, 5, 2) per profile and bucket (a, b).
    Returns cos (T, K) and LLR (T, K)."""
    cos = embs @ cents.T
    bk = np.array([bucket(m) for m in ms])
    a = consts[:, bk, 0].T  # (T, K)
    b = consts[:, bk, 1].T
    return cos, a * cos + b


def decide(turns, cents: np.ndarray, consts: np.ndarray, p, ovl=()):
    """turns: list of (win, ch, speech_ms, emb|None); p: Params, or a function k -> Params
    (the thresholds of the chosen profile). Returns (k* index or None, info)."""
    info: dict = {"reason": None}
    emb_idx = [i for i, t in enumerate(turns) if t[3] is not None]
    if not emb_idx or len(cents) == 0:
        info["reason"] = "no_evidence" if not emb_idx else "no_profiles"
        return None, info
    embs = np.stack([turns[i][3] for i in emb_idx])
    ms = np.array([turns[i][2] for i in emb_idx], float)
    cos, llr = llr_matrix(embs, ms, cents, consts)
    info["cos"], info["llr"], info["emb_idx"] = cos, llr, emb_idx
    clip = p.clip if isinstance(p, Params) else Params(0, 0, 0, 0).clip  # same for all
    sums = np.clip(llr, -clip, clip).sum(axis=0)
    order = np.argsort(-sums)
    k = int(order[0])
    if not isinstance(p, Params):
        p = p(k)
    info["k"], info["sum"] = k, float(sums[k])
    info["margin"] = float(sums[k] - sums[order[1]]) if len(order) > 1 else float("inf")
    # segmentation veto: windows holding two channels with >= VETO_MS of solo speech
    by_win: dict[int, list[int]] = {}
    for i, t in enumerate(turns):
        if t[2] >= VETO_MS:
            by_win.setdefault(t[0], []).append(i)
    multi = [ix for ix in by_win.values() if len(ix) >= 2]
    info["multi"] = bool(multi)
    pos = {i: j for j, i in enumerate(emb_idx)}
    for ix in multi:
        for i in ix:
            if i not in pos or llr[pos[i], k] < p.tau_c:
                info["reason"] = "seg_veto"
                return None, info
    info["ovl"] = max(ovl, default=0.0)
    if info["ovl"] >= p.ovl_ms:
        info["reason"] = "overlap_veto"
        return None, info
    if info["sum"] < p.tau_a:
        info["reason"] = "low_sum"
        return None, info
    if info["margin"] < p.margin:
        info["reason"] = "margin"
        return None, info
    if float(llr[:, k].min()) < -p.tau_v:
        info["reason"] = "llr_veto"
        return None, info
    return k, info
