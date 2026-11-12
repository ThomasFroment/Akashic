"""Per-stage cost of the C3 chunk pipeline on real fetched chunks (100k DB), ns per row."""

import sys
import time
from itertools import compress
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_db  # noqa: E402

conn = build_db.connect(build_db.db_path("100k"))
meta = build_db.ensure("100k")
model = meta["model"]
sql = build_db.passage_sql("segment")
chunks = []
cur = conn.execute(sql, {"model": model, "start": 0, "end": 2**62})
while rows := cur.fetchmany(4000):
    chunks.append(rows)
    if len(chunks) == 5:
        break
rows = chunks[2]
m = len(rows)
q = np.ones(256, np.float32) / 16
buf = np.empty((4000, 256), np.float32)


def b(fn, reps=200):
    best = 1e9
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best / m * 1e9


owners, sa, ea, vecs = zip(*rows)
blob = b"".join(vecs)
f16 = np.frombuffer(blob, np.float16).reshape(m, -1)
allowed = set(list(owners)[::2])
res = {
    "zip(*rows)": b(lambda: zip(*rows)),
    "zip(*rows) materialized": b(lambda: tuple(zip(*rows))),
    "join": b(lambda: b"".join(vecs)),
    "max(map(len))": b(lambda: max(map(len, vecs))),
    "fromiter owners": b(lambda: np.fromiter(owners, np.int64, m)),
    "np.array owners": b(lambda: np.array(owners, np.int64)),
    "allowed mask": b(lambda: np.fromiter(map(allowed.__contains__, owners), np.bool_, m)),
    "copyto f16->f32": b(lambda: np.copyto(buf[:m], f16)),
    "astype f32": b(lambda: f16.astype(np.float32)),
    "gemv": b(lambda: buf[:m] @ q),
    "compress+join half": b(lambda: b"".join(compress(vecs, [True, False] * (m // 2)))),
}
for k, v in res.items():
    print(f"{k:28s} {v:8.1f} ns/row")
