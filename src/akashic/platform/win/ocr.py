# pyright: basic
"""Windows.Media.Ocr. CPU only, ~10-60 ms per frame (measured in spikes)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine, OcrWord
from winrt.windows.security.cryptography import CryptographicBuffer

from akashic.capture.ocr_gate import pick_language
from akashic.capture.ports import Rect, TextBlock
from akashic.capture.text import render_blocks

if TYPE_CHECKING:
    from PIL import Image

log = logging.getLogger(__name__)


def available_languages() -> list[str]:
    return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]


class WindowsOcr:
    def __init__(self, wanted: str) -> None:
        installed = available_languages()
        tag = pick_language(wanted, installed)
        if tag is None:
            msg = "no Windows OCR recognizer is installed"
            raise RuntimeError(msg)
        if tag.split("-")[0].casefold() != wanted.split("-", maxsplit=1)[0].casefold():
            log.warning("OCR language %r is not installed; using %r instead", wanted, tag)
        self.language = tag
        engine = OcrEngine.try_create_from_language(Language(tag))
        if engine is None:
            msg = "could not create the OCR engine"
            raise RuntimeError(msg)
        self._engine = engine
        self._loop = asyncio.new_event_loop()

    def recognize(self, image: Image.Image) -> str:
        limit = OcrEngine.max_image_dimension
        if max(image.size) > limit:
            image = image.copy()
            image.thumbnail((limit, limit))
        # WinRT wants BGRA; Pillow gives RGB.
        bgra = np.asarray(image.convert("RGBA"))[:, :, [2, 1, 0, 3]].tobytes()
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            CryptographicBuffer.create_from_byte_array(bgra),
            BitmapPixelFormat.BGRA8,
            image.width,
            image.height,
            BitmapAlphaMode.PREMULTIPLIED,
        )
        result = self._loop.run_until_complete(self._engine.recognize_async(bitmap))
        blocks: list[TextBlock] = []
        for line in result.lines:
            group: list[OcrWord] = []
            right = 0.0
            for word in line.words:
                rect = word.bounding_rect
                if group and rect.x - right > max(24, rect.height * 2):
                    blocks.append(_word_block(group))
                    group = []
                group.append(word)
                right = rect.x + rect.width
            if group:
                blocks.append(_word_block(group))
        return render_blocks(blocks)


def _word_block(words: list[OcrWord]) -> TextBlock:
    rects = [w.bounding_rect for w in words]
    return TextBlock(
        " ".join(w.text for w in words),
        Rect(
            int(min(r.x for r in rects)),
            int(min(r.y for r in rects)),
            int(max(r.x + r.width for r in rects)),
            int(max(r.y + r.height for r in rects)),
        ),
    )
