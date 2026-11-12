"""Fit K for C3: linear cost models of probe(k) and scan(k) on the cached 40k DB (read-only).

Usage (from windowed-episode-vector-scan/):  ..\\.venv\\Scripts\\python.exe C3\\fit_k.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import build_db  # noqa: E402
import cand  # noqa: E402

FIT_K = [10, 100, 1_000, 2_000, 5_000]
CHECK_K = [10_000, 20_000, 30_000, 39_000]
RUNS = 7


def med(fn) -> float:
    fn()
    ts = []
    for _ in range(RUNS):
        t = time.perf_counter_ns()
        fn()
        ts.append((time.perf_counter_ns() - t) / 1e6)
    return float(np.median(ts))


def main() -> None:
    meta = build_db.ensure()
    model = str(meta["model"])
    conn = build_db.connect()
    eps = conn.execute("SELECT id FROM episodes ORDER BY start_ms DESC, id DESC").fetchall()
    ids = [int(r[0]) for r in eps]
    rng = np.random.default_rng(3)
    q = rng.standard_normal(build_db.DIMS).astype(np.float32)
    q /= np.linalg.norm(q)
    print("plan:", [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + cand.PROBE_SQL,
                                               (model, "[1,2]"))])  # fmt: skip
    rows = []
    for k in FIT_K + CHECK_K:
        off = int(rng.integers(0, len(ids) - k + 1))
        contig = ids[off : off + k]  # a time window: newest-first slice
        rand = [int(x) for x in rng.choice(ids, k, replace=False)]
        tp_c = med(lambda c=contig: cand.probe(conn, model, q, c))
        tp_r = med(lambda r=rand: cand.probe(conn, model, q, r))
        ts = med(lambda c=set(contig): cand.scan(conn, model, q, c))
        rows.append((k, tp_c, tp_r, ts))
        print(f"k={k:>6} probe(window)={tp_c:9.3f} ms probe(random)={tp_r:9.3f} ms "
              f"scan={ts:9.3f} ms", flush=True)  # fmt: skip
    a = np.array([r for r in rows if r[0] in FIT_K], dtype=np.float64)
    pw = np.polyfit(a[:, 0], a[:, 1], 1)
    pr = np.polyfit(a[:, 0], a[:, 2], 1)
    ps = np.polyfit(a[:, 0], a[:, 3], 1)
    print(f"probe(window) = {pw[1]:.3f} ms + {pw[0] * 1e3:.3f} us * k")
    print(f"probe(random) = {pr[1]:.3f} ms + {pr[0] * 1e3:.3f} us * k")
    print(f"scan          = {ps[1]:.3f} ms + {ps[0] * 1e3:.3f} us * k")
    for lbl, p in (("window", pw), ("random", pr)):
        kx = (ps[1] - p[1]) / (p[0] - ps[0])
        print(f"crossover K ({lbl} ids) = {kx:,.0f}")
    conn.close()


if __name__ == "__main__":
    main()
