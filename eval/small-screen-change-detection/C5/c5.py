"""C5: C2 (gated exact sparse clusters) + live/toggle temporal split, the previous check as a
second reference. Synthetic-harness candidate (contract in harness/hcommon.py).
v0 (candidates.md as written) is frozen in variants/c5_v0_spec.py; this file is after the fix
rounds (FIX1-FIX4 below, each measured in runs/).

Verdict per check (store = True):
  1. shapes differ -> True; ref == cur -> False.
  2. Dense regime (today's test, unchanged): more than 1e-4 of the reduced pixels moved > 16
     levels -> True. Every frame today stores is still stored (superset).
  3. Sparse regime, D_s = now vs stored reference: reduced pixels moved > SPARSE_DELTA (FIX3: 6,
     not 16, so delta-64 words keep their shape; above CAP_POINTS such points fall back to the
     > 16 points), linked at Chebyshev distance <= 2 into exact connected components.
     Shape-positive: (w >= 3, h >= 3, n >= 5) or (w >= W2, h >= 2, n >= 5) (FIX3: W2 = 4; C2's
     W2 = 3 fires on 2-row underscores and lines).
     For each positive cluster c, box = bbox(c) +- 1:
       toggle(c) = blake2b(now[box]) is one of the <= 2 contents recorded at box (at stores) and
                   (the box did not move since the previous check, or blake2b(prev[box]) is
                   recorded too): an exact A-B-A between known states.
                   FIX1: a toggle is stored while the box has toggled < TOGGLE_BUDGET times in
                   its episode (an episode ends after QUIET_K checks without a toggle at the
                   box), then suppressed. Badge flaps (A-B-A on consecutive checks) need it.
       otherwise live/fresh split (FIX2): a point of c is live when some reduced pixel within
                   Chebyshev LIVE_R of it moved (D_p = check vs previous check) at one of the
                   previous LIVE_K checks; c stores when its non-live points alone are
                   shape-positive. v0's box-overlap rule stored the seconds clock at every
                   check where only a settled digit differed from the reference.
     Store iff some positive cluster stores. A suppressed change stays in D_s and is stored at
     the first check where it is neither live nor a suppressed toggle.
  TASKBAR_BAND (FIX4, on): clusters entirely inside the bottom taskbar band keep
     today's behaviour (only the dense regime can store them).
State per monitor: last-move check index per reduced pixel (int32, 2.07 MB at 4K) and <= 64
box records. The previous check's gray is passed in by the harness (production: +518 KB at 4K).
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import numpy as np

GATE_DELTA = np.uint8(16)
CHANGED_FRACTION = 1e-4
SPARSE_DELTA = np.uint8(6)  # FIX3
CAP_POINTS = 128  # > this many sparse points: use the > 16 points (<= gate) instead (400: +2.5 ms on a chain)
LINK = 2
MIN_W, MIN_H, MIN_N = 3, 2, 5
W2 = {"4k": 4, "1440p": 4}  # FIX3: a 2-row cluster needs this width
MAX_RECORDS = 64

TOGGLE_BUDGET = 7  # FIX1: harness badge, up to 4 shows on consecutive checks = 7 flips
QUIET_K = 12  # FIX1: checks without a toggle at the box that end a toggle episode
LIVE_R = 2  # FIX2: reduced px (8 device px)
LIVE_K = 2  # FIX2: previous checks that make a neighbourhood live
TASKBAR_BAND = True  # FIX4 (on: the minute clock stored 26/h without it)
TASKBAR_ROWS = {"4k": 15, "1440p": 12}  # 48 CSS px taskbar / 4 (60 device px at 125 %)
_START = 1000  # first check index; 0 in the calloc-zeroed map means "never moved"
_OFF = np.arange(-LIVE_R, LIVE_R + 1)


class State:
    __slots__ = ("last", "records", "check")

    def __init__(self) -> None:
        self.last = None  # int32 [h, w]: check index of the last move (D_p), lazily allocated
        self.records: OrderedDict = OrderedDict()  # box -> [hashes (<= 2), toggles, last toggle check]
        self.check = _START


def make_state() -> State:
    return State()


def _diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d = np.maximum(a, b)
    d -= np.minimum(a, b)
    return d


def _positive(ys: np.ndarray, xs: np.ndarray, w2: int, first_only: bool = False) -> list[tuple[int, int, int, int]]:
    """Exact Chebyshev-<=LINK components; inclusive boxes (y0, y1, x0, x1) of the positive ones."""
    n = ys.size
    if n < MIN_N:
        return []
    adj = (np.abs(ys[:, None] - ys[None, :]) <= LINK) & (np.abs(xs[:, None] - xs[None, :]) <= LINK)
    lab = np.arange(n)
    while True:
        new = np.where(adj, lab[None, :], n).min(axis=1)
        new = new[new]
        if np.array_equal(new, lab):
            break
        lab = new
    out = []
    labels, counts = np.unique(lab, return_counts=True)
    for lb, cnt in zip(labels.tolist(), counts.tolist()):
        if cnt < MIN_N:
            continue
        sel = lab == lb
        y, x = ys[sel], xs[sel]
        y0, y1, x0, x1 = int(y.min()), int(y.max()), int(x.min()), int(x.max())
        cw, ch = x1 - x0 + 1, y1 - y0 + 1
        if (cw >= MIN_W and ch >= 3) or (cw >= w2 and ch >= MIN_H):
            out.append((y0, y1, x0, x1))
            if first_only:
                break
    return out


def _h(a: np.ndarray, box) -> bytes:
    y0, y1, x0, x1 = box
    return hashlib.blake2b(a[y0:y1, x0:x1].tobytes(), digest_size=8).digest()


def _update_last(state: State, cur, prev, shape) -> None:
    """Mark the reduced pixels that moved since the previous check (after the verdict)."""
    if prev is None or prev.shape != cur.shape or np.array_equal(prev, cur):
        return
    flat = np.flatnonzero(_diff(cur, prev) > SPARSE_DELTA)
    if state.last is None or state.last.shape != shape:
        state.last = np.zeros(shape, dtype=np.int32)
    state.last.flat[flat] = state.check


def decide(state: State, ref_gray, cur_gray, prev_gray, monitor) -> bool:
    state.check += 1
    if ref_gray.shape != cur_gray.shape:
        state.last = None
        state.records.clear()
        return True
    if np.array_equal(ref_gray, cur_gray):
        _update_last(state, cur_gray, prev_gray, cur_gray.shape)
        return False
    d = _diff(ref_gray, cur_gray)
    n16 = int(np.count_nonzero(d > GATE_DELTA))
    if n16 / d.size > CHANGED_FRACTION:  # today's verdict (same comparison as moved.mean())
        return True
    h, w = d.shape
    flat = np.flatnonzero(d > SPARSE_DELTA)
    if flat.size > CAP_POINTS:
        flat = np.flatnonzero(d > GATE_DELTA)
    ys, xs = np.divmod(flat, w)
    w2 = W2.get(monitor, 4)
    clusters = _positive(ys, xs, w2)
    if TASKBAR_BAND:
        band = h - TASKBAR_ROWS.get(monitor, 15)
        clusters = [c for c in clusters if c[1] < band]
    fire = False
    rows = []
    if clusters:
        have_prev = prev_gray is not None and prev_gray.shape == cur_gray.shape
        last = state.last
        live_from = state.check - LIVE_K
        for y0, y1, x0, x1 in clusters:
            box = (max(0, y0 - 1), min(h, y1 + 2), max(0, x0 - 1), min(w, x1 + 2))
            rec = state.records.get(box)
            hc = _h(cur_gray, box)
            toggle = False
            if rec is not None and hc in rec[0]:
                moved = have_prev and not np.array_equal(cur_gray[box[0]:box[1], box[2]:box[3]],
                                                         prev_gray[box[0]:box[1], box[2]:box[3]])
                toggle = (not moved) or _h(prev_gray, box) in rec[0]
            if toggle:
                if state.check - rec[2] > QUIET_K:
                    rec[1] = 0  # new episode
                rec[2] = state.check
                store_c = rec[1] < TOGGLE_BUDGET
            else:
                sel = (ys >= y0) & (ys <= y1) & (xs >= x0) & (xs <= x1)
                cy, cx = ys[sel], xs[sel]
                if last is not None:
                    yy = np.clip(cy[:, None, None] + _OFF[None, :, None], 0, h - 1)
                    xx = np.clip(cx[:, None, None] + _OFF[None, None, :], 0, w - 1)
                    keep = last[yy, xx].reshape(cy.size, -1).max(axis=1) < live_from
                    cy, cx = cy[keep], cx[keep]
                store_c = bool(_positive(cy, cx, w2, first_only=True))
            fire |= store_c
            rows.append((box, hc, toggle))
    if fire:
        for box, hc, toggle in rows:
            rec = state.records.get(box)
            if rec is None:
                rec = state.records[box] = [[], 0, state.check]
            else:
                state.records.move_to_end(box)
            for hh in (_h(ref_gray, box), hc):
                if hh not in rec[0]:
                    rec[0].append(hh)
                    del rec[0][:-2]
            if toggle:
                rec[1] += 1
        while len(state.records) > MAX_RECORDS:
            state.records.popitem(last=False)
    _update_last(state, cur_gray, prev_gray, cur_gray.shape)
    return fire
