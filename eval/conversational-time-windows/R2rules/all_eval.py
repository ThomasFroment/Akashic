"""Dev scorer for R2rules tuning: rules mode over a question file; prints failures.

Usage: python all_eval.py [phrases.jsonl|heldout2.jsonl] [-t]   (env CTW_GAZ=off|public|set)
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import harness  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("-")]
if args:
    harness.PHRASES = (HERE.parent / args[0]).resolve()
    sys.argv[1:1] = ["--phrases", args[0]]
mod = harness.load(HERE / "candidate.py", "r2rules")
rows = [r for r in harness.evaluate(mod) if r["mode"] == "rules"]
print("pass", harness.rate(rows), "future", sum(r["future"] for r in rows),
      "false", sum(r["false_filter"] for r in rows), "err", sum(r["error"] is not None for r in rows))
seen: dict = {}
for r in rows:
    if not r["pass"]:
        seen.setdefault((r["category"], r["question"]), []).append(r)
for (cat, q), rs in sorted(seen.items()):
    r = rs[0]
    print(f"FAIL [{cat}] x{len(rs)} {q!r}\n   {r['now']} gold={r['gold']} win={r['window']} err={r['error']}")
if "-t" in sys.argv:
    print(harness.timing(mod))
