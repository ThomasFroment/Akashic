"""Deferred OCR of whole-window captures.

While a fullscreen game runs, capture reads no text (no UI Automation walk, no OCR). It stores
the window image as an encrypted capture on the window row and queues an ``ocr_window`` job. Once
the game is over the worker reads the text, stores it on the window so it becomes searchable like
any other, and deletes the capture. Heavy, so the scheduler holds these jobs back during games.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, ClassVar, Protocol

from PIL import Image, UnidentifiedImageError

from akashic.storage.crypto import CryptoError
from akashic.storage.frames import OCR_WINDOW
from akashic.storage.jobs import PermanentJobError

if TYPE_CHECKING:
    from akashic.capture.ports import OcrEngine
    from akashic.storage.frames import FrameStore
    from akashic.storage.jobs import Job

log = logging.getLogger(__name__)


class OcrFactory(Protocol):
    def __call__(self) -> OcrEngine:
        """Build the engine; raises RuntimeError if no recognizer is installed."""
        ...


class OcrWindowHandler:
    kind: ClassVar[str] = OCR_WINDOW
    heavy: ClassVar[bool] = True

    def __init__(self, frames: FrameStore, make_engine: OcrFactory) -> None:
        self._frames = frames
        self._make_engine = make_engine
        self._engine: OcrEngine | None = None

    def blocked(self) -> str | None:
        return None

    def run(self, job: Job) -> None:
        window_id = job.payload.get("window_id")
        if not isinstance(window_id, int):
            msg = "ocr job without a window id"
            raise PermanentJobError(msg)
        try:
            webp = self._frames.window_capture(window_id)
        except FileNotFoundError as exc:
            msg = "capture file is gone"
            raise PermanentJobError(msg) from exc
        except CryptoError as exc:
            msg = "capture is not readable"
            raise PermanentJobError(msg) from exc
        if webp is None:
            return  # window deleted, or already read
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
            msg = "capture is not a readable image"
            raise PermanentJobError(msg) from exc
        self._frames.finish_capture(window_id, text or None)

    def give_up(self, job: Job) -> None:
        window_id = job.payload.get("window_id")
        if isinstance(window_id, int):
            self._frames.finish_capture(window_id, None)

    def idle(self, *, force: bool = False) -> None:
        """The OCR engine is small and stays loaded."""
