"""Fair cost ratio: reference and C1 attribute() timed back to back on the same segments, in one
process with the same models, alternating which runs first. Other jobs on the machine then load
both equally. Usage: C1_timing.py [N segments, default 300] (every k-th evaluation segment)."""

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from common import FIXTURE, SR, PrevSegment, Segment, SharedModels  # noqa: E402
from harness import decode, enrol_all, load_candidate  # noqa: E402

n_want = int(sys.argv[1]) if len(sys.argv) > 1 else 300
ref = load_candidate(str(HERE / "candidates" / "reference.py"))
c1 = load_candidate(str(HERE / "candidates" / "C1_linked.py"))
index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
blob = (FIXTURE / "segments.bin").read_bytes()
models = SharedModels()
warm = np.random.default_rng(0).standard_normal(SR * 3).astype(np.float32) * 0.05
models.turns(warm)
models.embed(warm)
stores = {}
for name, mod in (("ref", ref), ("c1", c1)):
    stores[name] = mod.make_store()
    enrol_all(mod, stores[name], models, index, blob)
segs = [e for e in index["segments"] if e["kind"] not in ("drift",)]
by_id = {e["id"]: e for e in index["segments"]}
step = max(1, len(segs) // n_want)
pick = segs[::step][:n_want]
rows = []
for j, e in enumerate(pick):
    x = decode(blob, e)
    prev = None
    if e["continues"] is not None:
        p = by_id[e["continues"]]
        prev = PrevSegment(p["id"], None, decode(blob, p))
    seg = Segment(e["id"] * 10 + 1, x, SR, e["lead_ms"] if prev else 0, prev)
    order = (("ref", ref), ("c1", c1)) if j % 2 == 0 else (("c1", c1), ("ref", ref))
    t = {}
    for name, mod in order:
        s = Segment(seg.segment_id + (0 if name == "ref" else 5), x, SR, seg.lead_ms, prev)
        t0 = time.perf_counter()
        mod.attribute(stores[name], models, s)
        t[name] = (time.perf_counter() - t0) * 1000
    rows.append((e["length_ms"], t["ref"], t["c1"]))
a = np.array(rows)
r, c = a[:, 1], a[:, 2]
print(f"n={len(a)} ref p50 {np.median(r):.1f} p95 {np.percentile(r, 95):.1f} | c1 p50 {np.median(c):.1f}"
      f" p95 {np.percentile(c, 95):.1f} | ratio p50 {np.median(c) / np.median(r):.2f}"
      f" p95 {np.percentile(c, 95) / np.percentile(r, 95):.2f} mean {c.mean() / r.mean():.2f}")
q = c / r
print("per-segment ratio p50 %.2f p95 %.2f max %.2f" % tuple(np.percentile(q, [50, 95, 100])))
for lo, hi in ((0, 10000), (10000, 10500), (10500, 20000), (20000, 31000)):
    m = (a[:, 0] >= lo) & (a[:, 0] < hi)
    if m.any():
        print(f"len {lo}-{hi} ms: n={m.sum()} ref {r[m].mean():.0f} c1 {c[m].mean():.0f} ratio {c[m].mean() / r[m].mean():.2f}")
