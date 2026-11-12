"""Interleaved A/B of the unwindowed path (40k candidates): today vs cand vs cand_replay."""
import sys, time
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]
import build_db, ref_today, cand  # noqa: E402
sys.path.insert(0, str(HERE / "f64")); import cand_f64  # noqa: E402

meta = build_db.ensure(); model = str(meta["model"]); conn = build_db.connect()
cands = ref_today.candidates(conn, 0, 2**62)
rng = np.random.default_rng(5)
fns = {"today": ref_today.episode_vector_step, "cand_f64 (first try)": cand_f64.episode_vector_step,
       "cand (final)": cand.episode_vector_step}
t = {k: [] for k in fns}
for n in range(32):
    q = rng.standard_normal(build_db.DIMS).astype(np.float32); q /= np.linalg.norm(q)
    for k in (list(fns) if n % 2 else list(fns)[::-1]):
        a = time.perf_counter_ns(); fns[k](conn, model, q, 0, 2**62, list(cands), limit=100)
        if n: t[k].append((time.perf_counter_ns() - a) / 1e6)
for k, v in t.items():
    print(f"{k:>12}: p50={np.median(v):8.2f} ms  p95={np.percentile(v, 95):8.2f} ms  n={len(v)}")
