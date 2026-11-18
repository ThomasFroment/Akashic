"""Unfinished and late UIA reads through the screen loop: they fail closed and fall back to OCR."""

from __future__ import annotations

import itertools
import threading
from typing import TYPE_CHECKING

import pytest
from capture_fakes import LONG_TEXT, FakeUia, GatedUia, blank, with_text_block
from store_reads import latest_frames

from akashic.capture import uia_threads
from akashic.capture.uia_threads import ThreadedUia

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from screen_rig import Rig

    from akashic.capture.ports import TopWindow


class Waits(threading.Event):
    """Stops the loop after ``limit`` waits, without waiting."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.count = 0

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.count += 1
        if self.count >= self.limit:
            self.set()
        return self.is_set()


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse
    monkeypatch.setattr(uia_threads, "WAIT_S", 0.05)
    monkeypatch.setattr(uia_threads, "TEXT_WAIT_S", 0.05)


def text_rig(make_rig: Callable[..., Rig]) -> tuple[Rig, GatedUia]:
    gated = GatedUia(texts={1: LONG_TEXT}, gated=frozenset({"text"}))
    rig = make_rig(uia=gated, shown=False)
    rig.show(1, image=with_text_block(blank()))
    return rig, gated


def last_source(rig: Rig) -> str | None:
    rig.drain()
    return latest_frames(rig.conn)[0].text_source


def test_an_unfinished_text_read_falls_back_to_ocr_and_is_counted(
    make_rig: Callable[..., Rig],
) -> None:
    rig, gated = text_rig(make_rig)
    rig.settle(0.0)
    assert rig.pictures()[0].uia_text is None
    assert last_source(rig) == "ocr"
    assert rig.capture.stats_snapshot()["uia_unfinished"] == 1
    gated.release.set()


def test_a_late_text_is_not_used_and_the_next_read_is_fresh(make_rig: Callable[..., Rig]) -> None:
    rig, gated = text_rig(make_rig)
    rig.settle(0.0)  # the text read misses the wait
    gated.release.set()
    rig.wait_until_uia_idle()
    rig.repaint(1, with_text_block(blank("gray")))
    rig.settle(5.0)
    assert rig.pictures()[-1].uia_text is None  # the late text started before this check
    rig.repaint(1, with_text_block(blank("silver")))
    rig.settle(10.0)
    assert rig.pictures()[-1].uia_text is not None


def record_listing_and_retiring(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    listed = rig.facts.windows

    def windows() -> list[TopWindow] | None:
        events.append("windows")
        return listed()

    def retire(_threads: ThreadedUia, _live_pids: Collection[int]) -> None:
        events.append("retire")

    monkeypatch.setattr(rig.facts, "windows", windows)
    monkeypatch.setattr(ThreadedUia, "retire", retire)
    return events


def test_workers_are_retired_after_the_window_list_is_read(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig()
    events = record_listing_and_retiring(rig, monkeypatch)
    rig.settle(0.0)
    assert events[:2] == ["windows", "retire"]


def tripped_rig(make_rig: Callable[..., Rig], gated: GatedUia) -> Rig:
    ticks = itertools.count(10, 2)
    rig = make_rig(
        uia=gated, shown=False, api_origin="http://127.0.0.1:5123", clock=lambda: float(next(ticks))
    )
    rig.show(1, "chrome.exe", "A page - Google Chrome", url="127.0.0.1:5123/")
    rig.tick(0.0)
    assert rig.own_flags == [True]
    return rig


def test_workers_are_retired_after_the_window_list_is_read_in_the_failsafe_watch(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    gated = GatedUia(gated=frozenset())
    rig = tripped_rig(make_rig, gated)
    events = record_listing_and_retiring(rig, monkeypatch)
    rig.capture.run(Waits(2), threading.Event(), threading.Event())
    assert events[:2] == ["windows", "retire"]


def test_the_failsafe_stays_halted_on_an_unfinished_or_late_address(
    make_rig: Callable[..., Rig],
) -> None:
    gated = GatedUia(gated=frozenset({"address"}))
    gated.release.set()
    rig = tripped_rig(make_rig, gated)
    rig.uia.urls[1] = "example.org/page"  # the own address is gone
    gated.release.clear()
    rig.capture.run(Waits(2), threading.Event(), threading.Event())  # unfinished
    assert rig.own_flags == [True]
    gated.release.set()
    rig.wait_until_uia_idle()
    rig.capture.run(Waits(2), threading.Event(), threading.Event())  # late
    assert rig.own_flags == [True]
    rig.capture.run(Waits(2), threading.Event(), threading.Event())  # fresh
    assert rig.own_flags == [True, False]


def test_an_empty_text_read_is_not_followed_by_a_window_or_address_read(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = make_rig(uia=FakeUia(texts={1: None}), shown=False)
    events: list[str] = []
    for owner, name, label in (
        (rig.uia, "read_text", "text"),
        (rig.uia, "read_url", "address"),
        (rig.windows, "focused_window", "focused"),
    ):
        real = getattr(owner, name)

        def spy(*args: object, _real: Callable[..., object] = real, _label: str = label) -> object:
            events.append(_label)
            return _real(*args)

        monkeypatch.setattr(owner, name, spy)
    rig.show(1, "chrome.exe", "A page - Google Chrome", url="example.org/page")
    rig.show(1, image=with_text_block(blank()))
    rig.settle(0.0)
    assert "text" in events
    after = events[len(events) - events[::-1].index("text") :]
    assert "focused" not in after
    assert "address" not in after
