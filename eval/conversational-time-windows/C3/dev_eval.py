"""Dev-split-only scorer for C3 tuning (never reads held-out rows)."""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import harness  # noqa: E402

_all = harness.phrases
harness.phrases = lambda: [r for r in _all() if r["split"] == "dev"]

mod = harness.load(HERE / "candidate.py", "c3")
rows = harness.evaluate(mod)
for mode in ("rules", "model"):
    m = [r for r in rows if r["mode"] == mode]
    print(mode, "pass", harness.rate(m), "future", sum(r["future"] for r in m),
          "false", sum(r["false_filter"] for r in m), "err", sum(r["error"] is not None for r in m))
    by = defaultdict(list)
    for r in m:
        by[r["category"]].append(r)
    print({k: harness.rate(v) for k, v in sorted(by.items())})
    ta = defaultdict(list)
    for r in m:
        if r["category"] == "two_anchor":
            ta[r["connector"]].append(r)
    print("two_anchor by connector", {k: harness.rate(v) for k, v in sorted(ta.items())})
verbose = "-v" in sys.argv
seen = set()
for r in rows:
    if r["mode"] != "rules" or r["pass"]:
        continue
    if not verbose and r["question"] in seen:
        continue
    seen.add(r["question"])
    print(f"FAIL [{r['category']}] {r['now']} {r['ref_date']} {r['question']!r}\n   gold={r['gold']}\n   win={r['window']} err={r['error']}")
t = harness.timing(mod)
print(t)
