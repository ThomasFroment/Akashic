"""side.png (same layout for every candidate) and ref.png, drawn with Pillow (no matplotlib in
the venv). Dark surface; 4K = blue, 1440p = orange, baseline = grey, candidate = blue."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hcommon import FONT

SURFACE, INK, INK2, GRID = (26, 26, 25), (240, 239, 236), (195, 194, 183), (60, 60, 58)
BLUE, ORANGE, GREY = (57, 135, 229), (217, 89, 38), (138, 137, 132)
W, H = 1600, 1110


def _f(px):
    return ImageFont.truetype(FONT, px)


def _bars(d, box, title, rows, series, vmax, fmt):
    """Horizontal grouped bars. rows: [(label, [value per series], [text per series])]."""
    x0, y0, x1, y1 = box
    d.text((x0, y0), title, font=_f(20), fill=INK)
    lx = x0 + 150
    top = y0 + 34
    leg = lx
    for name, col in series:
        d.rounded_rectangle((leg, top, leg + 14, top + 14), 3, fill=col)
        d.text((leg + 20, top - 3), name, font=_f(15), fill=INK2)
        leg += 30 + int(d.textlength(name, font=_f(15)))
    top += 26
    plot_w = x1 - lx - 120
    row_h = (y1 - top) / max(len(rows), 1)
    bh = max(3, min(12, (row_h - 6) / len(series)))
    for gx in range(5):
        x = lx + plot_w * gx / 4
        d.line((x, top, x, y1), fill=GRID)
        d.text((x - 10, y1 + 2), fmt(vmax * gx / 4), font=_f(13), fill=INK2)
    for r, (label, vals, texts) in enumerate(rows):
        y = top + r * row_h + (row_h - bh * len(series) - 2 * (len(series) - 1)) / 2
        d.text((x0, y - 2), label, font=_f(14), fill=INK2)
        for s, (v, t) in enumerate(zip(vals, texts)):
            yy = y + s * (bh + 2)
            w = 0 if vmax <= 0 else plot_w * min(v, vmax) / vmax
            if w > 0:
                d.rounded_rectangle((lx, yy, lx + max(w, 2), yy + bh), 2, fill=series[s][1])
            d.text((lx + w + 6, yy - 3), t, font=_f(12), fill=INK2)


def side(det: dict, name: str, path: Path) -> None:
    img = Image.new("RGB", (W, H), SURFACE)
    d = ImageDraw.Draw(img)
    d.text((24, 14), f"{name}: small-change detection harness (synthetic)", font=_f(24), fill=INK)
    mons = [("4K 125%", BLUE, "4k"), ("1440p 100%", ORANGE, "1440p")]
    series = [(a, b) for a, b, _ in mons]
    pc, groups = det["per_case"], det["groups"]

    # misses per positive: per word (full contrast, all sizes), low-contrast groups, badge, sentence
    rows = []
    from scenes import WORDS

    def agg(pred):
        out = {}
        for m in ("4k", "1440p"):
            bad = n = 0
            for key, v in det["cases_by_monitor"][m].items():
                if pred(key):
                    bad += v[0]
                    n += v[1]
            out[m] = (bad, n)
        return out

    for w in WORDS:
        a = agg(lambda k, w=w: k.startswith(f"word:{w}:") and k.endswith(":full"))
        rows.append((f'"{w}" full', [a[m][0] / max(a[m][1], 1) for _, _, m in mons], [f"{a[m][0]}/{a[m][1]}" for _, _, m in mons]))
    for g, label in (("d64", "words d64"), ("d40", "words d40")):
        a = agg(lambda k, g=g: k.startswith("word:") and k.endswith(f":{g}"))
        rows.append((label, [a[m][0] / max(a[m][1], 1) for _, _, m in mons], [f"{a[m][0]}/{a[m][1]}" for _, _, m in mons]))
    for c in ("badge", "sentence"):
        a = agg(lambda k, c=c: k == c)
        rows.append((c, [a[m][0] / max(a[m][1], 1) for _, _, m in mons], [f"{a[m][0]}/{a[m][1]}" for _, _, m in mons]))
    _bars(d, (24, 60, 780, 560), "Misses per positive (fraction of placements, both themes)", rows, series, 1.0,
          lambda v: f"{v:.2f}")

    rows = []
    for c in ("bar1", "bar2", "underscore", "hline1", "hline2", "block"):
        a = agg(lambda k, c=c: k == c)
        rows.append((c, [a[m][0] / max(a[m][1], 1) for _, _, m in mons], [f"{a[m][0]}/{a[m][1]}" for _, _, m in mons]))
    _bars(d, (820, 60, 1576, 400), "Fires per negative (fraction of placements)", rows, series, 1.0, lambda v: f"{v:.2f}")

    sv = det["superset_viol"]
    d.text((820, 430), f"superset violations vs baseline: {sv}", font=_f(18), fill=INK)
    ex = det["extra"]
    d.text((820, 458), f"extra pairs fired: candidate {sum(r['cand'] for r in ex)}/{len(ex)}, "
                       f"baseline {sum(r['base'] for r in ex)}/{len(ex)}", font=_f(16), fill=INK2)
    for i, line in enumerate(det.get("metrics_wrapped", [])):
        d.text((820, 490 + i * 20), line, font=_f(13), fill=INK2)

    t = det["timing"]
    keys = ("identical", "word", "caret", "large", "sig")
    vmax = max(max(t[f"t_{k}_ms"], t[f"t_{k}_base_ms"]) for k in keys) * 1.15
    rows = [(k if k != "sig" else "signature", [t[f"t_{k}_base_ms"], t[f"t_{k}_ms"]],
             [f"{t[f't_{k}_base_ms']:.2f}", f"{t[f't_{k}_ms']:.2f}"]) for k in keys]
    _bars(d, (24, 690, 780, 1070), f"4K timing, ms (median; extra worst {t['extra_ms_worst']:+.2f} ms)",
          rows, [("baseline", GREY), (name, BLUE)], vmax, lambda v: f"{v:.1f}")

    seq = det.get("sequences")
    if seq:
        rows = []
        for k in ("clock", "spinner", "block", "minclock", "typing", "badge", "replies_busy", "replies_quiet"):
            b, c = seq[k]["base"]["stores_h"], seq[k]["cand"]["stores_h"]
            rows.append((k, [b, c], [f"{b:.0f}", f"{c:.0f}"]))
        _bars(d, (820, 690, 1576, 1070), "Sequences: stores per hour (4K, 5 s checks)", rows,
              [("baseline", GREY), (name, BLUE)], 720, lambda v: f"{v:.0f}")
    img.save(path)


def make_ref(path: Path) -> None:
    """A synthetic 4K pair: before (caret), after ("ok" + caret moved), the 1/4 moved mask."""
    from hcommon import THEMES, today_reduce
    from scenes import dev, font, thin

    bg, fg = THEMES["dark"]
    cw, ch = 320, 96

    def frame(word: str) -> Image.Image:
        im = Image.new("RGB", (cw, ch), bg)
        dd = ImageDraw.Draw(im)
        f = font(dev(14, 1.25))
        x = 40
        if word:
            dd.text((x, 34), word, font=f, fill=fg)
            x += int(dd.textlength(word, font=f)) + 2
        dd.rectangle((x, 33, x + thin(2, 1.25) - 1, 33 + dev(19, 1.25) - 1), fill=fg)
        return im

    a, b = frame(""), frame("ok")
    m = np.abs(today_reduce(a).astype(np.int16) - today_reduce(b).astype(np.int16)) > 16
    mask = Image.fromarray(np.where(m, 255, 40).astype(np.uint8)).resize((cw, ch), Image.NEAREST).convert("RGB")
    z = 2
    out = Image.new("RGB", (3 * cw * z + 80, ch * z + 60), SURFACE)
    d = ImageDraw.Draw(out)
    for i, (im, label) in enumerate(((a, "before: caret (4K 125%)"), (b, 'after: "ok" + caret'),
                                     (mask, f"1/4 gray moved > 16: {int(m.sum())} px"))):
        x = 20 + i * (cw * z + 20)
        out.paste(im.resize((cw * z, ch * z), Image.NEAREST), (x, 40))
        d.text((x, 10), label, font=_f(18), fill=INK)
    out.save(path)


if __name__ == "__main__":  # uv run python .../harness/plot.py  -> writes <study>/ref.png
    from hcommon import STUDY

    make_ref(STUDY / "ref.png")
