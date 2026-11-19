import numpy as np
from PIL import Image, ImageDraw, ImageFont
import shapes_lib as L
words = ["va", "un", "eu", "me", "we", "us", "so", "re", "ow", "xx", "ww", "mmm", "euh", "vas", "uwu", "nan", "wow", "oe", "ca", "vu", "rn", "ez", "on", "no", "ou", "non", "oui", "ok", "gg", "lol"]
def ev(bg, draw_at, sname, w2):
    k = 0
    for dx in range(4):
        for dy in range(4):
            a, b = L.pair(bg, lambda d: draw_at(d, 40 + dx, 40 + dy))
            m = L.mask(L.gray(a, 4), L.gray(b, 4))
            n = int(m.sum())
            k += (n > L.GATE[sname]) or (n > 0 and L.c1(m, w2))
    return k
for sname, s in L.SCALES.items():
    for theme, (bg, fg) in L.THEMES.items():
        for px in (13, 14):
            font = ImageFont.truetype(L.SEG, round(px * s))
            miss = [(w, ev(bg, lambda d, x, y: d.text((x, y), w, font=font, fill=fg), sname, 3)) for w in words]
            print(sname, theme, px, "W2=3 misses", [m for m in miss if m[1] < 16])
    bg, fg = L.THEMES["dark"]
    for cw in (8, 9, 10, 11, 12):
        print(sname, "underscore 2px", cw, "W2=3 fires of 16:", ev(bg, lambda d, x, y: d.rectangle((x, y, x + cw - 1, y + 1), fill=fg), sname, 3))
