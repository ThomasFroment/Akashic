"""Per-stage cost of one 4000-row chunk (rows already fetched), ns per row."""
import sys, time
from itertools import compress
from operator import itemgetter
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / "C1"))
import build_db, cand
conn = build_db.connect(build_db.db_path("100k"))
rows = conn.execute("SELECT owner_id, vec FROM embeddings WHERE owner_kind='segment' LIMIT 4000 OFFSET 8000").fetchall()
n = len(rows); D = 256
allowed = {r[0] for r in rows[::2]}; excluded = {r[0] for r in rows[1::5]}
q = np.random.default_rng(0).standard_normal(D).astype(np.float32)
sc = cand._Scorer(q, exact_query=True)
blobs = list(map(itemgetter(1), rows)); J = b"".join(blobs)
u = np.frombuffer(J, np.int16).reshape(n, D)
i32 = np.empty((n, D), np.int32); t16 = np.empty((n, D), np.int16); f32 = np.empty((n, D), np.float32)
w = cand._rebias(u, i32.copy()); up = q * cand._UP
u64 = np.frombuffer(J, np.uint64)
M64 = np.uint64(0x7C007C007C007C00); A64 = np.uint64(0x0400040004000400); H64 = np.uint64(0x8000800080008000)
t64 = np.empty_like(u64)
def b(fn, reps=300):
    fn(); best = 1e9
    for _ in range(reps):
        t = time.perf_counter(); fn(); best = min(best, time.perf_counter() - t)
    return best / n * 1e9
tests = {
 "blobs list(map)": lambda: list(map(itemgetter(1), rows)),
 "len fromiter ==": lambda: np.fromiter(map(len, blobs), np.intp, n) == 512,
 "ids list(map)": lambda: list(map(itemgetter(0), rows)),
 "allowed fromiter": lambda: np.fromiter(map(allowed.__contains__, map(itemgetter(0), rows)), bool, n),
 "compress half": lambda: list(compress(blobs, [True, False] * (n // 2))),
 "join": lambda: b"".join(blobs),
 "frombuffer": lambda: np.frombuffer(J, np.int16).reshape(n, D),
 "exp check i16 and+max": lambda: np.bitwise_and(u, cand._EXP, out=t16).max(),
 "exp check u64 swar": lambda: (np.bitwise_and(u64, M64, out=t64), np.add(t64, A64, out=t64), np.bitwise_and(t64, H64, out=t64), t64.any()),
 "exp check u64 max-only?": lambda: u64.max(),
 "rebias prealloc": lambda: cand._rebias(u, i32),
 "copyto i16->i32 only": lambda: np.copyto(i32, u),
 "astype f16->f32 prealloc": lambda: np.copyto(f32, u.view(np.float16)),
 "gemv": lambda: w @ up,
 "scorer.scores total": lambda: sc.scores(J, n),
}
tot = 0
for k, fn in tests.items():
    v = b(fn); print(f"{k:28s} {v:7.1f} ns/row")
