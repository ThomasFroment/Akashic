"""Oracle ceiling of the closed form: per pair, the best of all 32 label combinations.

    python oracle.py [pairs.jsonl|heldout2.jsonl] [--gated]
--gated applies the production gates (own needs a real S keyword, kc needs S.kinds != E.kinds).
Prints per-field ceiling accuracy and every pair no label combination fully reproduces.
"""

import itertools
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import harness as h  # noqa: E402, I001
import candidate as c  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
gated = "--gated" in sys.argv
pairs = h.load_pairs(HERE.parent / (args[0] if args else "pairs.jsonl"))
prepared = h.prepare(pairs)
ok = dict.fromkeys(h.FIELDS, 0)
miss = []
for p, e, s in prepared:
    gt = c.gate(p["question"], s)
    best, best_n = None, -1
    combos = itertools.product("sclx", (False, True), (False, True), (False, True))
    if "--natural" in sys.argv:  # one label set read off the category and gold, no search
        g = p["gold"]
        cat = p["category"]
        rel = {"target": "s", "topic_switch": "s", "short_selfcontained": "s",
               "continuation": "c", "repo_tests": "c", "exclusion": "x"}.get(cat, "l")  # fmt: skip
        kc = g["kinds"] != "inherit" and set(s.kinds) != set(e.kinds)
        combos = [(rel, g["keywords"] == "own", kc, g["keywords"] == "inherit")]
    for rel, own, kc, ref in combos:
        if gated:
            own = own and bool(gt["kw"])
            kc = kc and set(s.kinds) != set(e.kinds)
        out = c.closed_form(rel, own, kc, ref, s, e, p["question"], p["prev_question"])
        res = {f: h.correct(p["gold"][f], h.value(out, f), h.value(s, f), h.value(e, f))
               for f in h.FIELDS}  # fmt: skip
        n = sum(res.values())
        if n > best_n:
            best, best_n = (rel, own, kc, ref, res), n
    for f in h.FIELDS:
        ok[f] += best[4][f]
    if best_n < 4:
        miss.append((p["id"], p["prev_question"], p["question"],
                     {f: p["gold"][f] for f in h.FIELDS if not best[4][f]}, best[:4]))  # fmt: skip
n = len(prepared)
print(f"{'gated' if gated else 'raw'} ceiling n={n} " + " ".join(f"{f}={ok[f]/n:.3f}" for f in h.FIELDS)
      + f" min={min(ok.values())/n:.3f} unreachable_pairs={len(miss)}")  # fmt: skip
for m in miss:
    print("  ", m)
