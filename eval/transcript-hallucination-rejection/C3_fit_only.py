"""Run a C3 candidate's fit() on the mining split only (no test segment is touched)."""

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402

cand = harness.load_candidate(Path(sys.argv[1]))
data = harness.load_cache()
ids = {cc["id"]: lab["seg_id"] for cc, lab in data}
helpers = harness.Helpers(ids)
helpers.mining = [(cc, lab) for cc, lab in data if lab["split"] == "mining"]
t0 = time.perf_counter()
cand.fit(helpers.mining, helpers)
print(f"fit {time.perf_counter() - t0:.1f} s, decodes={helpers.decodes}")
