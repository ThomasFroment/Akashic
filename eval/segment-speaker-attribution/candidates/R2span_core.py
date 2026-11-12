r"""QA-R2-C1 core, shared by the cache/fit (R2span_cache.py, R2span_fit.py) and the runtime
candidate (R2span_C1.py), so trials are built exactly as the candidate builds them.

Per segment body (the lead is dropped, as C2):
- windows: the app's count (C2 window_starts), starts spread evenly, frames owned by the nearest
  window centre; per window every local channel's owned solo frames; >= 300 ms -> embedded with
  the derived model (one forward = the app's embedding + the frame map);
- span bank per embedded channel: sub-windows of 6 steps (0.48 s) at hop 2 and edge spans of
  0.4/0.7/1/1.5/2.5 s at both ends of the joined audio, over steps not next to a run join
  (>= 4 usable steps); the rest of its group must keep >= 8 steps;
- groups: channels of one window merged when the calibrated same-voice LLR of the pair >= tau_m;
  a group's statistics are the exact sums of its channels' statistics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from C2_lib import MIN_EMBED_MS, PER_FRAME, SR, bucket
from R2span_lib import STEP_S, FrameEmbedder, Head, joined_to_abs_ms, segment_windows

SUB_STEPS, SUB_HOP, MIN_STEPS, MIN_REST = 6, 2, 4, 8
EDGE_S = (0.4, 0.7, 1.0, 1.5, 2.5)
# iteration 2: a channel under 1 s (bucket 0, EER ~9%) is never merged, and overlap frames
# between merged channels still count (iteration 1 merged backchannels and lifted their overlap)
MERGE_MIN_MS = 1000


@dataclass(slots=True)
class Chan:
    win: int
    ch: int
    start_ms: int
    end_ms: int
    speech_ms: int
    emb: np.ndarray | None  # unit, None when < 300 ms
    T: int = 0  # frame-map steps
    tot: tuple | None = None  # (n, s1, s2) over all steps
    bank: dict | None = None  # span bank (see span_bank)
    pieces_abs: list = field(default_factory=list)  # (start_ms, end_ms) body time of joined runs


@dataclass(slots=True)
class SegFeat:
    chans: list[Chan]
    pair_ovl: list[dict]  # per window: {(i, j): ms} of owned pair-class frames
    n_win: int


def span_bank(head: Head, st, turn) -> dict:
    """Sub-window and edge spans of one channel: their sums (usable steps only) and body times."""
    T = st.T
    v = st.valid.astype(np.float64)
    # masked prefix sums from the unmasked ones: rebuild h only where needed
    h = np.diff(st.c1, axis=0)  # (T, 2560)
    h2 = np.diff(st.c2, axis=0)
    cv = np.concatenate([[0.0], np.cumsum(v)])
    m1 = np.concatenate([np.zeros((1, h.shape[1])), np.cumsum(h * v[:, None], axis=0)])
    m2 = np.concatenate([np.zeros((1, h.shape[1])), np.cumsum(h2 * v[:, None], axis=0)])
    spans = []  # (a, b, kind)
    for a in range(0, max(0, T - SUB_STEPS) + 1, SUB_HOP):
        spans.append((a, min(T, a + SUB_STEPS), 0))
    for ls in EDGE_S:
        k = int(round(ls / STEP_S))
        if k < T:
            spans.append((0, k, 1))
            spans.append((T - k, T, 2))
    keep = []
    for a, b, kind in spans:
        n = cv[b] - cv[a]
        if n >= MIN_STEPS and T - n >= MIN_REST:
            keep.append((a, b, kind, n))
    if not keep:
        return {"n": np.zeros(0), "s1": np.zeros((0, h.shape[1])), "s2": np.zeros((0, h.shape[1])),
                "kind": np.zeros(0, int), "t": np.zeros((0, 2)), "e": np.zeros((0, 256), np.float32)}  # fmt: skip
    a = np.array([k[0] for k in keep])
    b = np.array([k[1] for k in keep])
    n = np.array([k[3] for k in keep], float)
    s1 = m1[b] - m1[a]
    s2 = m2[b] - m2[a]
    step = STEP_S * SR
    t = np.stack([joined_to_abs_ms(turn, a * step), joined_to_abs_ms(turn, np.minimum(b * step, len(turn.samples) - 1))], 1)
    e = head.embed(n, s1, s2).astype(np.float32)
    return {"n": n, "s1": s1, "s2": s2, "kind": np.array([k[2] for k in keep]), "t": t, "e": e}


def analyse(models, fe: FrameEmbedder, body: np.ndarray, banks: bool = True, keep_logits: bool = False):
    chans: list[Chan] = []
    pair_ovl = []
    logits = []
    wins = segment_windows(models, body)
    for wi, (turns, povl, lg, _st, _own, _valid) in enumerate(wins):
        pair_ovl.append(povl)
        if keep_logits:
            logits.append((_valid, lg.astype(np.float16), _st, _own))
        for t in turns:
            c = Chan(wi, t.ch, t.start_ms, t.end_ms, t.speech_ms, None)
            if t.samples is not None:
                e, st = fe.forward(t.samples, t.joins_s)
                c.emb, c.T = e, st.T
                c.tot = st.total()
                c.pieces_abs = [(ab * 1000 // SR, (ab + ln) * 1000 // SR) for _js, ab, ln in t.piece_t]
                c.bank = span_bank(fe.head, st, t) if banks else {"_st": st, "_turn": t}
            chans.append(c)
    f = SegFeat(chans, pair_ovl, len(wins))
    return (f, logits) if keep_logits else f


def ensure_bank(fe: FrameEmbedder, c: Chan) -> dict:
    if c.bank is not None and "_st" in c.bank:
        c.bank = span_bank(fe.head, c.bank["_st"], c.bank["_turn"])
    return c.bank


# --- groups ---------------------------------------------------------------------------------


def logit(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    return x @ w[:-1] + w[-1]


def pair_feats(cos: float, d1_ms: float, d2_ms: float) -> np.ndarray:
    lo, hi = sorted((d1_ms / 1000, d2_ms / 1000))
    return np.array([cos, np.log(lo), np.log(hi)])


def span_feats(cos: np.ndarray, n: np.ndarray, nrest: np.ndarray) -> np.ndarray:
    return np.stack([cos, np.log(n * STEP_S), np.log(nrest * STEP_S)], 1)


def groups_for(f: SegFeat, rules: list[tuple[np.ndarray, float]]) -> list[list[int]]:
    """Chan indices grouped: embedded channels of one window merged when LLR_same >= tau_m under
    every (w_pair, tau_m) rule given (no rule: no merge)."""
    parent = list(range(len(f.chans)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if rules:
        by: dict[int, list[int]] = {}
        for i, c in enumerate(f.chans):
            if c.emb is not None:
                by.setdefault(c.win, []).append(i)
        for ix in by.values():
            for p in range(len(ix)):
                for q in range(p + 1, len(ix)):
                    a, b = f.chans[ix[p]], f.chans[ix[q]]
                    if min(a.speech_ms, b.speech_ms) < MERGE_MIN_MS:
                        continue
                    x = pair_feats(float(a.emb @ b.emb), a.speech_ms, b.speech_ms)
                    if all(float(logit(x, w)) >= t for w, t in rules):
                        parent[find(ix[q])] = find(ix[p])
    out: dict[int, list[int]] = {}
    for i in range(len(f.chans)):
        out.setdefault(find(i), []).append(i)
    return list(out.values())


def group_emb(head: Head, f: SegFeat, g: list[int]) -> np.ndarray:
    if len(g) == 1:
        return f.chans[g[0]].emb
    n = sum(f.chans[i].tot[0] for i in g)
    s1 = sum(f.chans[i].tot[1] for i in g)
    s2 = sum(f.chans[i].tot[2] for i in g)
    return head.embed(np.array([n]), s1[None], s2[None])[0].astype(np.float32)


def turns_and_ovl(head: Head, f: SegFeat, groups: list[list[int]]):
    """-> (turns for C2 decide: (win, gid, speech_ms, emb|None), ovl per window, group embs)."""
    turns, embs = [], []
    gid_of = {}
    for gi, g in enumerate(groups):
        for i in g:
            gid_of[i] = gi
        c0 = f.chans[g[0]]
        ms = sum(f.chans[i].speech_ms for i in g)
        e = group_emb(head, f, g) if c0.emb is not None else None
        turns.append((c0.win, gi, ms, e))
        embs.append(e)
    ovl = []
    for wi, po in enumerate(f.pair_ovl):
        tot = 0.0
        for (a, b), ms in po.items():
            tot += ms  # iteration 2: kept even between merged channels
        ovl.append(tot)
    return turns, ovl, embs


def group_spans(head: Head, f: SegFeat, g: list[int]):
    """Spans of every member channel against the rest of the group: (eS, cos, n, nrest, kind, t)."""
    if f.chans[g[0]].emb is None:
        return None
    N = sum(f.chans[i].tot[0] for i in g)
    S1 = sum(f.chans[i].tot[1] for i in g)
    S2 = sum(f.chans[i].tot[2] for i in g)
    out = []
    for i in g:
        bk = f.chans[i].bank
        if bk is None or len(bk["n"]) == 0:
            continue
        nr = N - bk["n"]
        er = head.embed(nr, S1[None] - bk["s1"], S2[None] - bk["s2"])
        cos = np.einsum("jd,jd->j", bk["e"].astype(np.float64), er)
        out.append((bk["e"], cos, bk["n"], nr, bk["kind"], bk["t"]))
    if not out:
        return None
    return tuple(np.concatenate([o[k] for o in out]) for k in range(6))


def loo_stats(sp, k: int, C: np.ndarray, K: np.ndarray, w_span: np.ndarray):
    """-> (max -z, max Delta) over the spans: z = same-voice LLR of span vs rest, Delta = best
    other profile's LLR minus k*'s, on the span embedding (bucket constants by span length)."""
    if sp is None or len(sp[0]) == 0:
        return -np.inf, -np.inf
    eS, cos, n, nr, _kind, _t = sp
    z = logit(span_feats(cos, n, nr), w_span)
    cs = eS.astype(np.float64) @ C.T  # (J, P)
    bk = np.array([bucket(x * STEP_S * 1000) for x in n])
    llr = K[:, bk, 0].T * cs + K[:, bk, 1].T
    if llr.shape[1] > 1:
        other = np.delete(llr, k, axis=1).max(1)
        delta = float((other - llr[:, k]).max())
    else:
        delta = -np.inf
    return float((-z).max()), delta


# --- calibration helper ----------------------------------------------------------------------


def irls(X: np.ndarray, y: np.ndarray, w: np.ndarray, iters: int = 100, ridge: float = 1e-4) -> np.ndarray:
    """Weighted logistic regression, features X (n, d) + intercept -> weights (d + 1,)."""
    Xb = np.concatenate([X, np.ones((len(X), 1))], 1)
    beta = np.zeros(Xb.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Xb @ beta)))
        g = Xb.T @ (w * (y - p)) - ridge * beta
        H = (Xb * (w * p * (1 - p))[:, None]).T @ Xb + ridge * np.eye(len(beta))
        step = np.linalg.solve(H, g)
        beta += step
        if np.abs(step).max() < 1e-9:
            break
    return beta


_ = (MIN_EMBED_MS, PER_FRAME)
