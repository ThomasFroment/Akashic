"""C1 single-call strip mosaic with a budgeted per-strip scale (luma Lanczos), refuter fixes applied.

read(img, ctx):
  1. region = find_text_box crop (live) or the whole image (deferred / replay), as today;
  2. ink = |horizontal luma gradient| > INK_T (same cue as find_text_box, at full resolution, so
     horizontal rules vanish and vertical rules become thin columns that the cut isolates);
  3. recursive XY-cut: column gutters >= GUTTER px first, then row runs (gaps <= 1 px merged; a
     thin run within ACCENT_GAP px of a neighbour is merged into it so accents stay attached); the
     leaves are strips, trimmed to their ink;
  4. small := run height <= SMALL_H device px; one mosaic: strips stacked top to bottom in reading
     order, left aligned, edge-colour gaps max(6, 0.5 h_out), large strips at 1x, small strips at s;
  5. s = largest value <= S_MAX (and <= MaxImageDimension) whose predicted cost fits the allowance:
     predicted OCR ms = C0 + BLANK x blank Mpix + DENSE x strip Mpix (fitted on the harness corpus,
     fit_cost.py), plus preprocessing PRE_IN x input Mpix + PRE_OUT x upscaled Mpix; allowance =
     today's predicted ms on the crop + ALLOW_MS; removed whitespace is credited only at BLANK;
  6. one OCR call on the mosaic (luma, Lanczos); word rects mapped back to window pixels by strip;
     words regrouped into blocks like WindowsOcr.recognize and rendered with render_blocks.
  No small strips, or s < S_MIN: today's call on today's image (identical to baseline).
STATS collects numbers per call (no text) for the report.
"""

from __future__ import annotations

import atexit
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

from akashic.capture import imaging
from akashic.capture.ports import Rect, TextBlock
from akashic.capture.text import render_blocks

HERE = Path(__file__).resolve().parent
INK_T = 32  # luma step that counts as ink (find_text_box uses 48 on a half-size frame)
GUTTER = 14  # empty columns (device px at scale 1) that split a block into columns
ACCENT_GAP = 3  # a thin run this close to a neighbour belongs to it (accents, i dots)
SMALL_H = 15  # device px: a strip at most this tall is small
S_MAX = 3.0
S_MIN = 1.2
ALLOW_MS = 40.0
MARGIN = 8
MAX_DIM = 10000  # OcrEngine.max_image_dimension on the target PC
COST = {"c0": 20.0, "blank": 15.0, "dense": 50.0, "pre_in": 10.0, "pre_out": 8.0}  # ms, ms/Mpix
_cost_file = HERE / "cost.json"
if _cost_file.is_file():
    COST.update(json.loads(_cost_file.read_text(encoding="utf-8"))["model"])

STATS: list[dict] = []


@atexit.register
def _dump_stats() -> None:
    if STATS:
        (HERE / "c1_stats.json").write_text(json.dumps(STATS), encoding="utf-8")


class Strip:
    __slots__ = ("x0", "x1", "y0", "y1")

    def __init__(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


def ink_mask(lum: np.ndarray) -> np.ndarray:
    d = np.abs(np.diff(lum.astype(np.int16), axis=1)) > INK_T
    out = np.zeros(lum.shape, dtype=bool)
    out[:, 1:] |= d
    out[:, :-1] |= d
    return out


def _runs(on: np.ndarray, max_gap: int) -> list[list[int]]:
    """[start, end) runs of True in a 1-D array, gaps of <= max_gap False merged."""
    idx = np.flatnonzero(on)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > max_gap + 1)
    starts = np.concatenate(([idx[0]], idx[breaks + 1]))
    ends = np.concatenate((idx[breaks], [idx[-1]])) + 1
    return [[int(a), int(b)] for a, b in zip(starts, ends, strict=True)]


def _attach_thin(runs: list[list[int]]) -> list[list[int]]:
    """Merge a thin run (accent row, i dots) into the closer neighbour when the gap is small."""
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for i, (a, b) in enumerate(runs):
            h = b - a
            nb = [j for j in (i - 1, i + 1) if 0 <= j < len(runs)]
            cand = []
            for j in nb:
                gap = runs[j][0] - b if j > i else a - runs[j][1]
                hj = runs[j][1] - runs[j][0]
                if gap <= ACCENT_GAP and h <= max(3, 0.35 * hj):
                    cand.append((gap, -hj, j))
            if cand:
                _, _, j = min(cand)
                runs[j] = [min(a, runs[j][0]), max(b, runs[j][1])]
                del runs[i]
                changed = True
                break
    return runs


def xy_cut(ink: np.ndarray, x0: int, y0: int, x1: int, y1: int, gutter: int, out: list[Strip], depth: int = 0) -> None:
    sub = ink[y0:y1, x0:x1]
    cols = sub.any(axis=0)
    if not cols.any():
        return
    cruns = _runs(cols, gutter - 1)
    if len(cruns) > 1 and depth < 40:
        for a, b in cruns:
            xy_cut(ink, x0 + a, y0, x0 + b, y1, gutter, out, depth + 1)
        return
    a, b = cruns[0]
    x0, x1 = x0 + a, x0 + b  # trim columns
    sub = ink[y0:y1, x0:x1]
    rruns = _attach_thin(_runs(sub.any(axis=1), 1))
    if len(rruns) > 1 and depth < 40:
        for a, b in rruns:
            xy_cut(ink, x0, y0 + a, x1, y0 + b, gutter, out, depth + 1)
        return
    a, b = rruns[0]
    if x1 - x0 <= 4 and b - a > 3 * (x1 - x0):  # a vertical rule, not text
        return
    out.append(Strip(x0, y0 + a, x1, y0 + b))


def find_strips(lum: np.ndarray, scale: float) -> list[Strip]:
    ink = ink_mask(lum)
    out: list[Strip] = []
    xy_cut(ink, 0, 0, lum.shape[1], lum.shape[0], max(8, round(GUTTER * scale)), out)
    return out


def _gap(h_out: float) -> int:
    return max(6, round(0.5 * h_out))


def layout(strips: list[Strip], s: float, pad: int = 1) -> tuple[int, int, list[tuple[int, int, float]], float]:
    """Mosaic size, per strip (x, y, factor) of its padded box, and dense (strip) area in px."""
    places, y, wmax, dense = [], MARGIN, 0, 0.0
    prev_h = 0.0
    for st in strips:
        f = s if st.h <= SMALL_H else 1.0
        ho, wo = (st.h + 2 * pad) * f, (st.w + 2 * pad) * f
        if places:
            y += _gap(max(prev_h, ho))
        places.append((MARGIN, y, f))
        y += round(ho)
        wmax = max(wmax, round(wo))
        dense += ho * wo
        prev_h = ho
    return wmax + 2 * MARGIN, y + MARGIN, places, dense


def predict(w: int, h: int, dense: float) -> float:
    return COST["c0"] + COST["blank"] * (w * h - dense) / 1e6 + COST["dense"] * dense / 1e6


def choose_scale(strips: list[Strip], crop_w: int, crop_h: int) -> tuple[float, float, float]:
    """(s, predicted ms today, predicted ms of the mosaic at s incl. preprocessing)."""
    dense_today = float(sum((st.w + 2) * (st.h + 2) for st in strips))
    today = predict(crop_w, crop_h, dense_today)
    allow = today + ALLOW_MS
    small_in = sum((st.w + 2) * (st.h + 2) for st in strips if st.h <= SMALL_H)

    def cost(s: float) -> float:
        w, h, _, dense = layout(strips, s)
        pre = COST["pre_in"] * crop_w * crop_h / 1e6 + COST["pre_out"] * small_in * s * s / 1e6
        return predict(w, h, dense) + pre

    def fits_dim(s: float) -> bool:
        w, h, _, _ = layout(strips, s)
        return max(w, h) <= MAX_DIM

    lo, hi = 1.0, S_MAX
    if cost(hi) <= allow and fits_dim(hi):
        return hi, today, cost(hi)
    for _ in range(18):
        mid = (lo + hi) / 2
        if cost(mid) <= allow and fits_dim(mid):
            lo = mid
        else:
            hi = mid
    return lo, today, cost(lo)


def build_mosaic(lum: np.ndarray, strips: list[Strip], s: float, pad: int = 1):
    W, H, places, _ = layout(strips, s, pad)
    canvas = np.zeros((H, W), dtype=np.uint8)
    boxes = []  # (strip, x, y, w_out, h_out, f, sx0, sy0) in mosaic coords; sx0/sy0 = source origin
    rows_bg = []
    for st, (x, y, f) in zip(strips, places, strict=True):
        sx0, sy0 = max(0, st.x0 - pad), max(0, st.y0 - pad)
        sx1, sy1 = min(lum.shape[1], st.x1 + pad), min(lum.shape[0], st.y1 + pad)
        piece = lum[sy0:sy1, sx0:sx1]
        border = np.concatenate((piece[0], piece[-1], piece[:, 0], piece[:, -1]))
        bg = int(np.bincount(border, minlength=256).argmax())
        wo, ho = max(1, round(piece.shape[1] * f)), max(1, round(piece.shape[0] * f))
        im = Image.fromarray(piece)
        if f != 1.0:
            im = im.resize((wo, ho), Image.LANCZOS)
        boxes.append((st, x, y, wo, ho, f, sx0, sy0))
        rows_bg.append((y, ho, bg, np.asarray(im)))
    # background: each strip's band (incl. half of each neighbouring gap) takes its edge colour
    for i, (y, ho, bg, arr) in enumerate(rows_bg):
        top = 0 if i == 0 else (rows_bg[i - 1][0] + rows_bg[i - 1][1] + y) // 2
        bot = H if i == len(rows_bg) - 1 else (y + ho + rows_bg[i + 1][0]) // 2
        canvas[top:bot, :] = bg
        canvas[y : y + arr.shape[0], MARGIN : MARGIN + arr.shape[1]] = arr
    return Image.fromarray(canvas), boxes


def ocr_words(ocr, img: Image.Image) -> list[list[tuple[str, float, float, float, float]]]:
    """OcrLines as lists of (text, x, y, w, h) in img pixels. Same bitmap path as WindowsOcr."""
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.security.cryptography import CryptographicBuffer

    bgra = np.asarray(img.convert("RGBA"))[:, :, [2, 1, 0, 3]].tobytes()
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        CryptographicBuffer.create_from_byte_array(bgra), BitmapPixelFormat.BGRA8, img.width, img.height,
        BitmapAlphaMode.PREMULTIPLIED)
    result = ocr._loop.run_until_complete(ocr._engine.recognize_async(bitmap))
    lines = []
    for line in result.lines:
        ws = []
        for w in line.words:
            r = w.bounding_rect
            ws.append((w.text, r.x, r.y, r.width, r.height))
        lines.append(ws)
    return lines


def _blocks(words: list[tuple[str, float, float, float, float]]) -> list[TextBlock]:
    """WindowsOcr.recognize's grouping: split a line's words at gaps > max(24, 2 h)."""
    out, group, right = [], [], 0.0
    for wd in sorted(words, key=lambda t: t[1]):
        _, x, _, ww, hh = wd
        if group and x - right > max(24, hh * 2):
            out.append(group)
            group = []
        group.append(wd)
        right = x + ww
    if group:
        out.append(group)
    return [TextBlock(" ".join(t[0] for t in g), Rect(int(min(t[1] for t in g)), int(min(t[2] for t in g)),
                                                      int(max(t[1] + t[3] for t in g)), int(max(t[2] + t[4] for t in g)))) for g in out]


def mosaic_text(ocr, mosaic: Image.Image, boxes, ox: int, oy: int, stat: dict) -> str:
    lines = ocr_words(ocr, mosaic)
    ys = np.array([b[2] for b in boxes] + [10**9])
    blocks: list[TextBlock] = []
    spans = 0
    for ln in lines:
        per: dict[int, list] = {}
        for text, x, y, w, h in ln:
            cy = y + h / 2
            j = int(np.searchsorted(ys, cy, side="right")) - 1
            j = min(max(j, 0), len(boxes) - 1)
            # nearest strip by vertical distance (the word centre may fall in a gap)
            cands = [k for k in (j - 1, j, j + 1) if 0 <= k < len(boxes)]
            j = min(cands, key=lambda k: max(0, boxes[k][2] - cy, cy - (boxes[k][2] + boxes[k][4])))
            st, bx, by, wo, ho, f, sx0, sy0 = boxes[j]
            mx = ox + sx0 + (x - bx) / f
            my = oy + sy0 + (y - by) / f
            per.setdefault(j, []).append((text, mx, my, w / f, h / f))
        if len(per) > 1:
            spans += 1
        for ws in per.values():
            blocks += _blocks(ws)
    stat["ocr_lines"] = len(lines)
    stat["span_lines"] = spans
    return render_blocks(blocks)


def read(img: Image.Image, ctx) -> str:
    if ctx.path == "live":
        box = imaging.find_text_box(img)
        if box is None:
            return ""
        ox, oy = box.left, box.top
        region = img.crop((box.left, box.top, box.right, box.bottom))
    else:
        ox, oy, region = 0, 0, img
    lum = np.asarray(region.convert("L"))
    strips = find_strips(lum, ctx.scale)
    small = [st for st in strips if st.h <= SMALL_H]
    stat = {"path": ctx.path, "scale": ctx.scale, "crop_mpix": region.width * region.height / 1e6,
            "strips": len(strips), "small": len(small)}
    STATS.append(stat)
    if not small:
        stat["s"] = 1.0
        stat["mode"] = "today"
        return ctx.ocr.recognize(region)
    s, today, pred = choose_scale(strips, region.width, region.height)
    stat.update(s=s, pred_today=today, pred=pred)
    if s < S_MIN:
        stat["mode"] = "today"
        return ctx.ocr.recognize(region)
    mosaic, boxes = build_mosaic(lum, strips, s)
    stat.update(mode="mosaic", mosaic_mpix=mosaic.width * mosaic.height / 1e6)
    return mosaic_text(ctx.ocr, mosaic.convert("RGB"), boxes, ox, oy, stat)


def store(img: Image.Image, ctx) -> bytes:
    return imaging.encode_webp(img)


def load(data: bytes, ctx) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")
