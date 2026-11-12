"""Write-path cost of C5 vs today, beyond the harness's single p99 (dominated by WAL checkpoints).

1. harness.phase_write repeated 3x (same method as the harness) -> spread of add_p50/add_p99.
2. Same stream with wal_autocheckpoint=0 (checkpoint noise removed): add p50/p90/p99/mean.
3. HMAC-SHA256 alone on the stream's texts (per-frame cost of the key).
"""
import hashlib
import hmac
import shutil
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import fixtures  # noqa: E402
import harness  # noqa: E402
import ref_today  # noqa: E402

cand = harness.load(HERE / "cand.py")
work = HERE / "_wbench"
shutil.rmtree(work, ignore_errors=True)
work.mkdir()
lines = []

for i in range(3):
    w = harness.phase_write(cand, work)
    lines.append(f"harness phase_write run {i + 1}: add_p50={w['write_add_p50_ms']:.3f} ms"
                 f" add_p99={w['write_add_p99_ms']:.3f} ms (today p99 {w['write_today_p99_ms']:.2f},"
                 f" cand p99 {w['write_p99_ms']:.2f})")

stream = fixtures.write_stream(10_000)
ctx_ref = harness.open_db(ref_today, work, "nock_ref")
ctx_c = harness.open_db(cand, work, "nock_cand")
for c in (ctx_ref, ctx_c):
    c.conn.execute("PRAGMA wal_autocheckpoint = 0")
tr, tc = [], []
for at in range(0, len(stream), 500):
    for ctx, m, sink in ((ctx_ref, ref_today, tr), (ctx_c, cand, tc)):
        for fr in stream[at : at + 500]:
            t = time.perf_counter_ns()
            m.insert_frame(ctx, dict(fr))
            sink.append(time.perf_counter_ns() - t)
r, c = np.array(tr) / 1e6, np.array(tc) / 1e6
q = lambda a, p: float(np.percentile(a, p))  # noqa: E731
lines.append(
    "no-checkpoint: " + " ".join(f"p{p}: today={q(r, p):.3f} cand={q(c, p):.3f} add={q(c, p) - q(r, p):+.3f} ms" for p in (50, 90, 99))
    + f" mean: today={r.mean():.3f} cand={c.mean():.3f} add={c.mean() - r.mean():+.3f} ms"
)
harness.close(ctx_ref)
harness.close(ctx_c)

key = harness.Ctx.subkey("frame-text-key")
texts = [str(f["text"]).encode() for f in stream]
t = time.perf_counter_ns()
for b in texts:
    hmac.new(key, b, hashlib.sha256).digest()
per = (time.perf_counter_ns() - t) / len(texts) / 1e6
lines.append(f"HMAC-SHA256 alone: {per * 1000:.1f} us/frame (mean text {np.mean([len(b) for b in texts]) / 1000:.1f} KB)")
shutil.rmtree(work, ignore_errors=True)
out = "\n".join(lines)
print(out)
(HERE / "bench_write.txt").write_text(out + "\n", "utf-8")
