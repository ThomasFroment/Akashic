"""C5 refuter conditions, measured on extra synthetic 4K check streams (5 s, one hour):
  minclock  the taskbar minute clock at 16 sub-pixel residues (x, y mod 4) and 4 x positions,
            stores/h with and without the taskbar band (FIX4);
  nohold    drafts of 2-8 chars typed one char per check and sent (cleared) at the next check,
            never holding still: is the complete draft stored, and how many chars at most?
Usage: uv run python extra_streams.py"""

import sys
from pathlib import Path

import numpy as np

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
from hcommon import MONITORS, THEMES, Candidate, readonly, today_reduce  # noqa: E402
from scenes import N_CHECKS, Scene, _hms, dev, now_at, page, text_item  # noqa: E402

HERE = Path(__file__).resolve().parent
SCALE = MONITORS["4k"][2]
FG = THEMES["dark"][1]


def simulate(cand, ops):
    scene = Scene(page("4k", "dark"), {"t": today_reduce})
    state, ref, prev, stores = cand.make_state(), None, None, []
    for i in range(len(ops)):
        for key, item in ops[i]:
            scene.set(key, item)
        g = readonly(scene.render()["t"])
        if ref is None:
            ref = g
            stores.append(i)
        elif cand.decide(state, ref, g, prev, "4k", now=now_at(i)):
            ref = g
            stores.append(i)
        prev = g
    return stores


def minclock(cand):
    rates = []
    for bx in (3200, 3460, 3560, 3700):
        for ry in range(4):
            for rx in range(4):
                ops = [[("m", text_item(bx + rx, 2104 + ry, _hms(now_at(i))[:5], dev(12, SCALE), FG))] for i in range(N_CHECKS)]
                rates.append(len(simulate(cand, ops)) - 1)
    return rates


def nohold(cand):
    words = ["ok", "oui", "demain", "allons", "partie", "jouons", "simple", "vraiment"]
    ops = [[] for _ in range(N_CHECKS)]
    drafts = []
    t = 4
    k = 0
    while t + 12 < N_CHECKS:
        w = words[k % len(words)][: 2 + k % 7]
        for j in range(len(w)):
            ops[t + j].append(("d", text_item(2700, 1900, w[: j + 1], dev(14, SCALE), FG)))
        ops[t + len(w)].append(("d", None))
        drafts.append((t, w))
        t += len(w) + 6
        k += 1
    stores = set(simulate(cand, ops))
    out = []
    for t, w in drafts:
        best = max((j + 1 for j in range(len(w)) if t + j in stores), default=0)
        out.append((w, best))
    return out


def main():
    for path in (HERE / "c5.py", HERE / "variants" / "c5_noband.py"):
        cand = Candidate(path)
        r = minclock(cand)
        print(f"{path.name}: minute clock stores/h over 64 placements: min {min(r)} mean {np.mean(r):.1f} max {max(r)}", flush=True)
    cand = Candidate(HERE / "c5.py")
    res = nohold(cand)
    full = sum(b == len(w) for w, b in res)
    print(f"c5.py: no-hold drafts: complete draft stored {full}/{len(res)}")
    by_len = {}
    for w, b in res:
        by_len.setdefault(len(w), []).append(b)
    for n, bs in sorted(by_len.items()):
        print(f"  len {n}: max chars stored per draft {bs}")


if __name__ == "__main__":
    main()
