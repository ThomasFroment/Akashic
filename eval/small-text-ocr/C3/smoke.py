"""Quick numbers-only smoke check of c3 vs baseline on a few corpus pages (live path)."""
import json, sys, time
from pathlib import Path
import numpy as np
from PIL import Image
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
from shared import CORPUS, Candidate, Ctx, ROOT, best_line_cer, engines, hyp_of
cand, base = Candidate(HERE / "c3.py"), Candidate(ROOT / "baseline.py")
fr, en = engines()
pages = json.loads((CORPUS / "pages.json").read_text(encoding="utf-8"))["pages"]
sel = [p for p in pages if p["lang"] == "mixed" and p["theme"] in ("dark", "light") and p["px"] in (9, 10, 11, 12, 16)]
for p in sel:
    img = Image.open(CORPUS / "chromium" / p["dir"] / f"{p['id']}.png").convert("RGB")
    truth = [ln["text"] for ln in p["lines"]]
    out = []
    for c in (base, cand):
        t = time.perf_counter(); txt = c.read(img, Ctx("live", p["scale"], fr, en)); ms = (time.perf_counter() - t) * 1000
        out.append((np.mean([best_line_cer(x, hyp_of(txt)) for x in truth]), ms))
    st = cand.mod.STATS[-1]
    print(p["id"].split("-",1)[1], p["scale"], f"base {out[0][0]:.3f} {out[0][1]:.0f}ms  c3 {out[1][0]:.3f} {out[1][1]:.0f}ms", {k: (round(v, 2) if isinstance(v, float) else v) for k, v in st.items() if k in ("small", "refined", "capped", "pred_ms", "ocr2_ms", "native_ms", "total_ms")})
