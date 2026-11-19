"""Numbers-only diagnosis of C3's per-run M5.3 on one run: per truth run of one exe, its duration,
the longest overlapping same-app band (scorer rule) and the clipped same-app time.

    uv run python eval/app-time-between-frames/C3/diag.py <run_id> <exe> [variant]
"""

import importlib.util
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
H = HERE.parent / "harness"
sys.path.insert(0, str(H))
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("hrun", H / "run.py")
hrun = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hrun)
import core  # noqa: E402
import score  # noqa: E402

rid, exe = sys.argv[1], sys.argv[2].lower()
var = sys.argv[3] if len(sys.argv) > 3 else "c3"
cand = core.load(HERE / f"{var}.py")
tr = score.Truth(score.RESULTS / rid)
_, _, conn, _ = score.open_store(rid)
now = int(time.time() * 1000)
rows, _ = hrun.sim_fg_rows(tr, conn, now)
core.install_sim(conn, rows, [])
bands = cand.bands(conn, tr.t0, tr.t1, now, core.Ctx(conn, tr.t0, tr.t1, now))
runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
print("idx dur_s longest_s clipped_s n_same kind_next")
for k, r in enumerate(runs):
    if (r["exe"] or "").lower() != exe:
        continue
    same = [(x.start_ms, x.end_ms) for x in bands if (x.process or "").lower() == exe and x.start_ms < r["t1"] and x.end_ms > r["t0"]]
    longest = max((b - a for a, b in same), default=0)
    clipped = sum(max(0, min(b, r["t1"] + 5000) - max(a, r["t0"] - 5000)) for a, b in same)
    print(k, round((r["t1"] - r["t0"]) / 1000, 1), round(longest / 1000, 1), round(clipped / 1000, 1), len(same))
conn.close()
