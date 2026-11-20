"""Fit C2's OCR cost model on corpus timings: t = c0 + k_n*native + k_d*dense + k_b*blank (ms, Mpix),
and the preprocessing time pre = pre_c + pre_k * small output Mpix. Numbers only; writes cost.json.

Samples (raw Edge ClearType pages, all themes, en + mixed): today's crop (find_text_box), today's
deferred image (whole window) and C2 mosaics at s in {1.5, 2, 2.5, 3} with every strip small.
Each OCR time is the median of 3 calls after one warm-up.

    uv run python eval/small-text-ocr/C2/fit_cost.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c2  # noqa: E402
import corpus as corpus_mod  # noqa: E402
from shared import CORPUS, Ctx, engines  # noqa: E402

from akashic.capture import imaging  # noqa: E402


def ocr_ms(fr, im) -> float:
    fr.recognize(im)
    ts = []
    for _ in range(3):
        t = time.perf_counter()
        fr.recognize(im)
        ts.append((time.perf_counter() - t) * 1000)
    return float(np.median(ts))


def main() -> None:
    fr, en = engines()
    pages = [p for p in corpus_mod.build()["pages"] if p["lang"] in ("en", "mixed")]
    rows, pre = [], []
    for p in pages:
        img = Image.open(CORPUS / "chromium" / p["dir"] / f"{p['id']}.png").convert("RGB")
        box = imaging.find_text_box(img)
        crop = img.crop((box.left, box.top, box.right, box.bottom))
        for region, path in ((crop, "live"), (img, "deferred")):
            boxes = c2.strips(np.asarray(region.convert("L")))
            native = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
            rows.append({"kind": f"today_{path}", "native": native, "dense": native, "blank": region.width * region.height - native,
                         "ms": ocr_ms(fr, region), "px": p["px"], "scale": p["scale"]})
        for s in (1.5, 2.0, 2.5, 3.0):
            for method in ("lumanorm", "auto"):
                ctx = Ctx("live", p["scale"], fr, en, {"method": method, "force_s": s, "all_small": True})
                t = time.perf_counter()
                im, info = c2.mosaic(img, ctx)
                pre.append((info["dense"] / 1e6, (time.perf_counter() - t) * 1000))
                rows.append({"kind": f"mosaic_{method}", "s": s, "native": info["native"], "dense": info["dense"], "blank": info["blank"],
                             "ms": ocr_ms(fr, im), "px": p["px"], "scale": p["scale"]})
        print(p["id"], p["scale"], flush=True)
    X = np.array([[1.0, r["native"] / 1e6, r["dense"] / 1e6, r["blank"] / 1e6] for r in rows])
    y = np.array([r["ms"] for r in rows])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ coef
    # Same model without the content term, for comparison.
    X3 = X[:, [0, 2, 3]]
    coef3, *_ = np.linalg.lstsq(X3, y, rcond=None)
    res3 = y - X3 @ coef3
    P = np.array(pre)
    pc, *_ = np.linalg.lstsq(np.c_[np.ones(len(P)), P[:, 0]], P[:, 1], rcond=None)
    out = {"c0": float(coef[0]), "k_n": float(coef[1]), "k_d": float(coef[2]), "k_b": float(coef[3]),
           "pre_c": float(pc[0]), "pre_k": float(pc[1]),
           "fit": {"n": len(rows), "abs_err_p50": float(np.percentile(np.abs(res), 50)), "abs_err_p95": float(np.percentile(np.abs(res), 95)),
                   "rel_err_p95": float(np.percentile(np.abs(res) / y, 95)),
                   "no_content_term": {"c0": float(coef3[0]), "k_d": float(coef3[1]), "k_b": float(coef3[2]),
                                       "abs_err_p95": float(np.percentile(np.abs(res3), 95))},
                   "by_kind_mean_resid": {k: float(np.mean([res[i] for i, r in enumerate(rows) if r["kind"] == k]))
                                          for k in sorted({r["kind"] for r in rows})},
                   "pre_ms_p50": float(np.percentile(P[:, 1], 50)), "pre_ms_p95": float(np.percentile(P[:, 1], 95))}}
    (HERE / "cost.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    (HERE / "cost_samples.json").write_text(json.dumps(rows), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
