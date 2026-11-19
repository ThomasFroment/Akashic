"""side.png with Pillow (no matplotlib in the repo venv): the same four panels for every candidate."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

BG, FG, DIM, GRID = (18, 18, 22), (225, 225, 230), (120, 120, 130), (50, 50, 58)
BASE, CAND, BAD, WARN = (110, 110, 125), (90, 170, 255), (240, 95, 95), (240, 180, 70)
W, H = 1400, 900
SHORT = {"20261118-smoke-s11-target-2220": "smoke 2220", "20261118-smoke-s11-target-2241": "smoke 2241",
         "20261119-standard-s21-target-0315": "std 0315", "20261119-standard-s21-target-1818": "std 1818",
         "20261119-day-s31-target-0547": "day 0547"}


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


F, FS, FT = font(15), font(12), font(18)


def bars(d: ImageDraw.ImageDraw, box, title, labels, base, cand, limit=None, limit_label="", fmt="{:.2f}"):
    x0, y0, x1, y1 = box
    d.text((x0, y0), title, fill=FG, font=F)
    top, bottom, left = y0 + 30, y1 - 30, x0 + 10
    vmax = max([*base, *cand, limit or 0, 1e-9]) * 1.15
    for k in range(5):
        y = bottom - (bottom - top) * k / 4
        d.line((left, y, x1, y), fill=GRID)
        d.text((x1 - 50, y - 14), fmt.format(vmax * k / 4), fill=DIM, font=FS)
    n = len(labels)
    slot = (x1 - left - 60) / max(1, n)
    for i, lab in enumerate(labels):
        sx = left + i * slot + slot * 0.15
        bw = slot * 0.33
        for j, (v, col) in enumerate(((base[i], BASE), (cand[i], CAND))):
            bx = sx + j * (bw + 4)
            h = (bottom - top) * v / vmax
            d.rectangle((bx, bottom - h, bx + bw, bottom), fill=col)
            d.text((bx, bottom - h - 15), fmt.format(v), fill=FG, font=FS)
        d.text((sx, bottom + 6), lab, fill=DIM, font=FS)
    if limit is not None:
        y = bottom - (bottom - top) * limit / vmax
        for xx in range(int(left), int(x1 - 60), 12):
            d.line((xx, y, xx + 6, y), fill=WARN, width=2)
        d.text((left + 4, y - 16), limit_label, fill=WARN, font=FS)


def hbars(d: ImageDraw.ImageDraw, box, title, rows):
    """rows: (label, must_s, [(value, base_value, colour, name)])"""
    x0, y0, x1, y1 = box
    d.text((x0, y0), title, fill=FG, font=F)
    top, left, right = y0 + 30, x0 + 150, x1 - 70
    vmax = max([1.0, *[v for _, _, vals in rows for v, b, _, _ in vals], *[b for _, _, vals in rows for v, b, _, _ in vals]])
    rh = (y1 - top - 10) / max(1, len(rows))
    for i, (lab, must, vals) in enumerate(rows):
        y = top + i * rh
        d.text((x0, y + 2), lab, fill=FG, font=FS)
        d.text((x0, y + 16), must, fill=DIM, font=FS)
        for j, (v, b, col, name) in enumerate(vals):
            by = y + 3 + j * (rh / 2 - 2)
            bh = rh / 2 - 6
            w = (right - left) * v / vmax
            d.rectangle((left, by, left + max(w, 1), by + bh), fill=col if v > 0 else GRID)
            bxx = left + (right - left) * b / vmax
            d.line((bxx, by - 1, bxx, by + bh + 1), fill=FG, width=2)
            label = f"{name} {v:g} s (base {b:g})"
            inside = left + max(w, 1) + 6 + 230 > x1
            d.text((left + 6 if inside else left + max(w, 1) + 6, by), label, fill=BG if inside else (FG if v > 0 else DIM), font=FS)


def side(dd: dict, path, title: str) -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    runs = list(dd["runs"])
    labels = [SHORT.get(r, r[-4:]) for r in runs]
    sim = dd["uses_sim"]["cand"]
    d.text((20, 12), f"{title}   uses simulated records: {sim}", fill=BAD if sim != "no" else FG, font=FT)
    d.text((20, 38), "grey = baseline (today), blue = candidate", fill=DIM, font=FS)
    words, lines = dd["metrics"]["cand"].split(), [""]
    for wd in words:
        lines[-1] += ("" if not lines[-1] else " ") + wd
        if len(lines[-1]) > 190:
            lines.append("")
    for i, line in enumerate(lines[:2]):
        d.text((20, 54 + 14 * i), line, fill=DIM, font=FS)
    g = lambda who, k: [dd["runs"][r][who][k] for r in runs]  # noqa: E731
    bars(d, (20, 95, 690, 480), "M5.1 holes, % of truth app time", labels, g("baseline", "M5.1_pct"), g("cand", "M5.1_pct"), 5.0, "must <= 5 % (std, day)", "{:.1f}")
    bars(d, (710, 95, 1380, 480), "M5.3 total abs duration error ratio (must < baseline)", labels, g("baseline", "M5.3_error"), g("cand", "M5.3_error"), None, "", "{:.3f}")
    rows = []
    for name, s in dd["synthetic"].items():
        rows.append((name, f"present {s['present_s']:g} s, not {s['must_not_s']:g} s",
                     [(s["cand"]["away_counted_s"], s["baseline"]["away_counted_s"], BAD, "away counted"),
                      (s["cand"]["present_missed_s"], s["baseline"]["present_missed_s"], WARN, "present missed")]))
    hbars(d, (20, 500, 900, 890), "Synthetic timelines: seconds wrong (tick = baseline)", rows)
    bars(d, (920, 500, 1380, 890), "ms per day of data, median of 9 (metric: day run)", labels, g("baseline", "ms_per_day"), g("cand", "ms_per_day"), 50.0, "budget 50 ms/day", "{:.1f}")
    img.save(path)
