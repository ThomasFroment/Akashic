"""Numbers-only summary of one C3 harness run: musts per replay run, unrefined share, cost-model error.

    uv run python eval/small-text-ocr/C3/report.py <run dir with metrics.json and stats.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

d = Path(sys.argv[1])
m = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
rep = m["replay"]
print("replay (cer_join):")
for k, v in rep.items():
    if k.endswith("all=*") or "bucket=<=11" in k:
        print(f"  {k}: join={v['cer_join']:.3f} stored_join={v['stored_cer_join']:.3f} n={v['n']}")
for run in sorted({k.split('/')[0] for k in rep}):
    le = [rep[k] for k in rep if k.startswith(run) and "bucket=<=11" in k]
    n = sum(v["n"] for v in le)
    if n:
        print(f"  {run} <=11 both res: join={sum(v['cer_join'] * v['n'] for v in le) / n:.3f} n={n}")
mm = m["metrics"]
print("timing:", {k: round(mm[k], 3) for k in ("ms_p50", "ms_p95", "dms_p95", "ms_def_p95", "dms_def_p95", "cpu_ratio", "cpu_def_ratio")})
sp = d / "stats.json"
if sp.is_file():
    st = json.loads(sp.read_text(encoding="utf-8"))
    n_corpus = 144 * 3 * 2
    phases = {"corpus": st[:n_corpus], "replay": [s for s in st if s["path"] == "replay"]}
    for name, ss in phases.items():
        small = sum(s["small"] for s in ss)
        capped = sum(s["capped"] for s in ss)
        cached = sum(s["cached"] for s in ss)
        calls_capped = sum(1 for s in ss if s["capped"])
        print(f"{name}: calls={len(ss)} small_units={small} capped={capped} share_unrefined={capped / max(1, small):.3f} "
              f"cached={cached} calls_with_cap={calls_capped / max(1, len(ss)):.3f}")
        o = [s for s in ss if s.get("ocr2_ms")]
        if o:
            err = np.array([s["ocr2_ms"] - s["pred_ms"] for s in o])
            extra = np.array([s["total_ms"] - s["native_ms"] for s in ss])
            print(f"  extra call ms p50/p95={np.percentile([s['ocr2_ms'] for s in o], 50):.1f}/{np.percentile([s['ocr2_ms'] for s in o], 95):.1f} "
                  f"pred err (actual-pred) p50/p95/max={np.percentile(err, 50):.1f}/{np.percentile(err, 95):.1f}/{err.max():.1f} "
                  f"refine total (all calls) p50/p95={np.percentile(extra, 50):.1f}/{np.percentile(extra, 95):.1f}")
