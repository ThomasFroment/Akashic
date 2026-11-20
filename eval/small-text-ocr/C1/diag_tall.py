"""Numbers only: why replay rows produce tall strips. For the tallest strip of each <=11 row, per-row ink counts."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c1  # noqa: E402
import replay  # noqa: E402
from shared import score  # noqa: E402

for run_id in replay.RUNS:
    _p, _k, conn, media = score.open_store(run_id)
    try:
        rows, _ = replay.select(run_id, conn, 100)
        cache: dict = {}
        shown = 0
        for r in rows:
            if r["bucket"] != "<=11" or r["res"] != "2560x1440" or shown >= 4:
                continue
            crop, _ = replay._crop(conn, media, r, cache)
            lum = np.asarray(crop.convert("L"))
            ink = c1.ink_mask(lum)
            strips = c1.find_strips(lum, r["scale"])
            st = max(strips, key=lambda s: s.h)
            sub = ink[st.y0:st.y1, st.x0:st.x1]
            rc = sub.sum(axis=1)
            cc = sub.sum(axis=0)
            col_full = int((cc >= 0.9 * st.h).sum())
            print(run_id[9:13], "crop", crop.size, "tall strip x0,y0,w,h", st.x0, st.y0, st.w, st.h,
                  "row ink p0/p10/p50/p90", np.percentile(rc, [0, 10, 50, 90]).round(1),
                  "rows with <=2 ink", int((rc <= 2).sum()), "cols ink>=90% rows", col_full,
                  "col ink p50/p90/max", np.percentile(cc, [50, 90, 100]).round(0))
            # strips found if near-full columns (rules, scrollbars) are ignored
            colmask = cc >= 0.6 * st.h
            sub2 = sub.copy()
            sub2[:, colmask] = False
            runs = c1._runs(sub2.any(axis=1), 1)
            print("     after dropping >=60% columns:", len(runs), "row runs; heights p50", np.median([b - a for a, b in runs]) if runs else 0,
                  "dropped cols at x", np.flatnonzero(colmask)[:6].tolist())
            shown += 1
    finally:
        conn.close()
