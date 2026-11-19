"""Taskbar minute clock (HH:MM at 12 CSS px) per sub-pixel placement: stores per hour, ref =
last stored, one check per minute flip (the clock is the only change). Variants of c2.py."""
import importlib.util
import sys
from pathlib import Path
import numpy as np
H = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(H))
from hcommon import MONITORS, THEMES, today_reduce  # noqa: E402
from scenes import Scene, dev, page, text_item  # noqa: E402

spec = importlib.util.spec_from_file_location("c2", Path(__file__).with_name("c2.py"))
c2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c2)

VARIANTS = {"plain": (False, 0), "hyst": (True, 0), "hyst+taskbar": (True, None), "plain+taskbar": (False, None)}
TB = {"4k": 15, "1440p": 12}  # 48 CSS px taskbar / 4


def run(mon, theme, hyst, tb):
    w, hgt, scale = MONITORS[mon]
    c2.HYSTERESIS = hyst
    c2.TASKBAR_ROWS = {mon: TB[mon] if tb is None else tb}
    fg = THEMES[theme][1]
    px = dev(12, scale)
    ty = hgt - dev(48, scale) + dev(16, scale)  # inside the taskbar band
    rates = []
    for oy in range(4):
        for ox in range(4):
            sc = Scene(page(mon, theme), {"t": today_reduce})
            ref = None
            stores = 0
            for m in range(61):
                hh, mm = 12 + (m // 60), m % 60
                sc.set("c", text_item(w - dev(120, scale) + ox, ty + oy, f"{hh:02d}:{mm:02d}", px, fg))
                g = sc.render()["t"]
                if ref is None:
                    ref = g
                    continue
                if c2.decide(None, ref, g, ref, mon):
                    ref = g
                    stores += 1
            rates.append(stores)
    return rates


for mon in MONITORS:
    for theme in THEMES:
        for name, (hy, tb) in VARIANTS.items():
            r = run(mon, theme, hy, tb)
            print(f"{mon} {theme} {name:13s} stores/h per placement min {min(r)} mean {np.mean(r):.1f} max {max(r)}  {r}")
