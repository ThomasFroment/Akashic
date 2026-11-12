"""Diagnostics for C1: compare out/C1_linked/details.json with out/reference per segment."""

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "out"
c = json.loads((OUT / "C1_linked" / "details.json").read_text(encoding="utf-8"))
r = json.loads((OUT / "reference" / "details.json").read_text(encoding="utf-8"))
ref = {s["id"]: s for s in r["segments"]}
segs = c["segments"]
ids = [s["id"] for s in segs]
rs = [ref[i] for i in ids]
for k in ("ms", "seg_n", "seg_ms", "emb_n", "emb_ms"):
    a, b = np.array([s[k] for s in segs]), np.array([s[k] for s in rs])
    print(f"{k:7s} cand mean {a.mean():7.1f} p50 {np.median(a):7.1f} | ref mean {b.mean():7.1f} p50 {np.median(b):7.1f}")
print("ref same-subset: p50", np.percentile([s["ms"] for s in rs], 50), "p95", np.percentile([s["ms"] for s in rs], 95))
print("cand          : p50", np.percentile([s["ms"] for s in segs], 50), "p95", np.percentile([s["ms"] for s in segs], 95))
ratio = np.array([s["ms"] / max(t["ms"], 1e-3) for s, t in zip(segs, rs, strict=True)])
print("per-segment ratio p50 %.2f p95 %.2f max %.2f" % tuple(np.percentile(ratio, [50, 95, 100])))
for lo, hi in ((0, 10000), (10000, 10500), (10500, 20000), (20000, 31000)):
    sel = [i for i, s in enumerate(segs) if lo <= s["length_ms"] < hi]
    if sel:
        a = np.mean([segs[i]["ms"] for i in sel]); b = np.mean([rs[i]["ms"] for i in sel])
        print(f"len {lo}-{hi}: n={len(sel)} cand {a:.0f} ref {b:.0f} ratio {a / b:.2f}")
wrong = Counter(); lost = Counter(); gained = Counter(); fixed = Counter()
for s, t in zip(segs, rs, strict=True):
    key = s["kind"] + ("/" + "+".join(x for x in s["subsets"] if x.startswith(("mixed_", "cont_"))) if True else "")
    if s["wrong"]:
        wrong[key] += 1
    if s["expected"] is not None and not s["authored"]:
        lost[key] += 1
    if s["wrong"] and not t["wrong"]:
        gained[key] += 1
    if t["wrong"] and not s["wrong"]:
        fixed[key] += 1
print("wrong:", dict(wrong)); print("new wrong vs ref:", dict(gained)); print("wrong fixed vs ref:", dict(fixed))
print("not authored:", dict(lost))
if len(sys.argv) > 1:
    for s in segs:
        if (sys.argv[1] == "wrong" and s["wrong"]) or (sys.argv[1] == "lost" and s["expected"] and not s["authored"]):
            print(s["id"], s["kind"], s["subsets"], s["length_ms"], "single", s["single"], "author", s["author"], s["turns"])
