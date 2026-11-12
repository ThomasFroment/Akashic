"""When may heavy jobs run? Never during a game, and not while the GPU or CPU is busy.

The worker asks before every heavy job, so a game that starts mid-backlog stops it after the
current job. Light jobs ignore the scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from akashic.storage.sessions import gaming_now

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Scheduler as SchedulerSettings


class LoadSource(Protocol):
    def cpu_percent(self) -> float:
        """Busy share of all cores since the previous call, 0-100."""
        ...

    def gpu_percent(self) -> float | None:
        """GPU utilisation 0-100, or None when it can't be measured (no NVIDIA GPU)."""
        ...


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str = ""  # "game", "gpu busy", "cpu busy"; empty when allowed
    cpu: float | None = None  # the readings the verdict was based on (None: not measured)
    gpu: float | None = None


class Scheduler:
    def __init__(self, conn: Connection, load: LoadSource, settings: SchedulerSettings) -> None:
        self._conn = conn
        self._load = load
        self._settings = settings
        self.last: Verdict | None = None  # the latest verdict, for the Debug page

    def limits(self) -> dict[str, int]:
        return {
            "gpu_busy_percent": self._settings.gpu_busy_percent,
            "cpu_busy_percent": self._settings.cpu_busy_percent,
        }

    def heavy_allowed(self, now_ms: int) -> Verdict:
        self.last = self._decide(now_ms)
        return self.last

    def _decide(self, now_ms: int) -> Verdict:
        if gaming_now(self._conn, now_ms):
            return Verdict(allowed=False, reason="game")
        gpu = self._load.gpu_percent()
        cpu = self._load.cpu_percent()
        if gpu is not None and gpu >= self._settings.gpu_busy_percent:
            return Verdict(allowed=False, reason="gpu busy", cpu=cpu, gpu=gpu)
        if cpu >= self._settings.cpu_busy_percent:
            return Verdict(allowed=False, reason="cpu busy", cpu=cpu, gpu=gpu)
        return Verdict(allowed=True, cpu=cpu, gpu=gpu)
