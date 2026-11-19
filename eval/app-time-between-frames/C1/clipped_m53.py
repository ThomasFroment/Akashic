"""Clipped M5.3 beside the scorer's (numbers only), and where C1's holes fall.

    uv run python eval/app-time-between-frames/C1/clipped_m53.py

Clipped M5.3 per run = sum over truth runs (non-foreign) of |seconds of same-app bands inside
[t0 - 5 s, t1 + 5 s] - truth duration| / total truth seconds. Unlike score.m5 (longest overlapping
same-app band, unclipped), a hold that spans several truth runs of one app counts only inside each.
Holes are split into: truth time of processes that never have a focused row in the store
(excluded or unnamed windows: no band can cover them) and the rest.
"""
import sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
from core import Ctx, install_sim, merge, overlap_ms  # noqa: E402
import score  # noqa: E402

RUNS = ["20261118-smoke-s11-target-2220", "20261118-smoke-s11-target-2241", "20261119-standard-s21-target-0315",
        "20261119-standard-s21-target-1818", "20261119-day-s31-target-0547"]
SLACK = 5_000
fns = {"baseline": core.load(HERE.parent / "baseline" / "baseline.py").bands,
       "ref_confirmed": core.load(HERE.parent / "explored" / "ref_confirmed.py").bands,
       "C1": core.load(HERE / "c1.py").bands}
now = int(time.time() * 1000)
for rid in RUNS:
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    install_sim(conn, [], [])
    runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
    tot = sum(r["t1"] - r["t0"] for r in runs)
    stored = {p.lower() for (p,) in conn.execute("SELECT DISTINCT process FROM frame_windows WHERE focused = 1 AND process IS NOT NULL")}
    gaps = merge([(g.start_ms, g.end_ms) for g in core.overlapping(conn, tr.t0, tr.t1, now, ("all", "screen"))] + score.truth_gaps(tr))
    line = [rid[-9:]]
    for name, fn in fns.items():
        bands = fn(conn, tr.t0, tr.t1, now, Ctx(conn, tr.t0, tr.t1, now))
        by_app: dict[str, list] = {}
        for x in bands:
            by_app.setdefault((x.process or "").lower(), []).append((x.start_ms, x.end_ms))
        err = 0
        for r in runs:
            got = overlap_ms([(r["t0"] - SLACK, r["t1"] + SLACK)], by_app.get((r["exe"] or "").lower(), []))
            err += abs(got - (r["t1"] - r["t0"]))
        line.append(f"{name}_clipped={err / tot:.3f}")
        if name == "C1":
            cov = merge([(x.start_ms, x.end_ms) for x in bands]) + gaps
            hole_unstorable = hole_other = 0
            for r in runs:
                h = (r["t1"] - r["t0"]) - overlap_ms([(r["t0"], r["t1"])], cov)
                if (r["exe"] or "").lower() in stored:
                    hole_other += h
                else:
                    hole_unstorable += h
            line.append(f"C1_holes_s never_stored_proc={hole_unstorable / 1000:.1f} other={hole_other / 1000:.1f} ({100 * hole_other / tot:.2f} %)")
    print("  ".join(line))
    conn.close()
