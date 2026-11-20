"""Today's path, exactly.

live: OcrGate.read crops the window capture to imaging.find_text_box, then WindowsOcr.recognize.
deferred: ScreenWriter stores imaging.encode_webp(window) (WebP q70, method 2); the ocr_window job
decodes it, .convert("RGB") and recognizes the whole image (no text box).
replay: a crop of a stored WebP frame is read like the deferred path (whole crop).
"""

from __future__ import annotations

import io

from PIL import Image

from akashic.capture import imaging


def prepare(img: Image.Image, ctx) -> list[Image.Image]:
    if ctx.path == "live":
        box = imaging.find_text_box(img)
        return [] if box is None else [img.crop((box.left, box.top, box.right, box.bottom))]
    return [img]


def store(img: Image.Image, ctx) -> bytes:
    return imaging.encode_webp(img)


def load(data: bytes, ctx) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")
