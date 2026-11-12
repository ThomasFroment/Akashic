import sys, time, io
from operator import itemgetter
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "C1"))
import build_db, cand
conn = build_db.connect(build_db.db_path("100k"))
rows = conn.execute("SELECT owner_id, vec FROM embeddings WHERE owner_kind='segment' LIMIT 4000 OFFSET 8000").fetchall()
n = len(rows); D = 256
blobs = list(map(itemgetter(1), rows)); J = b"".join(blobs)
u = np.frombuffer(J, np.int16).reshape(n, D)
i32 = np.empty((n, D), np.int32)
bio = io.BytesIO(); bio.write(J)
def wl():
    bio.seek(0); bio.writelines(blobs); return bio.getbuffer()
def b(fn, reps=300):
    r = fn(); del r; best = 1e9
    for _ in range(reps):
        t = time.perf_counter(); r = fn(); best = min(best, time.perf_counter() - t); del r
    return best / n * 1e9
tests = {
 "join": lambda: b"".join(blobs),
 "bytesio writelines": wl,
 "lens list.count": lambda: list(map(len, blobs)).count(512) == n,
 "fused shift (dtype=int32)": lambda: np.left_shift(u, 13, out=i32, dtype=np.int32),
 "and inplace": lambda: np.bitwise_and(i32, cand._MASK, out=i32),
 "copyto": lambda: np.copyto(i32, u),
 "shift inplace": lambda: np.left_shift(i32, 13, out=i32),
 "check 2 max": lambda: u.max() >= 0x7C00 or u.view(np.uint16).max() >= 0xFC00,
 "check u max": lambda: u.max(),
}
for k, fn in tests.items():
    print(f"{k:28s} {b(fn):7.1f} ns/row")
