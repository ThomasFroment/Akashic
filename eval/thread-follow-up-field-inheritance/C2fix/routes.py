"""List C2fix routed cells over pairs.jsonl (all 170) with gold labels and the rule decision."""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import harness as h
import importlib.util
spec = importlib.util.spec_from_file_location("c2fixcand", HERE / "candidate.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
prepared = h.prepare(h.load_pairs())
n = 0
for p, e, s in prepared:
    m._rule_plan_thread(p["question"], p["prev_question"], e, s, h.names_of(p), h.NOW)
    if m.LAST["cue"]["C"]:
        continue
    f = m.route(p["question"], e, s, dict(m.LAST["carry"]))
    if f:
        n += 1
        print(f"[{p['id']}] {p['split']} {p['prev_question']!r} -> {p['question']!r} fields={f} gold={ {k: p['gold'][k] for k in f} }")
print(f"ROUTED {n}/{len(prepared)}")
