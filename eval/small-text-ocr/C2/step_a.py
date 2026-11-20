"""Step A: does the ClearType phase split beat luma Lanczos at equal output size? Numbers only.

Live path on the raw corpus renders (find_text_box crop, C2's strips), every strip treated as small and
scaled by a fixed s (no budget), one OCR call per page and method. CER per truth line (score's rule),
mean by renderer / scale / CSS px / method. Writes step_a.json and step_a.txt next to this file.

    uv run python eval/small-text-ocr/C2/step_a.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c2  # noqa: E402
import corpus as corpus_mod  # noqa: E402
from shared import CORPUS, Ctx, best_line_cer, engines, hyp_of  # noqa: E402

from akashic.capture import imaging  # noqa: E402

RUNS = {
    "chromium": [("today", 1), ("luma", 3), ("lumanorm", 3), ("rgb", 3), ("bgr", 3), ("auto", 3), ("auto_fir", 3), ("auto_l2", 3),
                 ("luma", 2), ("lumanorm", 2), ("auto", 2), ("auto_fir", 2)],
    "chromium_gray": [("today", 1), ("luma", 3), ("lumanorm", 3), ("auto", 3), ("luma", 2), ("auto", 2)],
}


def main() -> None:
    fr, en = engines()
    pages = corpus_mod.build()["pages"]
    acc: dict[str, list[float]] = defaultdict(list)
    orders: Counter = Counter()
    t0 = time.perf_counter()
    for r, methods in RUNS.items():
        for p in pages:
            img = Image.open(CORPUS / r / p["dir"] / f"{p['id']}.png").convert("RGB")
            truth = [ln["text"] for ln in p["lines"] if ln["fits"]]
            for m, s in methods:
                if m == "today":
                    box = imaging.find_text_box(img)
                    text = fr.recognize(img.crop((box.left, box.top, box.right, box.bottom)))
                else:
                    stats: dict = {}
                    ctx = Ctx("live", p["scale"], fr, en, {"method": m, "force_s": s, "all_small": True, "stats": stats})
                    text = c2.read(img, ctx)
                    if m.startswith("auto"):
                        orders.update(stats.get("order", []))
                hyp = hyp_of(text)
                cer = [best_line_cer(t, hyp) for t in truth]
                acc[f"{r}|{p['scale']}|{p['px']}|{m}@{s}"] += cer
        print(f"{r}: {time.perf_counter() - t0:.0f} s", flush=True)
    res = {k: {"cer": float(np.mean(v)), "n": len(v)} for k, v in acc.items()}
    (HERE / "step_a.json").write_text(json.dumps({"cells": res, "auto_order_votes": dict(orders)}, indent=1), encoding="utf-8")
    lines = []
    for r, methods in RUNS.items():
        cols = [f"{m}@{s}" for m, s in methods]
        for sc, name in ((1.0, "1440p"), (1.25, "4K")):
            lines.append(f"\n{r} {name} (CER by CSS px; <=11 device px = 9-11 px at 1440p, 9 px at 4K)")
            lines.append("px   " + " ".join(f"{c:>12}" for c in cols))
            for px in corpus_mod.SIZES:
                lines.append(f"{px:<4} " + " ".join(f"{res[f'{r}|{sc}|{px}|{c}']['cer']:>12.3f}" for c in cols))
            le = [px for px in corpus_mod.SIZES if px * sc < 12]
            vals = []
            for c in cols:
                xs = [x for px in le for x in acc[f"{r}|{sc}|{px}|{c}"]]
                vals.append(float(np.mean(xs)))
            lines.append("<=11 " + " ".join(f"{v:>12.3f}" for v in vals))
    lines.append(f"\nauto order votes per call: {dict(orders)}")
    txt = "\n".join(lines)
    (HERE / "step_a.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
