"""Summarize a C1 harness run against the baseline: corpus CER by CSS px, replay join buckets, mosaic stats."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "c1"
cand = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
base = json.loads((HERE.parent / "baseline" / "metrics.json").read_text(encoding="utf-8"))
up3 = json.loads((HERE.parent / "explored" / "up3x_lanczos" / "metrics.json").read_text(encoding="utf-8"))

print("corpus chromium live CER by CSS px  (base / 3x / C1)")
for sc in ("1440", "4k"):
    row = []
    for px in (9, 10, 11, 12, 13, 14, 16, 18):
        k = f"chromium/live/{sc}/px={px}"
        row.append(f"{px}: {base['corpus'][k]['cer']:.3f}/{up3['corpus'][k]['cer']:.3f}/{cand['corpus'][k]['cer']:.3f}")
    print(f"  {sc}: " + "  ".join(row))
for r in ("chromium_gray", "pillow"):
    for sc in ("1440",):
        row = []
        for px in (9, 10, 11, 12):
            k = f"{r}/live/{sc}/px={px}"
            if k in cand["corpus"]:
                row.append(f"{px}: {base['corpus'][k]['cer']:.3f}/{cand['corpus'][k]['cer']:.3f}")
        if row:
            print(f"  {r} {sc}: " + "  ".join(row))
if cand["replay"]:
    print("replay cer_join (base / 3x / C1)")
    for k, v in cand["replay"].items():
        if "bucket" in k or "all=" in k:
            print(f"  {k}: {base['replay'][k]['cer_join']:.3f} / {up3['replay'][k]['cer_join']:.3f} / {v['cer_join']:.3f}  n={v['n']}  ms_p50={v['ms_p50']:.0f}")
m = cand["metrics"]
print({k: round(m[k], 3) for k in ("cer_4k", "cer_1440", "dms_p95", "cpu_ratio", "cpu_def_ratio", "dms_def_p95") if m.get(k) is not None})
st_path = out / "stats.json" if (out / "stats.json").is_file() else HERE / f"{out.name}_stats.json"
if st_path.is_file():
    st = json.loads(st_path.read_text(encoding="utf-8"))
    for path in ("live", "deferred", "replay"):
        xs = [x for x in st if x["path"] == path]
        if not xs:
            continue
        mos = [x for x in xs if x.get("mode") == "mosaic"]
        ss = [x["s"] for x in mos]
        print(f"{path}: calls {len(xs)} mosaic {len(mos)} today {len(xs) - len(mos)}; s p10/50/90 "
              f"{np.percentile(ss, [10, 50, 90]).round(2) if ss else 'na'}; "
              f"ocr lines {sum(x.get('ocr_lines', 0) for x in mos)} spanning two strips {sum(x.get('span_lines', 0) for x in mos)}")
        for sc in sorted({x["scale"] for x in mos}):
            sub = [x["s"] for x in mos if x["scale"] == sc]
            print(f"   scale {sc}: n {len(sub)} s p10/50/90 {np.percentile(sub, [10, 50, 90]).round(2)}")
