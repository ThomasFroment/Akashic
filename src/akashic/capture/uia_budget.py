"""Per-app rationing of UI Automation text walks, plus a per-window backoff after slow failures.

Electron apps (Discord) can stutter when their accessibility tree is walked aggressively, so an
app whose recent walks were expensive is walked less often. Thresholds are screenpipe's: moderate
50 ms, heavy 150 ms, critical 250 ms. A skipped walk falls back to the OCR gate.

A reader that returned nothing after a slow call (a timeout on a hung window) puts that window
(HWND) out of UI Automation for a while, so a hung app costs one timeout per backoff, not one per
reader per attempt.

``BudgetedUia`` wraps the per-process UIA workers (``capture/uia_threads.py``) with both rules, so
every caller shares one backoff and one budget. Calls are timed on the worker, one measurement for
both rules; a late result can back a window off at the check that receives it. Its stats hold
counts and milliseconds per browser executable, never a title, an address, text, a window handle
or a pid.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import TYPE_CHECKING, Final

from akashic.capture.text import MAX_UIA_NODES
from akashic.capture.uia_threads import UiaRead

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from akashic.capture.ports import BrowserFamily, TextBlock, WindowInfo
    from akashic.capture.uia_threads import ThreadedUia

WINDOW: Final = 8  # walks remembered per app
MODERATE_MS: Final = 50.0
HEAVY_MS: Final = 150.0
CRITICAL_MS: Final = 250.0
# Minimum seconds between walks for each cost level.
MIN_INTERVAL_S: Final = (0.0, 2.0, 6.0, 20.0)
# Below the 1000 ms UIA connection timeout, above the slowest measured healthy read_url (626 ms).
SLOW_CALL_MS: Final = 900.0
# A multiple of `side_interval_s`, so a side window backed off at one of its checks is
# read again at the check where the backoff ends, not one interval later.
HWND_BACKOFF_S: Final = 30.0


class UiaBudget:
    def __init__(self) -> None:
        self._costs: dict[str, deque[float]] = {}
        self._last_walk: dict[str, float] = {}
        self._backoff_until: dict[int, float] = {}

    def window_allowed(self, hwnd: int, now: float) -> bool:
        until = self._backoff_until.get(hwnd)
        return until is None or now >= until

    def record_failure(self, hwnd: int, cost_ms: float, now: float) -> bool:
        """Back the window off if a failed reader was slow; returns whether it did."""
        if cost_ms < SLOW_CALL_MS:
            return False
        self._backoff_until = {h: t for h, t in self._backoff_until.items() if t > now}
        self._backoff_until[hwnd] = now + HWND_BACKOFF_S
        return True

    def average_ms(self, process: str) -> float:
        costs = self._costs.get(process)
        return sum(costs) / len(costs) if costs else 0.0

    def min_interval(self, process: str) -> float:
        avg = self.average_ms(process)
        level = sum(avg >= t for t in (MODERATE_MS, HEAVY_MS, CRITICAL_MS))
        return MIN_INTERVAL_S[level]

    def allow(self, process: str, now: float) -> bool:
        last = self._last_walk.get(process)
        return last is None or now - last >= self.min_interval(process)

    def record(self, process: str, cost_ms: float, now: float) -> None:
        self._costs.setdefault(process, deque(maxlen=WINDOW)).append(cost_ms)
        self._last_walk[process] = now


class BudgetedUia:
    def __init__(self, threads: ThreadedUia, budget: UiaBudget | None = None) -> None:
        self._threads = threads
        self._budget = budget or UiaBudget()
        self.stats: Counter[str] = Counter()

    def caption(self, window: WindowInfo, family: BrowserFamily, now: float) -> UiaRead[str]:
        """A browser frame's caption; unfinished while its worker is busy, a finished None when
        unreadable or the window is backed off."""
        return self._browser_read(window, now, lambda: self._threads.caption(window, family))

    def address(
        self, window: WindowInfo, family: BrowserFamily, now: float, *, ignore_backoff: bool = False
    ) -> UiaRead[str]:
        """A browser's address bar text as written; unfinished while its worker is busy, a
        finished None when unreadable or backed off (unless `ignore_backoff`, which reads anyway;
        a slow None then still renews the backoff)."""
        return self._browser_read(
            window,
            now,
            lambda: self._threads.address(window, family),
            ignore_backoff=ignore_backoff,
        )

    def backed_off(self, hwnd: int, now: float) -> bool:
        """Whether the window's browser readers are skipped at `now` (their reads are not real)."""
        return not self._budget.window_allowed(hwnd, now)

    def text(
        self, window: WindowInfo, now: float, max_nodes: int = MAX_UIA_NODES
    ) -> Sequence[TextBlock] | None:
        """The window's text blocks; None when it is backed off, over its app's budget, failed,
        unfinished or started before `now` (a late result only records its cost). A slow text
        walk never backs the window off: only the caption and address readers do."""
        if not (
            self._budget.window_allowed(window.hwnd, now)
            and self._budget.allow(window.process, now)
        ):
            return None
        read = self._threads.text(window, max_nodes)
        if read.started_at is None:
            self.stats["uia_unfinished"] += 1
            return None
        self._budget.record(window.process, read.ms, now)
        return read.value if read.started_since(now) is not None else None

    def _browser_read(
        self,
        window: WindowInfo,
        now: float,
        read: Callable[[], UiaRead[str]],
        *,
        ignore_backoff: bool = False,
    ) -> UiaRead[str]:
        """Run a reader unless its window is backed off. A slow call that returned nothing (a
        timeout) backs the window off until `now` + HWND_BACKOFF_S."""
        if not ignore_backoff and not self._budget.window_allowed(window.hwnd, now):
            return UiaRead(None, now)
        result = read()
        if result.started_at is None:
            self.stats["uia_unfinished"] += 1
            return result
        self.stats[f"uia_reads:{window.process}"] += 1
        self.stats[f"uia_ms:{window.process}"] += round(result.ms)
        if result.value is None and self._budget.record_failure(window.hwnd, result.ms, now):
            self.stats["uia_backoff"] += 1
        return result
