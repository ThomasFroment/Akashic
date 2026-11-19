"""Refuter checks: C1/C2/C3 shape rules on synthetic words, carets, underscores, taskbar clock."""
import sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

SEG = "C:/Windows/Fonts/segoeui.ttf"
VAR = "C:/Windows/Fonts/SegUIVar.ttf"
THEMES = {"dark": ((32, 33, 36), (220, 221, 222)), "light": ((255, 255, 255), (32, 33, 36))}
SCALES = {"1440p": 1.0, "4K": 1.25}
GATE = {"1440p": 23.04, "4K": 51.84}
W, H = 256, 128


def gray(img, f):
    return np.asarray(img.reduce(f).convert("L"), dtype=np.uint8)


def mask(a, b):
    return (np.maximum(a, b) - np.minimum(a, b)) > np.uint8(16)


def c1(m, w2):
    ys, xs = np.nonzero(m)
    for oy in (0, 8):
        for ox in (0, 8):
            ty, tx = (ys + oy) // 16, (xs + ox) // 16
            for key in set(zip(ty.tolist(), tx.tolist())):
                sel = (ty == key[0]) & (tx == key[1])
                y, x = ys[sel], xs[sel]
                w, h, n = int(x.max() - x.min() + 1), int(y.max() - y.min() + 1), int(sel.sum())
                if (w >= 3 and h >= 3 and n >= 4) or (w >= w2 and h >= 2 and n >= 5):
                    return True
    return False


def clusters(m):
    ys, xs = np.nonzero(m)
    n = len(ys)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(n):
        for j in range(i + 1, n):
            if abs(int(ys[i]) - int(ys[j])) <= 2 and abs(int(xs[i]) - int(xs[j])) <= 2:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [(xs[g], ys[g]) for g in groups.values()]


def c2(m):
    for x, y in clusters(m):
        if x.max() - x.min() + 1 >= 3 and y.max() - y.min() + 1 >= 2 and len(x) >= 5:
            return True
    return False


def c3(m42):
    ys, xs = np.nonzero(m42)
    for oy in (0, 16):
        for ox in (0, 8):
            ty, tx = (ys + oy) // 32, (xs + ox) // 16
            for key in set(zip(ty.tolist(), tx.tolist())):
                sel = (ty == key[0]) & (tx == key[1])
                y, x = ys[sel], xs[sel]
                if x.max() - x.min() + 1 >= 3 and y.max() - y.min() + 1 >= 3 and sel.sum() >= 4:
                    return True
    return False


def pair(bg, draw):
    a = Image.new("RGB", (W, H), bg)
    b = a.copy()
    draw(ImageDraw.Draw(b))
    return a, b


def evaluate(bg, draw_at, scale_name):
    """Return counts of fires over 16 placements for today, C1, C2, C3 and the N range."""
    w2 = 4 if scale_name == "1440p" else 5
    res = {"today": 0, "c1": 0, "c2": 0, "c3": 0, "nmin": 10**9, "nmax": 0, "wmin": 99, "hmin": 99}
    for dx in range(4):
        for dy in range(4):
            a, b = pair(bg, lambda d: draw_at(d, 40 + dx, 40 + dy))
            m = mask(gray(a, 4), gray(b, 4))
            n = int(m.sum())
            res["nmin"], res["nmax"] = min(res["nmin"], n), max(res["nmax"], n)
            if n:
                ys, xs = np.nonzero(m)
                res["wmin"] = min(res["wmin"], int(xs.max() - xs.min() + 1))
                res["hmin"] = min(res["hmin"], int(ys.max() - ys.min() + 1))
            today = n > GATE[scale_name]
            res["today"] += today
            res["c1"] += today or (n > 0 and c1(m, w2))
            res["c2"] += today or (n > 0 and c2(m))
            m42 = mask(gray(a, (4, 2)), gray(b, (4, 2)))
            res["c3"] += today or (int(m42.sum()) > 0 and c3(m42))
    return res


def main():
    out = sys.stdout
    words = ["on", "no", "non", "ou", "oui", "mm", "ok", "gg", "lol", "a", "y", "ca", "vu"]
    print("== words (fires of 16 placements) ==", file=out)
    for sname, s in SCALES.items():
        for theme, (bg, fg) in THEMES.items():
            for px in (13, 14, 16):
                font = ImageFont.truetype(SEG, round(px * s))
                for word in words:
                    r = evaluate(bg, lambda d, x, y: d.text((x, y), word, font=font, fill=fg), sname)
                    if min(r["c1"], r["c2"], r["c3"]) < 16:
                        print(sname, theme, px, word, r, file=out)
    print("== bar carets (width device px) ==", file=out)
    for sname, s in SCALES.items():
        for theme, (bg, fg) in THEMES.items():
            for cw in range(1, 8):
                r = evaluate(bg, lambda d, x, y: d.rectangle((x, y, x + cw - 1, y + round(18 * s)), fill=fg), sname)
                print(sname, theme, "bar", cw, r, file=out)
    print("== underscore carets (width x thickness) ==", file=out)
    for sname, s in SCALES.items():
        theme = "dark"
        bg, fg = THEMES[theme]
        for cw in (8, 9, 10, 11, 12, 13, 14, 16):
            for th in (1, 2):
                r = evaluate(bg, lambda d, x, y: d.rectangle((x, y, x + cw - 1, y + th - 1), fill=fg), sname)
                print(sname, theme, "under", cw, th, r, file=out)
    print("== taskbar clock minute flips (fires over 16 placements, summed over transitions) ==", file=out)
    for sname, s in SCALES.items():
        font = ImageFont.truetype(VAR, round(12 * s))
        bg, fg = (32, 32, 32), (255, 255, 255)
        tot = {"today": 0, "c1": 0, "c2": 0, "c3": 0}
        count = 0
        for minute in range(60):
            t0 = "14:%02d" % minute
            t1 = "14:%02d" % ((minute + 1) % 60) if minute < 59 else "15:00"
            def draw_at(d, x, y, t0=t0, t1=t1):
                pass
            for dx in range(4):
                for dy in range(4):
                    a = Image.new("RGB", (W, H), bg)
                    b = a.copy()
                    ImageDraw.Draw(a).text((40 + dx, 40 + dy), t0, font=font, fill=fg)
                    ImageDraw.Draw(b).text((40 + dx, 40 + dy), t1, font=font, fill=fg)
                    m = mask(gray(a, 4), gray(b, 4))
                    n = int(m.sum())
                    today = n > GATE[sname]
                    w2 = 4 if sname == "1440p" else 5
                    tot["today"] += today
                    tot["c1"] += today or (n > 0 and c1(m, w2))
                    tot["c2"] += today or (n > 0 and c2(m))
                    m42 = mask(gray(a, (4, 2)), gray(b, (4, 2)))
                    tot["c3"] += today or (int(m42.sum()) > 0 and c3(m42))
                    count += 1
        print(sname, "clock", {k: round(v / count, 3) for k, v in tot.items()}, "n=", count, file=out)


