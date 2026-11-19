"""Diagnose C2 misses / fires on sweep cases: per placement, best cluster (w, h, n) at several deltas."""
import sys
from pathlib import Path
import numpy as np
H = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(H))
from hcommon import today_reduce  # noqa: E402
from scenes import CASE_BY_NAME, QUICK_PLACEMENTS, Sweep  # noqa: E402


def comps(xs, ys, link=2):
    n = xs.size
    if n == 0:
        return []
    adj = (np.abs(xs[:, None] - xs[None, :]) <= link) & (np.abs(ys[:, None] - ys[None, :]) <= link)
    r = adj.copy()
    while True:
        nx = (r.astype(np.float32) @ r.astype(np.float32)) > 0
        if (nx == r).all():
            break
        r = nx
    out = set()
    for i in range(n):
        m = r[i]
        out.add((int(xs[m].max() - xs[m].min() + 1), int(ys[m].max() - ys[m].min() + 1), int(m.sum())))
    return sorted(out, key=lambda t: -t[2])


def main():
    mon, theme = sys.argv[1], sys.argv[2]
    names = sys.argv[3:]
    sw = Sweep(today_reduce, mon, theme)
    ref = sw.ref.astype(np.int16)
    for name in names:
        stats = {}
        for dx, dy, cur in sw.frames(CASE_BY_NAME[name], QUICK_PLACEMENTS):
            d = np.abs(cur.astype(np.int16) - ref)
            row = []
            for t in (16, 12, 8):
                ys, xs = np.nonzero(d > t)
                c = comps(xs, ys)
                row.append((t, len(xs), c[:2]))
            stats.setdefault(str(row[0][1:]), []).append((dx, dy, row))
        print("==", name)
        for k, v in list(stats.items())[:12]:
            print(len(v), v[0][2])
main()
