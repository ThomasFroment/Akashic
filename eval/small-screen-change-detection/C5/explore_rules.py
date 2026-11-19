"""C5 fix-round helper: exact per-residue evaluation of sparse shape rules.

C2's verdict is translation invariant in the reduced frame and the sweep pastes one reduced patch
per residue (dx mod 4, dy mod 4), so the 16 residues give every distinct mask of the 4096
placements. For each case x monitor x theme x residue x sparse delta, list the clusters
(w, h, n), then count misses / fires per group for candidate rules.
Usage: uv run python explore_rules.py"""

import itertools
import sys
from pathlib import Path

import numpy as np

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
from hcommon import MONITORS, THEMES, today_reduce  # noqa: E402
from scenes import CASES, Sweep  # noqa: E402

DELTAS = (16, 12, 10, 8, 6)
RES = [(dx, dy) for dy in range(4) for dx in range(4)]


def clusters(m):
    h, w = m.shape
    ys, xs = np.divmod(np.flatnonzero(m), w)
    n = ys.size
    if n == 0:
        return []
    adj = (np.abs(ys[:, None] - ys[None, :]) <= 2) & (np.abs(xs[:, None] - xs[None, :]) <= 2)
    lab = np.arange(n)
    while True:
        new = np.where(adj, lab[None, :], n).min(axis=1)
        new = new[new]
        if np.array_equal(new, lab):
            break
        lab = new
    return [(int(np.ptp(xs[lab == lb])) + 1, int(np.ptp(ys[lab == lb])) + 1, int((lab == lb).sum())) for lb in np.unique(lab)]


def collect():
    data = {}  # (mon, theme, case, delta) -> list over residues of cluster lists
    for mon in MONITORS:
        for th in THEMES:
            sw = Sweep(today_reduce, mon, th)
            ref = sw.ref
            for case in CASES:
                for _, _, cur in sw.frames(case, RES):
                    d = np.maximum(ref, cur) - np.minimum(ref, cur)
                    for t in DELTAS:
                        data.setdefault((mon, th, case.name, t), []).append(clusters(d > np.uint8(t)))
    return data


def rule(w2, h3n=5, n2=5):
    def f(cl, mon):
        W2 = w2[mon]
        return any((w >= 3 and h >= 3 and n >= h3n) or (w >= W2 and h >= 2 and n >= n2) for w, h, n in cl)
    return f


def main():
    data = collect()
    cases = {c.name: c for c in CASES}
    rules = {}
    for a, b in itertools.product((3, 4, 5, 6), (3, 4, 5)):
        rules[f"W2 4k={a} 1440p={b}"] = rule({"4k": a, "1440p": b})
    for t in DELTAS:
        print(f"== sparse delta > {t}")
        for name, f in rules.items():
            bad = {}
            for (mon, th, cn, tt), lst in data.items():
                if tt != t:
                    continue
                c = cases[cn]
                g = c.group
                for cl in lst:
                    v = f(cl, mon)
                    if v != c.positive:
                        key = f"{g}@{mon}"
                        bad[key] = bad.get(key, 0) + 1
            print(f"  {name:20} " + " ".join(f"{k}={v}" for k, v in sorted(bad.items()) if "d40" not in k))
    # detail: the residues where words full/d64 have their weakest cluster
    for t in (16, 8):
        print(f"== weakest word clusters at delta > {t} (min over residues of max w among h>=2 clusters, n>=5)")
        for (mon, th, cn, tt), lst in sorted(data.items()):
            if tt != t or not (cn.startswith("word") or cn in ("underscore", "badge")) or cn.endswith("d40"):
                continue
            ws = [max((w for w, h, n in cl if h >= 2 and n >= 5), default=0) for cl in lst]
            hs = [max((h for w, h, n in cl if n >= 5), default=0) for cl in lst]
            print(f"  {mon:5} {th:5} {cn:20} minW(h>=2)={min(ws)} maxW={max(ws)} minH={min(hs)}")


if __name__ == "__main__":
    main()
