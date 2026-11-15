"""Per-app rationing of UI Automation text walks, plus a per-window backoff after slow failures.

Electron apps (Discord) can stutter when their accessibility tree is walked aggressively, so an
app whose recent walks were expensive is walked less often. Thresholds are screenpipe's: moderate
50 ms, heavy 150 ms, critical 250 ms. A skipped walk falls back to the OCR gate.

A reader that returned nothing after a slow call (a timeout on a hung window) puts that window
(HWND) out of UI Automation for a while, so a hung app costs one timeout per backoff, not one per
reader per attempt.
"""

from __future__ import annotations

from collections import deque
from typing import Final

WINDOW: Final = 8  # walks remembered per app
MODERATE_MS: Final = 50.0
HEAVY_MS: Final = 150.0
CRITICAL_MS: Final = 250.0
# Minimum seconds between walks for each cost level.
MIN_INTERVAL_S: Final = (0.0, 2.0, 6.0, 20.0)
# Below the 1000 ms UIA connection timeout, above the slowest measured healthy read_url (626 ms).
SLOW_CALL_MS: Final = 900.0
# A multiple of `other_monitor_interval_s`, so a side window backed off at one of its checks is
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
