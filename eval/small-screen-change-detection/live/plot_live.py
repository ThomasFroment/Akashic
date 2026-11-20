"""side.png for the live check, in the layout of harness/plot.py (dark surface, Pillow only).

    .venv\\Scripts\\python.exe live\\plot_live.py live\\results.json live\\side.png
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
from plot import BLUE, GREY, H, INK, INK2, ORANGE, SURFACE, W, _bars, _f  # noqa: E402

GREEN, PURPLE = (77, 170, 110), (170, 120, 220)
SERIES = [("baseline", GREY), ("C2", ORANGE), ("C3", GREEN), ("C5", BLUE), ("C5fix", PURPLE)]
BROWSERS = ("edge", "edge2", "chrome")


def pooled_recall(res: dict, cand: str, key: str, phase: str = "all") -> tuple[int, int]:
    miss = n = 0
    for run in BROWSERS:
        if run in res:
            r = res[run]["candidates"][cand]["recall"][phase][key]
            miss += r["miss1"]
            n += r["n"]
    return miss, n


def pooled_useless(res: dict, cand: str, phase: str) -> tuple[int, float]:
    stores = hours = 0.0
    for run, r in res.items():
        u = r["candidates"][cand]["useless"].get(phase)
        if u:
            stores += u["stores"]
            hours += u["hours"]
    return int(stores), hours


def main() -> None:
    res = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    img = Image.new("RGB", (W, H), SURFACE)
    d = ImageDraw.Draw(img)
    d.text((24, 14), "Live check: real Edge, Chrome and Windows Terminal on the 1440p monitor (5 s checks, WGC)",
           font=_f(24), fill=INK)

    rows = []
    keys = [("reply_13px", "reply 13 px"), ("reply_14px", "reply 14 px"), ("reply_16px", "reply 16 px"),
            ("reply_dark", "reply dark"), ("reply_light", "reply light"), ("reply_adjacent_recent", "reply next line"),
            ("badge_on", "badge on"), ("badge_off", "badge off")]
    for key, label in keys:
        vals, texts = [], []
        for name, _col in SERIES:
            m, n = pooled_recall(res, name, key)
            vals.append(m / n if n else 0)
            texts.append(f"{m}/{n}")
        rows.append((label, vals, texts))
    _bars(d, (24, 60, 780, 640), "Missed at the first check (fraction; Edge + Chrome, quiet + busy)", rows, SERIES, 1.0,
          lambda v: f"{v:.2f}")

    rows = []
    for ph, label in (("caret", "native caret"), ("ecaret", "2 px CSS caret"), ("clock", "seconds clock"),
                      ("spinner", "spinner"), ("idle", "idle"), ("bar", "WT bar cursor"), ("block", "WT block cursor"),
                      ("quiet", "quiet replies*"), ("events", "busy replies*"), ("events_caret", "busy + caret*")):
        vals, texts = [], []
        for name, _col in SERIES:
            s, h = pooled_useless(res, name, ph)
            v = s / h if h else 0
            vals.append(v)
            texts.append(f"{v:.0f}")
        rows.append((label, vals, texts))
    _bars(d, (820, 60, 1576, 640), "Useless stores per hour (clean checks)", rows, SERIES, 720, lambda v: f"{v:.0f}")
    d.text((820, 648), "* stores not attributable to a reply or badge", font=_f(13), fill=INK2)

    rows = []
    for name, _col in SERIES:
        p50 = [res[r]["candidates"][name]["decide_ms"]["p50"] for r in res]
        p95 = [res[r]["candidates"][name]["decide_ms"]["p95"] for r in res]
        mx = [res[r]["candidates"][name]["decide_ms"]["max"] for r in res]
        rows.append((name, [max(p50), max(p95), max(mx)], [f"{max(p50):.2f}", f"{max(p95):.2f}", f"{max(mx):.2f}"]))
    vmax = max(max(r[1]) for r in rows) * 1.15
    _bars(d, (24, 690, 780, 1070), "Decide time per check on real 1440p frames, ms (worst run)", rows,
          [("p50", GREY), ("p95", BLUE), ("max", ORANGE)], vmax, lambda v: f"{v:.1f}")

    y = 690
    d.text((820, y), "Synthetic (4K sequences) vs live (1440p), C5", font=_f(20), fill=INK)
    lines = []
    m, n = pooled_recall(res, "C5", "reply")
    lines.append(f"replies missed: synthetic 0/50 busy, 0/50 quiet; live {m}/{n} at first check")
    m, n = pooled_recall(res, "C5", "badge_on")
    m2, n2 = pooled_recall(res, "C5", "badge_off")
    lines.append(f"badges missed: synthetic 0/60; live on {m}/{n}, off {m2}/{n2}")
    for ph, syn in (("clock", 7), ("spinner", 0), ("block", 8)):
        s, h = pooled_useless(res, "C5", ph)
        lines.append(f"{ph} stores/h: synthetic {syn}; live {s / h:.0f} ({s} in {h:.2f} h)" if h else f"{ph}: no clean checks")
    s, h = pooled_useless(res, "C5", "caret")
    lines.append(f"native caret stores/h: live {s / h:.0f} ({s} in {h:.2f} h); synthetic bar fire 0/16384" if h else "caret: none")
    mb, nb = pooled_recall(res, "baseline", "reply", "quiet")
    lines.append(f"today, quiet replies missed: synthetic 33/50; live {mb}/{nb}")
    dq = [res[r]["dirty"].get("quiet_marks") for r in res if res[r]["dirty"].get("quiet_marks")]
    if dq:
        lines.append("WGC dirty rects, quiet reply/badge checks: union p50 " +
                     ", ".join(f"{q['union_area_p50']} px2 (x{q['union_over_mark_p50']} the mark)" for q in dq))
    for ph in ("caret", "clock"):
        dd = [res[r]["dirty"].get(ph) for r in res if res[r]["dirty"].get(ph)]
        if dd:
            lines.append(f"WGC dirty union p50, {ph} checks: " + ", ".join(f"{x['union_area_p50']} px2" for x in dd))
    for i, line in enumerate(lines):
        d.text((820, y + 40 + i * 26), line, font=_f(15), fill=INK2)
    img.save(out)


if __name__ == "__main__":
    main()
