"""Refit the extra-call cost model on real c3 calls (corpus, all renderers, live + deferred). Numbers only."""
import json, sys
from pathlib import Path
import numpy as np
from PIL import Image
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness")); sys.path.insert(0, str(HERE))
import c3
from shared import CORPUS, Ctx, engines
fr, en = engines()
pages = json.loads((CORPUS / "pages.json").read_text(encoding="utf-8"))["pages"]
for cfg in ((3, False, 0.35), (2, True, 0.2), (3, True, 0.2)):
  c3.S_SMALL, c3.SHELF, c3.PAD_Y = cfg
  for r in ("chromium", "chromium_gray", "pillow"):
    for p in pages:
        if p["px"] > 13 or p["lang"] == "fr":
            continue
        img = Image.open(CORPUS / r / p["dir"] / f"{p['id']}.png").convert("RGB")
        c3.read(img, Ctx("live", p["scale"], fr, en))
S = [s for s in c3.STATS if s.get("ocr2_ms")]
y = np.array([s["ocr2_ms"] for s in S])
feats = {"px": lambda s: [1, s["mosaic_mpx"], s["tile_mpx"]],
                  "px+chars": lambda s: [1, s["mosaic_mpx"], s["tile_mpx"], s["n_chars"] / 1000],
         "px+native": lambda s: [1, s["mosaic_mpx"], s["tile_mpx"], s["native_kpx"]],
         "total+native": lambda s: [1, s["mosaic_mpx"], s["native_kpx"]]}
out = {"n": len(S)}
for name, f in feats.items():
    X = np.array([f(s) for s in S]); c, *_ = np.linalg.lstsq(X, y, rcond=None); e = y - X @ c
    out[name] = {"coef": c.round(3).tolist(), "resid_p95_abs": float(np.percentile(np.abs(e), 95)), "under_p95": float(np.percentile(e, 95))}
    print(name, out[name])
for s_ in S: s_.pop('split', None)
(HERE / 'fitcost_samples.json').write_text(json.dumps(S), encoding='utf-8')
print("old model under-prediction p95:", float(np.percentile([s["ocr2_ms"] - s["pred_ms"] for s in S], 95)))
(HERE / "fitcost.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
