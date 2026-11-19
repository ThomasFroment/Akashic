"""C3: anisotropic reduction 4 wide x 2 tall + gated sparse span test.

Signature gray = Pillow reduce((4, 2)) -> L, so a 1-3 px caret covers <= 2 columns while an
x-height of ~7 device px covers >= 3 rows. Verdict:
  * equal frames -> False (production: equal digest);
  * N = moved 4x2 pixels (|a-b| > 16, exact uint8 form) ;
  * N > 1e-4 x (number of 4x4 pixels) -> True: every moved 4x4 block has a moved 4x2 half, so
    this is a superset of today's verdict up to Pillow rounding;
  * else, on the moved coordinates only: 16-col x 32-row tiles (64x64 device px) on four grids
    offset (0,0), (0,8c), (16r,0), (16r,8c); a tile fires on cols >= 3, rows >= 3, count >= 4.

TASKBAR_PX (device px, per monitor) > 0 keeps the sparse shape test out of the bottom band
(today's behaviour there); 0 disables that exclusion.
"""

import numpy as np

PIXEL_DELTA = 16
CHANGED_FRACTION = 1e-4
TILE_W, TILE_H = 16, 32
GRIDS = ((0, 0), (0, 8), (16, 0), (16, 8))  # (row offset, col offset)
MIN_W, MIN_H, MIN_N = 3, 3, 4
TASKBAR_PX = {"4k": 0, "1440p": 0}
FY = 2


def reduce(img):
    g = img.reduce((4, 2)).convert("L")
    # frombuffer skips the extra copy np.asarray makes of a Pillow image (~0.3-0.5 ms per 1 MB)
    return np.frombuffer(g.tobytes(), dtype=np.uint8).reshape(g.size[1], g.size[0])


def make_state():
    return None


def _moved_mask(a, b):
    """Mask of |a-b| > 16, exact in uint8: (a-b) mod 256 in [17, 239]."""
    d = np.subtract(a, b)
    np.subtract(d, np.uint8(PIXEL_DELTA + 1), out=d)
    return d <= np.uint8(255 - 2 * PIXEL_DELTA - 1)


def _shape_fires(flat, width, height, band_row):
    ys, xs = np.divmod(flat, width)
    if band_row < height:
        keep = ys < band_row
        ys, xs = ys[keep], xs[keep]
    if ys.size < MIN_N:
        return False
    for oy, ox in GRIDS:
        key = ((ys + oy) // TILE_H) * 4096 + (xs + ox) // TILE_W
        order = np.argsort(key, kind="stable")
        k = key[order]
        starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
        n = np.diff(np.r_[starts, k.size])
        big = n >= MIN_N
        if not big.any():
            continue
        y, x = ys[order], xs[order]
        w = np.maximum.reduceat(x, starts) - np.minimum.reduceat(x, starts)
        h = np.maximum.reduceat(y, starts) - np.minimum.reduceat(y, starts)
        if np.any(big & (w >= MIN_W - 1) & (h >= MIN_H - 1)):
            return True
    return False


def decide(state, ref_gray, cur_gray, prev_gray, monitor):  # noqa: ARG001
    if ref_gray.shape != cur_gray.shape:
        return True
    if np.array_equal(ref_gray, cur_gray):
        return False
    m = _moved_mask(ref_gray, cur_gray)
    n = np.count_nonzero(m)
    if n == 0:
        return False
    if n > CHANGED_FRACTION * ref_gray.size / 2:  # today's threshold, in 4x4-pixel units
        return True
    height, width = ref_gray.shape
    band_row = height - TASKBAR_PX[monitor] // FY
    return _shape_fires(np.flatnonzero(m), width, height, band_row)
