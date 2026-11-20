"""Refit C2's preprocessing time (numbers only): pre = pre_c + pre_k * small output Mpix, measured on the
corpus live path (chromium, chromium_gray) and the replay <= 11 / all rows of both runs; median of 3."""
import json, sys, time
from pathlib import Path
import numpy as np
from PIL import Image
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness")); sys.path.insert(0, str(HERE))
import c2, replay, corpus as corpus_mod
from shared import CORPUS, Ctx, score

def med(img, ctx, s):
    ts = []
    for _ in range(3):
        t = time.perf_counter(); im, info = c2.mosaic(img, ctx); ts.append((time.perf_counter() - t) * 1000)
    return float(np.median(ts)), info
pts = []
for r in ("chromium", "chromium_gray"):
    for p in corpus_mod.build()["pages"]:
        if p["lang"] != "mixed": continue
        img = Image.open(CORPUS / r / p["dir"] / f"{p['id']}.png").convert("RGB")
        for s in (1.5, 2.0, 3.0):
            ms, info = med(img, Ctx("live", p["scale"], None, None, {"force_s": s}), s)
            if info.get("s", 0) > 1: pts.append((info["dense"] / 1e6, ms, "corpus"))
for run_id in replay.RUNS:
    _p, _k, conn, media = score.open_store(run_id)
    rows, _ = replay.select(run_id, conn, 30); cache = {}
    for row in rows:
        crop, k = replay._crop(conn, media, row, cache)
        for s in (1.5, 2.0):
            ms, info = med(crop, Ctx("replay", row["scale"], None, None, {"force_s": s}), s)
            if info.get("s", 0) > 1: pts.append((info["dense"] / 1e6, ms, "replay"))
    conn.close()
P = np.array([(a, b) for a, b, _ in pts])
coef, *_ = np.linalg.lstsq(np.c_[np.ones(len(P)), P[:, 0]], P[:, 1], rcond=None)
res = P[:, 1] - (coef[0] + coef[1] * P[:, 0])
print(f"n={len(P)} pre_c={coef[0]:.1f} ms pre_k={coef[1]:.1f} ms/Mpix(dense out) resid p95={np.percentile(np.abs(res), 95):.1f} "
      f"ms p50={np.percentile(P[:, 1], 50):.1f} p95={np.percentile(P[:, 1], 95):.1f}")
for src in ("corpus", "replay"):
    v = np.array([b for a, b, s_ in pts if s_ == src]); print(src, f"p50={np.percentile(v, 50):.1f} p95={np.percentile(v, 95):.1f}")
