"""P1 figures (Pillow, dark, numbers only): fig-cancellation.png (spike per-block depths) and
fig-overlap-pairs.png (overlap WER per pair of overlapping sources, baseline vs P1).

    uv run python eval/overlapping-speech/P1-per-process-streams/figs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
from draw import BASE, BG, CAND, DIM, FG, GRID, WARN, F, FS, FT  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

BAD = (240, 95, 95)
GREEN = (110, 190, 140)
PURPLE = (150, 120, 220)


def cancellation() -> None:
    s = json.loads((HERE / "spike.json").read_text(encoding="utf-8"))
    series = [
        ("CABLE 16k 8 taps", s["cable"]["16000"]["depth_alone_db"]["per_block"], BASE),
        ("CABLE 48k 8 taps", s["cable"]["48000"]["depth_alone_db"]["per_block"], CAND),
        ("DT-770 16k 8 taps", s["dt770"]["16000"]["depth_alone_db"]["per_block"], BAD),
        ("DT-770 16k 128 taps", s["dt770"]["16000"]["fir128"]["depth_alone_db"]["per_block"], GREEN),
    ]
    leaks = [
        ("DT-770 16k 128 taps, FIR held while the call talks", s["dt770"]["16000"]["fir128"]["leak_held_fir_db"]["per_block"], GREEN),
        ("DT-770 16k 128 taps, refit per 0.5 s block (call present)", s["dt770"]["16000"]["fir128"]["leak_db"]["per_block"], WARN),
        ("DT-770 16k 128 taps, refit over last 8 s (call present)", s["dt770"]["16000"]["fir128"]["leak_long_window_db"]["per_block"], PURPLE),
        ("CABLE 16k 8 taps, FIR held", s["cable"]["16000"]["leak_held_fir_db"]["per_block"], BASE),
    ]
    w, h = 1500, 900
    im = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(im)
    d.text((40, 16), "P1 spike: cancellation per 0.5 s block (lower is better), target PC, numbers only", fill=FG, font=FT)
    for (title, rows, box) in (("Source A alone: residual vs mix (dB)", series, (90, 90, 1440, 420)),
                               ("Both sources: what of A stays in the call residual, vs A (dB)", leaks, (90, 520, 1440, 850))):
        x0, y0, x1, y1 = box
        d.text((x0 - 50, y0 - 30), title, fill=FG, font=F)
        lo, hi = -100, 20
        def yv(v):
            return y1 - (min(max(v, lo), hi) - lo) / (hi - lo) * (y1 - y0)
        for t in range(lo, hi + 1, 20):
            d.line((x0, yv(t), x1, yv(t)), fill=GRID)
            d.text((x0 - 45, yv(t) - 7), f"{t}", fill=DIM, font=FS)
        d.line((x0, yv(-25), x1, yv(-25)), fill=BAD)
        d.text((x1 - 170, yv(-25) - 16), "validity switch -25 dB", fill=BAD, font=FS)
        n = max(len(r) for _, r, _ in rows)
        for k, (name, r, col) in enumerate(rows):
            pts = [(x0 + (i + 0.5) / n * (x1 - x0), yv(v)) for i, v in enumerate(r)]
            if len(pts) > 1:
                d.line(pts, fill=col, width=2)
            for p in pts:
                d.ellipse((p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3), fill=col)
            d.text((x0 + 10 + (k % 2) * 650, y1 + 8 + (k // 2) * 16), name, fill=col, font=FS)
        d.text((x1 - 120, y0 - 26), "block index", fill=DIM, font=FS)
    im.save(HERE / "fig-cancellation.png")


def pairs() -> None:
    base = json.loads((HERE.parent / "baseline" / "detail.json").read_text(encoding="utf-8"))
    cand = json.loads((HERE / "detail.json").read_text(encoding="utf-8"))
    w, h = 1500, 760
    im = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(im)
    d.text((40, 16), "Overlap WER (M9.1 groups) per pair of overlapping sources: baseline grey, P1 blue (local = order-free, hatched)", fill=FG, font=FT)
    for r, (name, box) in enumerate((("std", (90, 90, 1440, 360)), ("day", (90, 450, 1440, 720)))):
        bp = base["runs"].get(name, {}).get("overlap_by_pair", {})
        cp = cand["runs"].get(name, {}).get("overlap_by_pair", {})
        keys = sorted(set(bp) | set(cp))
        x0, y0, x1, y1 = box
        d.text((x0 - 50, y0 - 30), f"{name} run", fill=FG, font=F)
        for t in (0, 0.25, 0.5, 0.75, 1.0):
            y = y1 - t * (y1 - y0)
            d.line((x0, y, x1, y), fill=BAD if t == 0.25 else GRID)
            d.text((x0 - 45, y - 7), f"{t:.2f}", fill=DIM, font=FS)
        gw = (x1 - x0) / max(1, len(keys))
        for i, k in enumerate(keys):
            for j, (src, col) in enumerate(((bp, BASE), (cp, CAND))):
                v = src.get(k)
                if not v:
                    continue
                bx = x0 + i * gw + gw * (0.12 + 0.38 * j)
                for q, val in enumerate((v["wer"], v["local_errors"] / v["ref_words"] if v["ref_words"] else 0)):
                    xa = bx + q * gw * 0.18
                    d.rectangle((xa, y1 - min(val, 1.0) * (y1 - y0), xa + gw * 0.16, y1), fill=col if q == 0 else None, outline=col, width=2)
                    d.text((xa, y1 - min(val, 1.0) * (y1 - y0) - 15), f"{val:.2f}", fill=FG, font=FS)
            d.text((x0 + i * gw + 10, y1 + 6), f"{k} ({bp.get(k, cp.get(k, {})).get('ref_words', 0)} w)", fill=DIM, font=FS)
    im.save(HERE / "fig-overlap-pairs.png")


def musts() -> None:
    """Overlap / clean WER and DER, baseline vs P1, scored two ways: as the harness scores today, and
    with the words spoken while Akashic was paused removed (overlap shown order-free, ovl_local)."""
    base = json.loads((HERE.parent / "baseline" / "detail.json").read_text(encoding="utf-8"))
    bnp = json.loads((HERE / "baseline-rescore" / "detail.json").read_text(encoding="utf-8")).get("runs_nopause", {})
    cand = json.loads((HERE / "detail.json").read_text(encoding="utf-8"))
    ways = (("harness today", base["runs"], cand["runs"], lambda r: r["M9.1"]["overlap"]["wer"]),
            ("paused words removed, overlap order-free", bnp, cand.get("runs_nopause", {}), lambda r: r["overlap_local_wer"]))
    w, h = 1500, 760
    im = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(im)
    d.text((40, 16), "P1 musts: baseline grey, P1 blue; red line = overlap must 0.25 (clean and DER: must not rise)", fill=FG, font=FT)
    for r, (title, b_runs, c_runs, ovl) in enumerate(ways):
        x0, y0, x1, y1 = 90, 90 + r * 350, 1440, 360 + r * 350
        d.text((x0 - 50, y0 - 30), title, fill=FG, font=F)
        for t in (0, 0.25, 0.5, 0.75):
            y = y1 - t / 0.8 * (y1 - y0)
            d.line((x0, y, x1, y), fill=GRID)
            d.text((x0 - 45, y - 7), f"{t:.2f}", fill=DIM, font=FS)
        y25 = y1 - 0.25 / 0.8 * (y1 - y0)
        cols = [(f"{m} {run}", run, m) for run in ("std", "day") for m in ("overlap", "clean", "DER")]
        gw = (x1 - x0) / len(cols)
        for i, (lab, run, m) in enumerate(cols):
            for j, (src, col) in enumerate(((b_runs, BASE), (c_runs, CAND))):
                rr = src.get(run)
                if not rr:
                    continue
                v = ovl(rr) if m == "overlap" else rr["M9.1"]["no_overlap"]["wer"] if m == "clean" else rr["M8.2"]["der"]
                xa = x0 + i * gw + gw * (0.2 + 0.32 * j)
                d.rectangle((xa, y1 - min(v, 0.8) / 0.8 * (y1 - y0), xa + gw * 0.28, y1), fill=col)
                d.text((xa, y1 - min(v, 0.8) / 0.8 * (y1 - y0) - 15), f"{v:.3f}", fill=FG, font=FS)
            if m == "overlap":
                d.line((x0 + i * gw + 4, y25, x0 + (i + 1) * gw - 4, y25), fill=BAD, width=2)
            d.text((x0 + i * gw + 20, y1 + 6), lab, fill=DIM, font=FS)
    im.save(HERE / "fig-musts.png")


if __name__ == "__main__":
    cancellation()
    if (HERE / "detail.json").is_file():
        pairs()
        if (HERE / "baseline-rescore" / "detail.json").is_file():
            musts()
