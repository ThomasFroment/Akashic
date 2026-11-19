import numpy as np
from PIL import Image, ImageDraw, ImageFont
import shapes_lib as L

words = ["va", "un", "eu", "me", "we", "us", "so", "re", "ow", "xx", "ww", "mmm", "euh", "vas", "uwu", "nan", "wow", "oe", "ca", "vu", "rn", "ez", "on", "no", "ou", "non", "oui", "ok", "gg", "lol"]
for sname, s in L.SCALES.items():
    for theme, (bg, fg) in L.THEMES.items():
        for px in (13, 14, 16):
            font = ImageFont.truetype(L.SEG, round(px * s))
            miss = []
            for word in words:
                r = L.evaluate(bg, lambda d, x, y: d.text((x, y), word, font=font, fill=fg), sname)
                if min(r["c1"], r["c2"], r["c3"]) < 16:
                    miss.append((word, r["c1"], r["c2"], r["c3"]))
            print(sname, theme, px, "misses (word, c1, c2, c3 of 16):", miss)
# clock per placement
for sname, s in L.SCALES.items():
    font = ImageFont.truetype(L.VAR, round(12 * s))
    bg, fg = (32, 32, 32), (255, 255, 255)
    rates = []
    for dx in range(4):
        for dy in range(4):
            f = {"c1": 0, "c2": 0, "c3": 0}
            for minute in range(60):
                t0 = "14:%02d" % minute
                t1 = "14:%02d" % (minute + 1) if minute < 59 else "15:00"
                a = Image.new("RGB", (L.W, L.H), bg); b = a.copy()
                ImageDraw.Draw(a).text((40 + dx, 40 + dy), t0, font=font, fill=fg)
                ImageDraw.Draw(b).text((40 + dx, 40 + dy), t1, font=font, fill=fg)
                m = L.mask(L.gray(a, 4), L.gray(b, 4))
                w2 = 4 if sname == "1440p" else 5
                f["c1"] += L.c1(m, w2); f["c2"] += L.c2(m)
                m42 = L.mask(L.gray(a, (4, 2)), L.gray(b, (4, 2)))
                f["c3"] += L.c3(m42)
            rates.append(f)
    print(sname, "clock fires per hour by placement: c1 max", max(r["c1"] for r in rates), "mean", np.mean([r["c1"] for r in rates]),
          "c2 max", max(r["c2"] for r in rates), "c3 max", max(r["c3"] for r in rates), "c3 mean", np.mean([r["c3"] for r in rates]))
