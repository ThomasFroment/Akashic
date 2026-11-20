"""Numbers-only diagnosis of C1 on replay rows: per bucket/res, strip-height histogram, mode, s, costs.
No OCR is run; no captured text or pixels are printed or written."""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c1  # noqa: E402
import replay  # noqa: E402
from shared import score  # noqa: E402

agg = defaultdict(list)
for run_id in replay.RUNS:
    _p, _k, conn, media = score.open_store(run_id)
    try:
        rows, _ = replay.select(run_id, conn, 100)
        cache: dict = {}
        for r in rows:
            crop, _ = replay._crop(conn, media, r, cache)
            lum = np.asarray(crop.convert("L"))
            strips = c1.find_strips(lum, r["scale"])
            hs = np.array([st.h for st in strips]) if strips else np.array([0])
            small = [st for st in strips if st.h <= c1.SMALL_H]
            s, today, pred = c1.choose_scale(strips, crop.width, crop.height) if small else (1.0, 0, 0)
            small_area = sum((st.w + 2) * (st.h + 2) for st in small) / 1e6
            dense = sum((st.w + 2) * (st.h + 2) for st in strips) / 1e6
            extra = {}
            if small:
                for sx in (2.0, 3.0):
                    w_, h_, _, d_ = c1.layout(strips, sx)
                    pre = c1.COST["pre_in"] * crop.width * crop.height / 1e6 + c1.COST["pre_out"] * small_area * sx * sx
                    extra[f"x{sx:g}"] = c1.predict(w_, h_, d_) + pre - today
            talls = sorted(((st.h, st.w) for st in strips if st.h > 40), reverse=True)[:2]
            extra["tall_h"] = talls[0][0] if talls else 0
            extra["tall_w"] = talls[0][1] if talls else 0
            agg[(run_id[9:13], r["res"], r["bucket"])].append(extra | 
                {"w": crop.width, "h": crop.height, "n": len(strips), "small": len(small), "s": s, "today": today,
                 "small_mpix": small_area, "dense_mpix": dense, "h_p50": float(np.median(hs)),
                 "tall": int((hs > 40).sum()), "h16_30": int(((hs > 15) & (hs <= 30)).sum()),
                 "ink_frac": float(c1.ink_mask(lum).mean())})
    finally:
        conn.close()
for k in sorted(agg):
    xs = agg[k]
    f = lambda key: np.percentile([x[key] for x in xs], [10, 50, 90]).round(3)  # noqa: E731
    print(k, "n", len(xs), "size p50", f("w")[1], f("h")[1], "| strips", f("n"), "small", f("small"), "h16-30", f("h16_30"),
          "tall>40", f("tall"), "| s", f("s"), "| small Mpix", f("small_mpix"), "dense Mpix", f("dense_mpix"),
          "| pred today", f("today"), "ink", f("ink_frac"))
    if all("x3" in x for x in xs):
        print("    extra ms over today at s=2", f("x2"), "s=3", f("x3"), "| tallest strip h", f("tall_h"), "w", f("tall_w"))
    else:
        print("    tallest strip h", f("tall_h"), "w", f("tall_w"))
