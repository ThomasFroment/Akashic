"""Cold (fresh connection, like the harness) timing for straddle windows by rows scanned."""
import sys, time
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "C1"))
import build_db, ref_today, cand
meta = build_db.ensure(); conn = build_db.connect(); model = meta["model"]; DAY = build_db.DAY_MS
q = np.random.default_rng(3).standard_normal(build_db.DIMS).astype(np.float32); q /= np.linalg.norm(q)
for span in meta["forced_60day_spans"][:2]:
    for k in (1, 30, 59):
        s = (span[0] // DAY + k) * DAY; e = s + DAY; c = ref_today.candidates(conn, s, e)
        lo, hi = min(x for _, x in c), max(x for _, x in c) + 1
        n = conn.execute("SELECT count(*) FROM embeddings WHERE owner_kind='episode' AND model=? AND start_ms>=? AND start_ms<?", (model, lo, hi)).fetchone()[0]
        cold = []
        for _ in range(5):
            f = build_db.connect(); a = time.perf_counter_ns(); cand.episode_vector_step(f, model, q, s, e, list(c)); cold.append((time.perf_counter_ns() - a) / 1e6); f.close()
        print(f"k={k:>2} scanned={n:>5} cold_p50={np.median(cold):6.2f} ms  us/row={np.median(cold)*1e3/n:.2f}")
