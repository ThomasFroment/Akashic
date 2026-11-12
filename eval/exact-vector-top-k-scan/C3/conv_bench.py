"""f16 -> f32 conversion variants on one real 4,000-row chunk: cost and score bit-identity."""
import sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_db
conn = build_db.connect(build_db.db_path("100k")); meta = build_db.ensure("100k")
cur = conn.execute(build_db.passage_sql("segment"), {"model": meta["model"], "start": 0, "end": 2**62})
for _ in range(3): rows = cur.fetchmany(4000)
vecs = [r[3] for r in rows if len(r[3]) == 512]; m = len(vecs)
blob = b"".join(vecs); f16 = np.frombuffer(blob, np.float16).reshape(m, 256); u = f16.view(np.int16)
rng = np.random.default_rng(1); q = rng.standard_normal(256).astype(np.float32); q /= np.linalg.norm(q)
buf = np.empty((4000, 256), np.float32); i32 = buf.view(np.int32)
lut = np.arange(65536, dtype=np.uint32).astype(np.uint16).view(np.float16).astype(np.float32)
MASK = np.int32(-0x70000001); qs = (q * np.float32(2.0**112)).astype(np.float32)
def b(fn, reps=300):
    best = 1e9
    for _ in range(reps):
        t = time.perf_counter(); fn(); best = min(best, time.perf_counter() - t)
    return best / m * 1e9
def trick():
    w = i32[:m]; np.copyto(w, u); np.left_shift(w, 13, out=w); np.bitwise_and(w, MASK, out=w)
uu = f16.view(np.uint16)
print("copyto  ", b(lambda: np.copyto(buf[:m], f16)))
print("trick   ", b(trick))
print("lut take", b(lambda: np.take(lut, uu, out=buf[:m])))
print("lut idx ", b(lambda: lut[uu]))
print("nonfinite check", b(lambda: (uu & 0x7C00).max()))
ref = f16.astype(np.float32) @ q
trick(); s2 = buf[:m] @ qs
print("trick bit-identical:", np.array_equal(ref.view(np.int32), s2.view(np.int32)))
s16 = f16.view(np.int16); us = f16.view(np.uint16)
print("nonfinite 2x max int16", b(lambda: (s16.max() >= 0x7C00) or (us.max() >= 0xFC00)))
print("trick + check", b(lambda: (trick(), s16.max(), us.max())))
# full scan of the 100k table: bit identity over every chunk, trick vs astype
cur = conn.execute(build_db.passage_sql("segment"), {"model": meta["model"], "start": 0, "end": 2**62})
bad = tot = 0
while rows := cur.fetchmany(4000):
    v = [r[3] for r in rows if len(r[3]) == 512]; k = len(v); bl = b"".join(v)
    h = np.frombuffer(bl, np.float16).reshape(k, 256)
    ref = h.astype(np.float32) @ q
    w = i32[:k]; np.copyto(w, h.view(np.int16)); np.left_shift(w, 13, out=w); np.bitwise_and(w, MASK, out=w)
    s2 = buf[:k] @ qs
    bad += int((ref.view(np.int32) != s2.view(np.int32)).sum()); tot += k
print("full-scan trick mismatching scores:", bad, "of", tot)
