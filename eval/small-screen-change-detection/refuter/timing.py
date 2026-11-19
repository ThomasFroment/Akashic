import hashlib, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import shapes_lib as L

def base(w, h, scale, theme):
    bg, fg = L.THEMES[theme]
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(L.SEG, round(14 * scale))
    rng = np.random.default_rng(1)
    for i in range(int(h / (24 * scale)) - 4):
        n = int(rng.integers(30, 110))
        d.text((round(40 * scale), round(20 * scale + i * 24 * scale)), "lorem ipsum dolor " * (n // 18), font=font, fill=fg)
    return img

def med(fn, k=101):
    t = []
    for _ in range(k):
        s = time.perf_counter(); fn(); t.append((time.perf_counter() - s) * 1000)
    t.sort(); return round(t[k // 2], 3)

before = base(3840, 2160, 1.25, "dark")
after = before.copy()
ImageDraw.Draw(after).text((2800, 1080), "ok", font=ImageFont.truetype(L.SEG, round(14 * 1.25)), fill=(220, 221, 222))
caret = before.copy()
ImageDraw.Draw(caret).rectangle((2800, 1080, 2801, 1102), fill=(220, 221, 222))
print("mode", before.mode)
for name, f in (("4", 4), ("4x2", (4, 2))):
    print("sig", name, med(lambda: hashlib.blake2b(np.asarray(after.reduce(f).convert("L"), dtype=np.uint8).tobytes(), digest_size=16).digest(), 41))
    print("  reduce only", name, med(lambda: after.reduce(f), 41))
    g = np.asarray(after.reduce(f).convert("L"), dtype=np.uint8)
    print("  blake2b only", name, med(lambda: hashlib.blake2b(g.tobytes(), digest_size=16).digest()))
for name, f in (("4", 4), ("4x2", (4, 2))):
    a = np.asarray(before.reduce(f).convert("L"), dtype=np.uint8)
    b = np.asarray(after.reduce(f).convert("L"), dtype=np.uint8)
    c = np.asarray(caret.reduce(f).convert("L"), dtype=np.uint8)
    print(name, a.shape, "N word", int(L.mask(a, b).sum()), "N caret", int(L.mask(a, c).sum()))
    print("  today int16+mean", med(lambda: float((np.abs(a.astype(np.int16) - b.astype(np.int16)) > 16).mean()) > 1e-4))
    print("  uint8 max-min+count", med(lambda: int(np.count_nonzero((np.maximum(a, b) - np.minimum(a, b)) > np.uint8(16)))))
    print("  uint8 + nonzero", med(lambda: np.nonzero((np.maximum(a, b) - np.minimum(a, b)) > np.uint8(16))))
    print("  array_equal", med(lambda: np.array_equal(a, b)))
a = np.asarray(before.reduce(4).convert("L"), dtype=np.uint8)
b = np.asarray(after.reduce(4).convert("L"), dtype=np.uint8)
m = L.mask(a, b)
print("c1 python loop on word", med(lambda: L.c1(m, 5)))
print("c2 union-find on word", med(lambda: L.c2(m)))
rng = np.random.default_rng(3)
m51 = np.zeros_like(m); idx = rng.choice(m.size, 51, replace=False); m51.flat[idx] = True
print("c1 on 51 random pts", med(lambda: L.c1(m51, 5)))
print("c2 on 51 random pts", med(lambda: L.c2(m51)))
m51c = np.zeros_like(m); m51c[100:106, 200:209] = True; m51c[100,208]=False; m51c[101,208]=False; m51c[102,208]=False
print("c2 on 51 clustered pts", int(m51c.sum()), med(lambda: L.c2(m51c)))
