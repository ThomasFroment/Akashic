"""Compare full-history read paths for the unwindowed case (index range vs table scan)."""
import sys, time, statistics
sys.path.insert(0, ".")
import build_db
c = build_db.connect()
qs = {
 "index": "SELECT rowid, owner_id, start_ms, vec FROM embeddings WHERE owner_kind='episode' AND model=? AND start_ms>=? AND start_ms<?",
 "notindexed": "SELECT rowid, owner_id, start_ms, vec FROM embeddings NOT INDEXED WHERE owner_kind='episode' AND model=? AND start_ms>=? AND start_ms<?",
}
for name, q in qs.items():
    print(name, [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + q, ("m", 0, 2**62))])
    ts = []
    for _ in range(7):
        t = time.perf_counter(); n = len(c.execute(q, (build_db.MODEL, 0, 2**62)).fetchall()); ts.append((time.perf_counter()-t)*1e3)
    print(name, n, f"p50={statistics.median(ts):.1f} ms min={min(ts):.1f}")
