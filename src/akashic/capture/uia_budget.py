"""Per-app cost tracking for UI Automation walks, with backoff.

Electron apps (Discord) can stutter when their accessibility tree is walked aggressively, so an
app whose recent walks were expensive is walked less often. Thresholds are screenpipe's: moderate
50 ms, heavy 150 ms, critical 250 ms. A skipped walk falls back to the OCR gate.
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


class UiaBudget:
    def __init__(self) -> None:
        self._costs: dict[str, deque[float]] = {}
        self._last_walk: dict[str, float] = {}

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
