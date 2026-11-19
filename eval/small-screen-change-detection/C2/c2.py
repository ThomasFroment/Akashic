"""C2 Gated exact sparse clusters (with fix round 1: low-delta hysteresis).

verdict = today's (more than 1e-4 of the reduced pixels moved > 16 levels) OR, when
0 < N <= 1e-4 * size: link the moved points at Chebyshev distance <= 2, take exact connected
components (boolean closure of the n x n adjacency, n <= 51), fire when a component is >= 3
wide, >= 2 high and has >= 5 points.

Fix round 1 (delta-64 words missed at > 16 levels: 3-6 points, sometimes one row): a
component that fails the test is re-tested on the points moved > LOW_DELTA inside its bounding
box grown by LINK (hysteresis: the low mask is only read around points that moved > 16, so the
gate and the superset are unchanged). A bar caret touches <= 2 reduced columns whatever the
delta, so the width test still vetoes it.

Fix round 2 (taskbar minute clock fired up to 48/h per 4K monitor, 54/h with the hysteresis):
TASKBAR_ROWS: reduced rows at the bottom of the frame where the sparse test is skipped (today's
behaviour kept there); 0 = test everywhere.
"""

import numpy as np

PIXEL_DELTA = np.uint8(16)
LOW_DELTA = np.uint8(8)
HYSTERESIS = True
CHANGED_FRACTION = 1e-4
LINK = 2
MIN_W, MIN_H, MIN_N = 3, 2, 5
TASKBAR_ROWS = {"4k": 15, "1440p": 12}  # 48 CSS px taskbar band, reduced 4x (fix round 2)
_BIG = np.int64(1 << 30)


def make_state():
    return None


def components(xs: np.ndarray, ys: np.ndarray):
    """Exact components at Chebyshev distance <= LINK: (w, h, n, x0, y0, x1, y1) per component."""
    adj = (np.abs(xs[:, None] - xs[None, :]) <= LINK) & (np.abs(ys[:, None] - ys[None, :]) <= LINK)
    count = int(adj.sum())
    reach = adj
    while True:
        f = reach.astype(np.float32)
        nxt = (f @ f) > 0
        c = int(nxt.sum())
        reach = nxt
        if c == count:
            break
        count = c
    x0 = np.where(reach, xs[None, :], _BIG).min(1)
    x1 = np.where(reach, xs[None, :], -_BIG).max(1)
    y0 = np.where(reach, ys[None, :], _BIG).min(1)
    y1 = np.where(reach, ys[None, :], -_BIG).max(1)
    n = reach.sum(1)
    first = reach.argmax(1)  # smallest index in the component = its label
    keep = first == np.arange(xs.size)
    return x0[keep], y0[keep], x1[keep], y1[keep], n[keep]


def _passes(x0, y0, x1, y1, n) -> np.ndarray:
    return (x1 - x0 + 1 >= MIN_W) & (y1 - y0 + 1 >= MIN_H) & (n >= MIN_N)


def decide(state, ref_gray, cur_gray, prev_gray, monitor):  # noqa: ARG001
    if ref_gray.shape != cur_gray.shape:
        return True
    if np.array_equal(ref_gray, cur_gray):
        return False
    d = np.maximum(ref_gray, cur_gray) - np.minimum(ref_gray, cur_gray)
    moved = d > PIXEL_DELTA
    n = int(np.count_nonzero(moved))
    if n == 0:
        return False
    if n / moved.size > CHANGED_FRACTION:
        return True
    h, w = moved.shape
    ys, xs = np.divmod(np.flatnonzero(moved), w)
    tb = TASKBAR_ROWS.get(monitor, 0)
    if tb:
        keep = ys < h - tb
        xs, ys = xs[keep], ys[keep]
        h -= tb
        if xs.size == 0:
            return False
    x0, y0, x1, y1, cn = components(xs, ys)
    if _passes(x0, y0, x1, y1, cn).any():
        return True
    if not HYSTERESIS:
        return False
    # hysteresis: re-test each component on the low-delta points around it
    for a0, b0, a1, b1 in zip(x0, y0, x1, y1):
        wa, wb = max(0, a0 - LINK), max(0, b0 - LINK)
        win = d[wb:min(h, b1 + LINK + 1), wa:min(w, a1 + LINK + 1)] > LOW_DELTA
        if np.count_nonzero(win) < MIN_N:
            continue
        ly, lx = np.nonzero(win)
        if _passes(*components(lx, ly)).any():
            return True
    return False
