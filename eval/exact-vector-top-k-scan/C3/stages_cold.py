"""Stage costs over all prefetched chunks of the unwindowed embeddings/passages scans (cold data)."""
import sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_db
conn = build_db.connect(build_db.db_path("100k")); meta = build_db.ensure("100k")
q = np.ones(256, np.float32) / 16
buf = np.empty((4000, 256), np.float32)
for name, sql, args in (("emb", build_db.EMB_SQL, ("segment", meta["model"], 0, 2**62)),
                        ("pas", build_db.passage_sql("segment"), {"model": meta["model"], "start": 0, "end": 2**62})):
    cur = conn.execute(sql, args); chunks = []
    while rows := cur.fetchmany(4000): chunks.append(rows)
    n = sum(map(len, chunks))
    acc = dict.fromkeys(["zip", "join", "maxlen", "copyto", "gemv", "total"], 0.0)
    for rep in range(3):
        for rows in chunks:
            t0 = time.perf_counter(); cols = tuple(zip(*rows)); vecs = cols[-1]; t1 = time.perf_counter()
            blob = b"".join(vecs); t2 = time.perf_counter()
            ok = max(map(len, vecs)) == 512; t3 = time.perf_counter()
            if len(blob) % 512: continue
            k = len(blob) // 512
            np.copyto(buf[:k], np.frombuffer(blob, np.float16).reshape(k, -1)); t4 = time.perf_counter()
            s = buf[:k] @ q; t5 = time.perf_counter()
            for key, d in zip(("zip", "join", "maxlen", "copyto", "gemv"), (t1-t0, t2-t1, t3-t2, t4-t3, t5-t4)):
                acc[key] += d
    print(name, {k: round(v / (3 * n) * 1e9, 1) for k, v in acc.items()})
