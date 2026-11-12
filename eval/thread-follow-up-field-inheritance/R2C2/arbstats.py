import json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
import harness as h  # noqa
import candidate as c  # noqa
for pf, cf in (("pairs.jsonl", "calls_pairs.jsonl"), ("heldout2.jsonl", "calls_heldout2.jsonl")):
    calls = {(r["prev"], r["question"]): r["result"] for r in map(json.loads, open(cf, encoding="utf-8"))}
    ov = gk = gr = go = 0
    for p, e, s in h.prepare(h.load_pairs(HERE.parent / pf)):
        lab = calls.get((p["prev_question"], p["question"]))
        if not lab: continue
        gt = c.gate(p["question"], s); rule = c.rule_labels(p["question"], s, e, gt)
        a = c.arbitrate(lab, rule, s, e, gt)
        ov += a.get("overridden", False); gk += lab["kc"] and not a["kc"]; gr += lab["ref"] and not a["ref"]; go += lab["own"] and not a["own"]
    print(pf, "link->standalone overrides", ov, "kc gated off", gk, "ref gated off", gr, "own gated off", go)
