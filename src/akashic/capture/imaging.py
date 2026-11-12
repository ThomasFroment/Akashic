"""Cheap image analysis for capture: change detection, black-frame check, text regions, WebP.

Change detection is two layers (research §3.2): an exact hash of a 1/4-resolution grayscale
frame for the early exit, then one comparison of how many of those pixels moved. screenpipe
uses a histogram for the second step; a histogram cannot see a scrolled page (same pixels,
different place), so this counts changed pixels instead. Still one cheap pass, no SSIM.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np
from PIL import Image

from akashic.capture.ports import Rect

if TYPE_CHECKING:
    from numpy.typing import NDArray

type Gray = NDArray[np.uint8]

SMALL_FACTOR: Final = 4  # change detection works on a 1/4-resolution frame
PIXEL_DELTA: Final = 16  # gray levels a pixel must move to count as changed
CHANGED_FRACTION: Final = 1e-4  # fraction of changed pixels that makes a frame "different"
BLACK_MAX_LEVEL: Final = 10  # a frame whose brightest pixel is below this is black (DRM)
WEBP_QUALITY: Final = 70
# Stored screenshots are shrunk to this long side (text is read from the full-size frame).
# Measured on the 3-monitor PC: a 4K frame took 0.9 s to encode at method 4; 1920 px at method 2
# takes ~90 ms including the resize and yields 50-100 KB, the research §3.7 estimate.
STORED_MAX_SIDE: Final = 1920
WEBP_METHOD: Final = 2
TEXT_CELL: Final = 16  # text-region detection works on 16x16 cells of a half-size frame
TEXT_EDGE: Final = 48  # horizontal gradient that counts as an edge
TEXT_MIN_DENSITY: Final = 0.02
TEXT_MAX_DENSITY: Final = 0.5


@dataclass(frozen=True, slots=True)
class Signature:
    digest: bytes
    gray: Gray


def gray_small(image: Image.Image, factor: int = SMALL_FACTOR) -> Gray:
    return np.asarray(image.reduce(factor).convert("L"), dtype=np.uint8)


def signature(image: Image.Image) -> Signature:
    gray = gray_small(image)
    return Signature(hashlib.blake2b(gray.tobytes(), digest_size=16).digest(), gray)


def differs(a: Signature, b: Signature) -> bool:
    """True if the two frames show something different (exact hash first, then pixel count)."""
    if a.digest == b.digest:
        return False
    if a.gray.shape != b.gray.shape:
        return True
    moved = np.abs(a.gray.astype(np.int16) - b.gray.astype(np.int16)) > PIXEL_DELTA
    return float(moved.mean()) > CHANGED_FRACTION


def is_black(image: Image.Image) -> bool:
    """DRM-protected video (Netflix, Prime, Disney+) captures as pure black."""
    return int(np.asarray(image.reduce(SMALL_FACTOR)).max()) < BLACK_MAX_LEVEL


def encode_webp(image: Image.Image, quality: int = WEBP_QUALITY) -> bytes:
    """WebP of ``image`` shrunk to at most ``STORED_MAX_SIDE`` on its long side."""
    if max(image.size) > STORED_MAX_SIDE:
        image = image.copy()
        image.thumbnail(
            (STORED_MAX_SIDE, STORED_MAX_SIDE), Image.Resampling.BILINEAR, reducing_gap=2.0
        )
    out = io.BytesIO()
    image.save(out, format="WEBP", quality=quality, method=WEBP_METHOD)
    return out.getvalue()


def find_text_box(image: Image.Image) -> Rect | None:
    """Bounding box (in ``image`` pixels) of the cells that look like text, or None.

    Text is dense in strong horizontal edges but not saturated with them: flat backgrounds have
    none, photographs have many everywhere. Cheap stand-in for screenpipe's morphological
    pipeline; it only has to shrink the area that gets OCR'd and hashed.
    """
    half = np.asarray(image.reduce(2).convert("L"), dtype=np.int16)
    if half.shape[1] < 2:  # noqa: PLR2004 - need at least two columns for a gradient
        return None
    edges = np.abs(np.diff(half, axis=1)) > TEXT_EDGE
    rows = -(-edges.shape[0] // TEXT_CELL) * TEXT_CELL
    cols = -(-edges.shape[1] // TEXT_CELL) * TEXT_CELL
    padded = np.zeros((rows, cols), dtype=bool)
    padded[: edges.shape[0], : edges.shape[1]] = edges
    density = padded.reshape(rows // TEXT_CELL, TEXT_CELL, cols // TEXT_CELL, TEXT_CELL).mean(
        axis=(1, 3)
    )
    cells = (density >= TEXT_MIN_DENSITY) & (density <= TEXT_MAX_DENSITY)
    ys, xs = np.nonzero(cells)
    if ys.size == 0:
        return None
    scale = 2 * TEXT_CELL
    return Rect(
        max(int(xs.min()) * scale - scale, 0),
        max(int(ys.min()) * scale - scale, 0),
        min((int(xs.max()) + 2) * scale, image.width),
        min((int(ys.max()) + 2) * scale, image.height),
    )
