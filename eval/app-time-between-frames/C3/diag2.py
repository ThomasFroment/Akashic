"""Numbers-only: the truth runs with the largest clipped error under one c3.build config, with the
covering bands (process exe name or typed state) and whether the run's switch row was dropped.

    uv run python eval/app-time-between-frames/C3/diag2.py <run_id> "drop=0.1, seed=2"
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
import c3  # noqa: E402
import core  # noqa: E402
import score  # noqa: E402

rid = sys.argv[1]
kw = eval(f"dict({sys.argv[2] if len(sys.argv) > 2 else ''})")
tr = score.Truth(score.RESULTS / rid)
_, _, conn, _ = score.open_store(rid)
now = int(time.time() * 1000)
rows, _ = hrun.sim_fg_rows(tr, conn, now)
core.install_sim(conn, rows, [])
ctx = core.Ctx(conn, tr.t0, tr.t1, now)
kept = {r[0] for r in c3.perturb(rows, kw.get("drop", 0), False, kw.get("seed", 1))} if kw.get("drop") else {r[0] for r in rows}
bands = c3.build(**kw)(conn, tr.t0, tr.t1, now, ctx)
gaps = core.merge([(g.start_ms, g.end_ms) for g in ctx.gaps()])
runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
res = []
for k, r in enumerate(runs):
    exe = (r["exe"] or "").lower()
    same = [(x.start_ms, x.end_ms) for x in bands if (x.process or "").lower() == exe]
    got = core.overlap_ms(same, [(r["t0"] - 5000, r["t1"] + 5000)])
    dur = r["t1"] - r["t0"] - core.overlap_ms([(r["t0"], r["t1"])], gaps)
    cover = []
    for x in bands:
        if x.start_ms < r["t1"] and x.end_ms > r["t0"]:
            cover.append(((x.process or x.title or "?"), round((min(x.end_ms, r["t1"]) - max(x.start_ms, r["t0"])) / 1000, 1)))
    row_kept = any(abs(t - r["t0"]) < 2 for t in kept)
    res.append((abs(got - dur), k, r["exe"], round(dur / 1000, 1), round(got / 1000, 1), row_kept, cover[:4]))
res.sort(reverse=True)
print("err_s idx exe dur_s got_s row_kept cover")
for e, k, exe, d, g, kept_, cov in res[:8]:
    print(round(e / 1000, 1), k, exe, d, g, kept_, cov)
conn.close()
