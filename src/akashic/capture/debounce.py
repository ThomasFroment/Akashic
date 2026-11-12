"""Trailing-edge debounce for capture triggers.

A trigger fires ``delay`` after its *last* occurrence, so a burst of activity becomes one capture
once it settles. Continuous activity (the mouse never stops moving) would push it back forever,
so it also fires at most ``MAX_WAIT_S`` after its *first* occurrence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from akashic.capture.ports import Trigger

DEBOUNCE_S: Final[dict[str, float]] = {
    "focus": 0.3,
    "title": 0.5,
    "input": 0.4,  # keyboard or mouse activity (GetLastInputInfo changed)
}
MAX_WAIT_S: Final = 2.0


class Debouncer:
    def __init__(self, delays: dict[str, float] | None = None) -> None:
        self._delays = DEBOUNCE_S if delays is None else delays
        self._due: dict[Trigger, float] = {}
        self._first: dict[Trigger, float] = {}

    def note(self, trigger: Trigger, now: float) -> None:
        first = self._first.setdefault(trigger, now)
        self._due[trigger] = min(now + self._delays.get(trigger, 0.0), first + MAX_WAIT_S)

    def defer(self, trigger: Trigger, until: float) -> None:
        """Push a trigger later (e.g. the per-monitor minimum gap has not elapsed)."""
        self._due[trigger] = max(until, self._due.get(trigger, until))

    def next_due(self) -> float | None:
        return min(self._due.values(), default=None)

    def pop_due(self, now: float) -> list[Trigger]:
        ready: list[Trigger] = [t for t, due in self._due.items() if due <= now]
        ready.sort(key=lambda t: self._due[t])
        for trigger in ready:
            del self._due[trigger]
            self._first.pop(trigger, None)
        return ready

    def clear(self) -> None:
        self._due.clear()
        self._first.clear()
