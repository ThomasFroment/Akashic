"""What the screen loop learned about browser windows, handed to the media tracker.

Windows does not say which browser tab publishes a media session, and the address of a tab is
only read by the screen loop (UI Automation). So the screen loop publishes, for every browser
window it examines, whether that window was excluded and why; the media tracker asks before it
writes a session. Both run in the capture process, on different threads.

The state is conservative about what it does not know:

* A focused browser window needs a recent verdict *for that window and title*. Without one (the
  screen loop has not looked yet, stalled, or the title just changed) the answer is
  ``url_unknown`` and the session is not logged.
* An excluding verdict keeps rejecting that browser's sessions for ``HOLD_MS`` after the window
  was last seen, so moving focus off a bank page does not release a session it was playing.

What this cannot see is a playing tab of a browser window that is neither focused nor on a
monitor the screen loop looked at: that is the accepted background-session gap (docs/CAPTURE.md).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture.ports import BROWSERS

if TYPE_CHECKING:
    from collections.abc import Callable

    from akashic.capture.ports import WindowInfo

FRESH_S: Final = 30.0  # a focused window's verdict must be at most this old
HOLD_S: Final = 60.0  # an excluding verdict keeps applying this long after the window was seen


@dataclass(frozen=True, slots=True)
class _Verdict:
    process: str
    title: str
    reason: str | None  # None: examined and not excluded
    at: float


class BrowserState:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[int, _Verdict] = {}

    def publish(self, window: WindowInfo, reason: str | None) -> None:
        """Record the verdict on a browser window (``reason`` is the exclusion category)."""
        if window.process not in BROWSERS:
            return
        now = self._clock()
        with self._lock:
            self._windows[window.hwnd] = _Verdict(window.process, window.title, reason, now)
            for hwnd, seen in list(self._windows.items()):
                if now - seen.at > HOLD_S:
                    del self._windows[hwnd]

    def clear(self) -> None:
        """Forget everything (capture was paused or locked): a stale verdict proves nothing."""
        with self._lock:
            self._windows.clear()

    def media_reason(self, process: str | None, focus: WindowInfo | None) -> str | None:
        """Why a browser's media session must not be logged, or None.

        ``process`` is the browser behind the session; None when it is unknown, in which case any
        browser may be the one.
        """
        now = self._clock()
        with self._lock:
            for seen in self._windows.values():
                same = process is None or seen.process == process
                if same and seen.reason is not None and now - seen.at <= HOLD_S:
                    return seen.reason
            if focus is not None and focus.process in BROWSERS and process in (None, focus.process):
                seen = self._windows.get(focus.hwnd)
                if seen is None or seen.title != focus.title or now - seen.at > FRESH_S:
                    return "url_unknown"
        return None
