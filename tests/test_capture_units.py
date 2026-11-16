"""Small pure pieces of the capture package: text-box detection and the OCR language choice."""

from __future__ import annotations

import inspect
import io

import pytest
from capture_fakes import FakeOcr, blank, with_text_block
from PIL import Image

from akashic.capture import imaging
from akashic.capture.ocr_gate import OcrGate, pick_language


def test_find_text_box() -> None:
    assert imaging.find_text_box(blank()) is None
    assert imaging.find_text_box(blank(size=(1, 50))) is None
    box = imaging.find_text_box(with_text_block(blank()))
    assert box is not None
    assert box.left <= 100
    assert box.right >= 380
    assert box.top <= 100
    assert box.bottom >= 200
    assert box.width < 800
    assert box.height < 600


def test_encode_webp_keeps_native_resolution() -> None:
    assert not hasattr(imaging, "STORED_MAX_SIDE")
    for size in ((3840, 2160), (5120, 1440)):
        data = imaging.encode_webp(blank(size=size))
        assert Image.open(io.BytesIO(data)).size == size


# --- OCR gate -----------------------------------------------------------------------------


def test_ocr_gate_read_takes_only_a_key_and_a_frame() -> None:
    assert list(inspect.signature(OcrGate.read).parameters) == ["self", "key", "image"]
    ocr = FakeOcr()
    result = OcrGate(ocr).read("k", with_text_block(blank()))
    assert (result.text, result.ocr_ran, ocr.calls) == ("recognized words", True, 1)


@pytest.mark.parametrize(
    ("wanted", "available", "expected"),
    [
        ("fr", ["en-US", "fr-FR"], "fr-FR"),
        ("fr-FR", ["en-US", "fr-FR"], "fr-FR"),
        ("fr", ["de-DE", "en-US"], "en-US"),
        ("fr", ["de-DE"], "de-DE"),
        ("fr", [], None),
    ],
)
def test_pick_language(wanted: str, available: list[str], expected: str | None) -> None:
    assert pick_language(wanted, available) == expected
