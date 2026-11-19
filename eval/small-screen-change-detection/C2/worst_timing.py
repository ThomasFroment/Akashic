"""Worst-case sparse-path timing (not in run.py): <= 51 scattered moved reduced pixels on a 4K
reduced frame, each its own component (hysteresis window per component), vs the baseline."""
import importlib.util
import statistics
import sys
import time
from pathlib import Path
import numpy as np
H = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(H))
from hcommon import readonly, today_reduce  # noqa: E402
from scenes import page  # noqa: E402


def load(p):
    s = importlib.util.spec_from_file_location(Path(p).stem, p)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


c2, base = load(Path(__file__).with_name("c2.py")), load(H / "baseline.py")
ref = today_reduce(page("4k", "dark"))
rng = np.random.default_rng(3)
cases = {}
for n, spacing, sub in ((51, 0, 0), (51, 0, 60), (25, 1, 0), (17, 2, 0)):
    cur = ref.copy()
    # n groups of (spacing+1) adjacent pixels, far apart, plus `sub` pixels moved by 9-16 near each
    ys = rng.choice(np.arange(10, 520, 8), n, replace=False)
    xs = rng.choice(np.arange(10, 950, 12), n, replace=False)
    for x, y in zip(xs, ys):
        for k in range(spacing + 1):
            cur[y, x + k] = 255 if ref[y, x + k] < 128 else 0
        if sub:
            cur[y - 1:y + 2, x - 1:x + 2] = np.clip(ref[y - 1:y + 2, x - 1:x + 2].astype(int) + 12, 0, 255)
            cur[y, x] = 255 if ref[y, x] < 128 else 0
    cases[f"pts{n}x{spacing + 1}{'+low' if sub else ''}"] = readonly(cur)
r = readonly(ref)
for name, cur in cases.items():
    tb, tc = [], []
    for f, ts in ((base.decide, tb), (c2.decide, tc)):
        f(None, r, cur, r, "4k")
    for i in range(31):
        for f, ts in ((base.decide, tb), (c2.decide, tc)) if i % 2 == 0 else ((c2.decide, tc), (base.decide, tb)):
            t = time.perf_counter_ns()
            f(None, r, cur, r, "4k")
            ts.append(time.perf_counter_ns() - t)
    mb, mc = statistics.median(tb) / 1e6, statistics.median(tc) / 1e6
    print(f"{name}: N16={int(((np.maximum(r, cur) - np.minimum(r, cur)) > 16).sum())} base {mb:.3f} ms c2 {mc:.3f} ms extra {mc - mb:+.3f} ms fire={c2.decide(None, r, cur, r, '4k')}")
