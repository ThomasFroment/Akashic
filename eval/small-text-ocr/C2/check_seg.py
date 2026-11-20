"""Segmentation sanity check on the corpus (numbers only): strips per page, run heights by CSS px,
fringe energy of strips per renderer, preprocessing time."""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c2  # noqa: E402
from shared import CORPUS  # noqa: E402
import corpus as corpus_mod  # noqa: E402

from akashic.capture import imaging  # noqa: E402

pages = corpus_mod.build()["pages"]
hs = defaultdict(list)
nstrips = defaultdict(list)
fr = defaultdict(list)
tms = []
for r in ("chromium", "chromium_gray"):
    for p in pages:
        img = Image.open(CORPUS / r / p["dir"] / f"{p['id']}.png").convert("RGB")
        t = time.perf_counter()
        box = imaging.find_text_box(img)
        reg = img.crop((box.left, box.top, box.right, box.bottom))
        gray = np.asarray(reg.convert("L"))
        rgb = np.asarray(reg)
        bs = c2.strips(gray)
        tms.append((time.perf_counter() - t) * 1000)
        key = (r, p["scale"], p["px"])
        nstrips[key].append(len(bs))
        hs[key] += [b[4] for b in bs]
        for b in bs:
            cov = c2.coverage(rgb[b[1]:b[3], b[0]:b[2]])
            if cov is not None and b[4] <= c2.SMALL_H:
                fr[(r, p["theme"])].append(cov[2])
print(f"seg ms p50={np.percentile(tms, 50):.1f} p95={np.percentile(tms, 95):.1f}")
for k in sorted(nstrips):
    h = np.array(hs[k])
    print(k, "strips/page", np.mean(nstrips[k]), "h median", np.median(h), "h p10/p90", np.percentile(h, 10), np.percentile(h, 90),
          "small share", round(float((h <= c2.SMALL_H).mean()), 2))
for k in sorted(fr):
    v = np.array(fr[k])
    print("fringe", k, "n", v.size, "p5/p50/p95", np.percentile(v, [5, 50, 95]).round(3), "share>=T", round(float((v >= c2.FRINGE_T).mean()), 3))
