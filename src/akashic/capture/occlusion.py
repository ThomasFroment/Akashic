"""Pure geometry for hiding what covers a window: visible parts and a masked crop.

Rects are in desktop coordinates (negative values allowed). ``masked_crop`` takes the desktop
position of the image's top-left pixel as ``origin`` and maps desktop coordinates to pixels with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PIL import Image

from akashic.capture.ports import Rect

if TYPE_CHECKING:
    from collections.abc import Sequence


def _subtract(part: Rect, cover: Rect) -> list[Rect]:
    overlap = part.intersect(cover)
    if overlap is None:
        return [part]
    strips = (
        Rect(part.left, part.top, part.right, overlap.top),
        Rect(part.left, overlap.bottom, part.right, part.bottom),
        Rect(part.left, overlap.top, overlap.left, overlap.bottom),
        Rect(overlap.right, overlap.top, part.right, overlap.bottom),
    )
    return [s for s in strips if s.width > 0 and s.height > 0]


def visible_parts(target: Rect, above: Sequence[Rect]) -> list[Rect]:
    """Disjoint rects whose union is ``target`` minus the union of ``above``."""
    parts = [target] if target.width > 0 and target.height > 0 else []
    for cover in above:
        parts = [strip for part in parts for strip in _subtract(part, cover)]
    return parts


def area(parts: Sequence[Rect]) -> int:
    """Total area of ``parts``, which the caller keeps disjoint."""
    return sum(p.width * p.height for p in parts)


def masked_crop(
    image: Image.Image, origin: tuple[int, int], bounds: Rect, keep: Sequence[Rect]
) -> Image.Image | None:
    """Crop ``bounds`` out of ``image``, zeroing every pixel outside ``keep``.

    None when nothing of ``keep`` lands inside the crop.
    """
    ox, oy = origin
    crop = bounds.translate(-ox, -oy).intersect(Rect(0, 0, image.width, image.height))
    if crop is None:
        return None
    out = Image.new(image.mode, (crop.width, crop.height))
    pasted = False
    for part in keep:
        clipped = part.translate(-ox, -oy).intersect(crop)
        if clipped is None:
            continue
        piece = image.crop((clipped.left, clipped.top, clipped.right, clipped.bottom))
        out.paste(piece, (clipped.left - crop.left, clipped.top - crop.top))
        pasted = True
    return out if pasted else None
