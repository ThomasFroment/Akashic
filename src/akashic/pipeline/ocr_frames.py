"""Deferred OCR of game-mode screenshots.

While a fullscreen game runs, capture stores one screenshot a minute and reads no text (no UI
Automation walk, no OCR). Each such frame queues an ``ocr_frame`` job; the worker reads the text
once the game is over, and the frame becomes searchable like any other. Heavy, so the scheduler
holds these jobs back during games.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, ClassVar, Protocol

from PIL import Image, UnidentifiedImageError

from akashic.pipeline.jobs import PermanentJobError

if TYPE_CHECKING:
    from akashic.capture.ports import OcrEngine
    from akashic.pipeline.jobs import Job
    from akashic.storage.frames import FrameStore

log = logging.getLogger(__name__)


class OcrFactory(Protocol):
    def __call__(self) -> OcrEngine:
        """Build the engine; raises RuntimeError if no recognizer is installed."""
        ...


class OcrFrameHandler:
    kind: ClassVar[str] = "ocr_frame"
    heavy: ClassVar[bool] = True

    def __init__(self, frames: FrameStore, make_engine: OcrFactory) -> None:
        self._frames = frames
        self._make_engine = make_engine
        self._engine: OcrEngine | None = None

    def blocked(self) -> str | None:
        return None

    def run(self, job: Job) -> None:
        frame_id = job.payload.get("frame_id")
        if not isinstance(frame_id, int):
            msg = "ocr job without a frame id"
            raise PermanentJobError(msg)
        frame = self._frames.get(frame_id)
        if frame is None or frame.text or frame.image_path is None:
            return  # deleted, already read, or no screenshot: nothing to do
        try:
            webp = self._frames.image(frame)
        except FileNotFoundError as exc:
            msg = "screenshot file is gone"
            raise PermanentJobError(msg) from exc
        if webp is None:
            return
        if self._engine is None:
            try:
                self._engine = self._make_engine()
            except RuntimeError as exc:
                msg = "no OCR engine available"
                raise PermanentJobError(msg) from exc
        try:
            with Image.open(io.BytesIO(webp)) as image:
                text = self._engine.recognize(image.convert("RGB")).strip()
        except UnidentifiedImageError as exc:
            msg = "screenshot is not a readable image"
            raise PermanentJobError(msg) from exc
        if text:
            self._frames.set_text(frame_id, text, "ocr")

    def give_up(self, job: Job) -> None:
        del job  # the frame simply stays without text

    def idle(self, *, force: bool = False) -> None:
        """The OCR engine is small and stays loaded."""
