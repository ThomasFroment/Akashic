"""Fewer-pass variants of the bit-shift f16 -> f32 conversion (one real chunk)."""
import sys, time
from operator import itemgetter
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_db
conn = build_db.connect(build_db.db_path("100k")); meta = build_db.ensure("100k")
cur = conn.execute(build_db.passage_sql("segment"), {"model": meta["model"], "start": 0, "end": 2**62})
for _ in range(3): rows = cur.fetchmany(4000)
vecs = [r[3] for r in rows if len(r[3]) == 512]; m = len(vecs)
blob = b"".join(vecs); u = np.frombuffer(blob, np.int16).reshape(m, 256)
buf = np.empty((4000, 256), np.float32); i32 = buf.view(np.int32)
MASK = np.int32(-0x70000001)
def b(fn, reps=300):
    best = 1e9
    for _ in range(reps):
        t = time.perf_counter(); fn(); best = min(best, time.perf_counter() - t)
    return best / m * 1e9
def t3():
    w = i32[:m]; np.copyto(w, u); np.left_shift(w, 13, out=w); np.bitwise_and(w, MASK, out=w)
def t2():
    w = i32[:m]; np.left_shift(u, 13, out=w, dtype=np.int32); np.bitwise_and(w, MASK, out=w)
def t2b():
    w = i32[:m]; np.multiply(u, np.int32(8192), out=w, dtype=np.int32); np.bitwise_and(w, MASK, out=w)
t3(); r3 = i32[:m].copy(); t2(); ok2 = np.array_equal(r3, i32[:m]); t2b(); ok2b = np.array_equal(r3, i32[:m])
print("3-pass", b(t3)); print("2-pass shift dtype", b(t2), ok2); print("2-pass mult dtype", b(t2b), ok2b)
rows2 = rows
g0, g3 = itemgetter(0), itemgetter(3)
print("zip(*rows)->4 tuples", b(lambda: tuple(zip(*rows2))))
print("2x itemgetter", b(lambda: (np.fromiter(map(g0, rows2), np.int64, len(rows2)), list(map(g3, rows2)))))
