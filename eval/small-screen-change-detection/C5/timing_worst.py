"""C5 worst-case sparse-path cost on 4K reduced frames (populated state), vs today's compare.
Usage: uv run python timing_worst.py"""
import statistics
import sys
import time
from pathlib import Path

import numpy as np

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import baseline  # noqa: E402
import c5  # noqa: E402
from hcommon import today_reduce  # noqa: E402
from scenes import page  # noqa: E402

ref = today_reduce(page("4k", "dark"))
H, W = ref.shape
rng = np.random.default_rng(5)


def frame(points, amp):
    cur = ref.copy()
    ys, xs = np.divmod(points, W)
    cur[ys, xs] = np.clip(ref[ys, xs].astype(int) + amp, 0, 255).astype(np.uint8)
    return cur


cases = {}
# 128 faint points (delta 6..16, none > 16) scattered: cap path, 400-point clustering
cases["128 faint scattered"] = frame(rng.choice(H * W, 128, replace=False), 10)
# 128 faint points in a 2-px-spaced chain (one long component: worst label propagation)
chain = np.array([(300 + (i // 64) * 2) * W + 100 + (i % 64) * 2 for i in range(128)])
cases["128 faint chain"] = frame(chain, 10)
# 51 strong points (gate edge) in 10 blobs (10 positive clusters, live gathers, hashes)
blobs = np.concatenate([(100 + 40 * k + np.repeat(np.arange(2), 3)[:5]) * W + 500 + np.tile(np.arange(3), 2)[:5] for k in range(10)])
cases["51 strong in 10 clusters"] = frame(np.unique(blobs)[:51], 120)
cases["word region 4x6 + clock-like live"] = frame(np.array([(200 + y) * W + 600 + x for y in range(5) for x in range(8)]), 120)


def bench(f, runs=31):
    f()
    ts = []
    for _ in range(runs):
        t = time.perf_counter_ns()
        f()
        ts.append(time.perf_counter_ns() - t)
    return statistics.median(ts) / 1e6


for name, cur in cases.items():
    # populated state: previous check differs everywhere the current change is, recency map
    # marked around it at the previous check, records present
    def run_c5():
        st = c5.make_state()
        st.last = np.zeros_like(ref, dtype=np.int32)
        st.last[cur != ref] = st.check  # moved at the previous check -> live gathers
        st.check += 1
        return st

    states = [run_c5() for _ in range(40)]
    it = iter(states)
    t_c = bench(lambda: c5.decide(next(it), ref, cur, ref, "4k"))
    t_b = bench(lambda: baseline.decide(None, ref, cur, ref, "4k"))
    print(f"{name:34} c5 {t_c:.3f} ms  today {t_b:.3f} ms  extra {t_c - t_b:+.3f} ms")
# above the cap: 400 faint points fall back to the > 16 points (none here)
cur = frame(chain_400 := np.array([(300 + (i // 200) * 2) * W + 100 + (i % 200) * 2 for i in range(400)]), 10)
st = [c5.make_state() for _ in range(40)]
it2 = iter(st)
print(f"{'400 faint chain (over cap)':34} c5 {bench(lambda: c5.decide(next(it2), ref, cur, ref, '4k')):.3f} ms  today {bench(lambda: baseline.decide(None, ref, cur, ref, '4k')):.3f} ms")
