"""What capture tells the app about its own health: ``ready``, ``gaming`` or ``degraded``.

Problem codes are fixed strings, never captured content. The screen loop and the audio recorders
detect problems; the publisher thread hands them to ``CaptureStatus``, which turns them into the
one state the supervisor sees. Pausing clears every problem except ``audio_unavailable``.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from akashic import ipc

log = logging.getLogger(__name__)

GRAB_FAILURE_RUN: Final = 3  # consecutive failed grabs that mean the screen cannot be read
# Grab outcomes that say whether the screen can be read; every other outcome is neutral.
FAILED_GRABS: Final = frozenset(
    {"error", "loop_error", "image_unavailable", "stored_no_image", "unchanged_no_image"}
)
GOOD_GRABS: Final = frozenset({"stored", "stored_black", "unchanged", "game_stored", "game_black"})
DEVICE_DOWN_S: Final = 60.0  # seconds without a device read that mean an audio device is gone
AUDIO_UNAVAILABLE: Final = "audio_unavailable"
SCREEN_IMAGES_FAILING: Final = "screen_images_failing"
DEVICE_DOWN_CODES: Final = {"mic": "mic_unavailable", "system": "system_audio_unavailable"}


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
            log.warning("capture problems: %s", ", ".join(sorted(active)) or "none")
