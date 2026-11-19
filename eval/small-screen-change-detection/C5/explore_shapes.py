"""C5 fix-round helper: spatial cluster stats of every sweep case under several sparse mask
thresholds, to pick a shape rule that keeps words (full, d64) and badges and drops carets.
Reads the harness (import only). Usage: uv run python explore_shapes.py [--quick]"""

import sys
from pathlib import Path

import numpy as np

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
from hcommon import MONITORS, THEMES, today_reduce  # noqa: E402
from scenes import CASES, FULL_PLACEMENTS, QUICK_PLACEMENTS, Sweep  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import c5  # noqa: E402

THRESH = (16, 8, 6, 4)


def clusters(m):
    h, w = m.shape
    ys, xs = np.divmod(np.flatnonzero(m), w)
    n = ys.size
    if n == 0:
        return []
    if n > 400:
        return [(99, 99, n)]
    adj = (np.abs(ys[:, None] - ys[None, :]) <= 2) & (np.abs(xs[:, None] - xs[None, :]) <= 2)
    lab = np.arange(n)
    while True:
        new = np.where(adj, lab[None, :], n).min(axis=1)
        new = new[new]
        if np.array_equal(new, lab):
            break
        lab = new
    out = []
    for lb in np.unique(lab):
        s = lab == lb
        out.append((int(np.ptp(xs[s])) + 1, int(np.ptp(ys[s])) + 1, int(s.sum())))
    return out


def main():
    quick = "--quick" in sys.argv
    pl = QUICK_PLACEMENTS if quick else FULL_PLACEMENTS[::7]
    for mon in MONITORS:
        for th in THEMES:
            sw = Sweep(today_reduce, mon, th)
            ref = sw.ref
            for case in CASES:
                if case.group in ("word_d40",):
                    continue
                stats = {t: [] for t in THRESH}
                for _, _, cur in sw.frames(case, pl):
                    for t in THRESH:
                        m = (np.maximum(ref, cur) - np.minimum(ref, cur)) > np.uint8(t)
                        cl = clusters(m)
                        # best (w, h) over clusters with n >= 5: max h then max w
                        best = max(((h, w, n) for w, h, n in cl if n >= 5), default=(0, 0, 0))
                        stats[t].append(best)
                line = [f"{mon:5} {th:5} {case.name:22}"]
                for t in THRESH:
                    a = np.array(stats[t])
                    line.append(f"t{t}: h[{a[:, 0].min()}-{a[:, 0].max()}] w|h=2[{a[a[:, 0] == 2][:, 1].min() if (a[:, 0] == 2).any() else '-'}"
                                f"-{a[a[:, 0] == 2][:, 1].max() if (a[:, 0] == 2).any() else '-'}] h<=1:{int((a[:, 0] <= 1).sum())}")
                print("  ".join(line), flush=True)


if __name__ == "__main__":
    main()
