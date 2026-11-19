"""Minute-clock stores per hour for a candidate over many sub-pixel / tile placements (the
harness sequence uses one placement). Same Scene and store simulation as harness/run.py.

    uv run python eval/small-screen-change-detection/C3/minclock_sweep.py <candidate.py> [<candidate.py> ...]
"""

import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))

import numpy as np  # noqa: E402

from hcommon import MONITORS, THEMES, Candidate  # noqa: E402
from run import Sim  # noqa: E402
from scenes import N_CHECKS, Scene, _hms, dev, now_at, page, text_item  # noqa: E402

DX = (0, 1, 2, 3, 6, 13, 22, 37, 50)
DY = (0, 1, 3, 6, 9, 14)
BASE_XY = {"4k": (3560, 2100), "1440p": (2380, 1404)}


def run(cand: Candidate, monitor: str) -> list[float]:
    scale = MONITORS[monitor][2]
    fg = THEMES["dark"][1]
    hours = N_CHECKS * 5.0 / 3600
    out = []
    for dy in DY:
        for dx in DX:
            x, y = BASE_XY[monitor][0] + dx, BASE_XY[monitor][1] + dy
            scene = Scene(page(monitor, "dark"), {"c": cand.reduce})
            sim = Sim(cand)
            for i in range(N_CHECKS):
                scene.set("minclock", text_item(x, y, _hms(now_at(i))[:5], dev(12, scale), fg))
                g = scene.render()["c"]
                # Sim.step passes "4k"; call decide directly for the monitor under test
                gray = g
                if sim.ref is None:
                    sim.ref = gray
                    sim.stores.append(i)
                elif cand.decide(sim.state, sim.ref, gray, sim.prev, monitor, now=now_at(i)):
                    sim.ref = gray
                    sim.stores.append(i)
                sim.prev = gray
            out.append((len(sim.stores) - 1) / hours)
    return out


def main() -> None:
    for p in sys.argv[1:]:
        cand = Candidate(p)
        for monitor in ("4k", "1440p"):
            v = np.array(run(cand, monitor))
            print(f"{cand.name} {monitor} minclock stores/h over {v.size} placements: "
                  f"min {v.min():.0f} mean {v.mean():.1f} median {np.median(v):.0f} max {v.max():.0f}", flush=True)


if __name__ == "__main__":
    main()
