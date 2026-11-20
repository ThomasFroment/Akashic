"""Round-2 musts: append the new fields to metrics.txt and draw fig-round2.png (numbers only).

Inputs (this folder): v2_baseline.json, v2_p2.json, optional v2_p1.json (score_v2.py), sweep2.json.
New metrics.txt fields per run (s, st, d):
  noiseM_*    misread-token share of the index (occurrences), noiseM_x_* vs today
  noiseMd_x_* same, each distinct token once per FTS document, vs today
  noiseG_x_*  fragment-proof guard (near-miss of a window truth token), vs today
  misN_*      misread tokens in the index (count), misN_base_* today's count
  tlinkL_*    truth-visible link, each distinct line once; tlinkL_base_* today
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
from plot import BASE, CAND, GRID, INK, INK2, LIMIT, MUTED, PLANE, SURFACE, f  # noqa: E402

SHORT = {"smoke": "s", "standard": "st", "day": "d"}
RUNS = ("smoke", "standard", "day")
P1COL = "#9b8afb"


def load(name):
    p = HERE / name
    return json.loads(p.read_text()) if p.is_file() else None


def fields(base, cand) -> str:
    out = []
    for k in RUNS:
        b, c, s = base[k], cand[k], SHORT[k]
        out += [f"noiseM_{s}={c['noise_occ']:.4f}", f"noiseM_x_{s}={c['noise_occ'] / b['noise_occ']:.2f}",
                f"noiseMd_x_{s}={c['noise_doc'] / b['noise_doc']:.2f}", f"noiseG_x_{s}={c['noise_guard'] / b['noise_guard']:.2f}",
                f"misN_{s}={c['noise_occ_n']}", f"misN_base_{s}={b['noise_occ_n']}",
                f"tlinkL_{s}={c['tlink_line']:.4f}", f"tlinkL_base_{s}={b['tlink_line']:.4f}"]
    return " ".join(out)


def bars(d, box, title, groups, ymax, ticks, limit=None, fmt="{:.2f}"):
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=SURFACE)
    d.text((x0 + 12, y0 + 8), title, font=f(15, True), fill=INK)
    px0, py0, px1, py1 = x0 + 56, y0 + 44, x1 - 12, y1 - 36
    Y = lambda v: py1 - (py1 - py0) * min(max(v, 0), ymax) / ymax  # noqa: E731
    for t in ticks:
        d.line((px0, Y(t), px1, Y(t)), fill=GRID)
        d.text((px0 - 6, Y(t)), f"{t:g}", font=f(11), fill=MUTED, anchor="rm")
    if limit is not None:
        d.line((px0, Y(limit), px1, Y(limit)), fill=LIMIT, width=2)
    gw = (px1 - px0) / len(groups)
    bw = 30
    for gi, (name, vals) in enumerate(groups):
        cx = px0 + gw * (gi + 0.5)
        n = len(vals)
        for si, (v, col) in enumerate(vals):
            if v is None:
                continue
            bx = cx - n * (bw + 3) / 2 + si * (bw + 3)
            d.rounded_rectangle((bx, Y(v), bx + bw, py1), radius=4, fill=col)
            d.text((bx + bw / 2, Y(v) - 3), fmt.format(v), font=f(11), fill=INK, anchor="mb")
        d.text((cx, py1 + 8), name, font=f(13), fill=INK2, anchor="mt")


def figure(base, p2, p1, sw) -> None:
    W, H = 1600, 900
    im = Image.new("RGB", (W, H), PLANE)
    d = ImageDraw.Draw(im)
    d.text((24, 14), "Round-2 musts: misread noise and per-line truth link (today gray, P1 violet, P2 blue)", font=f(20, True), fill=INK)
    d.text((24, 42), "noise = misread harness tokens / every indexed token, vs today (amber = 0.5x must); link = each distinct line once",
           font=f(13), fill=MUTED)

    def grp(key, fn):
        return [(k, [(fn(base[k]), BASE)] + ([(fn(p1[k]), P1COL)] if p1 else []) + [(fn(p2[k]), CAND)]) for k in RUNS]

    xs = [(k, [(1.0, BASE)] + ([(p1[k]["noise_occ"] / base[k]["noise_occ"], P1COL)] if p1 else [])
               + [(p2[k]["noise_occ"] / base[k]["noise_occ"], CAND)]) for k in RUNS]
    bars(d, (16, 70, 790, 470), "misread noise share, x today (occurrences)", xs, 1.6, [0, 0.5, 1, 1.5], limit=0.5)
    bars(d, (806, 70, 1584, 470), "misread tokens in the index (thousands)",
         grp("n", lambda r: r["noise_occ_n"] / 1000), 55, [0, 10, 20, 30, 40, 50], fmt="{:.1f}")
    bars(d, (16, 486, 790, 886), "truth-visible link, per distinct line (must >= today)", grp("t", lambda r: r["tlink_line"]),
         1.0, [0, 0.25, 0.5, 0.75, 1.0], fmt="{:.3f}")
    # sweep: noise x vs drop on day
    x0, y0, x1, y1 = 806, 486, 1584, 886
    d.rectangle((x0, y0, x1, y1), fill=SURFACE)
    d.text((x0 + 12, y0 + 8), "P2 configs: misread noise x today vs content drop", font=f(15, True), fill=INK)
    if sw:
        px0, py0, px1, py1 = x0 + 56, y0 + 44, x1 - 260, y1 - 40
        xm, ym = 0.05, 2.0
        X = lambda v: px0 + (px1 - px0) * min(v, xm) / xm  # noqa: E731
        Y = lambda v: py1 - (py1 - py0) * min(v, ym) / ym  # noqa: E731
        for t in (0, 0.5, 1.0, 1.5, 2.0):
            d.line((px0, Y(t), px1, Y(t)), fill=GRID)
            d.text((px0 - 6, Y(t)), f"{t:g}x", font=f(11), fill=MUTED, anchor="rm")
        for t in (0, 0.01, 0.02, 0.03, 0.04, 0.05):
            d.text((X(t), py1 + 6), f"{t * 100:.0f} %", font=f(11), fill=MUTED, anchor="mt")
        d.line((px0, Y(0.5), px1, Y(0.5)), fill=LIMIT, width=2)
        d.line((X(0.01), py0, X(0.01), py1), fill=LIMIT, width=2)
        cols = {"smoke": P1COL, "standard": CAND, "day": "#46b98f"}
        ly = py0
        for i, (name, r) in enumerate(sw.items()):
            for k in RUNS:
                if k in r:
                    v = r[k]
                    cx, cy = X(v["drop"]), Y(v["noise"] / base[k]["noise_occ"])
                    d.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=cols[k])
                    if k == "day":
                        d.text((cx + 7, cy), str(i + 1), font=f(11, True), fill=INK, anchor="lm")
            d.text((px1 + 16, ly), f"{i + 1} {name}", font=f(11), fill=INK2)
            ly += 18
        ly += 8
        for k in RUNS:
            d.ellipse((px1 + 16, ly + 3, px1 + 26, ly + 13), fill=cols[k])
            d.text((px1 + 32, ly), k, font=f(11), fill=INK2)
            ly += 18
    im.save(HERE / "fig-round2.png")


def main() -> None:
    base, p2, p1, sw = load("v2_baseline.json"), load("v2_p2.json"), load("v2_p1.json"), load("sweep2.json")
    m = HERE / "metrics.txt"
    line = m.read_text(encoding="utf-8").split("\n")[0].split(" noiseM_")[0].strip()
    m.write_text(line + " " + fields(base, p2) + "\n", encoding="utf-8")
    figure(base, p2, p1, sw)
    print(fields(base, p2))
    if p1:
        print("P1:", fields(base, p1))


if __name__ == "__main__":
    main()
