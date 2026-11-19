"""Synthetic content for the harness: pages, small marks, the placement sweep, extra pairs and
the 4K check streams. Pillow only, no captured content.

Device sizes: CSS px x scale rounded (`dev`); thin strokes (caret bars, lines, underscore
thickness) round up (`thin`): a 1 px / 2 px stroke at 100 % is 2 px / 3 px at 125 %.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hcommon import FONT, MONITORS, THEMES

WORDS = ("ok", "gg", "lol", "on", "no", "non", "ou", "oui", "mm")
WORD_PX = (13, 14, 16)
CONTRASTS = ("full", "d64", "d40")
SENTENCE = "on se retrouve ce soir ?"
BADGE_RGB = (237, 66, 69)
PAD = 16  # device px around a mark in a sweep patch; a multiple of every reduction factor
ALIGN = 16  # patches and dirty regions are aligned to 16 device px (factors must divide it)


def dev(css: float, scale: float) -> int:
    return max(1, round(css * scale))


def thin(css: float, scale: float) -> int:
    return max(1, math.ceil(css * scale - 1e-9))


@lru_cache(maxsize=64)
def font(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT, px)


def contrast_rgb(theme: str, contrast: str) -> tuple[int, int, int]:
    bg, fg = THEMES[theme]
    if contrast == "full":
        return fg
    delta = int(contrast[1:])
    sign = 1 if theme == "dark" else -1
    return tuple(int(np.clip(c + sign * delta, 0, 255)) for c in bg)


# --- cases ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    name: str
    kind: str  # word, badge, sentence, bar, underscore, hline, block
    group: str  # word_full, word_d64, word_d40, badge, sentence, caret, line, block
    positive: bool
    text: str = ""
    px: int = 0
    contrast: str = "full"
    stroke: int = 0  # css stroke width for bar / hline


def _cases() -> list[Case]:
    out = [Case(f"word:{w}:{px}:{c}", "word", f"word_{c}", True, w, px, c)
           for c in CONTRASTS for w in WORDS for px in WORD_PX]
    out += [Case("badge", "badge", "badge", True), Case("sentence", "sentence", "sentence", True, SENTENCE, 14)]
    out += [Case("bar1", "bar", "caret", False, stroke=1), Case("bar2", "bar", "caret", False, stroke=2),
            Case("underscore", "underscore", "caret", False, stroke=2),
            Case("hline1", "hline", "line", False, stroke=1), Case("hline2", "hline", "line", False, stroke=2),
            Case("block", "block", "block", False)]
    return out


CASES = _cases()
CASE_BY_NAME = {c.name: c for c in CASES}


def draw_mark(d: ImageDraw.ImageDraw, x: int, y: int, case: Case, monitor: str, theme: str) -> None:
    scale = MONITORS[monitor][2]
    fg = THEMES[theme][1]
    if case.kind in ("word", "sentence"):
        d.text((x, y), case.text, font=font(dev(case.px, scale)), fill=contrast_rgb(theme, case.contrast))
    elif case.kind == "badge":
        r = dev(16, scale)
        d.ellipse((x, y, x + r - 1, y + r - 1), fill=BADGE_RGB)
    elif case.kind == "bar":
        w, h = thin(case.stroke, scale), dev(19, scale)
        d.rectangle((x, y, x + w - 1, y + h - 1), fill=fg)
    elif case.kind == "underscore":
        w, h = dev(9, scale), thin(case.stroke, scale)
        d.rectangle((x, y, x + w - 1, y + h - 1), fill=fg)
    elif case.kind == "hline":
        w, h = dev(30, scale), thin(case.stroke, scale)
        d.rectangle((x, y, x + w - 1, y + h - 1), fill=fg)
    elif case.kind == "block":
        w, h = dev(9, scale), dev(19, scale)
        d.rectangle((x, y, x + w - 1, y + h - 1), fill=fg)
    else:
        raise ValueError(case.kind)


# --- page -------------------------------------------------------------------------------------


@lru_cache(maxsize=4)
def page(monitor: str, theme: str) -> Image.Image:
    """A page of text lines on the left (as the change_detection_small spike); the right third
    is an empty chat area. Cached: copy before drawing on it."""
    w, h, scale = MONITORS[monitor]
    bg, fg = THEMES[theme]
    img = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(img)
    f = font(round(14 * scale))
    rng = np.random.default_rng(1)
    for i in range(int(h / (24 * scale)) - 4):
        n = int(rng.integers(30, 110))
        d.text((round(40 * scale), round(20 * scale + i * 24 * scale)), "lorem ipsum dolor " * (n // 18), font=f, fill=fg)
    return img


def chat_origin(monitor: str) -> tuple[int, int]:
    w, h, _ = MONITORS[monitor]
    return int(w * 0.70) // 64 * 64, int(h * 0.45) // 64 * 64


def factors(reduce, monitor: str, theme: str = "dark") -> tuple[int, int]:
    w, h, _ = MONITORS[monitor]
    g = reduce(page(monitor, theme))
    fy, fx = h // g.shape[0], w // g.shape[1]
    if fy * g.shape[0] != h or fx * g.shape[1] != w or ALIGN % fx or ALIGN % fy:
        raise ValueError(f"reduce gives {g.shape} for {w}x{h}: factors must be integers dividing {ALIGN}")
    return fx, fy


# --- placement sweep --------------------------------------------------------------------------

FULL_PLACEMENTS = [(dx, dy) for dy in range(64) for dx in range(64)]
_Q = [0, 1, 2, 3, 28, 29, 30, 31, 32, 33, 34, 35, 60, 61, 62, 63]
QUICK_PLACEMENTS = [(dx, dy) for dy in _Q for dx in _Q]  # every residue mod 4, offsets 0/7/8/15 mod 16 reduced


class Sweep:
    """Reduced frames of one page with one mark at offsets (dx, dy) from a 64-aligned origin.

    Rendering is translation invariant at integer positions on a uniform background, and the
    reduction is a block average, so the mark is rendered once per residue (dx mod fx, dy mod fy)
    in an aligned patch, reduced, and pasted into the reduced reference at the shifted place.
    `check()` compares this against a full-frame render.
    """

    def __init__(self, reduce, monitor: str, theme: str) -> None:
        self.reduce, self.monitor, self.theme = reduce, monitor, theme
        self.page = page(monitor, theme)
        self.ref = reduce(self.page)
        self.fx, self.fy = factors(reduce, monitor, theme)
        self.x0, self.y0 = chat_origin(monitor)
        self.bg = THEMES[theme][0]

    def _extent(self, case: Case) -> tuple[int, int]:
        img = Image.new("RGB", (1024, 256), self.bg)
        draw_mark(ImageDraw.Draw(img), PAD, PAD, case, self.monitor, self.theme)
        a = np.asarray(img).astype(np.int16) - np.array(self.bg, dtype=np.int16)
        ys, xs = np.nonzero(np.abs(a).max(axis=2))
        if xs.min() < 1 or ys.min() < 1:
            raise ValueError(f"{case.name}: ink left of / above the pad")
        up = lambda v: -(-v // ALIGN) * ALIGN  # noqa: E731
        return up(int(xs.max()) + 1 + ALIGN + PAD), up(int(ys.max()) + 1 + ALIGN + PAD)

    def patches(self, case: Case) -> dict[tuple[int, int], np.ndarray]:
        pw, ph = self._extent(case)
        a = np.asarray(self.page.crop((self.x0 - PAD, self.y0 - PAD, self.x0 + 64 + pw, self.y0 + 64 + ph)))
        if not (a == np.array(self.bg, dtype=np.uint8)).all():
            raise ValueError(f"{case.name}: sweep area is not plain background")
        out = {}
        for ry in range(self.fy):
            for rx in range(self.fx):
                img = Image.new("RGB", (pw, ph), self.bg)
                draw_mark(ImageDraw.Draw(img), PAD + rx, PAD + ry, case, self.monitor, self.theme)
                out[rx, ry] = self.reduce(img)
        return out

    def frames(self, case: Case, placements):
        """Yield (dx, dy, cur) with cur a read-only view of a reused buffer (valid until next)."""
        cur = self.ref.copy()
        view = cur.view()
        view.flags.writeable = False
        pats = self.patches(case)
        for dx, dy in placements:
            rx, ry = dx % self.fx, dy % self.fy
            p = pats[rx, ry]
            ox = (self.x0 + dx - rx - PAD) // self.fx
            oy = (self.y0 + dy - ry - PAD) // self.fy
            sl = (slice(oy, oy + p.shape[0]), slice(ox, ox + p.shape[1]))
            cur[sl] = p
            yield dx, dy, view
            cur[sl] = self.ref[sl]

    def full_render(self, case: Case, dx: int, dy: int) -> Image.Image:
        img = self.page.copy()
        draw_mark(ImageDraw.Draw(img), self.x0 + dx, self.y0 + dy, case, self.monitor, self.theme)
        return img

    def check(self, names=("word:ok:13:full", "badge", "bar2", "sentence"), offsets=((0, 0), (37, 61), (63, 5))) -> None:
        for name in names:
            case = CASE_BY_NAME[name]
            want = {o: self.reduce(self.full_render(case, *o)) for o in offsets}
            for dx, dy, cur in self.frames(case, offsets):
                if not np.array_equal(cur, want[dx, dy]):
                    raise AssertionError(f"fast sweep differs from a full render: {self.monitor} {self.theme} {name} {dx},{dy}")


# --- extra pairs (superset check) ------------------------------------------------------------


def extra_pairs(monitor: str, theme: str, seed: int = 7):
    """Yield (name, cur_image) against ref = page(monitor, theme): scrolls, windows, noise, and
    changes near today's threshold (scattered 4x4 blocks, blobs, thin lines)."""
    w, h, scale = MONITORS[monitor]
    bg, fg = THEMES[theme]
    ref = page(monitor, theme)
    a = np.asarray(ref)
    rng = np.random.default_rng(seed)
    t = 1e-4 * (w // 4) * (h // 4)  # today's threshold in moved reduced pixels
    gray = np.asarray(ref.convert("L"), dtype=np.int16)

    def blocks(cells) -> Image.Image:
        """Recolor aligned 4x4 device blocks so each reduced pixel moves by 20-120 levels."""
        b = a.copy()
        for bx, by in cells:
            mean = int(gray[by * 4:by * 4 + 4, bx * 4:bx * 4 + 4].mean())
            d = int(rng.integers(20, 121))
            v = mean + d if mean + d <= 255 else mean - d
            v = int(np.clip(v, 0, 255))
            b[by * 4:by * 4 + 4, bx * 4:bx * 4 + 4] = v
        return Image.fromarray(b)

    for s in (1, 2, 4, 7, 16, 40, 200):
        b = np.empty_like(a)
        b[:-s] = a[s:]
        b[-s:] = bg
        yield f"scroll{s}", Image.fromarray(b)
    for i in range(3):
        img = ref.copy()
        d = ImageDraw.Draw(img)
        x, y = int(rng.integers(0, w - dev(600, scale))), int(rng.integers(0, h - dev(400, scale)))
        d.rectangle((x, y, x + dev(600, scale), y + dev(400, scale)), fill=(60, 63, 68) if theme == "dark" else (240, 240, 240))
        d.text((x + 20, y + 20), "Settings  Display  Sound", font=font(dev(14, scale)), fill=fg)
        yield f"window{i}", img
    for sigma in (4, 8, 16, 32):
        n = rng.standard_normal((h, w, 1), dtype=np.float32) * sigma
        yield f"noise{sigma}", Image.fromarray(np.clip(a + n, 0, 255).astype(np.uint8))
    for n in sorted({max(1, math.floor(t) - 5), math.floor(t), math.floor(t) + 1, math.floor(t) + 2,
                     math.ceil(1.2 * t), math.ceil(2 * t), math.ceil(5 * t), math.ceil(20 * t)}):
        for rep in range(3):
            idx = rng.choice((w // 4) * (h // 4), n, replace=False)
            yield f"scatter{n}_{rep}", blocks([(int(i % (w // 4)), int(i // (w // 4))) for i in idx])
    for k in (4, 6, 7, 8, 9, 10, 12, 16):
        bx, by = int(rng.integers(0, w // 4 - k)), int(rng.integers(0, h // 4 - k))
        yield f"blob{k}", blocks([(bx + i, by + j) for i in range(k) for j in range(k)])
    for n in (math.floor(t), math.ceil(t) + 5, math.ceil(2 * t), math.ceil(5 * t)):
        bx, by = int(rng.integers(0, w // 4 - n)), int(rng.integers(0, h // 4 - n))
        yield f"vline{n}", blocks([(bx, by + j) for j in range(n)])
        yield f"hline{n}", blocks([(bx + i, by) for i in range(n)])
        yield f"band2x{n}", blocks([(bx + i, by + j) for i in range(n) for j in range(2)])
    b = a.astype(np.int16)
    x, y = int(rng.integers(0, w - 256)), int(rng.integers(0, h - 256))
    b[y:y + 256, x:x + 256] += 20 if theme == "dark" else -20
    yield "brighten256", Image.fromarray(np.clip(b, 0, 255).astype(np.uint8))
    img = ref.copy()
    d = ImageDraw.Draw(img)
    x0, y0 = chat_origin(monitor)
    for i in range(5):
        d.text((x0, y0 + i * dev(20, scale)), "on se retrouve ce soir vers neuf heures ?", font=font(dev(14, scale)), fill=fg)
    yield "paragraph", img
    yield "random", Image.fromarray(rng.integers(0, 256, (h, w, 3), dtype=np.uint8))


# --- sequences (4K check streams) --------------------------------------------------------------


@dataclass
class Item:
    kind: str  # text, rect, ellipse, arc
    x: int
    y: int
    args: tuple
    color: tuple
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)

    def draw(self, d: ImageDraw.ImageDraw, ox: int, oy: int) -> None:
        x, y = self.x + ox, self.y + oy
        if self.kind == "text":
            s, px = self.args
            d.text((x, y), s, font=font(px), fill=self.color)
        elif self.kind == "rect":
            w, h = self.args
            d.rectangle((x, y, x + w - 1, y + h - 1), fill=self.color)
        elif self.kind == "ellipse":
            (r,) = self.args
            d.ellipse((x, y, x + r - 1, y + r - 1), fill=self.color)
        elif self.kind == "arc":
            r, start, width = self.args
            d.arc((x, y, x + r - 1, y + r - 1), start, start + 270, fill=self.color, width=width)


def text_item(x, y, s, px, color) -> Item:
    l, t, r, b = font(px).getbbox(s)
    return Item("text", x, y, (s, px), color, (x + l - 2, y + t - 2, x + r + 2, y + b + 2))


def box_item(kind, x, y, w, h, args, color) -> Item:
    return Item(kind, x, y, args, color, (x - 2, y - 2, x + w + 2, y + h + 2))


class Scene:
    """A full-res frame as base page + keyed items; render() re-composes only the dirty,
    16-aligned regions and updates each reducer's gray frame there."""

    def __init__(self, base: Image.Image, reducers: dict) -> None:
        self.base, self.reducers = base, reducers
        self.items: dict[str, Item] = {}
        self.grays = {k: np.array(f(base)) for k, f in reducers.items()}
        w, h = base.size
        self.fac = {k: (w // g.shape[1], h // g.shape[0]) for k, g in self.grays.items()}
        self.dirty: list[tuple[int, int, int, int]] = []

    def set(self, key: str, item: Item | None) -> None:
        old = self.items.pop(key, None)
        if old is not None:
            self.dirty.append(old.bbox)
        if item is not None:
            self.items[key] = item
            self.dirty.append(item.bbox)

    def render(self) -> dict[str, np.ndarray]:
        w, h = self.base.size
        for x0, y0, x1, y1 in self.dirty:
            x0, y0 = max(0, x0 // ALIGN * ALIGN), max(0, y0 // ALIGN * ALIGN)
            x1, y1 = min(w, -(-x1 // ALIGN) * ALIGN), min(h, -(-y1 // ALIGN) * ALIGN)
            crop = self.base.crop((x0, y0, x1, y1))
            d = ImageDraw.Draw(crop)
            for it in self.items.values():
                bx0, by0, bx1, by1 = it.bbox
                if bx0 < x1 and bx1 > x0 and by0 < y1 and by1 > y0:
                    it.draw(d, -x0, -y0)
            for k, f in self.reducers.items():
                fx, fy = self.fac[k]
                self.grays[k][y0 // fy:y1 // fy, x0 // fx:x1 // fx] = f(crop)
        self.dirty.clear()
        return {k: g.copy() for k, g in self.grays.items()}


@dataclass
class Stream:
    name: str
    nuisance: bool
    ops: list[list[tuple[str, Item | None]]]
    events: list[dict] = field(default_factory=list)  # kind, t, end (exclusive), near_clock


N_CHECKS = 720  # one hour at 5 s
INTERVAL_S = 5.0
T0 = 12 * 3600 + 34 * 60 + 56  # 12:34:56


def now_at(i: int) -> float:
    return float(T0 + INTERVAL_S * i)


def _hms(sec: float) -> str:
    s = int(sec) % 86400
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def streams(seed: int = 11) -> list[Stream]:
    """4K (125 %) dark-page check streams at 5 s spacing."""
    scale = MONITORS["4k"][2]
    bg, fg = THEMES["dark"]
    rng = np.random.default_rng(seed)
    n = N_CHECKS
    clock_xy, spin_xy, block_xy = (3200, 128), (3000, 128), (2800, 1700)
    badge_xy, minclock_xy, typing_xy = (3600, 320), (3560, 2100), (2700, 1900)
    clock_px = dev(14, scale)

    def clock(i):
        return text_item(*clock_xy, _hms(now_at(i)), clock_px, fg)

    def spinner(i):
        r = dev(16, scale)
        return box_item("arc", *spin_xy, r, r, (r, 45 * (i % 8), dev(2, scale)), (88, 101, 242))

    blink = rng.random(n) < 0.5

    def block(i):
        return box_item("rect", *block_xy, dev(9, scale), dev(19, scale), (dev(9, scale), dev(19, scale)), fg) if blink[i] else None

    out = []
    out.append(Stream("clock", True, [[("clock", clock(i))] for i in range(n)]))
    out.append(Stream("spinner", True, [[("spin", spinner(i))] for i in range(n)]))
    out.append(Stream("block", True, [[("block", block(i))] for i in range(n)]))
    out.append(Stream("minclock", True, [[("minclock", text_item(*minclock_xy, _hms(now_at(i))[:5], dev(12, scale), fg))]
                                         for i in range(n)]))

    # badge flaps: show, clear, show ... (k shows on consecutive checks), last show held 24 checks
    ops = [[] for _ in range(n)]
    events = []
    r = dev(16, scale)
    badge = box_item("ellipse", *badge_xy, r, r, (r,), BADGE_RGB)
    t = 6
    for k in rng.permutation([1, 2, 3, 4] * 3):
        marks = [(t + j, j % 2 == 0) for j in range(2 * k - 1)] + [(t + 2 * k - 2 + 24, False)]
        for idx, (tt, show) in enumerate(marks):
            ops[tt].append(("badge", badge if show else None))
            end = marks[idx + 1][0] if idx + 1 < len(marks) else tt + 24
            events.append({"kind": "badge", "t": tt, "end": end, "show": show, "flaps": int(k)})
        t = marks[-1][0] + 24
    out.append(Stream("badge", False, ops, events))

    # typing: one char per check for 6 checks, pause, then the draft is sent (cleared)
    drafts = ["allons", "demain", "ouvert", "simple", "partie", "jouons"]
    ops = [[] for _ in range(n)]
    events = []
    px = dev(14, scale)
    for c, start in enumerate(range(0, n - 12, 12)):
        word = drafts[c % len(drafts)]
        for j in range(6):
            ops[start + j].append(("draft", text_item(*typing_xy, word[:j + 1], px, fg)))
        ops[start + 12 if start + 12 < n else n - 1].append(("draft", None))
        events.append({"kind": "draft", "t": start + 5, "end": start + 12})
    out.append(Stream("typing", False, ops, events))

    # replies: 50 one-word replies at random times; every 5th one within 0-8 px of the clock
    times = np.sort(rng.choice(np.arange(6, n - 6 - 3 * 49), 50, replace=False)) + 3 * np.arange(50)
    slots = rng.choice(5 * 18, 50, replace=False)
    cl = [clock(i).bbox for i in range(n)]
    cbox = (min(b[0] for b in cl) + 2, min(b[1] for b in cl) + 2, max(b[2] for b in cl) - 2, max(b[3] for b in cl) - 2)
    replies = []
    for j, t in enumerate(times):
        word = WORDS[int(rng.integers(len(WORDS)))]
        rpx = dev(int(rng.choice(WORD_PX)), scale)
        near = j % 5 == 2
        if near:
            l, tt, rr, bb = font(rpx).getbbox(word)
            g = int(rng.integers(0, 9))
            if (j // 5) % 2 == 0:  # right of the clock
                x, y = cbox[2] + g - l, clock_xy[1]
            else:  # below the clock
                x, y = clock_xy[0], cbox[3] + g - tt
            key = "near"
        else:
            sx, sy = divmod(int(slots[j]), 18)
            x, y = 2680 + sx * 192 + int(rng.integers(0, 16)), 480 + sy * 64 + int(rng.integers(0, 16))
            key = f"reply{j}"
        replies.append((int(t), key, text_item(x, y, word, rpx, fg), near))

    def reply_stream(name, busy):
        ops = [[] for _ in range(n)]
        events = []
        for j, (t, key, item, near) in enumerate(replies):
            ops[t].append((key, item))
            end = n
            if near:
                end = next((t2 for t2, k2, _, _ in replies[j + 1:] if k2 == "near"), n)
            events.append({"kind": "reply", "t": t, "end": end, "near_clock": near})
        if busy:
            for i in range(n):
                ops[i] += [("clock", clock(i)), ("spin", spinner(i)), ("block", block(i))]
        return Stream(name, False, ops, events)

    out.append(reply_stream("replies_quiet", False))
    out.append(reply_stream("replies_busy", True))
    return out
