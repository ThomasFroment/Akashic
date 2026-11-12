"""Scorer over all phrases.jsonl rows (dev + held-out) for C3fix tuning; prints failures."""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import harness  # noqa: E402

mod = harness.load(HERE / "candidate.py", "c3fix")
rows = harness.evaluate(mod)
for mode in ("rules", "model"):
    m = [r for r in rows if r["mode"] == mode]
    for split in ("dev", "heldout"):
        s = [r for r in m if r["split"] == split]
        print(mode, split, harness.rate(s), "future", sum(r["future"] for r in s),
              "false", sum(r["false_filter"] for r in s), "err", sum(r["error"] is not None for r in s))
seen = set()
for r in rows:
    if r["pass"]:
        continue
    key = (r["question"], r["mode"])
    if key in seen:
        continue
    seen.add(key)
    print(f"FAIL {r['mode']} [{r['category']}/{r['split']}] {r['now']} {r['question']!r}\n   gold={r['gold']}\n   win={r['window']} err={r['error']}")
if "-t" in sys.argv:
    print(harness.timing(mod))
