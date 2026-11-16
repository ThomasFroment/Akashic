"""Per-window UI Automation backoff: a slow reader that returned nothing parks its HWND."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from capture_fakes import LONG_TEXT, SCREEN, FakeUia, blank, window, with_text_block
from screen_rig import MON_B, Rig
from store_reads import latest_frames

from akashic.capture import uia_budget
from akashic.capture.uia_budget import BudgetedUia, UiaBudget

if TYPE_CHECKING:
    from collections.abc import Callable

    from akashic.capture.ports import BrowserFamily, TextBlock, WindowInfo


@dataclass
class SlowUia(FakeUia):
    """Logs each reader call and advances a fake perf clock. ``script`` queues scripted
    (delay_ms, result) pairs per reader, consumed before the defaults apply."""

    perf: list[float] = field(default_factory=lambda: [0.0])
    delay_ms: dict[str, float] = field(default_factory=dict[str, float])
    script: dict[str, list[tuple[float, object]]] = field(
        default_factory=dict[str, "list[tuple[float, object]]"]
    )
    calls: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])

    def _step(self, name: str, hwnd: int) -> tuple[bool, object]:
        self.calls.append((name, hwnd))
        queued = self.script.get(name)
        if queued:
            delay, value = queued.pop(0)
            self.perf[0] += delay / 1000
            return True, value
        self.perf[0] += self.delay_ms.get(name, 0.0) / 1000
        return False, None

    def count(self, name: str, hwnd: int | None = None) -> int:
        return sum(1 for n, h in self.calls if n == name and hwnd in (None, h))

    def browser_caption(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        scripted, value = self._step("caption", window.hwnd)
        return value if scripted else super().browser_caption(window, family)  # type: ignore[return-value]

    def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        scripted, value = self._step("url", window.hwnd)
        return value if scripted else super().read_url(window, family)  # type: ignore[return-value]

    def read_text(self, window: WindowInfo, max_nodes: int) -> list[TextBlock] | None:
        self._step("text", window.hwnd)
        return super().read_text(window, max_nodes)


@pytest.fixture
def slow_rig(make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch) -> Callable[..., Rig]:
    def build(**kwargs: object) -> Rig:
        slow = SlowUia(texts={1: LONG_TEXT})
        rig = make_rig(uia=slow, **kwargs)
        fake_time = SimpleNamespace(
            monotonic=time.monotonic, time_ns=time.time_ns, perf_counter=lambda: slow.perf[0]
        )
        monkeypatch.setattr(uia_budget, "time", fake_time)
        return rig

    return build


def slow_uia(rig: Rig) -> SlowUia:
    assert isinstance(rig.uia, SlowUia)
    return rig.uia


def test_a_slow_none_url_read_backs_the_window_off_and_skips_the_rest(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig(shown=False)
    uia = slow_uia(rig)
    rig.show(1, "msedge.exe", "Page - Edge")
    uia.delay_ms["url"] = 1000
    uia.urls[1] = None
    rig.tick(0.0)
    assert rig.capture.stats_snapshot()["uia_backoff"] == 1
    assert uia.count("url") == 1
    calls_after_first = len(uia.calls)
    rig.tick(10.0)
    assert len(uia.calls) == calls_after_first  # no UIA call for a backed-off window


def test_a_slow_call_that_returns_a_value_never_backs_off(slow_rig: Callable[..., Rig]) -> None:
    rig = slow_rig(shown=False)
    uia = slow_uia(rig)
    rig.show(1, "chrome.exe", "Page - Google Chrome", url="https://example.org/page")
    uia.delay_ms["url"] = 1500
    rig.settle(0.0)
    assert rig.capture.stats_snapshot().get("uia_backoff", 0) == 0
    assert len(rig.pictures()) == 1


def test_a_fast_none_never_backs_off(slow_rig: Callable[..., Rig]) -> None:
    rig = slow_rig(shown=False)
    rig.show(1, "msedge.exe", "Page - Edge")
    rig.uia.urls[1] = None  # no omnibox found, instantly
    rig.tick(0.0)
    assert rig.capture.stats_snapshot().get("uia_backoff", 0) == 0
    first = slow_uia(rig).count("url")
    rig.tick(10.0)
    assert slow_uia(rig).count("url") > first  # still read


def test_a_slow_none_caption_on_chrome_hides_the_window_and_the_text_is_not_read(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig(shown=False)
    uia = slow_uia(rig)
    rig.show(1, "chrome.exe", "Page - Google Chrome", url="https://example.org/page")
    uia.captions[1] = None
    uia.delay_ms["caption"] = 1000
    rig.tick(0.0)
    assert rig.capture.stats_snapshot()["uia_backoff"] == 1
    assert rig.last_note() == "hidden"  # caption unknown: excluded
    rig.tick(10.0)  # backed off: the caption is not asked again, the window stays hidden
    assert uia.count("caption") == 1
    assert uia.text_reads == []


def test_a_backed_off_window_skips_its_text_read_and_the_writer_uses_ocr(
    slow_rig: Callable[..., Rig],
) -> None:
    budget = UiaBudget()
    budget.record_failure(1, 1000.0, 0.0)
    rig = slow_rig(budget=budget, shown=False)
    rig.show(1, image=with_text_block(blank()))
    rig.settle(0.0)
    rig.drain()
    assert slow_uia(rig).count("text") == 0
    assert rig.pictures()[0].uia_text is None
    frame = latest_frames(rig.conn)[0]
    assert (frame.text, frame.text_source) == ("recognized words", "ocr")


def test_the_backoff_is_per_window_and_a_side_window_is_read_again_at_t_plus_30(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig(monitors=[SCREEN, MON_B])
    uia = slow_uia(rig)
    rig.show(7, "zen.exe", "Side - Zen", MON_B, focus=False, url="https://example.org/side")
    uia.script["url"] = [(1000.0, None)]  # the side window hangs once
    rig.settle(0.0)
    assert rig.capture.stats_snapshot()["uia_backoff"] == 1
    assert uia.count("text", 1) >= 1  # the focused window was still read
    assert uia.count("url", 7) == 1
    rig.tick(15.0)  # the next side check is inside the backoff
    assert uia.count("url", 7) == 1
    rig.tick(30.0)  # the backoff ends at 0 + 30
    assert uia.count("url", 7) > 1


def test_a_second_slow_failure_after_recovery_starts_a_new_backoff(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig(shown=False)
    uia = slow_uia(rig)
    rig.show(1, "msedge.exe", "Page - Edge")
    uia.urls[1] = None
    uia.delay_ms["url"] = 1000
    rig.tick(0.0)
    rig.tick(10.0)
    rig.tick(30.0)
    assert rig.capture.stats_snapshot()["uia_backoff"] == 2
    assert uia.count("url") == 2


def test_a_slow_read_text_never_backs_off(slow_rig: Callable[..., Rig]) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    uia.delay_ms["text"] = 1900
    rig.settle(0.0)
    assert rig.capture.stats_snapshot().get("uia_backoff", 0) == 0
    assert uia.count("text") == 1
    assert len(rig.pictures()) == 1


def test_a_backed_off_game_window_asks_uia_for_nothing(slow_rig: Callable[..., Rig]) -> None:
    budget = UiaBudget()
    budget.record_failure(7, 1000.0, 0.0)
    rig = slow_rig(budget=budget, shown=False)
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.settle(0.0)
    assert slow_uia(rig).calls == []


@pytest.mark.parametrize("backed_off", [True, False])
def test_in_gaming_mode_a_side_browser_is_judged_unless_it_is_backed_off(
    slow_rig: Callable[..., Rig], *, backed_off: bool
) -> None:
    budget = UiaBudget()
    if backed_off:
        budget.record_failure(8, 1000.0, 0.0)
    rig = slow_rig(monitors=[SCREEN, MON_B], budget=budget, shown=False)
    rig.show(8, "chrome.exe", "Side - Google Chrome", MON_B, focus=False, url="https://a.org/")
    rig.show(7, "game.exe", "The Game", has_frame=False)
    rig.tick(0.0)
    uia = slow_uia(rig)
    assert (uia.count("caption", 8), uia.count("url", 8)) == ((0, 0) if backed_off else (1, 1))
    assert uia.text_reads == []


def _assert_no_leak(rig: Rig, caplog: pytest.LogCaptureFixture, secrets: list[str]) -> None:
    assert rig.capture.stats_snapshot()["uia_backoff"] == 1
    assert isinstance(rig.capture.stats_snapshot()["uia_backoff"], int)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    stats = " ".join(f"{k} {v}" for k, v in rig.capture.stats_snapshot().items())
    for secret in secrets:
        assert secret not in logged
        assert secret not in stats


def test_a_started_backoff_logs_and_counts_nothing_identifying(
    slow_rig: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="akashic")
    rig = slow_rig(shown=False)
    rig.show(424242, "msedge.exe", "ZZ-Sentinel-Title")
    rig.uia.urls[424242] = None
    slow_uia(rig).delay_ms["url"] = 1000
    rig.tick(0.0)
    _assert_no_leak(rig, caplog, ["ZZ-Sentinel-Title", "424242"])


def test_a_started_url_backoff_logs_and_counts_no_url_or_title(
    slow_rig: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="akashic")
    rig = slow_rig(shown=False)
    rig.show(424243, "chrome.exe", "ZZ-Sentinel-Title - Google Chrome")
    slow_uia(rig).script["url"] = [(1000.0, None)]
    rig.tick(0.0)
    _assert_no_leak(rig, caplog, ["zz-sentinel", "ZZ-Sentinel-Title", "424243"])


def test_expired_backoffs_are_pruned() -> None:
    budget = UiaBudget()
    assert budget.record_failure(1, 900.0, 0.0)
    assert not budget.window_allowed(1, 29.9)
    assert budget.window_allowed(1, 30.0)
    assert not budget.record_failure(3, 899.0, 5.0)
    assert budget.record_failure(2, 1000.0, 40.0)
    assert budget.window_allowed(1, 0.0)  # hwnd 1's entry is gone, not just expired


def _budgeted(
    slow_rig: Callable[..., Rig], budget: UiaBudget | None = None
) -> tuple[BudgetedUia, SlowUia]:
    slow = slow_uia(slow_rig(budget=budget))
    return BudgetedUia(slow, budget), slow


def test_address_returns_the_raw_bar_text_and_counts_the_read(
    slow_rig: Callable[..., Rig],
) -> None:
    uia, slow = _budgeted(slow_rig)
    slow.script["url"] = [(40.0, "  example.org/page ")]
    assert uia.address(window(process="zen.exe"), "firefox", 0.0) == "  example.org/page "
    assert uia.stats["uia_reads:zen.exe"] == 1
    assert uia.stats["uia_ms:zen.exe"] == 40


def test_a_backed_off_hwnd_gets_none_from_caption_and_address_without_a_call(
    slow_rig: Callable[..., Rig],
) -> None:
    uia, slow = _budgeted(slow_rig)
    slow.script["url"] = [(1000.0, None)]
    chrome = window(hwnd=5, process="chrome.exe")
    assert uia.address(chrome, "chromium", 10.0) is None
    assert uia.stats["uia_backoff"] == 1
    calls = len(slow.calls)
    assert uia.caption(chrome, "chromium", 39.9) is None
    assert uia.address(chrome, "chromium", 39.9) is None
    assert len(slow.calls) == calls
    assert uia.caption(chrome, "chromium", 40.0) == "Notes"  # backoff ended at now + 30


def test_a_slow_none_caption_backs_off_and_a_fast_one_does_not(
    slow_rig: Callable[..., Rig],
) -> None:
    uia, slow = _budgeted(slow_rig)
    slow.script["caption"] = [(850.0, None), (950.0, None)]
    assert uia.caption(window(hwnd=1), "chromium", 0.0) is None
    assert uia.stats["uia_backoff"] == 0
    assert uia.caption(window(hwnd=2), "chromium", 0.0) is None
    assert uia.stats["uia_backoff"] == 1


def test_text_skips_the_reader_when_backed_off_or_over_budget(
    slow_rig: Callable[..., Rig],
) -> None:
    budget = UiaBudget()
    budget.record_failure(1, 1000.0, 0.0)
    uia, slow = _budgeted(slow_rig, budget)
    assert uia.text(window(hwnd=1), 5.0) is None
    budget.record(window(hwnd=2).process, 300.0, 5.0)  # critical cost: next walk 20 s later
    assert uia.text(window(hwnd=2), 6.0) is None
    assert slow.count("text") == 0


def test_text_reads_records_the_cost_and_never_backs_off(slow_rig: Callable[..., Rig]) -> None:
    budget = UiaBudget()
    uia, slow = _budgeted(slow_rig, budget)
    slow.delay_ms["text"] = 1500
    assert uia.text(window(hwnd=3), 0.0) is None  # no texts configured: a None read
    assert slow.count("text") == 1
    assert budget.average_ms("notepad.exe") == pytest.approx(1500)
    assert budget.window_allowed(3, 0.0)
    assert uia.stats["uia_backoff"] == 0


def test_stats_are_keyed_by_browser_exe_only(slow_rig: Callable[..., Rig]) -> None:
    uia, _ = _budgeted(slow_rig)
    uia.caption(window(hwnd=424244, process="firefox.exe", title="Secret"), "firefox", 0.0)
    assert set(uia.stats) <= {"uia_reads:firefox.exe", "uia_ms:firefox.exe"}
    assert not [k for k in uia.stats if "424244" in k or "Secret" in k]
