import platform, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import shapes_lib as L
from timing import base, med  # reruns timing prints; acceptable
print(platform.processor())
before = base(3840, 2160, 1.25, "dark")
after = before.copy()
ImageDraw.Draw(after).text((2800, 1080), "ok", font=ImageFont.truetype(L.SEG, round(14 * 1.25)), fill=(220, 221, 222))
for f in (4, (4, 2)):
    a = np.asarray(before.reduce(f).convert("L"), dtype=np.uint8)
    b = np.asarray(after.reduce(f).convert("L"), dtype=np.uint8)
    m = (np.maximum(a, b) - np.minimum(a, b)) > np.uint8(16)
    print(f, "flatnonzero", med(lambda: np.flatnonzero(m)), "nonzero", med(lambda: np.nonzero(m)), "count", med(lambda: np.count_nonzero(m)))
    d = np.empty_like(a)
    def fused():
        np.maximum(a, b, out=d); d2 = np.minimum(a, b); np.subtract(d, d2, out=d); mm = d > 16
        k = np.count_nonzero(mm)
        return np.flatnonzero(mm) if k else None
    print(f, "fused mask+count+flatnonzero", med(fused))

def c1_vec(flat, width, w2):
    ys, xs = np.divmod(flat, width)
    for oy in (0, 8):
        for ox in (0, 8):
            key = ((ys + oy) // 16) * 1000 + (xs + ox) // 16
            order = np.argsort(key, kind="stable")
            k = key[order]; y = ys[order]; x = xs[order]
            starts = np.flatnonzero(np.r_[True, k[1:] != k[:-1]])
            n = np.diff(np.r_[starts, k.size])
            w = np.maximum.reduceat(x, starts) - np.minimum.reduceat(x, starts) + 1
            h = np.maximum.reduceat(y, starts) - np.minimum.reduceat(y, starts) + 1
            if np.any(((w >= 3) & (h >= 3) & (n >= 4)) | ((w >= w2) & (h >= 2) & (n >= 5))):
                return True
    return False

def c2_vec(flat, width):
    ys, xs = np.divmod(flat, width)
    adj = (np.abs(ys[:, None] - ys[None, :]) <= 2) & (np.abs(xs[:, None] - xs[None, :]) <= 2)
    r = adj.astype(np.uint8)
    for _ in range(6):
        r = ((r @ r) > 0).astype(np.uint8)
    lab = r.argmax(axis=1)
    for l in np.unique(lab):
        s = lab == l
        if s.sum() >= 5 and np.ptp(xs[s]) + 1 >= 3 and np.ptp(ys[s]) + 1 >= 2:
            return True
    return False

rng = np.random.default_rng(3)
flat51 = np.sort(rng.choice(518400, 51, replace=False))
a = np.asarray(before.reduce(4).convert("L"), dtype=np.uint8)
b = np.asarray(after.reduce(4).convert("L"), dtype=np.uint8)
fw = np.flatnonzero((np.maximum(a, b) - np.minimum(a, b)) > np.uint8(16))
print("c1_vec word", med(lambda: c1_vec(fw, 960, 5)), "c1_vec 51 random", med(lambda: c1_vec(flat51, 960, 5)))
print("c2_vec word", med(lambda: c2_vec(fw, 960)), "c2_vec 51 random", med(lambda: c2_vec(flat51, 960)))
