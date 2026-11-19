"""Real-desktop false-trigger replay (no labels): how often does each candidate fire on the
user's real screens compared with today's test?

    uv run python eval/small-screen-change-detection/harness/live_replay.py --seconds 600 \
        [--interval 5] [--out counts.json] cand1.py [cand2.py ...]

Grabs the whole virtual desktop with Pillow ImageGrab (GDI; a WGC capture of Progman showed only
the wallpaper layer) every --interval seconds, splits it per monitor with
eval/quality/winapi.py monitors(), reduces each monitor to gray frames IN MEMORY, and runs a
store simulation per candidate and monitor (ref = last stored frame, prev = previous check).
Only counts are printed (and written with --out). No image, frame or captured content is ever
written anywhere. `--seconds 0` is a dry run that captures nothing.

Caveat to check on the first real run: a window capture of Progman may show only the desktop
layer (wallpaper, icons), not the application windows above it. `changed_checks` per monitor
shows whether the frames move at all.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "eval" / "quality"))

import numpy as np  # noqa: E402

import winapi  # noqa: E402
from hcommon import Candidate, baseline, readonly  # noqa: E402


def label(m: dict) -> str:
    return "4k" if m["w"] >= 3840 else "1440p"


class Sim:
    def __init__(self, c: Candidate) -> None:
        self.c, self.state = c, c.make_state()
        self.ref = self.prev = None
        self.checks = self.fires = 0
        self.ns = 0

    def step(self, gray, monitor: str, now: float) -> None:
        if self.ref is None:
            self.ref = self.prev = gray
            return
        t = time.perf_counter_ns()
        fire = self.c.decide(self.state, self.ref, gray, self.prev, monitor, now=now)
        self.ns += time.perf_counter_ns() - t
        self.checks += 1
        if fire:
            self.fires += 1
            self.ref = gray
        self.prev = gray


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidates", nargs="*")
    ap.add_argument("--seconds", type=float, required=True)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    cands = [baseline()] + [Candidate(p) for p in a.candidates]
    winapi.set_dpi_aware()
    mons = winapi.monitors()
    vx, vy = min(m["x"] for m in mons), min(m["y"] for m in mons)
    vw = max(m["x"] + m["w"] for m in mons) - vx
    vh = max(m["y"] + m["h"] for m in mons) - vy
    plan = {"monitors": [{"name": m["name"], "rect": [m["x"], m["y"], m["w"], m["h"]], "scale": m["scale"],
                          "label": label(m)} for m in mons],
            "virtual": [vx, vy, vw, vh], "candidates": [c.name for c in cands], "seconds": a.seconds,
            "interval": a.interval}
    print(json.dumps(plan))
    if a.seconds <= 0:
        print("dry run: nothing captured")
        return

    # GDI grab of the whole virtual desktop (Pillow ImageGrab): a WGC capture of Progman showed only
    # the wallpaper layer (changed_checks 0 on every monitor in the first live run).
    from PIL import ImageGrab

    reducers = {}
    for c in cands:
        reducers.setdefault(id(c.reduce), c.reduce)
    sims = {(c.name, m["name"]): Sim(c) for c in cands for m in mons}
    changed = {m["name"]: 0 for m in mons}
    last = {m["name"]: None for m in mons}
    counts = {"grabs": 0, "no_frame": 0, "size_mismatch": 0, "frame_size": None, "source": "gdi"}
    end = time.monotonic() + a.seconds
    tick = time.monotonic()
    while tick < end:
        try:
            img = ImageGrab.grab(bbox=(vx, vy, vx + vw, vy + vh), all_screens=True)
        except OSError:
            img = None
        if img is None:
            counts["no_frame"] += 1
        else:
            counts["grabs"] += 1
            counts["frame_size"] = list(img.size)
            if img.size != (vw, vh):
                counts["size_mismatch"] += 1
            else:
                now = time.time()
                for m in mons:
                    crop = img.crop((m["x"] - vx, m["y"] - vy, m["x"] - vx + m["w"], m["y"] - vy + m["h"]))
                    grays = {k: readonly(np.asarray(f(crop))) for k, f in reducers.items()}
                    del crop
                    g0 = grays[id(cands[0].reduce)]
                    if last[m["name"]] is not None and not np.array_equal(g0, last[m["name"]]):
                        changed[m["name"]] += 1
                    last[m["name"]] = g0
                    for c in cands:
                        sims[c.name, m["name"]].step(grays[id(c.reduce)], label(m), now)
            del img
        tick += a.interval
        time.sleep(max(0.0, tick - time.monotonic()))
    rows = []
    for m in mons:
        base = sims[cands[0].name, m["name"]]
        for c in cands:
            s = sims[c.name, m["name"]]
            rows.append({"monitor": m["name"], "label": label(m), "candidate": c.name, "checks": s.checks,
                         "fires": s.fires, "baseline_fires": base.fires, "extra_fires": s.fires - base.fires,
                         "changed_checks": changed[m["name"]],
                         "decide_ms_mean": round(s.ns / max(s.checks, 1) / 1e6, 3)})
    result = {"plan": plan, "counts": counts, "rows": rows}
    for r in rows:
        print(f"{r['monitor']:<8} {r['candidate']:<24} checks={r['checks']} fires={r['fires']} "
              f"baseline={r['baseline_fires']} extra={r['extra_fires']:+d} changed={r['changed_checks']} "
              f"decide_ms={r['decide_ms_mean']}")
    print(json.dumps(counts))
    if a.out:
        a.out.write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
