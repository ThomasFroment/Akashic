"""Log what plays, from the Windows media sessions (research §5).

The OS is polled every couple of seconds rather than subscribed to: one code path covers new
sessions, title changes (YouTube autoplay), play/pause and sessions that vanish, and a missed
event can't leave a row open forever. A media session becomes a row when it first plays; the
row stays open across pauses (``played_ms`` counts only the time spent playing) and closes when
the title changes, the source disappears or a pause lasts too long.

Exclusions apply before anything is written. A media title is not a window title: for a browser
the screen loop's verdict on the browser's windows decides (private window, password manager,
finance, user address rules, Akashic's own pages; see ``browser_state``), and an unknown or
stale verdict rejects the session. Other apps are judged by process and the user's own rules.
Windows does not say which browser *window* publishes a session, so a private window in the
background is the one case this cannot see.

Logs here contain counts only, never titles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture.ports import BROWSERS

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from akashic.capture.browser_state import BrowserState
    from akashic.capture.exclusions import Exclusions
    from akashic.capture.ports import MediaSource, MediaState, PlaybackState, WindowInfo
    from akashic.storage.sessions import MediaLog

log = logging.getLogger(__name__)

POLL_INTERVAL_S: Final = 2.0
MAX_POLL_GAP_MS: Final = 10_000  # a longer gap (suspend, stall) is not counted as playing time
PAUSE_CLOSE_MS: Final = 10 * 60_000  # a session paused this long is over
WRITE_INTERVAL_MS: Final = 10_000  # heartbeat for end_ms / played_ms
URL_SEARCH_WINDOW_MS: Final = 120_000  # keep looking for the page address this long
URL_SEARCH_EVERY_MS: Final = 5_000
URL_LOOKBACK_MS: Final = 60_000
NAME_RETRY_MS: Final = 5_000  # first retry of an app name that could not be resolved; doubles
NAME_RETRY_MAX_MS: Final = 300_000

_BROWSER_STEMS: Final = {
    "chrome": "chrome.exe",
    "brave": "brave.exe",
    "edge": "msedge.exe",
    "firefox": "firefox.exe",
    "zen": "zen.exe",
}


def process_for_app(app: str) -> str:
    """Best guess at the executable behind a display name ("Google Chrome" -> chrome.exe)."""
    name = app.casefold()
    for stem, process in _BROWSER_STEMS.items():
        if stem in name:
            return process
    return name if name.endswith(".exe") else f"{name}.exe"


@dataclass(slots=True)
class _Open:
    session_id: int
    app: str
    title: str
    artist: str
    state: PlaybackState
    start_ms: int
    last_poll_ms: int
    last_write_ms: int
    played_ms: int = 0
    paused_since_ms: int | None = None
    url_found: bool = False
    url_tried_ms: int = -URL_SEARCH_EVERY_MS


class MediaTracker:
    def __init__(  # noqa: PLR0913 - the ports are the dependencies; keyword-only
        self,
        *,
        source: MediaSource,
        log_store: MediaLog,
        exclusions: Exclusions,
        focused_window: Callable[[], WindowInfo | None],
        now_ms: Callable[[], int],
        browsers: BrowserState,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        self._source = source
        self._store = log_store
        self._exclusions = exclusions
        self._focused_window = focused_window
        self._now_ms = now_ms
        self._interval_s = interval_s
        self._browsers = browsers
        self._open: dict[str, _Open] = {}
        self._names: dict[str, str] = {}  # resolved display names only
        self._name_retry: dict[str, tuple[int, int]] = {}  # app id -> (retry at ms, misses)
        self.stats: dict[str, int] = {}

    def run(
        self, stop: threading.Event, paused: threading.Event, locked: Callable[[], bool]
    ) -> None:
        """Poll until ``stop``. While paused or locked nothing is read and open rows are closed."""
        self._store.close_stale()
        while not stop.is_set():
            try:
                if paused.is_set() or locked():
                    self.close_all()
                else:
                    self.poll()
            except Exception as exc:  # noqa: BLE001 - one bad poll must never stop the thread
                log.warning("media poll failed: %s", type(exc).__name__)
            stop.wait(self._interval_s)
        self.close_all()

    def close_all(self) -> None:
        now = self._now_ms()
        for app_id in list(self._open):
            self._close(app_id, now)

    def poll(self) -> None:
        now = self._now_ms()
        seen: set[str] = set()
        for state in self._source.sessions():
            if state.app_id in seen:  # two sessions from one app: the first (current) wins
                continue
            app, known = self._app_name(state.app_id, now)
            if not self._allowed(state, app, known=known):
                continue
            seen.add(state.app_id)
            self._update(state, app, now)
        for app_id in list(self._open):
            if app_id not in seen:  # gone: it ended when we last saw it
                self._close(app_id, self._open[app_id].last_poll_ms)

    # --- internals ---------------------------------------------------------------------------

    def _app_name(self, app_id: str, now: int) -> tuple[str, bool]:
        """Display name and whether it is a real one. A miss is retried with a growing delay and
        never remembered: until it resolves the id stands in, and the process is unknown."""
        name = self._names.get(app_id)
        if name is not None:
            return name, True
        retry = self._name_retry.get(app_id)
        if retry is None or now >= retry[0]:
            name = self._source.app_name(app_id)
            if name:
                self._names[app_id] = name
                self._name_retry.pop(app_id, None)
                return name, True
            misses = (retry[1] if retry else 0) + 1
            delay = min(NAME_RETRY_MS * 2 ** (misses - 1), NAME_RETRY_MAX_MS)
            self._name_retry[app_id] = (now + delay, misses)
        return app_id, False

    def _allowed(self, state: MediaState, app: str, *, known: bool) -> bool:
        if not state.title.strip():
            return False
        process = process_for_app(app)
        known = known or process in BROWSERS  # "Chrome" names its browser even unresolved
        # A process that cannot be told keeps every rule in force (D2): it may be a browser.
        reason = self._exclusions.check_window(process if known else "", state.title, media=True)
        if "private" in state.app_id.casefold():
            reason = "private_window"
        if reason is None and (not known or process in BROWSERS):
            reason = self._browser_reason(process if known else None)
        if reason is not None:
            self.stats[f"excluded:{reason}"] = self.stats.get(f"excluded:{reason}", 0) + 1
            return False
        return True

    def _browser_reason(self, process: str | None) -> str | None:
        """Why this browser's (or, with None, any browser's) media must not be logged."""
        focus = self._focused_window()
        if (
            focus is not None
            and focus.process in BROWSERS
            and process in (None, focus.process)
            and self._exclusions.is_private_title(focus.title)
        ):
            return "private_window"
        return self._browsers.media_reason(process, focus)

    def _update(self, state: MediaState, app: str, now: int) -> None:
        current = self._open.get(state.app_id)
        if current is not None and (current.title, current.artist) != (state.title, state.artist):
            self._close(state.app_id, now)
            current = None
        if current is None:
            if state.state != "playing":
                return  # a paused leftover we never saw play is not something that was watched
            self._open_session(state, app, now)
            return
        elapsed = min(now - current.last_poll_ms, MAX_POLL_GAP_MS)
        if current.state == "playing":
            current.played_ms += elapsed
        current.last_poll_ms = now
        changed = state.state != current.state
        if changed:
            current.state = state.state
            current.paused_since_ms = now if state.state != "playing" else None
            self._store.add_event(current.session_id, now, state.state)
        if current.paused_since_ms is not None and now - current.paused_since_ms >= PAUSE_CLOSE_MS:
            self._close(state.app_id, now)
            return
        self._find_url(current, now)
        if changed or now - current.last_write_ms >= WRITE_INTERVAL_MS:
            current.last_write_ms = now
            self._store.touch(current.session_id, end_ms=now, played_ms=current.played_ms)

    def _open_session(self, state: MediaState, app: str, now: int) -> None:
        session_id = self._store.open(
            app_id=state.app_id, app=app, title=state.title, artist=state.artist, ts_ms=now
        )
        self._store.add_event(session_id, now, state.state)
        opened = _Open(
            session_id=session_id,
            app=app,
            title=state.title,
            artist=state.artist,
            state=state.state,
            start_ms=now,
            last_poll_ms=now,
            last_write_ms=now,
        )
        self._open[state.app_id] = opened
        self.stats["opened"] = self.stats.get("opened", 0) + 1
        self._find_url(opened, now)

    def _find_url(self, current: _Open, now: int) -> None:
        """Match the browser tab by title. The frame may be captured a moment after the
        session starts, so keep trying for a short while."""
        if (
            current.url_found
            or process_for_app(current.app) not in BROWSERS
            or now - current.start_ms > URL_SEARCH_WINDOW_MS
            or now - current.url_tried_ms < URL_SEARCH_EVERY_MS
        ):
            return
        current.url_tried_ms = now
        url = self._store.find_url(current.title, current.start_ms - URL_LOOKBACK_MS)
        if url is not None:
            current.url_found = True
            self._store.set_url(current.session_id, url)

    def _close(self, app_id: str, end_ms: int) -> None:
        current = self._open.pop(app_id)
        elapsed = min(max(end_ms - current.last_poll_ms, 0), MAX_POLL_GAP_MS)
        played = current.played_ms + (elapsed if current.state == "playing" else 0)
        end = max(end_ms, current.last_poll_ms)
        self._store.add_event(current.session_id, end, "stopped")
        self._store.close(current.session_id, end_ms=end, played_ms=played)
