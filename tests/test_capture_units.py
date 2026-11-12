"""Small pure pieces of the capture package: text-box detection and the OCR language choice."""

from __future__ import annotations

import pytest
from capture_fakes import blank, with_text_block

from akashic.capture import imaging
from akashic.capture.ocr_gate import pick_language


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


# --- OCR gate -----------------------------------------------------------------------------


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
