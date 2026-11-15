"""Per-window UI Automation backoff: a slow reader that returned nothing parks its HWND."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from capture_fakes import SCREEN, FakeUia, blank, window, with_text_block
from screen_rig import MON_B, Rig
from store_reads import latest_frames

from akashic.capture import screen as capture_screen
from akashic.capture.ports import WindowInfo
from akashic.capture.uia_budget import UiaBudget

if TYPE_CHECKING:
    from collections.abc import Callable

    from akashic.capture.ports import BrowserFamily, TextBlock


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

    def focused_is_password(self) -> bool | None:
        scripted, value = self._step("password", 0)
        return value if scripted else super().focused_is_password()  # type: ignore[return-value]

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
        rig = make_rig(**kwargs)
        slow = SlowUia(texts=rig.uia.texts)
        rig.uia = slow
        rig.capture._uia = slow
        fake_time = SimpleNamespace(
            monotonic=time.monotonic, time_ns=time.time_ns, perf_counter=lambda: slow.perf[0]
        )
        monkeypatch.setattr(capture_screen, "time", fake_time)
        return rig

    return build


def slow_uia(rig: Rig) -> SlowUia:
    assert isinstance(rig.uia, SlowUia)
    return rig.uia


def test_a_slow_none_url_read_backs_the_window_off_and_skips_the_rest(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    rig.windows.focused = window(process="msedge.exe", title="Page - Edge")
    uia.delay_ms["url"] = 1000
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 1
    assert uia.count("url") == 1
    calls_after_first = len(uia.calls)
    rig.capture.tick(10.0)
    assert len(uia.calls) == calls_after_first  # no UIA call for a backed-off window
    assert rig.capture.stats["excluded:password_unknown"] == 1
    assert latest_frames(rig.conn) == []


def test_a_slow_call_that_returns_a_value_never_backs_off(slow_rig: Callable[..., Rig]) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    rig.windows.focused = window(process="chrome.exe", title="Page - Google Chrome")
    rig.uia.urls[1] = "https://example.org/page"
    uia.delay_ms["url"] = 1500
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 0
    assert len(latest_frames(rig.conn)) == 1


def test_a_fast_none_never_backs_off(slow_rig: Callable[..., Rig]) -> None:
    rig = slow_rig()
    rig.windows.focused = window(process="msedge.exe", title="Page - Edge")
    rig.uia.urls[1] = None  # no omnibox found, instantly
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 0
    first = slow_uia(rig).count("url")
    rig.capture.tick(10.0)
    assert slow_uia(rig).count("url") > first  # still read


def test_a_slow_none_caption_on_chrome_is_caption_unknown_then_skipped(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    rig.windows.focused = window(process="chrome.exe", title="Page - Google Chrome")
    rig.uia.captions[1] = None
    uia.delay_ms["caption"] = 1000
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 1
    assert rig.capture.stats["excluded:caption_unknown"] == 1
    assert uia.count("url") == 0
    rig.capture.tick(10.0)
    assert uia.count("caption") == 1
    assert rig.capture.stats["excluded:password_unknown"] == 1


def test_a_slow_none_password_check_fails_closed_and_skips_the_rest(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    rig.uia.password = None
    uia.delay_ms["password"] = 1000
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 1
    assert rig.capture.stats["excluded:password_unknown"] == 1
    assert latest_frames(rig.conn) == []
    assert uia.count("text") == 0


def test_a_url_that_turns_unreadable_after_a_backoff_rejects_the_frame(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    rig.windows.focused = window(process="chrome.exe", title="Page - Google Chrome")
    uia.script["url"] = [(0.0, "https://example.org/page"), (1000.0, None)]
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 1
    assert rig.capture.stats["window_changed"] == 1
    assert latest_frames(rig.conn) == []


def test_the_backoff_is_per_window_and_recovers_on_the_tick_clock(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig(monitors=[SCREEN, MON_B])
    uia = slow_uia(rig)
    side = window(hwnd=7, process="zen.exe", title="Side - Zen", rect=MON_B)
    other = window(hwnd=9, process="zen.exe", title="Other - Zen", rect=MON_B)
    rig.windows.at[MON_B.center] = side
    uia.script["url"] = [(1000.0, None)]  # the side window hangs once
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 1
    assert uia.count("password") >= 1  # the focused window was still read
    assert uia.count("url", 7) == 1
    rig.capture.tick(15.0)  # next side check is inside the backoff
    assert uia.count("url", 7) == 1
    rig.windows.at[MON_B.center] = other  # another window on that monitor is unaffected
    rig.capture.tick(30.0)
    assert uia.count("url", 9) >= 1
    rig.windows.at[MON_B.center] = side
    rig.capture.tick(60.0)
    assert uia.count("url", 7) > 1  # backoff over


def test_a_backed_off_side_window_is_read_again_at_t_plus_30(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig(monitors=[SCREEN, MON_B])
    uia = slow_uia(rig)
    rig.windows.at[MON_B.center] = window(hwnd=7, process="zen.exe", title="S - Zen", rect=MON_B)
    uia.script["url"] = [(1000.0, None)]
    rig.capture.tick(0.0)
    rig.capture.tick(15.0)
    assert uia.count("url", 7) == 1
    rig.capture.tick(30.0)
    assert uia.count("url", 7) > 1


def test_a_second_slow_failure_after_recovery_starts_a_new_backoff(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    rig.windows.focused = window(process="msedge.exe", title="Page - Edge")
    uia.delay_ms["url"] = 1000
    rig.capture.tick(0.0)
    rig.capture.tick(10.0)
    rig.capture.tick(30.0)
    assert rig.capture.stats["uia_backoff"] == 2
    assert uia.count("url") == 2


def test_a_slow_read_text_never_backs_off(slow_rig: Callable[..., Rig]) -> None:
    rig = slow_rig()
    uia = slow_uia(rig)
    uia.delay_ms["text"] = 1900
    rig.capture.tick(0.0)
    assert rig.capture.stats["uia_backoff"] == 0
    assert uia.count("text") == 1
    assert len(latest_frames(rig.conn)) == 1


def test_a_backed_off_game_window_fails_the_game_frame_password_check(
    slow_rig: Callable[..., Rig],
) -> None:
    budget = UiaBudget()
    budget.record_failure(7, 1000.0, 0.0)
    rig = slow_rig(budget=budget)
    rig.windows.focused = WindowInfo(7, "game.exe", "The Game", SCREEN, has_frame=False)
    rig.capture.tick(0.0)
    assert slow_uia(rig).count("password") == 0
    assert rig.capture.stats["window_changed"] == 1
    assert latest_frames(rig.conn) == []


def test_a_backed_off_side_window_is_not_read_in_gaming_mode(slow_rig: Callable[..., Rig]) -> None:
    budget = UiaBudget()
    budget.record_failure(8, 1000.0, 0.0)
    rig = slow_rig(monitors=[SCREEN, MON_B], budget=budget)
    rig.windows.focused = WindowInfo(7, "game.exe", "The Game", SCREEN, has_frame=False)
    rig.windows.at[MON_B.center] = window(
        hwnd=8, process="chrome.exe", title="Side - Google Chrome", rect=MON_B
    )
    rig.capture.tick(0.0)
    assert slow_uia(rig).count("caption", 8) == 0
    assert rig.capture.stats["excluded:caption_unknown"] == 1


def test_expired_backoffs_are_pruned() -> None:
    budget = UiaBudget()
    assert budget.record_failure(1, 900.0, 0.0)
    assert not budget.window_allowed(1, 29.9)
    assert budget.window_allowed(1, 30.0)
    assert not budget.record_failure(3, 899.0, 5.0)
    assert budget.record_failure(2, 1000.0, 40.0)
    assert budget.window_allowed(1, 0.0)  # hwnd 1's entry is gone, not just expired


def _backed_off_rig(slow_rig: Callable[..., Rig]) -> Rig:
    budget = UiaBudget()
    budget.record_failure(1, 1000.0, 0.0)
    return slow_rig(budget=budget)


def test_a_backed_off_window_with_pixels_skips_read_text_and_uses_ocr(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = _backed_off_rig(slow_rig)
    target = capture_screen._Target(window(), is_focused=True)
    image = with_text_block(blank())
    text, source, _ = rig.capture._read_text(target, image, black=False, ocr=True, now=0.0)
    assert slow_uia(rig).count("text") == 0
    assert (text, source) == ("recognized words", "ocr")
    assert rig.ocr.calls == 1


def test_a_backed_off_window_without_pixels_stores_empty_text(
    slow_rig: Callable[..., Rig],
) -> None:
    rig = _backed_off_rig(slow_rig)
    target = capture_screen._Target(window(), is_focused=True)
    text, source, gate = rig.capture._read_text(target, None, black=False, ocr=True, now=0.0)
    assert slow_uia(rig).count("text") == 0
    assert (text, source, gate) == ("", None, None)
    assert rig.ocr.calls == 0


def _assert_no_leak(rig: Rig, caplog: pytest.LogCaptureFixture, secrets: list[str]) -> None:
    assert rig.capture.stats["uia_backoff"] == 1
    assert isinstance(rig.capture.stats["uia_backoff"], int)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    stats = " ".join(f"{k} {v}" for k, v in rig.capture.stats.items())
    for secret in secrets:
        assert secret not in logged
        assert secret not in stats


def test_a_started_backoff_logs_and_counts_nothing_identifying(
    slow_rig: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="akashic")
    rig = slow_rig()
    rig.windows.focused = window(hwnd=424242, process="zzsentinel.exe", title="ZZ-Sentinel-Title")
    rig.uia.password = None
    slow_uia(rig).delay_ms["password"] = 1000
    rig.capture.tick(0.0)
    _assert_no_leak(rig, caplog, ["zzsentinel", "ZZ-Sentinel-Title", "424242"])


def test_a_started_url_backoff_logs_and_counts_no_url_or_title(
    slow_rig: Callable[..., Rig], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="akashic")
    rig = slow_rig()
    rig.windows.focused = window(
        hwnd=424243, process="chrome.exe", title="ZZ-Sentinel-Title - Google Chrome"
    )
    slow_uia(rig).script["url"] = [(0.0, "https://zz-sentinel.example/secret"), (1000.0, None)]
    rig.capture.tick(0.0)
    _assert_no_leak(rig, caplog, ["zz-sentinel", "ZZ-Sentinel-Title", "424243"])
