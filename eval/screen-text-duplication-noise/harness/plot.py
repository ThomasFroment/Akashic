"""side.png with Pillow (matplotlib is not in the environment). Same layout for every candidate:
baseline (gray) beside the candidate (blue), fixed axes, limits drawn in amber."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SURFACE, PLANE = "#1a1a19", "#0d0d0d"
INK, INK2, MUTED, GRID, AXIS = "#f0efec", "#c3c2b7", "#898781", "#2c2c2a", "#383835"
BASE, CAND, LIMIT = "#6b6a64", "#3987e5", "#e0a030"
FONT = "C:/Windows/Fonts/segoeui.ttf"
FONT_B = "C:/Windows/Fonts/segoeuib.ttf"
W, H = 1600, 900


def f(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def g(d, *path):
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def panel(d: ImageDraw.ImageDraw, box, title: str, groups: list[tuple[str, float | None, float | None]], ymax: float,
          ticks: list[float], limit: float | None = None, fmt: str = "{:.2f}", unit: str = "") -> None:
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=SURFACE)
    d.text((x0 + 12, y0 + 8), title, font=f(15, True), fill=INK)
    px0, py0, px1, py1 = x0 + 52, y0 + 40, x1 - 12, y1 - 40
    for t in ticks:
        y = py1 - (py1 - py0) * t / ymax
        d.line((px0, y, px1, y), fill=GRID)
        d.text((px0 - 6, y), f"{t:g}{unit}", font=f(11), fill=MUTED, anchor="rm")
    d.line((px0, py1, px1, py1), fill=AXIS)
    if limit is not None:
        y = py1 - (py1 - py0) * min(limit, ymax) / ymax
        d.line((px0, y, px1, y), fill=LIMIT, width=2)
    gw = (px1 - px0) / max(len(groups), 1)
    bw = min(26, (gw - 12) / 2 - 2)
    for gi, grp in enumerate(groups):
        name, b, c = grp[:3]
        cx = px0 + gw * (gi + 0.5)
        if len(grp) > 3 and grp[3] is not None:  # per-group limit (e.g. half of today's noise)
            y = py1 - (py1 - py0) * min(grp[3], ymax) / ymax
            d.line((cx - bw - 6, y, cx + bw + 6, y), fill=LIMIT, width=2)
        for si, (v, col) in enumerate(((b, BASE), (c, CAND))):
            if v is None:
                continue
            bx = cx - bw - 1 + si * (bw + 2)
            h = (py1 - py0) * min(max(v, 0), ymax) / ymax
            if h >= 1:
                d.rounded_rectangle((bx, py1 - h, bx + bw, py1), radius=min(4, bw / 2, h / 2), fill=col)
            if v > ymax:
                d.text((bx + bw / 2, py0 - 2), "^", font=f(11, True), fill=INK, anchor="mb")
            if si == 1:
                d.text((bx + bw / 2, py1 - h - 3), fmt.format(v), font=f(10), fill=INK2, anchor="mb")
        d.text((cx, py1 + 6), name, font=f(11), fill=MUTED, anchor="mt")


def side(r: dict, base: dict, path: Path, name: str) -> None:
    img = Image.new("RGB", (W, H), PLANE)
    d = ImageDraw.Draw(img)
    d.text((16, 10), f"{name}: duplicated and noisy screen text", font=f(20, True), fill=INK)
    x = 16 + d.textlength(f"{name}: duplicated and noisy screen text", font=f(20, True)) + 30
    for lab, col in (("baseline (today)", BASE), (name, CAND), ("limit / budget", LIMIT)):
        d.rounded_rectangle((x, 17, x + 12, 29), radius=3, fill=col)
        d.text((x + 18, 13), lab, font=f(13), fill=INK2)
        x += 40 + d.textlength(lab, font=f(13))
    runs = [("s", "smoke"), ("st", "standard"), ("d", "day")]
    pw, ph, top = (W - 16 * 5) / 4, (H - 60 - 16 * 3) / 2, 50
    boxes = [(16 + i * (pw + 16), top + j * (ph + 16), 16 + i * (pw + 16) + pw, top + j * (ph + 16) + ph) for j in range(2) for i in range(4)]
    sy, by = r.get("synthetic", {}), base.get("synthetic", {})
    panel(d, boxes[0], "M3.2 duplicate chars (must <= 0.3)",
          [*[(s, g(base, k, "m32"), g(r, k, "m32")) for s, k in runs], ("syn", by.get("m32"), sy.get("m32"))], 1.0, [0, 0.25, 0.5, 0.75, 1], 0.3)
    panel(d, boxes[1], "M3.3 nonce recall (must >= today)", [(s, g(base, k, "m33"), g(r, k, "m33")) for s, k in runs], 1.0, [0, 0.25, 0.5, 0.75, 1])
    pct = lambda v: None if v is None else 100 * v  # noqa: E731
    panel(d, boxes[2], "content lines dropped % (must <= 1)",
          [*[(s, pct(g(base, k, "drop", "all", "rate")), pct(g(r, k, "drop", "all", "rate"))) for s, k in runs],
           ("syn", pct(by.get("drop_content")), pct(sy.get("drop_content"))), ("syn-vol", pct(by.get("drop_vcontent")), pct(sy.get("drop_vcontent")))],
          5.0, [0, 1, 2, 3, 4, 5], 1.0, "{:.2f}")
    panel(d, boxes[3], "noise tokens % of indexed (must <= half)",
          [*[(s, pct(g(base, k, "noise", "share")), pct(g(r, k, "noise", "share")), (pct(g(base, k, "noise", "share")) or 0) / 2 or None) for s, k in runs],
           ("syn", pct(by.get("noise_share")), pct(sy.get("noise_share")), (pct(by.get("noise_share")) or 0) / 2 or None)], 10.0, [0, 2, 4, 6, 8, 10], None, "{:.2f}")
    ratio = lambda a, b: None if a is None or not b else a / b  # noqa: E731
    panel(d, boxes[4], "FTS index size vs today", [(s, 1.0 if g(base, k, "fts_kb") else None, ratio(g(r, k, "fts_kb"), g(base, k, "fts_kb"))) for s, k in runs]
          + [(f"text {s}", 1.0 if g(base, k, "text_kb") else None, ratio(g(r, k, "text_kb"), g(base, k, "text_kb"))) for s, k in runs[1:]], 1.2, [0, 0.25, 0.5, 0.75, 1])
    panel(d, boxes[5], "link valid (frame text) / truth-visible",
          [*[(s, g(base, k, "link", "valid"), g(r, k, "link", "valid")) for s, k in runs[1:]],
           *[(f"truth {s}", g(base, k, "link", "truth_valid"), g(r, k, "link", "truth_valid")) for s, k in runs[1:]],
           ("syn", by.get("link_valid"), sy.get("link_valid"))], 1.0, [0, 0.25, 0.5, 0.75, 1])
    panel(d, boxes[6], "top-5 slots repeating a window",
          [*[(f"nonce {s}", g(base, k, "crowding", "nonce", "dup_same_window"), g(r, k, "crowding", "nonce", "dup_same_window")) for s, k in runs[1:]],
           *[(f"word {s}", g(base, k, "crowding", "word", "dup_same_window"), g(r, k, "crowding", "word", "dup_same_window")) for s, k in runs[1:]]],
          1.0, [0, 0.25, 0.5, 0.75, 1])
    wk = "standard" if "standard" in r else "smoke"
    panel(d, boxes[7], "cost / budget (write 1 ms, deferred 2 %)",
          [("write p50", g(base, wk, "timing", "write_us_p50") / 1000 if g(base, wk, "timing") else None, (g(r, wk, "timing", "write_us_p50") or 0) / 1000),
           ("write p99", g(base, wk, "timing", "write_us_p99") / 1000 if g(base, wk, "timing") else None, (g(r, wk, "timing", "write_us_p99") or 0) / 1000),
           *[(f"defer {s}", (g(base, k, "timing", "defer_pct") or 0) / 2 if g(base, k) else None, (g(r, k, "timing", "defer_pct") or 0) / 2 if g(r, k) else None)
             for s, k in runs[1:]]], 1.5, [0, 0.5, 1, 1.5], 1.0, "{:.3f}")
    img.save(path)
