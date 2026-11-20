"""Fit C1's OCR cost model on the harness corpus (Edge pages, both scales). Numbers only.

For each page: today's OCR call on the find_text_box crop, and the strip mosaic at s in S_GRID; wall ms
of RecognizeAsync (median of REPS after one warm-up) against blank and dense (strip) Mpix:
    ms = c0 + blank * blank_mpix + dense * dense_mpix
blank (and the empty-call intercept) from flat canvases of 0.3-8.3 Mpix; c0 (empty intercept + per-call
text overhead) and dense by least squares on the page samples with blank fixed.
Preprocessing (strip finding + mosaic build) is fitted as pre_in * crop_mpix + pre_out * upscaled_mpix.
Writes cost.json (model, fit errors, samples).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c1  # noqa: E402
from shared import CORPUS, engines  # noqa: E402

from akashic.capture import imaging  # noqa: E402

S_GRID = (1.0, 1.5, 2.0, 2.5, 3.0)
REPS = 5


def timed(fn, *a):
    fn(*a)
    ts = []
    for _ in range(REPS):
        t = time.perf_counter()
        fn(*a)
        ts.append((time.perf_counter() - t) * 1000)
    return float(np.median(ts))


def main() -> None:
    fr, _ = engines()
    pages = json.loads((CORPUS / "pages.json").read_text(encoding="utf-8"))["pages"]
    sel = [p for p in pages if p["lang"] == "mixed"]
    samples, pre = [], []
    for p in sel:
        img = Image.open(CORPUS / "chromium" / p["dir"] / f"{p['id']}.png").convert("RGB")
        box = imaging.find_text_box(img)
        reg = img.crop((box.left, box.top, box.right, box.bottom))
        t = time.perf_counter()
        lum = np.asarray(reg.convert("L"))
        strips = c1.find_strips(lum, p["scale"])
        t_find = (time.perf_counter() - t) * 1000
        area = reg.width * reg.height
        dense0 = float(sum((st.w + 2) * (st.h + 2) for st in strips))
        ms = timed(lambda: c1.ocr_words(fr, reg))
        samples.append({"page": p["id"], "scale": p["scale"], "kind": "today", "s": 1.0, "blank": (area - dense0) / 1e6,
                        "dense": dense0 / 1e6, "ms": ms})
        small_in = sum((st.w + 2) * (st.h + 2) for st in strips if st.h <= c1.SMALL_H)
        for s in S_GRID:
            t = time.perf_counter()
            m, _ = c1.build_mosaic(lum, strips, s)
            m = m.convert("RGB")
            t_build = (time.perf_counter() - t) * 1000
            w, h, _, dense = c1.layout(strips, s)
            ms = timed(lambda: c1.ocr_words(fr, m))
            samples.append({"page": p["id"], "scale": p["scale"], "kind": "mosaic", "s": s, "blank": (w * h - dense) / 1e6,
                            "dense": dense / 1e6, "ms": ms})
            pre.append({"crop": area / 1e6, "out": small_in * s * s / 1e6, "ms": t_find + t_build})
        print(p["id"], p["scale"], f"today {samples[-len(S_GRID) - 1]['ms']:.0f} ms, 3x {samples[-1]['ms']:.0f} ms", flush=True)
    # blank rate and intercept from flat canvases (edge colour only), then the dense rate on the pages
    blanks = []
    for w, h in ((800, 400), (1400, 720), (2000, 1000), (2560, 1440), (3000, 2000), (3840, 2160)):
        for v in (24, 250):
            im = Image.new("RGB", (w, h), (v, v, v))
            blanks.append({"blank": w * h / 1e6, "ms": timed(lambda: c1.ocr_words(fr, im))})
    Xb = np.array([[1.0, b["blank"]] for b in blanks])
    cb, *_ = np.linalg.lstsq(Xb, np.array([b["ms"] for b in blanks]), rcond=None)
    y = np.array([x["ms"] for x in samples])
    d = np.array([x["dense"] for x in samples])
    r = y - cb[0] - cb[1] * np.array([x["blank"] for x in samples])
    ct, k = np.linalg.lstsq(np.c_[np.ones(len(d)), d], r, rcond=None)[0]  # per-call text overhead, dense rate
    coef = np.array([cb[0] + ct, cb[1], k])
    X = np.array([[1.0, x["blank"], x["dense"]] for x in samples])
    pred = X @ coef
    free, *_ = np.linalg.lstsq(X, y, rcond=None)
    err = pred - y
    Xp = np.array([[x["crop"], x["out"]] for x in pre])
    yp = np.array([x["ms"] for x in pre])
    cp, *_ = np.linalg.lstsq(Xp, yp, rcond=None)
    # two-rate model with no intercept, for comparison with the refuter's figures
    model = {"c0": float(coef[0]), "blank": float(coef[1]), "dense": float(coef[2]), "pre_in": float(cp[0]), "pre_out": float(cp[1])}
    fit = {"n": len(samples), "mae_ms": float(np.mean(np.abs(err))), "rmse_ms": float(np.sqrt(np.mean(err**2))),
           "mape": float(np.mean(np.abs(err) / y)), "max_abs_ms": float(np.max(np.abs(err))),
           "today_mae_ms": float(np.mean(np.abs(err[[x["kind"] == "today" for x in samples]]))),
           "s3_mae_ms": float(np.mean(np.abs(err[[x["s"] == 3.0 and x["kind"] == "mosaic" for x in samples]]))),
           "bias_by_s": {str(s): float(np.mean(err[[x["s"] == s and x["kind"] == "mosaic" for x in samples]])) for s in S_GRID},
           "pre_mae_ms": float(np.mean(np.abs(Xp @ cp - yp))),
           "blank_fit": {"c0": float(cb[0]), "blank": float(cb[1]), "samples": blanks},
           "free_fit": {"c0": float(free[0]), "blank": float(free[1]), "dense": float(free[2])}}
    (HERE / "cost.json").write_text(json.dumps({"model": model, "fit": fit, "samples": samples, "pre": pre}, indent=1), encoding="utf-8")
    print(json.dumps({"model": model, "fit": {k_: v for k_, v in fit.items() if k_ != "blank_fit"} | {"blank_fit": {"c0": float(cb[0]), "blank": float(cb[1])}}}, indent=1))


if __name__ == "__main__":
    main()
