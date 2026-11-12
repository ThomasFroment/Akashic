"""Open-long-run fixture for C2: a realistic 10k-visit prefix (G=500 games over the prefix)
followed by one chained work run of 40k visits. Horizon "open": the long run's end + GAP is
past the horizon, so C2 folds 40k visits into accumulators and then discards them. Horizon
"closed": the same rows with the run closed and emitted.

Also times split_only(): the same game filter / run split / horizon stop without any
accumulation, which bounds what the folding costs. Equivalence against ref_today is checked
on the same shape with a 10k-visit long run (today's run split is quadratic in run length).

    ..\\.venv\\Scripts\\python.exe C2\\bench_open_run.py     (from app-run-single-pass)
"""

from __future__ import annotations

import gc
import math
import pathlib
import statistics
import sys
import time
from bisect import bisect_right

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import fixtures  # noqa: E402
import harness  # noqa: E402

GAP = fixtures.GAP


def shape(long_n, prefix_v=10_000, G=500):
    rows, games, _ = fixtures.realistic(1, prefix_v, G)
    t = max(r[5] for r in rows) + 2 * fixtures.HOUR
    nid = max(r[0] for r in rows) + 1
    procs = ("code.exe", "code.exe", "windowsterminal.exe", "notepad.exe")
    for i in range(long_n):
        rows.append((nid + i, procs[i % 4], f"doc {i % 50}" if i % 9 else None, None, t, t + 900))
        t += 1_000
    end = rows[-1][5]
    return rows, games, end + GAP - 1, end + 10 * GAP  # open horizon, closed horizon


def split_only(rows, games, horizon, add_episode):
    """C2's loop without accumulators: the floor for any fused design on this input."""
    gs = sorted(games, key=lambda g: g[1])
    ends = [g[1] for g in gs] + [math.inf]
    G = len(gs)
    suf = [math.inf] * (G + 1)
    m = math.inf
    for k in range(G - 1, -1, -1):
        m = min(m, gs[k][0])
        suf[k] = m
    j, prv, nxt, lim = 0, -math.inf, ends[0], suf[0]
    open_, rb, rend, n = False, False, 0, 0
    for r in rows:
        _, p, t, u, s, e = r
        if s >= nxt or s < prv:
            j = bisect_right(ends, s, 0, G)
            prv, nxt, lim = (ends[j - 1] if j else -math.inf), ends[j], suf[j]
        if lim < e:
            continue
        b = p in fixtures.BROWSERS
        if open_ and b == rb and s - rend <= GAP:
            if e > rend:
                rend = e
        else:
            if open_ and rend + GAP > horizon:
                return n
            open_, rb, rend = True, b, e
            n += 1
    return n


def timeit(fn, rows, games, horizon, reps=15):
    harness._time_once(fn, rows, games, horizon)
    ts = [harness._time_once(fn, rows, games, horizon) for _ in range(reps)]
    return min(ts), statistics.median(ts)


def main():
    ref = harness.load(HERE.parent / "ref_today.py", "ref_today_b")
    cand = harness.load(HERE / "cand.py", "cand_b")
    rows, games, h_open, h_closed = shape(10_000)
    ok = all(
        harness.outcome(ref.build_app_runs, rows, games, h)
        == harness.outcome(cand.build_app_runs, rows, games, h)
        for h in (h_open, h_closed)
    )
    print(f"equivalence vs ref_today (10k prefix + 10k long run, open and closed): {ok}")
    rows, games, h_open, h_closed = shape(40_000)
    print(f"rows={len(rows)} games={len(games)} long run=40000 visits")
    out = []
    for label, fn, h in (
        ("C2 open (long run folded then discarded)", cand.build_app_runs, h_open),
        ("C2 closed (long run emitted)", cand.build_app_runs, h_closed),
        ("split_only open (no accumulation)", split_only, h_open),
        ("split_only closed (no accumulation)", split_only, h_closed),
    ):
        lo, med = timeit(fn, rows, games, h)
        out.append(f"{label}: p50={med:.2f} ms min={lo:.2f} ms")
        print(out[-1])
    gc.collect()
    return ok, out


if __name__ == "__main__":
    main()
