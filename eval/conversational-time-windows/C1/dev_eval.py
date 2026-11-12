"""Dev-split-only scoring for tuning C1 (never prints held-out rows)."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import harness  # noqa: E402

mod = harness.load(HERE / "candidate.py", "c1")
rows = [r for r in harness.evaluate(mod) if r["split"] == "dev"]
for mode in ("rules", "model"):
    m = [r for r in rows if r["mode"] == mode]
    print(mode, "pass", harness.rate(m), "future", sum(r["future"] for r in m),
          "false", sum(r["false_filter"] for r in m), "err", sum(r["error"] is not None for r in m))
    cats = Counter(r["category"] for r in m)
    ok = Counter(r["category"] for r in m if r["pass"])
    print("  ", {c: round(ok[c] / n, 2) for c, n in sorted(cats.items())})
if "-v" in sys.argv:
    for r in rows:
        if r["mode"] == "rules" and not r["pass"]:
            g = r["gold"]
            w = r["window"]
            fmt = lambda x: None if x is None else [s[5:16] for s in x]  # noqa: E731
            print(f"{r['id']} {r['category']:10} {r['now']:>5} {r['ref_date'][5:]} | {r['question'][:70]:70} "
                  f"| gold {fmt(g)} win {fmt(w)} cov={r['covers']} tight={r['tight']}")
