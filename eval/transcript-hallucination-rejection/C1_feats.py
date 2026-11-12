"""Dump per-segment features for the mining split (dev only)."""
import sys, json, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import numpy as np
import harness, prod

data = harness.load_cache()
ids = {cc["id"]: lab["seg_id"] for cc, lab in data}
h = harness.Helpers(ids)
out = {}
t0 = time.perf_counter()
for cc, lab in data:
    s = h.samples(cc)
    out[cc["id"]] = h.silero(s)
print("silero all", time.perf_counter() - t0, len(out))
np.savez_compressed(HERE / "C1" / "silero_fresh.npz", **out)
