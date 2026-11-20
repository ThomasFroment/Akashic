"""Shared pieces of the small-text OCR harness: paths, OCR engines, candidate loading, CER.

CER follows eval/quality/score.py exactly (score.fold, best_line_cer over folded OCR lines, font
buckets in device px); only the Levenshtein distance is a faster bit-parallel version (Hyyro 2001),
checked against score.lev by `uv run python harness/shared.py`.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]  # eval/small-text-ocr
REPO = ROOT.parents[1]
QUALITY = REPO / "eval" / "quality"
CORPUS = Path(__file__).resolve().parent / "corpus"
if str(QUALITY) not in sys.path:
    sys.path.insert(0, str(QUALITY))

import score  # noqa: E402  (eval/quality/score.py, read-only use)

FONT_BUCKETS = score.FONT_BUCKETS
SCALES = {1.0: "1440", 1.25: "4k"}  # device scale of the corpus page -> monitor it stands for


def bucket(device_px: float) -> str:
    return next((b[2] for b in FONT_BUCKETS if b[0] <= device_px < b[1] + 1), "?")


def short_bucket(b: str) -> str:
    return {"<=11": "le11", "12-15": "12_15", ">=16": "ge16"}[b]


# --- Levenshtein ------------------------------------------------------------------------------


def lev(a: str, b: str) -> int:
    """Global edit distance, bit-parallel over `a` (Myers 1999 / Hyyro 2001), Python big ints."""
    if not a:
        return len(b)
    if not b:
        return len(a)
    m = len(a)
    full = (1 << m) - 1
    top = 1 << (m - 1)
    peq: dict[str, int] = {}
    for i, c in enumerate(a):
        peq[c] = peq.get(c, 0) | (1 << i)
    pv, mv, dist = full, 0, m
    for c in b:
        eq = peq.get(c, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & full
        mh = pv & xh
        if ph & top:
            dist += 1
        elif mh & top:
            dist -= 1
        ph = ((ph << 1) | 1) & full
        mh = (mh << 1) & full
        pv = (mh | ~(xv | ph)) & full
        mv = ph & xv
    return dist


def best_line_cer(ref: str, hyp_lines: list[str]) -> float:
    """score.best_line_cer with pruning: hyp_lines are already folded (score.m3 folds them)."""
    r = score.fold(ref)
    if not r:
        return 0.0
    best = 1.0 if not hyp_lines else float("inf")
    n = len(r)
    for h in sorted(hyp_lines, key=lambda h: abs(len(h) - n)):
        if abs(len(h) - n) / n >= best:
            break
        best = min(best, lev(r, h) / n)
    return best


def hyp_of(text: str) -> list[str]:
    return [score.fold(x) for x in text.splitlines() if x.strip()]


def joined(hyp: list[str], k: int = 3) -> list[str]:
    """Wrap-tolerant hypothesis lines: each OCR line plus runs of 2..k consecutive lines joined."""
    out = list(hyp)
    for n in range(2, k + 1):
        out += [" ".join(hyp[i : i + n]) for i in range(len(hyp) - n + 1)]
    return out


# --- OCR engines and candidates ---------------------------------------------------------------


@dataclass
class Ctx:
    """What a candidate may know about a window crop. No ground truth (no font size, no language)."""

    path: str  # "live" (raw window capture), "deferred" (stored copy, decoded), "replay" (stored frame crop)
    scale: float  # Windows display scale of the monitor (1.0 = the 1440p side monitors, 1.25 = 4K primary)
    ocr: object  # Akashic's engine: WindowsOcr("fr") -> fr-FR, the config default (config.py ocr_language)
    ocr_en: object  # an en-US engine, for candidates that want to try it
    extra: dict = field(default_factory=dict)


_ENGINES: dict[str, object] = {}


def engines() -> tuple[object, object]:
    if not _ENGINES:
        from akashic.platform.win.ocr import WindowsOcr

        _ENGINES["fr"] = WindowsOcr("fr")
        _ENGINES["en"] = WindowsOcr("en")
    return _ENGINES["fr"], _ENGINES["en"]


def load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Candidate:
    """A candidate module with baseline defaults for what it leaves out."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.stem
        self.mod = load_module(path)
        self._base = self.mod if path.resolve() == (ROOT / "baseline.py").resolve() else load_module(ROOT / "baseline.py")

    def _fn(self, name: str):
        return getattr(self.mod, name, None) or getattr(self._base, name)

    def read(self, img, ctx: Ctx) -> str:
        """Text of one window crop: the candidate's read(), else merge(OCR of each prepared image)."""
        own = getattr(self.mod, "read", None)
        if own is not None:
            return own(img, ctx)
        images = self._fn("prepare")(img, ctx)
        results = [ctx.ocr.recognize(i) for i in images]
        merge = getattr(self.mod, "merge", None)
        return merge(results, ctx) if merge else "\n".join(r for r in results if r)

    def store(self, img, ctx: Ctx) -> bytes:
        return self._fn("store")(img, ctx)

    def load(self, data: bytes, ctx: Ctx):
        return self._fn("load")(data, ctx)


def decode(data: bytes):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


if __name__ == "__main__":  # self-check of the fast Levenshtein against score.lev
    import random

    rng = random.Random(1)
    alpha = "abcdeé ffgh"
    for _ in range(3000):
        a = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 90)))
        b = "".join(rng.choice(alpha) for _ in range(rng.randint(0, 90)))
        assert lev(a, b) == score.lev(a, b), (a, b)
    for _ in range(300):
        ref = "".join(rng.choice(alpha) for _ in range(rng.randint(1, 60)))
        hyp = ["".join(rng.choice(alpha) for _ in range(rng.randint(0, 70))) for _ in range(rng.randint(0, 6))]
        assert abs(best_line_cer(ref, hyp) - score.best_line_cer(ref, hyp)) < 1e-12
    print("lev ok")
