"""Compose one monitor's picture from window layers, bottom to top (painter's order).

Rects are in desktop coordinates (negative values allowed). The taskbar and desktop are
unlisted frames: painted like any frame, never counted in the facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from PIL import Image

from akashic.capture import imaging

if TYPE_CHECKING:
    from collections.abc import Sequence

    from akashic.capture.ports import Rect

BLANK: Final = (0, 0, 0)

type LayerKind = Literal["frame", "excluded", "pending"]


@dataclass(frozen=True, slots=True)
class Layer:
    rect: Rect
    kind: LayerKind
    listed: bool
    image: Image.Image | None  # frame pixels with the rect's top-left at (0, 0)


@dataclass(frozen=True, slots=True)
class Composite:
    image: Image.Image | None  # None when only excluded content is visible
    has_hidden: bool
    only_hidden: bool
    black: bool


def _paint(canvas: Image.Image, layer: Layer, on_monitor: Rect, monitor: Rect) -> None:
    target = on_monitor.translate(-monitor.left, -monitor.top)
    if layer.kind == "frame" and layer.image is not None:
        source = on_monitor.translate(-layer.rect.left, -layer.rect.top)
        # crop pads a smaller image with blank
        part = layer.image.crop((source.left, source.top, source.right, source.bottom))
        canvas.paste(part, (target.left, target.top))
    else:
        canvas.paste(BLANK, (target.left, target.top, target.right, target.bottom))


def compose(monitor: Rect, layers: Sequence[Layer]) -> Composite:
    """Paint the layers onto the monitor; the monitor must have positive size."""
    canvas = Image.new("RGB", (monitor.width, monitor.height), BLANK)
    on_monitor = [layer.rect.intersect(monitor) for layer in layers]
    for layer, part in zip(layers, on_monitor, strict=True):
        if part is not None:
            _paint(canvas, layer, part, monitor)

    # Every layer passed is visible on this monitor (the desk only passes those).
    has_hidden = any(layer.kind == "excluded" for layer in layers)
    only_hidden = has_hidden and not any(
        layer.kind == "pending" or (layer.kind == "frame" and layer.listed) for layer in layers
    )
    if only_hidden:
        return Composite(image=None, has_hidden=True, only_hidden=True, black=False)
    return Composite(
        image=canvas, has_hidden=has_hidden, only_hidden=False, black=imaging.is_black(canvas)
    )
