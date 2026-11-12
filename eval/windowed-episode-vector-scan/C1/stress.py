"""Rows scanned per scenario and a straddle sweep (1-day windows at day k of each 60-day episode)."""
import sys, time, json
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "C1"))
import build_db, ref_today, cand
meta = build_db.ensure(); conn = build_db.connect(); model = meta["model"]; DAY = build_db.DAY_MS
q = np.random.default_rng(3).standard_normal(build_db.DIMS).astype(np.float32); q /= np.linalg.norm(q)
def scanned(c):
    lo, hi = min(s for _, s in c), max(s for _, s in c) + 1
    return conn.execute("SELECT count(*) FROM embeddings WHERE owner_kind='episode' AND model=? AND start_ms>=? AND start_ms<?", (model, lo, hi)).fetchone()[0]
def t(s, e, c):
    cand.episode_vector_step(conn, model, q, s, e, list(c))
    w = []
    for _ in range(21):
        a = time.perf_counter_ns(); cand.episode_vector_step(conn, model, q, s, e, list(c)); w.append((time.perf_counter_ns() - a) / 1e6)
    return float(np.median(w)), float(np.percentile(w, 95))
d = json.loads((HERE / "C1" / "details.json").read_text())
for sc in ("day", "month", "straddle"):
    s, e = d["scenarios"][sc]["window"]; c = ref_today.candidates(conn, s, e)
    print(f"{sc}: candidates={len(c)} rows_scanned={scanned(c)}")
print("straddle sweep: span, day k, candidates, rows scanned, extra rows, p50 ms, p95 ms")
for span in meta["forced_60day_spans"]:
    for k in (1, 10, 30, 45, 59):
        s = (span[0] // DAY + k) * DAY; e = s + DAY
        if s >= span[1]: continue
        c = ref_today.candidates(conn, s, e); n = scanned(c); p50, p95 = t(s, e, c)
        print(f"  {span[0]} k={k:>2} cands={len(c):>3} scanned={n:>5} extra={n-len(c):>5} p50={p50:6.2f} p95={p95:6.2f}")
