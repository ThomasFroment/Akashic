"""Summary tables from a harness details.json (dev only). Usage: C1_summary.py <out_dir>"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import wilson_upper
d = json.loads((Path(sys.argv[1]) / "details.json").read_text(encoding="utf-8"))
m, rows = d["metrics"], d["rows"]
print("FRR (harness) per subset:")
for k, v in m["frr"].items():
    print(f"  {k:7s} {v['k']:3d}/{v['n']:4d} = {v['rate']:.4f} ub95 {v['ub95']:.4f}")
sp = [r for r in rows if r["role"] != "nonspeech"]
subs = {"all": lambda r: True, "short": lambda r: r["subset"] == "short", "names": lambda r: r["subset"] == "names",
        "normal": lambda r: r["subset"] == "normal", "concat": lambda r: r["subset"].startswith("concat"),
        "opus": lambda r: r["opus"], "fr": lambda r: r["lang"] == "fr", "en": lambda r: r["lang"] == "en"}
print("Word-loss FRR (rejected/stripped AND >= 1 correctly heard reference word lost):")
for k, f in subs.items():
    rs = [r for r in sp if f(r)]
    kk = sum(r["matched_kept"] < r["matched_raw"] for r in rs)
    print(f"  {k:7s} {kk:3d}/{len(rs):4d} = {kk/len(rs):.4f} ub95 {wilson_upper(kk, len(rs)):.4f}")
print("rejected/stripped with matched_raw == 0 (raw had no reference word):",
      sum(1 for r in sp if r["false_reject"] and r["matched_raw"] == 0))
print("recall by source:", {k: f"{v['k']}/{v['n']}" for k, v in m["nonspeech_recall_by_source"].items()})
print("tail:", m["tail_recall"])
print("unclear:", {k: f"{v['rate']:.3f}" for k, v in m["unclear"].items()})
print("reject recall:", m["hallucination_reject_recall"])
