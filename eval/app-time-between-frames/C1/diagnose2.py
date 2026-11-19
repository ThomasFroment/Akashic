"""Holes after a 'monitor_frame' band end (numbers only): what covers the truth run next, how the
truth run's monitor relates to the ended band's monitor, and right/wrong-app coverage per candidate.

    uv run python eval/app-time-between-frames/C1/diagnose2.py
"""
import sys, time
from bisect import bisect_right
from collections import Counter
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
from core import Ctx, install_sim, merge, overlap_ms  # noqa: E402
import score  # noqa: E402

RUNS = ["20261118-smoke-s11-target-2220", "20261118-smoke-s11-target-2241", "20261119-standard-s21-target-0315",
        "20261119-standard-s21-target-1818", "20261119-day-s31-target-0547"]
fns = {"baseline": core.load(HERE.parent / "baseline" / "baseline.py").bands,
       "ref_confirmed": core.load(HERE.parent / "explored" / "ref_confirmed.py").bands}
c1 = core.load(HERE / "c1.py")
fns["C1"] = c1.bands
now = int(time.time() * 1000)
for rid in RUNS:
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    install_sim(conn, [], [])
    runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
    tot = sum(r["t1"] - r["t0"] for r in runs)
    out = [rid[-9:]]
    for name, fn in fns.items():
        bands = fn(conn, tr.t0, tr.t1, now, Ctx(conn, tr.t0, tr.t1, now))
        by_app = {}
        for x in bands:
            by_app.setdefault((x.process or "").lower(), []).append((x.start_ms, x.end_ms))
        allb = [(x.start_ms, x.end_ms) for x in bands]
        right = wrong = 0
        for r in runs:
            rr = overlap_ms([(r["t0"], r["t1"])], by_app.get((r["exe"] or "").lower(), []))
            right += rr
            wrong += overlap_ms([(r["t0"], r["t1"])], allb) - rr
        out.append(f"{name}: right={100 * right / tot:.2f}% wrong_app={100 * wrong / tot:.2f}%")
    print("  ".join(out))
    if "day" not in rid:
        conn.close()
        continue
    # monitor_frame holes on the day run
    c1.DEBUG = []
    bands = c1.bands(conn, tr.t0, tr.t1, now, Ctx(conn, tr.t0, tr.t1, now))
    dbg = c1.DEBUG
    c1.DEBUG = None
    mon_of = {}
    for s, e, mid in conn.execute("SELECT s.start_ms, s.end_ms, f.monitor_id FROM app_sessions s LEFT JOIN frames f ON f.id = s.last_frame_id"):
        mon_of[(s, e)] = mid
    names = score.monitor_names(conn)
    ends = sorted((d[2], d[4], mon_of.get((d[0], d[1]))) for d in dbg)
    end_ts = [x[0] for x in ends]
    starts = sorted((x.start_ms, (x.process or "").lower()) for x in bands)
    st = [x[0] for x in starts]
    cov = merge([(x.start_ms, x.end_ms) for x in bands] + [(g.start_ms, g.end_ms) for g in core.overlapping(conn, tr.t0, tr.t1, now, ("all", "screen"))])
    mon_rel, nxt_is_truth, lat = Counter(), Counter(), []
    for r in runs:
        a = r["t0"]
        for c0, c1_ in cov + [(r["t1"], r["t1"])]:
            if c1_ <= a:
                continue
            h0, h1 = a, min(max(c0, a), r["t1"])
            if h1 > h0:
                k = bisect_right(end_ts, h0) - 1
                if k >= 0 and ends[k][1] == "monitor_frame":
                    band_mon = names.get(ends[k][2], "?")
                    mon_rel[f"band_on_{band_mon}/truth_on_{r['monitor']}"] += h1 - h0
                    j = bisect_right(st, h0 - 1)
                    nxt_is_truth["next_band_is_truth_app" if j < len(starts) and starts[j][1] == (r["exe"] or "").lower() else "next_band_other"] += h1 - h0
                    lat.append((h1 - h0) / 1000)
            a = max(a, c1_)
            if a >= r["t1"]:
                break
    lat.sort()
    print("  monitor_frame holes: n=%d s=%.1f median=%.1f p90=%.1f max=%.1f" % (len(lat), sum(lat), lat[len(lat) // 2], lat[int(len(lat) * .9)], lat[-1]))
    print("  by monitor (band/truth):", {k: round(v / 1000, 1) for k, v in mon_rel.items()})
    print("  next band:", {k: round(v / 1000, 1) for k, v in nxt_is_truth.items()})
    conn.close()
