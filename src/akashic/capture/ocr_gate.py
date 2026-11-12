"""OCR only when the text actually changed (research §3.2).

For each app: crop to the window, shrink the crop to the cells that look like text, and compare
that crop's pixels with the crop behind the last *stored* OCR result. Unchanged: reuse the stored
text without running OCR. The signature is only remembered by ``commit``, which the caller calls
after the database write succeeded, so a failed write retries naturally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture import imaging

MAX_CACHED_WINDOWS: Final = 32

if TYPE_CHECKING:
    from collections.abc import Sequence

    from PIL import Image

    from akashic.capture.ports import OcrEngine, Rect


@dataclass(frozen=True, slots=True)
class GateResult:
    text: str
    signature: imaging.Signature | None  # None: no text-like area on screen
    ocr_ran: bool


def pick_language(wanted: str, available: Sequence[str]) -> str | None:
    """Choose a recognizer tag. Exact tag, then same language (``fr`` -> ``fr-FR``), then English
    (the other Latin-script language we expect), then whatever is installed."""
    lowered = {tag.casefold(): tag for tag in available}
    want = wanted.casefold()
    if want in lowered:
        return lowered[want]
    for tag_lower, tag in lowered.items():
        if tag_lower.split("-")[0] == want.split("-")[0]:
            return tag
    for tag_lower, tag in lowered.items():
        if tag_lower.split("-")[0] == "en":
            return tag
    return next(iter(available), None)


class OcrGate:
    def __init__(self, engine: OcrEngine) -> None:
        self._engine = engine
        self._indexed: dict[str, tuple[imaging.Signature | None, str]] = {}

    def read(self, key: str, frame: Image.Image, region: Rect) -> GateResult:
        """Text on screen inside ``region`` (frame pixels). Runs OCR only if the crop changed."""
        crop = frame.crop((region.left, region.top, region.right, region.bottom))
        box = imaging.find_text_box(crop)
        sig: imaging.Signature | None = None
        if box is not None:
            crop = crop.crop((box.left, box.top, box.right, box.bottom))
            sig = imaging.signature(crop)
        previous = self._indexed.get(key)
        if previous is not None and _same(previous[0], sig):
            return GateResult(previous[1], sig, ocr_ran=False)
        text = self._engine.recognize(crop) if box is not None else ""
        return GateResult(text, sig, ocr_ran=box is not None)

    def commit(self, key: str, result: GateResult) -> None:
        """Remember ``result`` as the indexed state for ``key`` (after the DB write succeeded)."""
        # Window handles churn over long sessions; retain only recent stored image signatures.
        self._indexed.pop(key, None)
        if len(self._indexed) >= MAX_CACHED_WINDOWS:
            del self._indexed[next(iter(self._indexed))]
        self._indexed[key] = (result.signature, result.text)


def _same(a: imaging.Signature | None, b: imaging.Signature | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return not imaging.differs(a, b)
