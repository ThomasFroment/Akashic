"""Dev-only view: prints dev pairs with S/E plans and gold (held-out rows filtered out)."""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import harness as h
pairs = [p for p in h.load_pairs() if p["split"] == "dev"]
for p, e, s in h.prepare(pairs):
    print(f"[{p['id']}] {p['lang']} {p['category']}  {p['prev_question']!r} -> {p['question']!r}")
    for f in h.FIELDS:
        print(f"    {f:8} gold={p['gold'][f]:7} S={h.show(h.value(s,f))} E={h.show(h.value(e,f))}")
