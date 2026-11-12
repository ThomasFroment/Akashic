"""The tray icon is the app's logo, coloured by state."""

from __future__ import annotations

import pytest

from akashic.tray import ACCENT, GREY, SIZE, make_icon


def _colours(state: str) -> set[object]:
    """The colours drawn (the shape is all in the alpha channel)."""
    found = make_icon(state).convert("RGB").getcolors()
    assert found is not None
    return {colour for _, colour in found}


@pytest.mark.parametrize(
    "state", ["recording", "gaming", "backlog", "paused", "starting", "stopped"]
)
def test_every_state_draws_the_logo_at_tray_size(state: str) -> None:
    img = make_icon(state)
    assert img.size == (SIZE, SIZE)
    assert img.mode == "RGBA"
    alpha = img.getchannel("A")
    assert alpha.getbbox() is not None  # something is drawn
    assert alpha.getpixel((0, 0)) == 0  # the corners stay transparent


def test_recording_is_terracotta_and_paused_is_grey() -> None:
    assert _colours("recording") == {ACCENT[:3]}
    assert _colours("paused") == {GREY[:3]}


def test_starting_is_a_faded_version_of_recording() -> None:
    on = make_icon("recording").getchannel("A").getextrema()[1]
    faded = make_icon("starting").getchannel("A").getextrema()[1]
    assert isinstance(on, int)
    assert isinstance(faded, int)
    assert 0 < faded < on
