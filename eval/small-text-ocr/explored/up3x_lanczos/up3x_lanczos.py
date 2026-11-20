"""3x Lanczos upscale of whatever today's path hands to OCR, for every crop (the earlier research's
best fixed factor), capped at OcrEngine.MaxImageDimension. Storage unchanged (baseline WebP)."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import baseline  # noqa: E402

FACTOR = 3.0
MAX_DIM = 10000  # OcrEngine.max_image_dimension on the target PC


def prepare(img: Image.Image, ctx) -> list[Image.Image]:
    out = []
    for im in baseline.prepare(img, ctx):
        f = min(FACTOR, MAX_DIM / max(im.size))
        out.append(im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS))
    return out
