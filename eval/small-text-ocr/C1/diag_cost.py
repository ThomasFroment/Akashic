"""Numbers only: C1 cost-model prediction vs measured OCR ms on replay rows (today's call and the mosaic).

    uv run python diag_cost.py <module stem> [rows per res per run]
Writes <stem>_cost_replay.json (numbers and split labels only)."""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import replay  # noqa: E402
from shared import engines, score  # noqa: E402

stem = sys.argv[1] if len(sys.argv) > 1 else "c1_r1"
per = int(sys.argv[2]) if len(sys.argv) > 2 else 40
c1 = importlib.import_module(stem)
fr, _ = engines()


def timed(fn):
    fn()
    t = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t) * 1000, out


recs = []
for run_id in replay.RUNS:
    _p, _k, conn, media = score.open_store(run_id)
    try:
        rows, _ = replay.select(run_id, conn, per)
        cache: dict = {}
        for r in rows:
            crop, _ = replay._crop(conn, media, r, cache)
            lum = np.asarray(crop.convert("L"))
            t = time.perf_counter()
            strips = c1.find_strips(lum, r["scale"])
            t_find = (time.perf_counter() - t) * 1000
            small = [st for st in strips if st.h <= c1.SMALL_H]
            ms_today, lines_today = timed(lambda: c1.ocr_words(fr, crop))
            rec = {"run": run_id[9:13], "res": r["res"], "bucket": r["bucket"], "ms_today": ms_today, "t_find": t_find,
                   "words_today": sum(len(x) for x in lines_today), "lines_today": len(lines_today),
                   "crop_mpix": crop.width * crop.height / 1e6, "strips": len(strips), "small": len(small)}
            if small:
                s, today, pred = c1.choose_scale(strips, crop.width, crop.height)
                t = time.perf_counter()
                m, boxes = c1.build_mosaic(lum, strips, s)
                m = m.convert("RGB")
                t_build = (time.perf_counter() - t) * 1000
                W, H, _, dense = c1.layout(strips, s)
                ms_m, lines_m = timed(lambda: c1.ocr_words(fr, m))
                rec |= {"s": s, "pred_today": today, "pred_mosaic": pred, "ms_mosaic": ms_m, "t_build": t_build,
                        "mosaic_mpix": W * H / 1e6, "dense_mpix": dense / 1e6, "words_mosaic": sum(len(x) for x in lines_m),
                        "lines_mosaic": len(lines_m)}
            recs.append(rec)
    finally:
        conn.close()
(HERE / f"{stem}_cost_replay.json").write_text(json.dumps(recs, indent=0), encoding="utf-8")
m = [x for x in recs if "s" in x]
e_today = np.array([x["pred_today"] - x["ms_today"] for x in m])
e_mos = np.array([x["pred_mosaic"] - (x["ms_mosaic"] + x["t_find"] + x["t_build"]) for x in m])
delta = np.array([x["ms_mosaic"] + x["t_find"] + x["t_build"] - x["ms_today"] for x in m])
print(f"rows {len(recs)}, mosaic {len(m)}")
print("today pred-actual ms p10/50/90", np.percentile(e_today, [10, 50, 90]).round(1), "mape", np.mean(np.abs(e_today) / [x["ms_today"] for x in m]).round(3))
print("mosaic pred-actual ms p10/50/90", np.percentile(e_mos, [10, 50, 90]).round(1))
print("actual delta (mosaic+pre - today) ms p50/90/95/max", np.percentile(delta, [50, 90, 95, 100]).round(1))
for key in sorted({(x["run"], x["res"], x["bucket"]) for x in m}):
    sub = [x for x in m if (x["run"], x["res"], x["bucket"]) == key]
    print(key, len(sub), "today ms p50", round(float(np.median([x["ms_today"] for x in sub]))),
          "mosaic ms p50", round(float(np.median([x["ms_mosaic"] for x in sub]))),
          "pred today p50", round(float(np.median([x["pred_today"] for x in sub]))),
          "pred mosaic p50", round(float(np.median([x["pred_mosaic"] for x in sub]))),
          "s p50", round(float(np.median([x["s"] for x in sub])), 2),
          "words today/mosaic p50", round(float(np.median([x["words_today"] for x in sub]))), round(float(np.median([x["words_mosaic"] for x in sub]))),
          "dense Mpix", round(float(np.median([x["dense_mpix"] for x in sub])), 2), "mosaic Mpix", round(float(np.median([x["mosaic_mpix"] for x in sub])), 2))
# refit on replay: ms = a + b * blank + k * dense + w * words
X = np.array([[1, x["mosaic_mpix"] - x["dense_mpix"], x["dense_mpix"], x["words_mosaic"]] for x in m])
y = np.array([x["ms_mosaic"] for x in m])
c, *_ = np.linalg.lstsq(X, y, rcond=None)
print("replay mosaic fit [c0, blank, dense, per word]", c.round(3), "mae", np.abs(X @ c - y).mean().round(1))
