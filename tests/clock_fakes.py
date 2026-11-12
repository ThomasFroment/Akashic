"""Controllable clocks and a Europe/Paris time zone around the autumn fall-back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import override

from memory_data import NOW

SWITCH = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)  # Paris: 03:00 CEST becomes 02:00 CET


@dataclass
class ManualClock:
    """A millisecond clock that moves only when a test sets ``now``."""

    now: int = NOW

    def __call__(self) -> int:
        return self.now


class ParisAutumn(tzinfo):
    """Europe/Paris around one fall-back, without depending on the machine's time zone."""

    @override
    def utcoffset(self, dt: datetime | None) -> timedelta:
        del dt
        return timedelta(hours=1)

    @override
    def dst(self, dt: datetime | None) -> timedelta:
        del dt
        return timedelta(0)

    @override
    def tzname(self, dt: datetime | None) -> str:
        del dt
        return "Paris"

    @override
    def fromutc(self, dt: datetime) -> datetime:
        hours = 2 if dt.replace(tzinfo=UTC) < SWITCH else 1
        return (dt + timedelta(hours=hours)).replace(tzinfo=self)


class LocalParis(datetime):
    @override
    def astimezone(self, tz: tzinfo | None = None) -> datetime:
        return super().astimezone(tz or ParisAutumn())
