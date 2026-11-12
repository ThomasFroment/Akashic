"""Print the mining G distributions from C3\\fit_<run>.json (mining split only)."""

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
d = json.loads((HERE / "C3" / f"fit_{sys.argv[1]}.json").read_text(encoding="utf-8"))
print(d["fit"])
rows = d["rows"]


def q(vals):
    v = np.array([x for x in vals if x is not None])
    v = np.where(np.isfinite(v), v, 99)
    if not len(v):
        return "n=0"
    return f"n={len(v)} p50={np.percentile(v, 50):.2f} p90={np.percentile(v, 90):.2f} p95={np.percentile(v, 95):.2f} p99={np.percentile(v, 99):.2f} max={v.max():.2f} none={sum(x is None for x in vals)}"


groups = {}
for r in rows:
    k = r["role"] if r["role"] == "nonspeech" else f"{r['role']}/{r['subset'].split('/')[0]}/{'opus' if r['opus'] else 'clean'}"
    groups.setdefault(k, []).append(r["G"])
for k, v in sorted(groups.items()):
    print(f"{k:32s} {q(v)}")
ns = [r for r in rows if r["role"] == "nonspeech"]
print("nonspeech <= 1.0:")
for r in sorted(ns, key=lambda r: r["G"] if r["G"] is not None else -1)[:25]:
    print(f"  G={r['G']} ns={r['ns']:.2f} {r['subset']} {r['text']!r}")
sp = [r for r in rows if r["role"] != "nonspeech" and r["G"] is not None]
print("speech, highest G:")
for r in sorted(sp, key=lambda r: -r["G"])[:25]:
    print(f"  G={r['G']:.2f} ns={r['ns']:.2f} {r['subset']} opus={r['opus']} head={r['head']} {r['text']!r}")
print("sweep (g, new rejects/strips on speech, speech G>g, recall):")
for row in d["sweep"][:: max(1, len(d["sweep"]) // 40)]:
    print("  ", row)
