"""C5: C2 (gated exact sparse clusters) + live/toggle temporal split, the previous check as a
second reference. Synthetic-harness candidate (contract in harness/hcommon.py).

Verdict per check (store = True):
  1. shapes differ -> True; ref == cur -> False.
  2. Dense regime (today's test, unchanged): more than 1e-4 of the reduced pixels moved > 16
     levels -> True. So every frame today stores is still stored (superset).
  3. Sparse regime: moved points (np.flatnonzero of the uint8 max-min mask) are linked at
     Chebyshev distance <= 2 into exact connected components (D_s = now vs stored reference).
     A cluster is shape-positive when >= 3 wide, >= 2 high and >= 5 points (C2).
     For each positive cluster c, box = bbox(c) +- 1:
       p_pos(c)   = the same shape rule is positive in D_p = now vs previous check, inside box
       recorded   = blake2b(now[box]) is one of the <= 2 contents recorded at box (at stores)
       toggle(c)  = recorded and (not p_pos or blake2b(prev[box]) is recorded too): an exact
                    A-B-A between known states
       live(c)    = p_pos and box overlaps a box that was fresh (p_pos) at the previous check
     SPEC (as candidates.md): c suppressed if live or toggle.
     FIX1 (default): a toggle is stored while the box has toggled < TOGGLE_BUDGET times in its
       current episode (episode = changes no more than QUIET_S apart), then suppressed; live
       applies only to non-toggle clusters. Badge flaps (A-B-A on consecutive checks) need it.
     Store iff some positive cluster is not suppressed. A suppressed change stays in D_s and is
     stored at the first check where it is neither live nor a suppressed toggle.
  TASKBAR_BAND (FIX2, off by default): clusters entirely inside the bottom taskbar band keep
     today's behaviour (only the dense regime can store them).
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

import numpy as np

PIXEL_DELTA = np.uint8(16)
CHANGED_FRACTION = 1e-4
LINK = 2
MIN_W, MIN_H, MIN_N = 3, 2, 5
MAX_REGION_POINTS = 256  # D_p points in one box above this count as shape-positive
MAX_RECORDS = 64  # boxes remembered per monitor (LRU)

SPEC_TOGGLES = True  # v0: candidates.md as written
TOGGLE_BUDGET = 7  # FIX1: toggles stored per episode (harness badge: up to 4 shows = 7 flips)
QUIET_S = 60.0  # FIX1: a box with no change for this long starts a new episode
TASKBAR_BAND = False  # FIX2
TASKBAR_ROWS = {"4k": 15, "1440p": 12}  # 48 CSS px taskbar / 4 (60 device px at 125 %)


class State:
    __slots__ = ("fresh", "records", "check")

    def __init__(self) -> None:
        self.fresh: list[tuple[int, int, int, int]] = []  # boxes fresh at the previous check
        self.records: OrderedDict = OrderedDict()  # box -> [hashes(list<=2), toggles, last_change_s]
        self.check = 0


def make_state() -> State:
    return State()


def _mask(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (np.maximum(a, b) - np.minimum(a, b)) > PIXEL_DELTA


def _positive_boxes(ys: np.ndarray, xs: np.ndarray, first_only: bool = False) -> list[tuple[int, int, int, int]]:
    """Exact Chebyshev-<=2 components; boxes (y0, y1, x0, x1) inclusive of the positive ones."""
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
        if x1 - x0 + 1 >= MIN_W and y1 - y0 + 1 >= MIN_H:
            out.append((y0, y1, x0, x1))
            if first_only:
                return out
    return out


def _region_positive(cur: np.ndarray, prev: np.ndarray, box) -> bool:
    y0, y1, x0, x1 = box
    m = _mask(cur[y0:y1, x0:x1], prev[y0:y1, x0:x1])
    flat = np.flatnonzero(m)
    if flat.size > MAX_REGION_POINTS:
        return True
    ys, xs = np.divmod(flat, x1 - x0)
    return bool(_positive_boxes(ys, xs, first_only=True))


def _h(a: np.ndarray, box) -> bytes:
    y0, y1, x0, x1 = box
    return hashlib.blake2b(a[y0:y1, x0:x1].tobytes(), digest_size=8).digest()


def _overlaps(b, others) -> bool:
    y0, y1, x0, x1 = b
    return any(y0 < o[1] and o[0] < y1 and x0 < o[3] and o[2] < x1 for o in others)


def decide(state: State, ref_gray, cur_gray, prev_gray, monitor, now: float = 0.0) -> bool:
    state.check += 1
    if ref_gray.shape != cur_gray.shape:
        state.fresh = []
        return True
    if np.array_equal(ref_gray, cur_gray):
        state.fresh = []
        return False
    m = _mask(ref_gray, cur_gray)
    n = int(np.count_nonzero(m))
    if n / m.size > CHANGED_FRACTION:  # today's verdict (same comparison as moved.mean())
        state.fresh = []
        return True
    h, w = m.shape
    ys, xs = np.divmod(np.flatnonzero(m), w)
    clusters = _positive_boxes(ys, xs)
    if TASKBAR_BAND:
        band = h - TASKBAR_ROWS.get(monitor, 15)
        clusters = [c for c in clusters if c[1] < band]
    if not clusters:
        state.fresh = []
        return False
    have_prev = prev_gray is not None and prev_gray.shape == cur_gray.shape
    fresh_prev, fresh_now = state.fresh, []
    rows = []  # (box, h_cur, is_toggle_path)
    fire = False
    for y0, y1, x0, x1 in clusters:
        box = (max(0, y0 - 1), min(h, y1 + 2), max(0, x0 - 1), min(w, x1 + 2))
        p_pos = (not have_prev) or _region_positive(cur_gray, prev_gray, box)
        rec = state.records.get(box)
        if rec is not None and p_pos and now - rec[2] > QUIET_S:
            rec[1] = 0  # new episode
        hc = _h(cur_gray, box)
        recorded = rec is not None and hc in rec[0]
        toggle = recorded and (not p_pos or (have_prev and _h(prev_gray, box) in rec[0]))
        live = p_pos and _overlaps(box, fresh_prev)
        if SPEC_TOGGLES:
            store_c = not (toggle or live)
        elif toggle:
            store_c = rec[1] < TOGGLE_BUDGET
        else:
            store_c = not live
        fire |= store_c
        if p_pos:
            fresh_now.append(box)
            if rec is not None:
                rec[2] = now
        rows.append((box, hc, toggle))
    state.fresh = fresh_now
    if fire:
        for box, hc, toggle in rows:
            rec = state.records.get(box)
            if rec is None:
                rec = state.records[box] = [[], 0, now]
            else:
                state.records.move_to_end(box)
            for hh in (_h(ref_gray, box), hc):
                if hh not in rec[0]:
                    rec[0].append(hh)
                    del rec[0][:-2]
            if toggle:
                rec[1] += 1
            rec[2] = now
        while len(state.records) > MAX_RECORDS:
            state.records.popitem(last=False)
    return fire
