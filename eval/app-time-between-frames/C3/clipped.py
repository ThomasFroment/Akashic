"""Clipped M5.3 and robustness seeds for C3 (numbers only; score.py untouched).

Per truth run r (observer foreground runs, foreign excluded, as score.m5):
  clipped      = sum_r |same-app band time inside [r.t0 - 5 s, r.t1 + 5 s] - dur(r)| / sum_r dur(r)
  clipped_gap  = the same with dur(r) minus the stored all/screen gap time inside r (Akashic records
                 nothing in a pause; the scorer's M5.3 still counts that time as app time)
Also the scorer's own M5.1 % and M5.3 (unclipped longest band) via score.m5, as run.py does, for
configurations run.py was not given (drop seeds), and how many fallback edges fired.

    uv run python eval/app-time-between-frames/C3/clipped.py  -> C3/clipped.json
"""

import importlib.util
import json
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
from akashic.pipeline import sessions  # noqa: E402

PAD = 5_000
base = core.load(HERE.parent / "baseline" / "baseline.py").bands
refc = core.load(HERE.parent / "explored" / "ref_confirmed.py").bands


def configs():
    yield "baseline", base, None
    yield "ref_confirmed", refc, None
    for name, kw in (("c3", {}), ("c3_nosub", {"subedges": False}), ("c3_delay", {"delay": True}),
                     ("c3_delay_nosub", {"delay": True, "subedges": False}), ("c3_poll", {"poll": True}),
                     ("c3_poll_nosub", {"poll": True, "subedges": False}), ("c3_poll_drop10", {"poll": True, "drop": 0.1})):
        st = {}
        yield name, c3.build(stats=st, **kw), st
    for seed in range(1, 6):
        for suf, kw in (("", {}), ("_noec", {"endcheck": False}), ("_nofb", {"fallback": False})):
            st = {}
            yield f"c3_drop10_s{seed}{suf}", c3.build(drop=0.1, seed=seed, stats=st, **kw), st


def run_one(rid):
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    now = int(time.time() * 1000)
    rows, _ = hrun.sim_fg_rows(tr, conn, now)
    core.install_sim(conn, rows, [])
    runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
    gaps = core.merge([(g.start_ms, g.end_ms) for g in hrun.score_overlapping(conn, tr.t0, tr.t1, now, ("all", "screen"))])
    out = {}
    for name, fn, st in configs():
        if st is not None:
            st.clear()
        bands = fn(conn, tr.t0, tr.t1, now, core.Ctx(conn, tr.t0, tr.t1, now))
        fb = None if st is None else f"{st.get('fallback_edges', 0)}+{st.get('endcheck_edges', 0)}q{st.get('lookups', 0)}"
        e1 = e2 = tot = tot2 = 0
        for r in runs:
            exe = (r["exe"] or "").lower()
            same = [(x.start_ms, x.end_ms) for x in bands if (x.process or "").lower() == exe]
            got = core.overlap_ms(same, [(r["t0"] - PAD, r["t1"] + PAD)])
            dur = r["t1"] - r["t0"]
            dur2 = dur - core.overlap_ms([(r["t0"], r["t1"])], gaps)
            e1 += abs(got - dur)
            e2 += abs(got - dur2)
            tot += dur
            tot2 += dur2
        sessions.app_bands = hrun.as_app_bands(fn, now, core.Usage())
        try:
            m = score.m5(tr, conn, now)
        finally:
            sessions.app_bands = core.ORIGINAL_APP_BANDS
        out[name] = {"M5.1_pct": round(100 * m["M5.1"]["seconds"] / m["M5.1"]["truth_seconds"], 2),
                     "M5.3": round(m["M5.3"]["total_abs_error_ratio"], 3),
                     "clipped": round(e1 / tot, 3), "clipped_gap": round(e2 / tot2, 3), "fallback_edges": fb}
    conn.close()
    return out


def main():
    res = {}
    for g, ids in hrun.RUNS.items():
        for rid in ids:
            res[rid] = run_one(rid)
    (HERE / "clipped.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    names = list(next(iter(res.values())))
    short = [r[-4:] for r in res]
    print("config".ljust(22), "  ".join(f"{s}:M5.1%/M5.3/clip/clipgap/fb".ljust(30) for s in short))
    for n in names:
        cells = []
        for rid in res:
            v = res[rid][n]
            cells.append(f"{v['M5.1_pct']:g}/{v['M5.3']:.3f}/{v['clipped']:.3f}/{v['clipped_gap']:.3f}/{v['fallback_edges']}".ljust(30))
        print(n.ljust(22), "  ".join(cells))


if __name__ == "__main__":
    main()
