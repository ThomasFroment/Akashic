"""side.png with Pillow (matplotlib is not in the environment). Same layout for every candidate:
baseline (gray) beside the candidate (blue); fixed axes so candidates compare by eye."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SURFACE, PLANE = "#1a1a19", "#0d0d0d"
INK, INK2, MUTED, GRID, AXIS = "#f0efec", "#c3c2b7", "#898781", "#2c2c2a", "#383835"
BASE, CAND, STORED = "#6b6a64", "#3987e5", "#199e70"
FONT = "C:/Windows/Fonts/segoeui.ttf"
FONT_B = "C:/Windows/Fonts/segoeuib.ttf"


def f(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def bars(d: ImageDraw.ImageDraw, box, title: str, groups: list[tuple[str, list[float | None]]], colors: list[str],
         ymax: float, ticks: list[float], label_series: int = 1, unit: str = "") -> None:
    """Grouped bars in box=(x0,y0,x1,y1). Values above ymax are clipped and marked with a caret."""
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=SURFACE)
    d.text((x0 + 12, y0 + 8), title, font=f(15, True), fill=INK)
    px0, py0, px1, py1 = x0 + 48, y0 + 40, x1 - 12, y1 - 44
    for t in ticks:
        y = py1 - (py1 - py0) * t / ymax
        d.line((px0, y, px1, y), fill=GRID)
        d.text((px0 - 6, y), f"{t:g}{unit}", font=f(11), fill=MUTED, anchor="rm")
    d.line((px0, py1, px1, py1), fill=AXIS)
    gw = (px1 - px0) / len(groups)
    nb = len(colors)
    bw = min(22, (gw - 10) / nb - 2)
    for gi, (name, vals) in enumerate(groups):
        cx = px0 + gw * (gi + 0.5)
        left = cx - (nb * (bw + 2) - 2) / 2
        for si, v in enumerate(vals):
            if v is None:
                continue
            bx = left + si * (bw + 2)
            h = (py1 - py0) * min(v, ymax) / ymax
            if h >= 1:
                d.rounded_rectangle((bx, py1 - h, bx + bw, py1), radius=min(4, bw / 2, h / 2), fill=colors[si])
            if v > ymax:
                d.text((bx + bw / 2, py0 - 2), "^", font=f(11, True), fill=INK, anchor="mb")
            if si == label_series:
                d.text((bx + bw / 2, py1 - h - 3), f"{v:.2f}" if v < 10 else f"{v:.0f}", font=f(10), fill=INK2, anchor="mb")
        for li, part in enumerate(name.split("\n")):
            d.text((cx, py1 + 6 + li * 14), part, font=f(11), fill=MUTED, anchor="mt")


def legend(d, x, y, items):
    for name, color in items:
        d.rounded_rectangle((x, y + 3, x + 12, y + 15), radius=3, fill=color)
        d.text((x + 18, y), name, font=f(13), fill=INK2)
        x += 26 + d.textlength(name, font=f(13))


def side(path: Path, name: str, m: dict, base: dict, tim: dict) -> None:
    W, H = 1500, 900
    img = Image.new("RGB", (W, H), PLANE)
    d = ImageDraw.Draw(img)
    d.text((20, 14), f"{name} vs baseline: Windows OCR on small screen text", font=f(20, True), fill=INK)
    legend(d, 20, 46, [("baseline", BASE), (f"candidate: {name}", CAND), ("stored text (today, replay only)", STORED)])
    groups = []
    for path_tag, plabel in (("", "live"), ("def_", "deferred")):
        for sc in ("1440", "4k"):
            for b, blabel in (("le11", "<=11"), ("12_15", "12-15"), ("ge16", ">=16")):
                k = f"cer_{path_tag}{b}_{sc}"
                groups.append((f"{blabel}\n{plabel} {sc}", [base.get(k), m.get(k)]))
    bars(d, (20, 76, W - 20, 400), "Corpus CER (Edge headless, ClearType AA) by device-px font bucket; ^ = clipped",
         groups, [BASE, CAND], 0.8, [0, 0.2, 0.4, 0.6, 0.8])
    pgroups = []
    for tag, lab in (("gray_", "Edge gray AA"), ("pil_", "Pillow")):
        for k, kl in (("cer_le11_1440", "<=11 1440"), ("cer_1440", "all 1440"), ("cer_def_1440", "def 1440")):
            pgroups.append((f"{kl}\n{lab}", [base.get(tag + k), m.get(tag + k)]))
    bars(d, (20, 412, 640, 660), "Other renderers, CER", pgroups, [BASE, CAND], 0.8, [0, 0.2, 0.4, 0.6, 0.8])
    rgroups = []
    for sc, lab in (("1440", "1440p"), ("4k", "4K")):
        rgroups.append((f"{lab}\nscore rule", [base.get(f"replay_cer_{sc}"), m.get(f"replay_cer_{sc}"), m.get(f"replay_stored_{sc}")]))
        rgroups.append((f"{lab}\nwrap-tolerant", [base.get(f"replay_join_{sc}"), m.get(f"replay_join_{sc}"), m.get(f"replay_stored_join_{sc}")]))
    bars(d, (652, 412, W - 20, 660), "Replay on stored WebP frames (standard + day runs), CER", rgroups, [BASE, CAND, STORED], 0.6,
         [0, 0.2, 0.4, 0.6])
    tgroups = [("live p50", [tim["base"]["live_ms_p50"], tim["cand"]["live_ms_p50"]]),
               ("live p95", [tim["base"]["live_ms_p95"], tim["cand"]["live_ms_p95"]]),
               ("deferred p50", [tim["base"]["def_ms_p50"], tim["cand"]["def_ms_p50"]]),
               ("deferred p95", [tim["base"]["def_ms_p95"], tim["cand"]["def_ms_p95"]])]
    bars(d, (20, 672, 900, H - 20), "OCR wall time per window call, ms (12 pages x 9 repeats, after warm-up)", tgroups,
         [BASE, CAND], 1000, [0, 250, 500, 750, 1000])
    x, y = 912, 672
    d.rectangle((x, y, W - 20, H - 20), fill=SURFACE)
    d.text((x + 12, y + 8), "Budget ratios (candidate / baseline)", font=f(15, True), fill=INK)
    rows = [("CPU per live call", m.get("cpu_ratio"), "<= 2.0"), ("CPU per deferred window", m.get("cpu_def_ratio"), "<= 2.0"),
            ("stored bytes per window", m.get("bytes_ratio"), "<= 1.2 (of media)"),
            ("p95 live call, +ms", m.get("dms_p95"), "<= +50"), ("p95 deferred call, +ms", m.get("dms_def_p95"), "")]
    for i, (lab, v, budget) in enumerate(rows):
        yy = y + 38 + i * 31
        d.text((x + 12, yy), lab, font=f(14), fill=INK2)
        d.text((x + 300, yy), "na" if v is None else f"{v:.2f}" if abs(v) < 100 else f"{v:.0f}", font=f(18, True), fill=INK)
        d.text((x + 400, yy + 2), budget, font=f(13), fill=MUTED)
    img.save(path)
