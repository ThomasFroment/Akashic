"""What capture tells the app about its own health: ``ready``, ``gaming`` or ``degraded``.

Problem codes are fixed strings, never captured content; a screen problem is followed by the
label of its monitor ("Monitor 2", counted left to right). The screen loop and the audio recorders
detect problems; the publisher thread hands them to ``CaptureStatus``, which turns them into the
one state the supervisor sees. Pausing clears every problem except ``audio_unavailable``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from akashic import ipc

log = logging.getLogger(__name__)

GRAB_FAILURE_RUN: Final = 3  # consecutive failed grabs or drops that mean the screen is in trouble
DEVICE_DOWN_S: Final = 60.0  # seconds without a device read that mean an audio device is gone
DROP_PROBLEM_S: Final = 60.0  # seconds a dropped audio segment keeps counting as a problem
HOOK_DOWN_PROBLEM_S: Final = 30.0  # seconds of unhealthy drains that make a problem
AUDIO_UNAVAILABLE: Final = "audio_unavailable"
WINDOW_EVENTS_DOWN: Final = "window_events_down"
SCREEN_IMAGES_FAILING: Final = "screen_images_failing"
SCREEN_FRAMES_DROPPED: Final = "screen_frames_dropped"
SCREEN_DISK_FULL: Final = "screen_disk_full"
MONITOR_SEPARATOR: Final = "@"  # a screen problem reads "<code>@<monitor label>"
DEVICE_DOWN_CODES: Final = {"mic": "mic_unavailable", "system": "system_audio_unavailable"}
SEGMENTS_DROPPED_CODES: Final = {"mic": "mic_segments_dropped", "system": "system_segments_dropped"}

type RunKind = Literal["failure", "disk_full", "capture", "drop"]
RUN_PROBLEMS: Final[dict[RunKind, str]] = {
    "failure": SCREEN_IMAGES_FAILING,
    "disk_full": SCREEN_DISK_FULL,
    "capture": SCREEN_IMAGES_FAILING,
    "drop": SCREEN_FRAMES_DROPPED,
}
RUN_LENGTHS: Final[dict[RunKind, int]] = {  # events in a row that make each kind a problem
    "failure": 1,
    "disk_full": 1,
    "capture": GRAB_FAILURE_RUN,
    "drop": GRAB_FAILURE_RUN,
}


class MonitorHealth:
    """Per-monitor runs of failures, capture failures and drops. Not thread-safe."""

    def __init__(self) -> None:
        self._runs: dict[tuple[str, RunKind], deque[float]] = {}

    def record(self, key: str, kind: RunKind, now: float) -> None:
        self._runs.setdefault((key, kind), deque(maxlen=RUN_LENGTHS[kind])).append(now)

    def clear(self, key: str, kinds: Iterable[RunKind]) -> None:
        for kind in kinds:
            self._runs.pop((key, kind), None)

    def problems(self, since: float, labels: Mapping[str, str]) -> list[str]:
        """``code@monitor label`` of each run that holds its ``RUN_LENGTHS`` events, all after
        ``since``. A monitor without a label is named by its key."""
        return sorted(
            {
                f"{RUN_PROBLEMS[kind]}{MONITOR_SEPARATOR}{labels.get(key, key)}"
                for (key, kind), run in self._runs.items()
                if len(run) == RUN_LENGTHS[kind] and run[0] >= since
            }
        )


class ActiveSetter(Protocol):
    def __call__(self, state: ipc.State, *, epoch: int | None) -> bool: ...


class CaptureStatus:
    """Combines the game flag and the active problems into the state capture reports."""

    def __init__(
        self,
        set_active: ActiveSetter,
        pause_epoch: Callable[[], int],
        *,
        audio_failed: bool,
    ) -> None:
        self._set_active = set_active
        self._pause_epoch = pause_epoch
        self.sticky: tuple[str, ...] = (AUDIO_UNAVAILABLE,) if audio_failed else ()
        self._lock = threading.Lock()
        self._gaming = False
        self._problems: frozenset[str] = frozenset()
        self._problems_epoch = -1
        self._logged: frozenset[str] = frozenset()

    def set_gaming(self, active: bool) -> None:  # noqa: FBT001 - the on_game callback shape
        with self._lock:
            self._gaming = active
            self._apply()

    def set_problems(self, problems: Iterable[str], epoch: int) -> None:
        """``epoch`` is the pause epoch the problems were detected under: after a pause or a
        resume they are stale and ignored until the next publish."""
        with self._lock:
            self._problems = frozenset(problems)
            self._problems_epoch = epoch
            self._apply()

    def _apply(self) -> None:
        epoch = self._pause_epoch()
        fresh: frozenset[str] = (
            self._problems if self._problems_epoch == epoch else frozenset[str]()
        )
        active = fresh | frozenset(self.sticky)
        state: ipc.State = "degraded" if active else "gaming" if self._gaming else "ready"
        if self._set_active(state, epoch=epoch) and active != self._logged:
            self._logged = active
            if active:
                log.warning("capture problems: %s", ", ".join(sorted(active)))
            else:
                log.info("capture problems cleared")
