"""Numbers-only config sweep on <= 12 px corpus pages (chromium, both scales, live): CER, refine cost, unrefined share."""
import json, sys, time
from pathlib import Path
import numpy as np
from PIL import Image
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness")); sys.path.insert(0, str(HERE))
import c3
from shared import CORPUS, Ctx, best_line_cer, engines, hyp_of, bucket
fr, en = engines()
pages = [p for p in json.loads((CORPUS / "pages.json").read_text(encoding="utf-8"))["pages"] if p["px"] <= 12 and p["lang"] != "fr" and p["theme"] != "color"]
cer = {"le11_1440": [], "le11_4k": [], "12_1440": []}
for p in pages:
    img = Image.open(CORPUS / "chromium" / p["dir"] / f"{p['id']}.png").convert("RGB")
    hyp = hyp_of(c3.read(img, Ctx("live", p["scale"], fr, en)))
    b = bucket(p["px"] * p["scale"]); key = ("le11" if b == "<=11" else "12") + ("_1440" if p["scale"] == 1.0 else "_4k")
    if key in cer:
        cer[key] += [best_line_cer(ln["text"], hyp) for ln in p["lines"]]
S = c3.STATS
extra = [s["total_ms"] - s["native_ms"] for s in S]
print(sys.argv[1] if len(sys.argv) > 1 else "", {k: round(float(np.mean(v)), 3) for k, v in cer.items() if v},
      "unrefined", round(sum(s["capped"] for s in S) / max(1, sum(s["small"] for s in S)), 3),
      "extra p50/p95", round(float(np.percentile(extra, 50)), 1), round(float(np.percentile(extra, 95)), 1),
      "ocr2-pred p95", round(float(np.percentile([s["ocr2_ms"] - s["pred_ms"] for s in S if s["ocr2_ms"]], 95)), 1))
