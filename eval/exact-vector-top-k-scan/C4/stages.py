import sys, time, numpy as np
from operator import itemgetter
from itertools import compress
sys.path.insert(0, ".")
import build_db
conn = build_db.connect(build_db.db_path("100k"))
meta = build_db.ensure("100k")
model = meta["model"]
chunks = []
cur = conn.execute("SELECT owner_id, vec FROM embeddings WHERE owner_kind='segment' AND model=? AND start_ms>=0 AND start_ms<?", (model, 2**62))
while r := cur.fetchmany(4000): chunks.append(r)
N = sum(map(len, chunks)); D = 256
q = np.random.default_rng(0).standard_normal(D).astype(np.float32)
buf = np.empty((4000, D), np.float32)
g0, g1 = itemgetter(0), itemgetter(1)
def T(name, f, reps=15):
    ts = []
    for _ in range(reps):
        t = time.perf_counter(); f(); ts.append(time.perf_counter() - t)
    print(f"{name:40s} {np.median(ts)/N*1e9:7.1f} ns/row")
T("list(map(get_vec))", lambda: [list(map(g1, c)) for c in chunks])
bl = [[b for b in map(g1, c) if len(b)==2*D] for c in chunks]
T("len fromiter", lambda: [np.fromiter(map(len, b), np.intp, len(b)) for b in bl])
T("join", lambda: [b"".join(b) for b in bl])
js = [b"".join(b) for b in bl]
T("frombuffer", lambda: [np.frombuffer(j, np.float16) for j in js])
hs = [np.frombuffer(j[: (len(j)//(2*D))*2*D], np.float16).reshape(-1, D) for j in js]
def cp():
    for h in hs: np.copyto(buf[:len(h)], h)
T("copyto f16->f32", cp)
def cp2():
    for h in hs: h.astype(np.float32)
T("astype f16->f32 (alloc)", cp2)
def mv():
    for h in hs: buf[:len(h)] @ q
T("gemv", mv)
T("ids fromiter", lambda: [np.fromiter(map(g0, c), np.int64, len(c)) for c in chunks])
T("ids list(map)", lambda: [list(map(g0, c)) for c in chunks])

def sub(k):
    def f():
        for b in bl:
            for a in range(0, len(b), k):
                h = np.frombuffer(b"".join(b[a:a+k]), np.float16).reshape(-1, D)
                np.copyto(buf[a:a+len(h)], h)
    return f
for k in (4000, 2000, 1000, 500, 250, 128):
    T(f"join+frombuffer+copyto sub={k}", sub(k))

