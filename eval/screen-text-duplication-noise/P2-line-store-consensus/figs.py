"""P2 figures (Pillow, same palette as harness/plot.py), from sweep.json, noise_floor.json, drop_causes.json.

    uv run python .../figs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
from plot import AXIS, BASE, CAND, GRID, INK, INK2, LIMIT, MUTED, PLANE, SURFACE, f  # noqa: E402

RUNS = ("smoke", "standard", "day")
RUN_COL = {"smoke": "#9b8afb", "standard": CAND, "day": "#46b98f"}
BASE_NOISE = {k: json.loads((HERE.parent / "baseline" / "details.json").read_text())[k]["noise"]["share"] for k in RUNS}


def frame(w, h, title, sub):
    im = Image.new("RGB", (w, h), PLANE)
    d = ImageDraw.Draw(im)
    d.text((24, 16), title, font=f(20, True), fill=INK)
    d.text((24, 44), sub, font=f(13), fill=MUTED)
    return im, d


def scatter_noise_drop() -> None:
    sw = json.loads((HERE / "sweep.json").read_text())
    W, H = 1500, 820
    im, d = frame(W, H, "P2 garbage rules: noise share vs content lines dropped",
                  "one dot per (config, run); amber = musts (noise <= 0.5x today, drop <= 1 %); labels = config")
    x0, y0, x1, y1 = 110, 90, W - 360, H - 80
    d.rectangle((x0 - 10, y0 - 10, x1 + 10, y1 + 10), fill=SURFACE)
    xmax, ymax = 0.06, 1.4  # drop rate, noise x
    X = lambda v: x0 + (x1 - x0) * min(v, xmax) / xmax  # noqa: E731
    Y = lambda v: y1 - (y1 - y0) * min(v, ymax) / ymax  # noqa: E731
    for t in (0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06):
        d.line((X(t), y0, X(t), y1), fill=GRID)
        d.text((X(t), y1 + 8), f"{t * 100:.0f} %", font=f(12), fill=MUTED, anchor="mt")
    for t in (0, 0.25, 0.5, 0.75, 1.0, 1.25):
        d.line((x0, Y(t), x1, Y(t)), fill=GRID)
        d.text((x0 - 8, Y(t)), f"{t:.2f}x", font=f(12), fill=MUTED, anchor="rm")
    d.line((X(0.01), y0, X(0.01), y1), fill=LIMIT, width=2)
    d.line((x0, Y(0.5), x1, Y(0.5)), fill=LIMIT, width=2)
    d.line((x0, Y(1.0), x1, Y(1.0)), fill=AXIS, width=1)
    d.text((x1 - 4, Y(1.0) - 4), "today", font=f(12), fill=MUTED, anchor="rb")
    d.text(((x0 + x1) / 2, y1 + 30), "content lines dropped or down-ranked (harness drop rate)", font=f(13), fill=INK2, anchor="mt")
    d.text((x0 - 70, y0 - 30), "noise share / today", font=f(13), fill=INK2)
    names = list(sw)
    for i, name in enumerate(names):
        for k in RUNS:
            r = sw[name].get(k)
            if not r:
                continue
            nx = r["noise"] / BASE_NOISE[k]
            cx, cy = X(r["drop"]), Y(nx)
            d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=RUN_COL[k])
            if k == "day":
                d.text((cx + 9, cy), str(i + 1), font=f(12, True), fill=INK, anchor="lm")
    ly = y0
    for k in RUNS:
        d.ellipse((x1 + 40, ly + 3, x1 + 52, ly + 15), fill=RUN_COL[k])
        d.text((x1 + 60, ly), k, font=f(13), fill=INK)
        ly += 22
    ly += 12
    for i, name in enumerate(names):
        d.text((x1 + 40, ly), f"{i + 1}  {name}", font=f(12), fill=INK2)
        ly += 20
    im.save(HERE / "fig-noise-vs-drop.png")


def bars_noise_floor() -> None:
    nf = json.loads((HERE / "noise_floor.json").read_text())
    W, H = 1300, 640
    im, d = frame(W, H, "Noise share of what search sees, against floors no garbage rule can pass",
                  "today = 1.0x; P2 = chosen config; oracle = P2 with every garbled token removed / with every unknown OCR token removed")
    x0, y0, x1, y1 = 100, 100, W - 40, H - 80
    d.rectangle((x0 - 10, y0 - 10, x1 + 10, y1 + 10), fill=SURFACE)
    ymax = 1.4
    Y = lambda v: y1 - (y1 - y0) * min(v, ymax) / ymax  # noqa: E731
    for t in (0, 0.25, 0.5, 0.75, 1.0, 1.25):
        d.line((x0, Y(t), x1, Y(t)), fill=GRID)
        d.text((x0 - 8, Y(t)), f"{t:.2f}x", font=f(12), fill=MUTED, anchor="rm")
    d.line((x0, Y(0.5), x1, Y(0.5)), fill=LIMIT, width=2)
    series = [("today", lambda r: 1.0, BASE), ("P2", lambda r: r["x"], CAND),
              ("oracle: no garbled", lambda r: r["oracle_no_garbled_x"], "#46b98f"), ("oracle: no unknown OCR", lambda r: r["oracle_no_ocr_unknown_x"], "#9b8afb")]
    gw = (x1 - x0) / len(RUNS)
    bw = 46
    for gi, k in enumerate(RUNS):
        r = nf.get(k)
        if not r:
            continue
        cx = x0 + gw * (gi + 0.5)
        for si, (_, fn, col) in enumerate(series):
            v = fn(r)
            bx = cx - 2 * (bw + 4) + si * (bw + 4)
            d.rounded_rectangle((bx, Y(v), bx + bw, y1), radius=4, fill=col)
            d.text((bx + bw / 2, Y(v) - 4), f"{v:.2f}", font=f(12), fill=INK, anchor="mb")
        d.text((cx, y1 + 14), k, font=f(14, True), fill=INK, anchor="mt")
    lx = x0 + 10
    for name, _, col in series:
        d.rectangle((lx, 70, lx + 14, 84), fill=col)
        d.text((lx + 20, 70), name, font=f(13), fill=INK2)
        lx += 230
    im.save(HERE / "fig-noise-floor.png")


def bars_drop_causes() -> None:
    dc = json.loads((HERE / "drop_causes.json").read_text())
    causes = sorted({k.split("|")[1] for r in dc.values() for k in r if k.startswith("why|")})
    W, H = 1300, 600
    im, d = frame(W, H, "Content lines lost by P2, by cause (count of harness lines)",
                  "minority_other_line_diff_truth_text = false merge / real edit voted away; canonical_garbage_cut = garbage rule cut the line")
    x0, y0, x1, y1 = 100, 100, W - 40, H - 90
    d.rectangle((x0 - 10, y0 - 10, x1 + 10, y1 + 10), fill=SURFACE)
    vals = {k: {c: sum(v for kk, v in dc[k].items() if kk.startswith(f"why|{c}|")) for c in causes} for k in RUNS if k in dc}
    ymax = max(4, max((v for r in vals.values() for v in r.values()), default=1) + 1)
    Y = lambda v: y1 - (y1 - y0) * v / ymax  # noqa: E731
    for t in range(0, int(ymax) + 1, max(1, int(ymax) // 5)):
        d.line((x0, Y(t), x1, Y(t)), fill=GRID)
        d.text((x0 - 8, Y(t)), str(t), font=f(12), fill=MUTED, anchor="rm")
    cols = ["#e0a030", CAND, "#46b98f", "#9b8afb", "#e5534b", BASE]
    gw = (x1 - x0) / len(RUNS)
    bw = 34
    for gi, k in enumerate(RUNS):
        if k not in vals:
            continue
        cx = x0 + gw * (gi + 0.5)
        n = len(causes)
        for si, c in enumerate(causes):
            v = vals[k][c]
            bx = cx - n * (bw + 4) / 2 + si * (bw + 4)
            if v:
                d.rounded_rectangle((bx, Y(v), bx + bw, y1), radius=4, fill=cols[si % len(cols)])
            d.text((bx + bw / 2, Y(v) - 4), str(v), font=f(12), fill=INK, anchor="mb")
        d.text((cx, y1 + 12), f"{k}  ({dc[k].get('dropped', 0)} of {dc[k].get('captured', 0)} lines)", font=f(14, True), fill=INK, anchor="mt")
    lx, ly = x0 + 10, 66
    for si, c in enumerate(causes):
        d.rectangle((lx, ly + 2, lx + 14, ly + 16), fill=cols[si % len(cols)])
        d.text((lx + 20, ly), c, font=f(13), fill=INK2)
        lx += 330
    im.save(HERE / "fig-drop-causes.png")


if __name__ == "__main__":
    for fn in (scatter_noise_drop, bars_noise_floor, bars_drop_causes):
        try:
            fn()
            print("ok", fn.__name__)
        except FileNotFoundError as e:
            print("skip", fn.__name__, e)
