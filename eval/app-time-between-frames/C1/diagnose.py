"""Where C1's holes come from (numbers only): hole seconds by the cause that ended the band before
the hole, and whether the hole is at the start of a truth run (switch latency) or inside it.

    uv run python eval/app-time-between-frames/C1/diagnose.py
"""
import sys, time
from bisect import bisect_right
from collections import Counter
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
from core import Ctx, install_sim, merge  # noqa: E402
import score  # noqa: E402

RUNS = ["20261119-standard-s21-target-0315", "20261119-standard-s21-target-1818", "20261119-day-s31-target-0547"]
c1 = core.load(HERE / "c1.py")
now = int(time.time() * 1000)
for rid in RUNS:
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    install_sim(conn, [], [])
    c1.DEBUG = []
    bands = c1.bands(conn, tr.t0, tr.t1, now, Ctx(conn, tr.t0, tr.t1, now))
    dbg = sorted(c1.DEBUG)
    c1.DEBUG = None
    ends = sorted((d[2], d[4], d[2] - d[1], (d[3] or "").lower()) for d in dbg)
    end_ts = [x[0] for x in ends]
    gaps = merge([(g.start_ms, g.end_ms) for g in core.overlapping(conn, tr.t0, tr.t1, now, ("all", "screen"))] + score.truth_gaps(tr))
    cov = merge([(x.start_ms, x.end_ms) for x in bands] + gaps)
    runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
    by_cause, at_start, n_by_cause, same_app = Counter(), Counter(), Counter(), Counter()
    excluded = 0
    for r in runs:
        excl = "#private" in (r["window"] or "") or (r["exe"] or "").lower() in core.EXCLUDED_PROCESSES
        # uncovered pieces of [t0, t1)
        a = r["t0"]
        pieces = []
        for c0, c1_ in cov:
            if c1_ <= a:
                continue
            if c0 >= r["t1"]:
                break
            if c0 > a:
                pieces.append((a, min(c0, r["t1"])))
            a = max(a, c1_)
            if a >= r["t1"]:
                break
        if a < r["t1"]:
            pieces.append((a, r["t1"]))
        for h0, h1 in pieces:
            if excl:
                excluded += h1 - h0
                continue
            k = bisect_right(end_ts, h0) - 1
            cause = ends[k][1] if k >= 0 else "none"
            by_cause[cause] += h1 - h0
            n_by_cause[cause] += 1
            at_start["run_start" if h0 == r["t0"] else "inside_run"] += h1 - h0
            if k >= 0 and ends[k][3] == (r["exe"] or "").lower():
                same_app[cause] += h1 - h0
    hold_by_cause = Counter()
    for x in ends:
        hold_by_cause[x[1]] += 1
    print(rid[-9:], "excluded_window_s=%.1f" % (excluded / 1000),
          "hole_s_by_prev_end_cause", {k: round(v / 1000, 1) for k, v in by_cause.items()},
          "n", dict(n_by_cause), "same_app_s", {k: round(v / 1000, 1) for k, v in same_app.items()},
          "position_s", {k: round(v / 1000, 1) for k, v in at_start.items()}, "band_end_causes", dict(hold_by_cause))
    conn.close()
