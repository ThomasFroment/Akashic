"""C3: today's native OCR call, then one capped extra call that re-reads the small lines at 2-3x.

read(img, ctx):
1. Native pass exactly as today (live: find_text_box crop; deferred/replay: whole image), but keeping
   word boxes (same engine, same thumbnail rule, same word grouping as WindowsOcr.recognize).
2. Small units: native blocks whose median word height is below H_SMALL (calibrated on the corpus,
   calib.py), plus ink strips inside find_text_box cells that no native word box covers.
3. Rank (uncovered strips first, then smallest height), look each up in a blake2b(pixels) cache,
   pack the rest into one mosaic (3x Lanczos, 2x for estimated 11-12 px; edge-colour padding) while
   the cost model predicts <= CAP_MS for the extra call. One OCR call on the mosaic.
4. Refined blocks replace the native blocks they cover; native blocks without a refined counterpart
   stay (M3.3). Text rendered with Akashic's render_blocks, like today.

Stats (counts only, no text) go to STATS and are written at exit to $C3_STATS if set.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import time
from collections import OrderedDict


import numpy as np
from PIL import Image

from akashic.capture import imaging
from akashic.capture.ports import Rect, TextBlock
from akashic.capture.text import render_blocks

# --- calibrated constants (calib.py; see calib.json) ---------------------------------------------
H_PER_PX = 0.75  # native word-box height / device font px at >= 12 px (calib.json: 9 px -> 7, 11 -> 8, 12 -> 9, 12.5-14 -> 10)
H_SMALL = 9.5  # word height <= 9 px: text up to 12 device px (calib.json)
H_2X = 7.5  # word height 8-9 (11-12 px) -> 2x, <= 7 (9-10 px) -> 3x
COST_A, COST_TOTAL, COST_UNIT, COST_NATIVE = -5.87, 7.70, 68.56, 0.46  # ms; per mosaic Mpix, per tile Mpix, per native kpx (fitcost.json px+native)
CAP_MS = float(os.environ.get("C3_CAP_MS", 30))  # predicted ms of the extra call; 40 measured dms_p95 +54.5, 35 +50.6, 30 +35.7 (timing_only.py)
CACHE_BYTES = int(os.environ.get("C3_CACHE_BYTES", 3 * 1024 * 1024))
LUMA = os.environ.get("C3_LUMA", "0") == "1"
INK_OVER_WORD = 1.3  # ink-strip height / word-box height (calib.json)
PAD_X = 8
PAD_Y = float(os.environ.get("C3_PAD_Y", 0.2))  # vertical padding per tile side, share of the scaled unit height
S_SMALL = int(os.environ.get("C3_S_SMALL", 2))  # scale for text estimated below 11 px
SHELF = os.environ.get("C3_SHELF", "1") == "1"  # pack narrow tiles side by side
MAX_DIM = 10000

STATS: list[dict] = []


class Unit:
    """A line-ish rectangle to re-read. h = estimated native word height; kind native|uncovered."""

    __slots__ = ("h", "kind", "native", "x0", "x1", "y0", "y1")

    def __init__(self, x0: int, y0: int, x1: int, y1: int, h: float, kind: str, native: tuple[int, ...] = ()) -> None:
        self.x0, self.y0, self.x1, self.y1, self.h, self.kind, self.native = x0, y0, x1, y1, h, kind, native


# --- OCR with word boxes ---------------------------------------------------------------------------


def ocr_words(ocr, image: Image.Image) -> list[list[tuple[str, float, float, float, float]]]:
    """OcrLines as lists of (text, x, y, w, h) in `image` pixels; same input handling as recognize()."""
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.security.cryptography import CryptographicBuffer

    limit = OcrEngine.max_image_dimension
    f = 1.0
    if max(image.size) > limit:
        image = image.copy()
        w0 = image.width
        image.thumbnail((limit, limit))
        f = w0 / image.width
    bgra = np.asarray(image.convert("RGBA"))[:, :, [2, 1, 0, 3]].tobytes()
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        CryptographicBuffer.create_from_byte_array(bgra), BitmapPixelFormat.BGRA8, image.width, image.height,
        BitmapAlphaMode.PREMULTIPLIED)
    result = ocr._loop.run_until_complete(ocr._engine.recognize_async(bitmap))
    out = []
    for line in result.lines:
        ws = []
        for w in line.words:
            r = w.bounding_rect
            ws.append((w.text, r.x * f, r.y * f, r.width * f, r.height * f))
        if ws:
            out.append(ws)
    return out


def group_blocks(words: list[tuple[str, float, float, float, float]]) -> list[list[tuple]]:
    """WindowsOcr.recognize's split of one OcrLine into blocks at wide gaps."""
    groups, group, right = [], [], 0.0
    for wd in words:
        _, x, _, w, h = wd
        if group and x - right > max(24, h * 2):
            groups.append(group)
            group = []
        group.append(wd)
        right = x + w
    if group:
        groups.append(group)
    return groups


def to_block(words: list[tuple]) -> TextBlock:
    return TextBlock(" ".join(w[0] for w in words), Rect(int(min(w[1] for w in words)), int(min(w[2] for w in words)),
                                                         int(max(w[1] + w[3] for w in words)), int(max(w[2] + w[4] for w in words))))


# --- small units ------------------------------------------------------------------------------------


def text_cells(gray_half: np.ndarray) -> np.ndarray:
    """find_text_box's cell grid (cells of 16 half-size px = 32 device px)."""
    c = imaging.TEXT_CELL
    edges = np.abs(np.diff(gray_half.astype(np.int16), axis=1)) > imaging.TEXT_EDGE
    rows, cols = -(-edges.shape[0] // c) * c, -(-edges.shape[1] // c) * c
    padded = np.zeros((rows, cols), dtype=bool)
    padded[: edges.shape[0], : edges.shape[1]] = edges
    dens = padded.reshape(rows // c, c, cols // c, c).mean(axis=(1, 3))
    return (dens >= imaging.TEXT_MIN_DENSITY) & (dens <= imaging.TEXT_MAX_DENSITY)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) runs of True in a 1-D bool array."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.nonzero(d == 1)[0].tolist(), np.nonzero(d == -1)[0].tolist(), strict=True))


def uncovered_units(img: Image.Image, covered: list[tuple[int, int, int, int]]) -> list[Unit]:
    """Ink strips in text cells that no native word box covers (row profile of horizontal edges)."""
    if img.width < 8 or img.height < 8:
        return []
    cells = text_cells(np.asarray(img.reduce(2).convert("L")))
    if not cells.any():
        return []
    cs = 2 * imaging.TEXT_CELL
    ys, xs = np.nonzero(cells)
    bx0, by0 = int(xs.min()) * cs, int(ys.min()) * cs
    bx1, by1 = min(img.width, (int(xs.max()) + 1) * cs), min(img.height, (int(ys.max()) + 1) * cs)
    gray = np.asarray(img.crop((bx0, by0, bx1, by1)).convert("L"))
    H, W = gray.shape
    sub_cells = cells[by0 // cs : -(-by1 // cs), bx0 // cs : -(-bx1 // cs)]
    rows, cols = sub_cells.shape
    pad = np.zeros((rows * cs, cols * cs), dtype=bool)
    a, b = gray[:, 1:], gray[:, :-1]
    pad[:H, 1:W] = (np.maximum(a, b) - np.minimum(a, b)) > imaging.TEXT_EDGE
    pad.reshape(rows, cs, cols, cs)[...] &= sub_cells[:, None, :, None]
    for x0, y0, x1, y1 in covered:
        pad[max(0, y0 - 2 - by0) : max(0, y1 + 2 - by0), max(0, x0 - 2 - bx0) : max(0, x1 + 2 - bx0)] = False
    live = np.count_nonzero(pad.reshape(rows, cs, cols, cs), axis=(1, 3)) >= imaging.TEXT_MIN_DENSITY / 2 * cs * cs
    edges = pad[:H, :W]
    units: list[Unit] = []
    # regions: runs of live cells per cell row, joined across rows when their columns overlap
    regions: list[list[int]] = []  # [r0, r1, c0, c1] in cells
    open_: list[list[int]] = []
    for r in range(rows):
        nxt = []
        for c0, c1 in _runs(live[r]):
            hit = next((g for g in open_ if g[2] < c1 + 1 and c0 < g[3] + 1), None)
            if hit is not None:
                hit[1], hit[2], hit[3] = r + 1, min(hit[2], c0), max(hit[3], c1)
                if hit not in nxt:
                    nxt.append(hit)
            else:
                g = [r, r + 1, c0, c1]
                regions.append(g)
                nxt.append(g)
        open_ = nxt
    hmax = INK_OVER_WORD * H_SMALL + 2
    for r0, r1, c0, c1 in regions:
        ry0, ry1, rx0, rx1 = r0 * cs, min(H, r1 * cs), c0 * cs, min(W, c1 * cs)
        sub = edges[ry0:ry1, rx0:rx1]
        prof = sub.any(axis=1)
        prof[1:-1] |= prof[:-2] & prof[2:]  # one blank row inside a line (accents) stays in it
        for s, e in _runs(prof):
            h = e - s
            if 4 <= h <= hmax:
                colp = np.nonzero(sub[s:e].any(axis=0))[0]
                if colp.size and colp[-1] - colp[0] >= 2 * h:
                    units.append(Unit(bx0 + rx0 + int(colp[0]) - 3, by0 + ry0 + s - 2, bx0 + rx0 + int(colp[-1]) + 4,
                                      by0 + ry0 + e + 2, h / INK_OVER_WORD, "uncovered"))
    return units


# --- mosaic ---------------------------------------------------------------------------------------


def scale_of(u: Unit) -> int:
    return 2 if u.h >= H_2X else S_SMALL


def edge_colour(a: np.ndarray) -> tuple[int, ...]:
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    return tuple(int(v) for v in np.median(border, axis=0))


def predicted_ms(total_px: float, unit_px: float, native_px: float) -> float:
    return COST_A + COST_TOTAL * total_px / 1e6 + COST_UNIT * unit_px / 1e6 + COST_NATIVE * native_px / 1e3


def tile_geometry(u: Unit) -> tuple[int, int, int, int]:
    """(tile width, tile height, pad_x, pad_y) at the unit's scale."""
    s = scale_of(u)
    w, h = (u.x1 - u.x0) * s, (u.y1 - u.y0) * s
    pad_y = max(5, round(PAD_Y * h))
    pad_x = PAD_X if not SHELF else max(PAD_X, h)
    return w + 2 * pad_x, h + 2 * pad_y, pad_x, pad_y


def layout(geo: list[tuple[int, int, int, int]], width: int) -> tuple[list[tuple[int, int]], int, int]:
    """Tile origins. Single column (SHELF off) or shelves of `width` filled left to right."""
    if not SHELF:
        ys = np.cumsum([0] + [g[1] for g in geo])
        return [(0, int(y)) for y in ys[:-1]], max(g[0] for g in geo), int(ys[-1])
    pos, x, y, shelf_h, W = [], 0, 0, 0, 0
    for tw, th, _, _ in geo:
        if x and x + tw > width:
            y, x, shelf_h = y + shelf_h, 0, 0
        pos.append((x, y))
        x += tw
        shelf_h = max(shelf_h, th)
        W = max(W, x)
    return pos, W, y + shelf_h


def plan(units: list[Unit], cap_ms: float, width: int) -> list[Unit]:
    chosen: list[Unit] = []
    geo: list[tuple[int, int, int, int]] = []
    for u in units:
        g = tile_geometry(u)
        _, W, H = layout([*geo, g], width)
        nat = sum((v.x1 - v.x0) * (v.y1 - v.y0) for v in [*chosen, u])
        if H > MAX_DIM or W > MAX_DIM or predicted_ms(W * H, sum(t[0] * t[1] for t in [*geo, g]), nat) > cap_ms:
            continue  # a smaller one later may still fit
        chosen.append(u)
        geo.append(g)
    return chosen


def build_mosaic(img: Image.Image, units: list[Unit], width: int) -> tuple[Image.Image, list[tuple[int, ...]]]:
    """Mosaic image and per unit its tile (x0, y0, x1, y1, pad_x, pad_y, scale)."""
    geo = [tile_geometry(u) for u in units]
    pos, W, H = layout(geo, width)
    mode = "L" if LUMA else "RGB"
    src = img.convert("L") if LUMA else img
    tiles = []
    for u, (tw, th, pad_x, pad_y), (x, y) in zip(units, geo, pos, strict=True):
        crop = src.crop((u.x0, u.y0, u.x1, u.y1))
        a = np.asarray(crop)
        col = edge_colour(a if a.ndim == 3 else a[:, :, None])
        tiles.append((crop, col, x, y, tw, th, pad_x, pad_y, scale_of(u)))
    mos = Image.new(mode, (W, H), tiles[0][1] if mode == "RGB" else tiles[0][1][0])
    spans = []
    for crop, col, x, y, tw, th, pad_x, pad_y, s in tiles:
        mos.paste(col if mode == "RGB" else col[0], (x, y, x + tw, y + th))
        mos.paste(crop.resize((crop.width * s, crop.height * s), Image.LANCZOS), (x + pad_x, y + pad_y))
        spans.append((x, y, x + tw, y + th, pad_x, pad_y, s))
    return mos.convert("RGB"), spans


def unit_ink(img: Image.Image, u: Unit) -> int:
    """Horizontal-edge pixels in the unit at native resolution (a character-count proxy)."""
    g = np.asarray(img.crop((u.x0, u.y0, u.x1, u.y1)).convert("L"), dtype=np.int16)
    return int((np.abs(np.diff(g, axis=1)) > imaging.TEXT_EDGE).sum())


# --- cache ------------------------------------------------------------------------------------------


class Cache:
    def __init__(self, limit: int) -> None:
        self.limit, self.used, self.d = limit, 0, OrderedDict()

    @staticmethod
    def size(v) -> int:
        return 96 + sum(48 + len(t) * 2 for t, *_ in v)

    def get(self, k):
        v = self.d.get(k)
        if v is not None:
            self.d.move_to_end(k)
        return v

    def put(self, k, v) -> None:
        if self.limit <= 0:
            return
        if k in self.d:
            return
        self.d[k] = v
        self.used += self.size(v)
        while self.used > self.limit and self.d:
            _, old = self.d.popitem(last=False)
            self.used -= self.size(old)


CACHE = Cache(CACHE_BYTES)


def unit_key(img: Image.Image, u: Unit) -> bytes:
    crop = img.crop((u.x0, u.y0, u.x1, u.y1))
    return hashlib.blake2b(crop.tobytes() + f"{crop.size}{scale_of(u)}".encode(), digest_size=16).digest()


def _same_row(a: Unit, b: Unit) -> bool:
    ov = min(a.y1, b.y1) - max(a.y0, b.y0)
    if ov < 0.5 * min(a.y1 - a.y0, b.y1 - b.y0):
        return False
    gap = max(a.x0, b.x0) - min(a.x1, b.x1)
    return gap <= max(24, 3 * max(a.h, b.h))


def merge_rows(units: list[Unit], native: list[list[tuple]], size: tuple[int, int]) -> list[Unit]:
    """Join small pieces of one text row (native blocks around words native missed) into one line
    unit, and pull in the row's other native blocks, so a refined row replaces the whole row."""
    used = {i for u in units for i in u.native}
    pool = list(units)
    for i, g in enumerate(native):  # non-small native blocks may join a small row, never start one
        if i not in used:
            b = to_block(g).rect
            pool.append(Unit(b.left - 3, b.top - 2, b.right + 3, b.bottom + 2, float(np.median([w[4] for w in g])), "big", (i,)))
    n_small = len(units)
    parent = list(range(len(pool)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(pool)), key=lambda i: pool[i].y0)
    for a, i in enumerate(order):  # pairwise relation, union-find gives the rows; only y-neighbours can match
        for j in order[a + 1 :]:
            if pool[j].y0 >= pool[i].y1:
                break
            if (i < n_small or j < n_small) and _same_row(pool[i], pool[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[Unit]] = {}
    for i, u in enumerate(pool):
        groups.setdefault(find(i), []).append(u)
    out = []
    W, H = size
    for members in groups.values():
        if all(m.kind == "big" for m in members):
            continue
        small = [m for m in members if m.kind != "big"]
        out.append(Unit(max(0, min(m.x0 for m in members)), max(0, min(m.y0 for m in members)), min(W, max(m.x1 for m in members)),
                        min(H, max(m.y1 for m in members)), float(np.median([m.h for m in small])),
                        "uncovered" if any(m.kind == "uncovered" for m in members) else "native",
                        tuple(i for m in members for i in m.native)))
    return out


# --- read -------------------------------------------------------------------------------------------


def read(img: Image.Image, ctx) -> str:
    t0 = time.perf_counter()
    if img.mode != "RGB":
        img = img.convert("RGB")
    ox = oy = 0
    target = img
    if ctx.path == "live":
        box = imaging.find_text_box(img)
        if box is None:
            return ""
        ox, oy = box.left, box.top
        target = img.crop((box.left, box.top, box.right, box.bottom))
    lines = ocr_words(ctx.ocr, target)
    t1 = time.perf_counter()
    native: list[list[tuple]] = []  # blocks of words in img coordinates
    for ws in lines:
        for g in group_blocks(ws):
            native.append([(t, x + ox, y + oy, w, h) for t, x, y, w, h in g])
    units: list[Unit] = []
    for i, g in enumerate(native):
        hm = float(np.median([w[4] for w in g]))
        if hm < H_SMALL:
            b = to_block(g).rect
            units.append(Unit(max(0, b.left - 3), max(0, b.top - 2), min(img.width, b.right + 3),
                              min(img.height, b.bottom + 2), hm, "native", (i,)))
    covered = [(int(w[1]), int(w[2]), int(w[1] + w[3]), int(w[2] + w[4])) for g in native for w in g]
    region = img if ctx.path != "live" else target
    T = {"a": time.perf_counter()}
    unc = uncovered_units(region, [(a - ox, b - oy, c - ox, d - oy) for a, b, c, d in covered])
    for u in unc:
        u.x0, u.x1, u.y0, u.y1 = max(0, u.x0 + ox), min(img.width, u.x1 + ox), max(0, u.y0 + oy), min(img.height, u.y1 + oy)
    units += [u for u in unc if u.x1 > u.x0 + 4 and u.y1 > u.y0 + 3]
    T["unc"] = time.perf_counter()
    units = merge_rows(units, native, img.size)
    T["rows"] = time.perf_counter()
    units.sort(key=lambda u: (u.kind != "uncovered", u.h))
    stat = {"path": ctx.path, "w": img.width, "h": img.height, "small": len(units), "uncovered": len(unc),
            "cached": 0, "refined": 0, "capped": 0, "pred_ms": 0.0, "mosaic_mpx": 0.0, "ocr2_ms": 0.0}
    results: dict[int, list[tuple]] = {}  # unit index -> refined words in img coords
    todo = []
    for i, u in enumerate(units):
        k = unit_key(img, u)
        hit = CACHE.get(k)
        if hit is not None:
            results[i] = [[(t, u.x0 + x, u.y0 + y, w, h) for t, x, y, w, h in ln] for ln in hit]
            stat["cached"] += 1
        else:
            todo.append((i, u, k))
    T["keys"] = time.perf_counter()
    width = max((tile_geometry(u)[0] for _, u, _ in todo), default=0)
    chosen = plan([u for _, u, _ in todo], CAP_MS, width)
    chosen_ids = {id(u) for u in chosen}
    picked = [(i, u, k) for i, u, k in todo if id(u) in chosen_ids]
    stat["capped"] = len(todo) - len(picked)
    if picked:
        T["plan"] = time.perf_counter()
        mos, spans = build_mosaic(img, [u for _, u, _ in picked], width)
        geo = [tile_geometry(u) for _, u, _ in picked]
        stat["mosaic_mpx"] = mos.width * mos.height / 1e6
        stat["pred_ms"] = predicted_ms(mos.width * mos.height, sum(g[0] * g[1] for g in geo),
                                       sum((u.x1 - u.x0) * (u.y1 - u.y0) for _, u, _ in picked))
        per: dict[int, list[list[tuple]]] = {j: [] for j in range(len(picked))}
        t2 = T["mos"] = time.perf_counter()
        mos_lines = ocr_words(ctx.ocr, mos)
        stat["ocr2_ms"] = (time.perf_counter() - t2) * 1000
        stat["n_chars"] = sum(len(w[0]) for ws in mos_lines for w in ws)
        stat["tile_mpx"] = sum(g[0] * g[1] for g in geo) / 1e6
        stat["native_kpx"] = sum((u.x1 - u.x0) * (u.y1 - u.y0) for _, u, _ in picked) / 1e3
        for ws in mos_lines:
            parts: dict[int, list[tuple]] = {}
            for t, x, y, w, h in ws:
                cx, cy = x + w / 2, y + h / 2
                j = next((j for j, sp in enumerate(spans) if sp[0] <= cx < sp[2] and sp[1] <= cy < sp[3]), None)
                if j is None:
                    continue
                x0, y0, _, _, pad_x, pad_y, sc = spans[j]
                parts.setdefault(j, []).append((t, (x - x0 - pad_x) / sc, (y - y0 - pad_y) / sc, w / sc, h / sc))
            for j, part in parts.items():
                per[j].append(part)
        for j, (i, u, k) in enumerate(picked):
            CACHE.put(k, per[j])
            results[i] = [[(t, u.x0 + x, u.y0 + y, w, h) for t, x, y, w, h in ln] for ln in per[j]]
            stat["refined"] += 1
    # merge: refined units replace the native blocks they stand for or cover
    drop: set[int] = set()
    nrects = [to_block(g).rect for g in native]
    extra: list[TextBlock] = []
    for i, lns in results.items():
        if not any(lns):
            continue  # nothing read: native kept
        u = units[i]
        drop.update(u.native)
        for n, r in enumerate(nrects):
            ix = max(0, min(r.right, u.x1) - max(r.left, u.x0)) * max(0, min(r.bottom, u.y1) - max(r.top, u.y0))
            if ix > 0.5 * max(1, r.width * r.height):
                drop.add(n)
        for ln in lns:
            extra += [to_block(g) for g in group_blocks(ln)]
    blocks = [to_block(g) for n, g in enumerate(native) if n not in drop] + extra
    T["end"] = time.perf_counter()
    ks = list(T)
    stat["split"] = {k: round((T[k] - T[ks[i - 1]]) * 1000, 1) for i, k in enumerate(ks) if i}
    stat["native_ms"] = (t1 - t0) * 1000
    stat["total_ms"] = (time.perf_counter() - t0) * 1000
    STATS.append(stat)
    return render_blocks(blocks)


def _dump() -> None:
    p = os.environ.get("C3_STATS")
    if p and STATS:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(STATS, fh)


atexit.register(_dump)
