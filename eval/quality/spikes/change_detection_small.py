"""Does Akashic's frame-change test (`imaging.signature` + `imaging.differs`) see small UI changes?

In memory, no window. A synthetic monitor frame (1440p at 100 %, 4K at 125 %) with page-like
content, dark and light; each case adds one small change and asks `differs(before, after)`:
one-word chat replies ("ok", "gg", "lol") at 13-16 CSS px, a short sentence, a notification badge,
and a text caret (1 px wide at 100 %, 2 px at 125 %) that should NOT count as a change.

    uv run python eval/quality/spikes/change_detection_small.py <out.json>
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from akashic.capture import imaging

FONT = "C:/Windows/Fonts/segoeui.ttf"
MONITORS = {"1440p@100%": (2560, 1440, 1.0), "4K@125%": (3840, 2160, 1.25)}
THEMES = {"dark": ((32, 33, 36), (220, 221, 222)), "light": ((255, 255, 255), (32, 33, 36))}


def base(w: int, h: int, scale: float, theme: str) -> Image.Image:
    bg, fg = THEMES[theme]
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, round(14 * scale))
    rng = np.random.default_rng(1)
    for i in range(int(h / (24 * scale)) - 4):  # a page of text lines on the left two thirds
        n = int(rng.integers(30, 110))
        d.text((round(40 * scale), round(20 * scale + i * 24 * scale)), "lorem ipsum dolor " * (n // 18), font=font, fill=fg)
    return img


def changed(img: Image.Image, case: str, scale: float, theme: str, dx: int = 0, dy: int = 0) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    bg, fg = THEMES[theme]
    x, y = round(img.width * 0.72) + dx, round(img.height * 0.5) + dy
    d.rectangle((x - 10, y - 10, x + round(400 * scale), y + round(40 * scale)), fill=bg)  # an empty chat line
    before = out.copy()
    kind, *rest = case.split(":")
    if kind == "word":
        word, px = rest[0], int(rest[1])
        d.text((x, y), word, font=ImageFont.truetype(FONT, round(px * scale)), fill=fg)
    elif kind == "line":
        d.text((x, y), "on se retrouve ce soir ?", font=ImageFont.truetype(FONT, round(14 * scale)), fill=fg)
    elif kind == "badge":
        r = round(8 * scale)
        d.ellipse((x, y, x + 2 * r, y + 2 * r), fill=(237, 66, 69))
    elif kind == "caret":
        cw = 1 if scale == 1.0 else 2
        d.rectangle((x, y, x + cw - 1, y + round(18 * scale)), fill=fg)
    return before, out


def main() -> None:
    cases = [f"word:{w}:{px}" for w in ("ok", "gg", "lol") for px in (13, 14, 16)] + ["line", "badge", "caret"]
    rows = []
    for mon, (w, h, scale) in MONITORS.items():
        for theme in THEMES:
            page = base(w, h, scale, theme)
            for case in cases:
                before, after = changed(page, case, scale, theme)
                a, b = imaging.signature(before), imaging.signature(after)
                moved = np.abs(a.gray.astype(np.int16) - b.gray.astype(np.int16)) > imaging.PIXEL_DELTA
                tiles = {}
                for t in (8, 16, 32):  # max count of moved pixels in one t x t tile of the reduced frame
                    hh, ww = moved.shape[0] // t * t, moved.shape[1] // t * t
                    tiles[t] = int(moved[:hh, :ww].reshape(hh // t, t, ww // t, t).sum(axis=(1, 3)).max())
                rows.append({"monitor": mon, "theme": theme, "case": case, "differs": imaging.differs(a, b),
                             "moved_small_px": int(moved.sum()), "needed_more_than": round(imaging.CHANGED_FRACTION * moved.size, 1),
                             "max_tile_8": tiles[8], "max_tile_16": tiles[16], "max_tile_32": tiles[32]})
    timing = time_compare()
    sweep = placement_sweep(cases)
    Path(sys.argv[1]).write_text(json.dumps({"rows": rows, "timing_ms_4k": timing, "placement_sweep": sweep}, indent=1), encoding="utf-8")
    print("4K timing (ms, median of 50):", timing)
    for mon in MONITORS:
        for key in ("moved_small_px", "max_tile_8", "max_tile_16", "max_tile_32"):
            words = [r[key] for r in rows if r["monitor"] == mon and r["case"].startswith(("word", "badge"))]
            caret = [r[key] for r in rows if r["monitor"] == mon and r["case"] == "caret"]
            print(f"{mon:<11} {key:<15} words+badges min={min(words):>3}  caret max={max(caret):>3}")
    print("missed by differs:", sum(1 for r in rows if not r["differs"] and r["case"] != "caret"), "of", sum(1 for r in rows if r["case"] != "caret"))


def shape(moved: np.ndarray) -> dict:
    """Moved-pixel count, bounding box, and the largest count in one 16x16 tile (edges padded),
    over two tile grids offset by half a tile."""
    ys, xs = np.nonzero(moved)
    tile = 0
    for off in (0, 8):
        m = np.pad(moved[off:, off:], ((0, -moved[off:].shape[0] % 16), (0, -moved[:, off:].shape[1] % 16)))
        h, w = m.shape
        tile = max(tile, int(m.reshape(h // 16, 16, w // 16, 16).sum(axis=(1, 3)).max()))
    return {"px": int(moved.sum()), "w": int(xs.max() - xs.min() + 1) if xs.size else 0,
            "h": int(ys.max() - ys.min() + 1) if ys.size else 0, "tile16": tile}


def placement_sweep(cases: list[str]) -> dict:
    """Every 4x4 sub-block placement of each change (a 1/4 frame pixel is a 4x4 block): the
    min and max of each shape measure per monitor and case."""
    out = {}
    for mon, (w, h, scale) in MONITORS.items():
        for theme in THEMES:
            page = base(w, h, scale, theme)
            for case in cases:
                vals = []
                for dx in range(4):
                    for dy in range(4):
                        before, after = changed(page, case, scale, theme, dx, dy)
                        a, b = imaging.signature(before), imaging.signature(after)
                        vals.append(shape(np.abs(a.gray.astype(np.int16) - b.gray.astype(np.int16)) > imaging.PIXEL_DELTA))
                key = f"{mon}|{case}"
                prev = out.get(key, {})
                out[key] = {m: [min([v[m] for v in vals] + prev.get(m, [10**9])[:1]), max([v[m] for v in vals] + prev.get(m, [0])[1:])]
                            for m in ("px", "w", "h", "tile16")}
    for key, v in out.items():
        print(f"{key:<24} " + "  ".join(f"{m}={v[m][0]}-{v[m][1]}" for m in v))
    return out


def time_compare() -> dict:
    """Median ms on a 4K page frame: signature, current differs, and a 16x16 per-tile max count."""
    before, after = changed(base(3840, 2160, 1.25, "dark"), "word:ok:14", 1.25, "dark")
    a = imaging.signature(before)
    out = {}
    for name, fn in {
        "signature": lambda: imaging.signature(after),
        "differs": lambda: imaging.differs(a, imaging.signature(after)) and None,
    }.items():
        out[name] = _median(fn)
    b = imaging.signature(after)

    def tile_max() -> int:
        moved = np.abs(a.gray.astype(np.int16) - b.gray.astype(np.int16)) > imaging.PIXEL_DELTA
        hh, ww = moved.shape[0] // 16 * 16, moved.shape[1] // 16 * 16
        return int(moved[:hh, :ww].reshape(hh // 16, 16, ww // 16, 16).sum(axis=(1, 3)).max())

    def fraction() -> bool:
        return float((np.abs(a.gray.astype(np.int16) - b.gray.astype(np.int16)) > imaging.PIXEL_DELTA).mean()) > imaging.CHANGED_FRACTION

    out["compare_fraction"] = _median(fraction)
    out["compare_tile_max"] = _median(tile_max)
    return out


def _median(fn) -> float:
    times = []
    for _ in range(50):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    return round(sorted(times)[25], 3)


main()
