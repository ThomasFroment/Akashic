"""Probe C1 on the synthetic corpus: strip heights per CSS px and scale, and a sample mosaic. Numbers only."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c1_r1 as c1  # noqa: E402
from shared import CORPUS  # noqa: E402

from akashic.capture import imaging  # noqa: E402

pages = json.loads((CORPUS / "pages.json").read_text(encoding="utf-8"))["pages"]
hs = defaultdict(list)
for p in pages:
    img = Image.open(CORPUS / "chromium" / p["dir"] / f"{p['id']}.png").convert("RGB")
    box = imaging.find_text_box(img)
    reg = img.crop((box.left, box.top, box.right, box.bottom))
    lum = np.asarray(reg.convert("L"))
    st = c1.find_strips(lum, p["scale"])
    hs[(p["scale"], p["px"])].append([s.h for s in st])
    if p["id"] == "mixed-dark-9" and p["scale"] == 1.0:
        s, today, pred = c1.choose_scale(st, reg.width, reg.height)
        m, boxes = c1.build_mosaic(lum, st, s)
        m.save(HERE / "probe_r1_mixed-dark-9.png")
        print("sample s", round(s, 2), "today", round(today, 1), "pred", round(pred, 1), "mosaic", m.size, "crop", reg.size)
for k in sorted(hs):
    allh = [h for x in hs[k] for h in x]
    n = np.mean([len(x) for x in hs[k]])
    print(k, f"strips/page {n:.1f}", "heights p10/50/90", np.percentile(allh, [10, 50, 90]).round(1), "max", max(allh),
          "small frac", round(np.mean([h <= c1.SMALL_H for h in allh]), 2))
