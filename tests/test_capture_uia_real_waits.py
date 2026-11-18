"""The real text wait: a slow text walk, within ``TEXT_WAIT_S``, is still read."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
from capture_fakes import LONG_TEXT, FakeUia, blank, with_text_block

from akashic.capture import uia_threads

if TYPE_CHECKING:
    from collections.abc import Callable

    from screen_rig import Rig

# Read at import, before the autouse fixture makes every wait long for the other tests.
REAL_TEXT_WAIT_S = uia_threads.TEXT_WAIT_S
SLOW_WALK_S = 0.2


@pytest.fixture(autouse=True)
def _real_text_wait(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse
    monkeypatch.setattr(uia_threads, "TEXT_WAIT_S", REAL_TEXT_WAIT_S)


def test_a_text_walk_slower_than_the_address_wait_is_stored_and_does_not_hold_its_browser(
    make_rig: Callable[..., Rig],
) -> None:
    assert SLOW_WALK_S < REAL_TEXT_WAIT_S
    uia = FakeUia(texts={1: LONG_TEXT}, on_read=lambda: time.sleep(SLOW_WALK_S))
    rig = make_rig(uia=uia, shown=False)
    rig.show(1, "chrome.exe", "Page - Google Chrome", url="https://example.org/page",
             image=with_text_block(blank()))  # fmt: skip
    rig.settle(0.0)
    assert len(rig.pictures()) == 1
    assert rig.pictures()[0].uia_text is not None
    assert "uia_unfinished" not in rig.capture.stats_snapshot()
