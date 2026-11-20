"""Calibrate C3 on the synthetic corpus: word-box height per device px, ink-strip height ratio,
and the extra-call cost model t = a + b * mosaic_Mpix + c * unit_Mpix. Numbers only -> calib.json.

    uv run python eval/small-text-ocr/C3/calib.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c3  # noqa: E402
from shared import CORPUS, engines  # noqa: E402

from akashic.capture import imaging  # noqa: E402

pages = json.loads((CORPUS / "pages.json").read_text(encoding="utf-8"))["pages"]
fr, _ = engines()

# --- 1. heights -------------------------------------------------------------------------------
word_h = defaultdict(list)  # device px -> median word height of the block matched to a truth line
ink_h = defaultdict(list)  # device px -> ink-strip height in the truth box
found = defaultdict(lambda: [0, 0])
for r in ("chromium", "chromium_gray", "pillow"):
    for p in pages:
        img = Image.open(CORPUS / r / p["dir"] / f"{p['id']}.png").convert("RGB")
        dpx = round(p["px"] * p["scale"], 2)
        box = imaging.find_text_box(img)
        if box is None:
            continue
        lines = c3.ocr_words(fr, img.crop((box.left, box.top, box.right, box.bottom)))
        blocks = [[(t, x + box.left, y + box.top, w, h) for t, x, y, w, h in g] for ws in lines for g in c3.group_blocks(ws)]
        gray = np.asarray(img.convert("L"), dtype=np.int16)
        for ln in p["lines"]:
            x0, y0, x1, y1 = ln["box"]
            found[dpx][1] += 1
            hit = [g for g in blocks if any(y0 <= w[2] + w[4] / 2 <= y1 and x0 - 4 <= w[1] <= x1 for w in g)]
            if hit:
                found[dpx][0] += 1
                word_h[dpx].append(float(np.median([w[4] for g in hit for w in g])))
            if r == "chromium":
                sub = gray[max(0, y0 - 3) : y1 + 3, x0:x1]
                e = np.abs(np.diff(sub, axis=1)) > imaging.TEXT_EDGE
                rows = np.nonzero(e.any(axis=1))[0]
                if rows.size:
                    ink_h[dpx].append(float(rows[-1] - rows[0] + 1))
heights = {str(k): {"word_h_med": float(np.median(word_h[k])) if word_h[k] else None, "n": len(word_h[k]),
                    "found_share": found[k][0] / found[k][1], "ink_h_med": float(np.median(ink_h[k])) if ink_h[k] else None}
           for k in sorted(found)}
ratios = [v["word_h_med"] / float(k) for k, v in heights.items() if v["word_h_med"] and float(k) >= 12]
ink_ratio = [heights[k]["ink_h_med"] / heights[k]["word_h_med"] for k in heights if heights[k]["word_h_med"] and heights[k]["ink_h_med"] and float(k) >= 12]
print("heights", json.dumps(heights))
print("H_PER_PX (>=12 px, reliable native boxes)", np.median(ratios), "ink/word", np.median(ink_ratio))

# --- 2. cost model ------------------------------------------------------------------------------
samples = []
rng = np.random.default_rng(0)
sel = [p for p in pages if p["px"] in (9, 10, 11, 12) and p["lang"] in ("en", "mixed") and p["theme"] in ("dark", "light")]
for p in sel:
    img = Image.open(CORPUS / "chromium" / p["dir"] / f"{p['id']}.png").convert("RGB")
    units = []
    for ln in p["lines"]:
        x0, y0, x1, y1 = ln["box"]
        hpx = p["px"] * p["scale"]
        units.append(c3.Unit(max(0, x0 - 3), max(0, y0 - 2), x1 + 3, y1 + 2, (10 if hpx < 11 else 12) * c3.H_PER_PX, "native"))
    for n in (1, 2, 3, 5, 7, 10):
        us = list(rng.choice(units, size=n, replace=False))
        mos, _ = c3.build_mosaic(img, us, max(c3.tile_geometry(u)[0] for u in us))
        unit_px = sum(g[0] * g[1] for g in (c3.tile_geometry(u) for u in us))
        c3.ocr_words(fr, mos)
        ts = []
        for _ in range(3):
            t = time.perf_counter()
            c3.ocr_words(fr, mos)
            ts.append((time.perf_counter() - t) * 1000)
        samples.append({"total": mos.width * mos.height / 1e6, "unit": unit_px / 1e6, "ms": float(np.median(ts)), "kind": "text"})
for w, h in ((800, 200), (2000, 400), (3000, 800), (4000, 1500)):
    for theme in ((22, 24, 29), (250, 250, 250)):
        mos = Image.new("RGB", (w, h), theme)
        c3.ocr_words(fr, mos)
        ts = []
        for _ in range(3):
            t = time.perf_counter()
            c3.ocr_words(fr, mos)
            ts.append((time.perf_counter() - t) * 1000)
        samples.append({"total": w * h / 1e6, "unit": 0.0, "ms": float(np.median(ts)), "kind": "blank"})
X = np.array([[1, s["total"], s["unit"]] for s in samples])
y = np.array([s["ms"] for s in samples])
coef, *_ = np.linalg.lstsq(X, y, rcond=None)
pred = X @ coef
txt = np.array([s["kind"] == "text" for s in samples])
err = (y - pred)[txt]
print("cost fit a, b_total, c_unit:", coef.round(2), "text residual p50/p95/max ms:",
      np.percentile(np.abs(err), 50).round(1), np.percentile(np.abs(err), 95).round(1), np.abs(err).max().round(1))
under = y[txt] - pred[txt]
print("text: share under-predicted by > 5 ms", float((under > 5).mean()))
(HERE / "calib.json").write_text(json.dumps({"heights": heights, "H_PER_PX": float(np.median(ratios)),
                                             "ink_over_word": float(np.median(ink_ratio)), "cost": coef.tolist(),
                                             "cost_resid_p95": float(np.percentile(np.abs(err), 95)), "samples": samples}, indent=1),
                                 encoding="utf-8")
