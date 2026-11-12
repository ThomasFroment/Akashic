"""Score candidate.py on the DEV split only (held-out rows are never evaluated or printed)."""
import json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import harness as h
pairs = [p for p in h.load_pairs() if p["split"] == "dev"]
prepared = h.prepare(pairs)
_, fn, _ = h.load_candidate(HERE)
res = h.evaluate(fn, prepared)
rows = res["rows"]
acc = h.accuracy(rows)["all"]
print("DEV", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in acc.items()}, "exc", res["exceptions"])
print("by cat", {c: round(v["min"], 2) for c, v in h.accuracy(rows, "category").items()})
for r in rows:
    bad = {f: v for f, v in r["fields"].items() if not v["ok"]}
    if bad:
        print(f"[{r['id']}] {r['prev_question']!r} -> {r['question']!r}")
        for f, v in bad.items():
            print(f"    {f}: gold={v['gold']} got={v['got']} C={v['candidate']} S={v['standalone']} E={v['earlier']}")
import importlib.util
spec = importlib.util.spec_from_file_location("c2cand", HERE / "candidate.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
routed = [(p, m.analyse(p["question"], e, s)) for p, e, s in prepared]
routed = [(p, a) for p, a in routed if a["undecided"]]
print(f"ROUTED {len(routed)}/{len(prepared)}")
for p, a in routed:
    print(f"   [{p['id']}] {p['question']!r} undecided={a['undecided']} gold={ {f: p['gold'][f] for f in a['undecided']} }")
