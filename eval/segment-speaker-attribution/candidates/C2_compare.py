"""Per-subset authorship / wrong-author of C2 iterations vs reference (from details.json)."""
import json, sys
from pathlib import Path
O = Path(__file__).resolve().parents[1] / "out"
runs = {"ref": O / "reference/details.json"}
for a in sys.argv[1:]:
    runs[a] = O / "C2_calib" / a / "details.json"
D = {k: json.loads(p.read_text(encoding="utf-8")) for k, p in runs.items() if p.exists()}
f = lambda v: "  na " if v is None else f"{v:5.3f}"
print("subset".ljust(20) + "".join(f"{k + ' auth':>12}{k + ' wrong':>12}" for k in D))
print("ALL".ljust(20) + "".join(f"{f(d['summary']['authorship'][0]):>12}{f(d['summary']['wrong'][0]):>12}" for d in D.values()))
for s in next(iter(D.values()))["summary"]["subsets"]:
    print(s.ljust(20) + "".join(f"{f(d['summary']['subsets'][s]['authorship'][0]):>12}{f(d['summary']['subsets'][s]['wrong'][0]):>12}" for d in D.values()))
for k, d in D.items():
    t = d["summary"]["timing"]; dr = d.get("drift") or {}
    print(k, {x: round(v, 2) for x, v in t.items()}, "profiles_end", d["n_profiles_end"], "drift wrong_all", dr.get("wrong_all"), "auth f/l", dr.get("auth_first100"), dr.get("auth_last100"))
