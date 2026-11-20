"""C2: single-call strip mosaic with a budgeted scale for small strips, plus a ClearType phase split.

read(img, ctx):
1. Region: live = imaging.find_text_box crop (today's region); deferred/replay = the whole image.
2. Strips: ink = horizontal luma gradient > EDGE_T; recursive XY-cut (column gutters >= GAP_X first,
   then row runs with gaps <= 1 px merged and accent-sized runs attached to their neighbour).
   small := run height <= SMALL_H device px.
3. Scale s for small strips (<= S_MAX): the largest s whose predicted OCR + preprocessing time stays
   within today's predicted time + BUDGET_MS. Cost model (fit_cost.py -> cost.json):
   t = c0 + k_n * native strip pixels + k_d * dense output pixels + k_b * blank output pixels.
4. Small strips: on the live path, strips whose per-channel coverage shows ClearType fringes
   (mean |a_R - a_B| >= FRINGE_T) get the phase split: a_c = (v_c - bg_c) / (fg_c - bg_c), interleaved
   to 3W samples (RGB or BGR, chosen per call by lower total variation), Lanczos to s*W x s*h, dark on
   light. Other small strips (grayscale AA, every deferred/replay image) take luma Lanczos.
   Large strips stay at 1x luma.
5. One OCR call on the mosaic (strips stacked top to bottom, each in a band of its own background).
   No small strip -> today's call exactly (so >= 16 px text and UIA-like pages are untouched).

ctx.extra overrides (step A only): method in {"luma", "lumanorm", "rgb", "bgr", "auto", "auto_fir",
"auto_l2"}, force_s (fixed s, no budget), all_small (treat every strip as small), stats (dict to fill).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from akashic.capture import imaging

HERE = Path(__file__).resolve().parent
EDGE_T = 32  # horizontal luma gradient that counts as ink
GAP_X = 16  # empty columns that split a block (column gutter), device px
MERGE_Y = 1  # empty rows merged inside a run
SMALL_H = 15  # run height (device px) at or below which a strip is upscaled
S_MAX = 3.0
S_MIN = 1.25  # below this the upscale is not worth the mosaic
MAX_DIM = 10000  # OcrEngine.MaxImageDimension on the target PC
BUDGET_MS = 40.0
FRINGE_T = np.float32(0.05)
WEAK = 24  # |fg_c - bg_c| below this: the channel carries no phase, take its neighbours
INK_T = 24
PAD = 2
ORDER_VOTE = 4  # strips that vote RGB vs BGR per call (largest first)
_COST = {"c0": 20.0, "k_n": 0.0, "k_d": 60.0, "k_b": 15.0, "pre_c": 2.0, "pre_k": 10.0}
if (HERE / "cost.json").is_file():
    _COST |= json.loads((HERE / "cost.json").read_text(encoding="utf-8"))


# --- segmentation -------------------------------------------------------------------------------


def _runs(profile: np.ndarray, merge_gap: int) -> list[list[int]]:
    idx = np.flatnonzero(profile)
    if idx.size == 0:
        return []
    br = np.flatnonzero(np.diff(idx) > merge_gap + 1)
    starts = np.r_[idx[0], idx[br + 1]]
    ends = np.r_[idx[br], idx[-1]] + 1
    return [[int(a), int(b)] for a, b in zip(starts, ends, strict=True)]


def _attach_tiny(rs: list[list[int]]) -> list[list[int]]:
    """Attach accent/dot-sized runs to the closest neighbour run within 4 px."""
    while len(rs) > 1:
        best = None
        for i, (a, b) in enumerate(rs):
            for j in (i - 1, i + 1):
                if 0 <= j < len(rs):
                    na, nb = rs[j]
                    gap = a - nb if j < i else na - b
                    if gap <= 4 and (b - a) <= max(3, 0.35 * (nb - na)) and (best is None or gap < best[0]):
                        best = (gap, i, j)
        if best is None:
            break
        _, i, j = best
        lo, hi = min(i, j), max(i, j)
        rs[lo:hi + 1] = [[rs[lo][0], rs[hi][1]]]
    return rs


def _xy(mask: np.ndarray, x0: int, y0: int, x1: int, y1: int, out: list, depth: int = 0) -> None:
    cr = _runs(mask[y0:y1, x0:x1].any(axis=0), GAP_X - 1)
    if not cr:
        return
    if len(cr) > 1 and depth < 40:
        for a, b in cr:
            _xy(mask, x0 + a, y0, x0 + b, y1, out, depth + 1)
        return
    xa, xb = x0 + cr[0][0], x0 + cr[0][1]
    rr = _attach_tiny(_runs(mask[y0:y1, xa:xb].any(axis=1), MERGE_Y))
    if len(rr) > 1 and depth < 40:
        for a, b in rr:
            _xy(mask, xa, y0 + a, xb, y0 + b, out, depth + 1)
        return
    if rr:
        out.append((xa, y0 + rr[0][0], xb, y0 + rr[0][1]))


def strips(gray: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """(x0, y0, x1, y1, h) padded boxes in reading order (top to bottom, then left to right); h = run height."""
    g = gray.astype(np.int16)
    mask = np.zeros(g.shape, dtype=bool)
    mask[:, 1:] = np.abs(np.diff(g, axis=1)) > EDGE_T
    raw: list = []
    _xy(mask, 0, 0, g.shape[1], g.shape[0], raw)
    H, W = g.shape
    out = []
    for x0, y0, x1, y1 in raw:
        if x1 - x0 < 4 or y1 - y0 < 3:
            continue
        out.append((max(0, x0 - PAD), max(0, y0 - PAD), min(W, x1 + PAD), min(H, y1 + PAD), y1 - y0))
    out.sort(key=lambda b: (b[1], b[0]))
    return out


# --- strip renderings ---------------------------------------------------------------------------


def _mode(ch: np.ndarray) -> int:
    return int(np.bincount(ch.ravel(), minlength=256).argmax())


def coverage(rgb: np.ndarray):
    """Per-channel coverage a (H, W, 3) float32 in [0, 1], weak-channel flags, fringe energy; None if no ink."""
    bg = np.array([_mode(rgb[..., c]) for c in range(3)], dtype=np.int16)
    d = rgb.astype(np.int16) - bg
    ink = np.abs(d).max(axis=2) > INK_T
    di = d[ink]
    if di.shape[0] < 8:
        return None
    ext = np.where(di.sum(axis=0) >= 0, di.max(axis=0), di.min(axis=0)).astype(np.float32)
    weak = np.abs(ext) < WEAK
    if weak.all():
        return None
    inv = (np.float32(1) / np.where(weak, np.float32(1), ext)).astype(np.float32)
    a = np.clip(d.astype(np.float32) * inv, np.float32(0), np.float32(1))
    ai = np.clip(di.astype(np.float32) * inv, np.float32(0), np.float32(1))
    if not weak[0] and not weak[2]:
        fringe = float(np.abs(ai[:, 0] - ai[:, 2]).mean())
    else:
        ok = ai[:, ~weak]
        fringe = float((ok.max(axis=1) - ok.min(axis=1)).mean())
    return a, weak, fringe


def interleave(a: np.ndarray, weak: np.ndarray, order: tuple[int, int, int]) -> np.ndarray:
    """g[y, 3x + k] = a[y, x, order[k]]; weak channels take the mean of their interleaved neighbours."""
    h, w, _ = a.shape
    g = np.ascontiguousarray(a[:, :, list(order)]).reshape(h, 3 * w)
    for k, c in enumerate(order):
        if weak[c]:
            gp = np.pad(g, ((0, 0), (1, 1)), mode="edge")
            g[:, k::3] = np.float32(0.5) * (gp[:, k : k + 3 * w : 3] + gp[:, k + 2 : k + 2 + 3 * w : 3])
    return g


def tv(g: np.ndarray) -> float:
    return float(np.abs(np.diff(g, axis=1)).sum())


def _to_l(cov: np.ndarray) -> Image.Image:
    return Image.fromarray(((np.float32(1) - cov) * np.float32(255) + np.float32(0.5)).astype(np.uint8), "L")


def phase_split(g: np.ndarray, w: int, h: int, s: float, smooth: str = "") -> Image.Image:
    """Interleaved coverage (h, 3w) -> dark-on-light L image of round(s*w) x round(s*h)."""
    if smooth == "fir":  # mild [1, 2, 1] / 4 along the subpixel axis
        gp = np.pad(g, ((0, 0), (1, 1)), mode="edge")
        g = np.float32(0.25) * gp[:, :-2] + np.float32(0.5) * gp[:, 1:-1] + np.float32(0.25) * gp[:, 2:]
    im = _to_l(g)
    if smooth == "l2":  # Lanczos round trip through 2 samples per pixel
        im = im.resize((2 * w, h), Image.LANCZOS)
    return im.resize((max(1, round(s * w)), max(1, round(s * h))), Image.LANCZOS)


def luma_norm(gray: np.ndarray) -> np.ndarray | None:
    bg = np.float32(_mode(gray))
    d = gray.astype(np.float32) - bg
    ink = np.abs(d) > INK_T
    if int(ink.sum()) < 8:
        return None
    di = d[ink]
    ext = np.float32(di.max() if di.sum() >= 0 else di.min())
    return np.clip(d / ext, np.float32(0), np.float32(1))


# --- cost model and mosaic ----------------------------------------------------------------------


def _gap(h: int) -> int:
    return max(3, round(0.25 * h))


def _layout(boxes, s: float, small: list[bool]):
    dims = []
    for (x0, y0, x1, y1, _), sm in zip(boxes, small, strict=True):
        w, h = x1 - x0, y1 - y0
        dims.append((max(1, round(s * w)), max(1, round(s * h))) if sm else (w, h))
    margin = 8
    W = max(w for w, _ in dims) + 2 * margin
    H = sum(h + 2 * _gap(h) for _, h in dims)
    return dims, W, H, margin


def predict(native: float, dense: float, blank: float) -> float:
    c = _COST
    return c["c0"] + (c["k_n"] * native + c["k_d"] * dense + c["k_b"] * blank) / 1e6


def choose_s(boxes, small: list[bool], region_area: int) -> tuple[float, dict]:
    native = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
    today = predict(native, native, max(0, region_area - native))
    small_native = sum((b[2] - b[0]) * (b[3] - b[1]) for b, sm in zip(boxes, small, strict=True) if sm)
    info = {"today_ms": today}
    for s in np.arange(S_MAX, S_MIN - 1e-9, -0.125):
        dims, W, H, _ = _layout(boxes, float(s), small)
        if max(W, H) > MAX_DIM:
            continue
        dense = sum(w * h for w, h in dims)
        pred = predict(native, dense, W * H - dense)
        pre = _COST["pre_c"] + _COST["pre_k"] * small_native * s * s / 1e6
        if pred + pre <= today + BUDGET_MS:
            info |= {"pred_ms": pred + pre, "dense": dense, "blank": W * H - dense}
            return float(s), info
    return 1.0, info


def read(img: Image.Image, ctx) -> str:
    return ctx.ocr.recognize(mosaic(img, ctx)[0])


def mosaic(img: Image.Image, ctx) -> tuple[Image.Image, dict]:
    """(image for the single OCR call, info). info: s, native/dense/blank pixel counts, prediction."""
    ex = getattr(ctx, "extra", None) or {}
    stats = ex.get("stats")
    if ctx.path == "live":
        box = imaging.find_text_box(img)
        if box is None:
            return Image.new("L", (8, 8), 255), {"s": 0.0}
        region = img.crop((box.left, box.top, box.right, box.bottom))
    else:
        region = img
    rgb = np.asarray(region.convert("RGB"))
    gray = np.asarray(region.convert("L"))
    boxes = strips(gray)
    small = [True] * len(boxes) if ex.get("all_small") else [b[4] <= SMALL_H for b in boxes]
    native = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
    today = {"s": 1.0, "native": native, "dense": native, "blank": max(0, region.width * region.height - native)}
    if not boxes or not any(small):
        return region, today
    if ex.get("force_s"):
        s, info = float(ex["force_s"]), {}
    else:
        s, info = choose_s(boxes, small, region.width * region.height)
    if s <= 1.0:
        if stats is not None:
            stats.setdefault("calls", []).append({"s": 1.0, **info})
        return region, today
    method = ex.get("method", "auto")
    if ctx.path != "live" and method not in ("luma", "lumanorm"):
        method = "luma"
    pieces: list[tuple[Image.Image, int]] = []  # (L image, background level)
    pending = []  # (index, a, weak, w, h) ClearType strips waiting for the per-call order
    n_ct = 0
    for i, ((x0, y0, x1, y1, _), sm) in enumerate(zip(boxes, small, strict=True)):
        gl = gray[y0:y1, x0:x1]
        w, h = x1 - x0, y1 - y0
        if not sm:
            pieces.append((Image.fromarray(gl, "L"), _mode(gl)))
            continue
        if method in ("rgb", "bgr", "auto", "auto_fir", "auto_l2"):
            cov = coverage(rgb[y0:y1, x0:x1])
            if cov is not None and (cov[2] >= FRINGE_T or method in ("rgb", "bgr")):
                pending.append((i, cov[0], cov[1], w, h))
                pieces.append((None, 255))
                n_ct += 1
                continue
        if method in ("luma", "auto", "auto_fir", "auto_l2"):
            pieces.append((Image.fromarray(gl, "L").resize((round(s * w), round(s * h)), Image.LANCZOS), _mode(gl)))
        else:  # normalized luma coverage, dark on light (also the fallback of the phase-split methods)
            a = luma_norm(gl)
            im = Image.fromarray(gl, "L") if a is None else _to_l(a)
            pieces.append((im.resize((round(s * w), round(s * h)), Image.LANCZOS), _mode(gl) if a is None else 255))
    if pending:
        if method == "bgr":
            order = (2, 1, 0)
        elif method == "rgb":
            order = (0, 1, 2)
        else:
            vote = sorted(pending, key=lambda q: -q[3] * q[4])[:ORDER_VOTE]
            t_rgb = sum(tv(interleave(a, wk, (0, 1, 2))) for _, a, wk, _, _ in vote)
            t_bgr = sum(tv(interleave(a, wk, (2, 1, 0))) for _, a, wk, _, _ in vote)
            order = (0, 1, 2) if t_rgb <= t_bgr else (2, 1, 0)
            if stats is not None:
                stats.setdefault("order", []).append("rgb" if order == (0, 1, 2) else "bgr")
        smooth = {"auto_fir": "fir", "auto_l2": "l2"}.get(method, "")
        for i, a, wk, w, h in pending:
            pieces[i] = (phase_split(interleave(a, wk, order), w, h, s, smooth), 255)
    if stats is not None:
        stats.setdefault("calls", []).append({"s": s, "strips": len(boxes), "small": sum(small), "cleartype": n_ct, **info})
    dims = [p.size for p, _ in pieces]
    margin = 8
    W = max(w for w, _ in dims) + 2 * margin
    H = sum(h + 2 * _gap(h) for _, h in dims)
    canvas = np.empty((H, W), dtype=np.uint8)
    y = 0
    for (im, bg), (w, h) in zip(pieces, dims, strict=True):
        band = h + 2 * _gap(h)
        canvas[y : y + band] = bg
        canvas[y + _gap(h) : y + _gap(h) + h, margin : margin + w] = np.asarray(im)
        y += band
    dense = sum(w * h for w, h in dims)
    return Image.fromarray(canvas, "L"), {**info, "s": s, "native": native, "dense": dense, "blank": W * H - dense}
