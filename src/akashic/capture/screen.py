"""The event-driven screen capture loop.

Input activity (polled: only *when* the last input happened) and focus changes are debounced
into capture requests. A request becomes a frame
only if it survives, in this order: lock check, exclusions on the window (in memory, before any
pixel is grabbed), the password-field check, URL exclusions, then change detection. Text comes
from UI Automation first and from the OCR gate when that yields (almost) nothing. Screenshot and
text share one timestamp and are written together, after every exclusion has been applied.

Logs here contain counts and durations only, never window titles, URLs or text.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture import imaging
from akashic.capture.debounce import Debouncer
from akashic.capture.exclusions import normalize_url
from akashic.capture.gaming import GameMode, GameSettings
from akashic.capture.ports import BROWSERS, Rect, Trigger, WindowInfo
from akashic.capture.text import MAX_UIA_NODES, MIN_UIA_CHARS, assemble_text
from akashic.capture.uia_budget import UiaBudget
from akashic.storage.frames import NewFrame, TextSource

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from PIL import Image

    from akashic.capture.browser_state import BrowserState
    from akashic.capture.exclusions import Exclusions
    from akashic.capture.ocr_gate import GateResult, OcrGate
    from akashic.capture.ports import ScreenGrabber, WindowSource
    from akashic.capture.ports import TextSource as UiaSource
    from akashic.storage.frames import FrameStore
    from akashic.storage.sessions import GameLog

log = logging.getLogger(__name__)

STATS_LOG_INTERVAL_S: Final = 300.0
MONITOR_REFRESH_S: Final = 5.0
# Chromium browsers whose private-mode marker is not in the Win32 title (Edge's is).
_CAPTION_ONLY: Final = frozenset({"chrome.exe", "brave.exe"})


@dataclass(frozen=True, slots=True)
class Settings:
    """Starting values from screenpipe (research §3.1); tune by measurement."""

    idle_interval_s: float = 5.0  # focused monitor: capture at least this often (deduplicated)
    other_monitor_interval_s: float = 15.0  # monitors without the focused window
    min_gap_s: float = 1.0  # minimum time between captures of one monitor
    focus_poll_s: float = 0.1  # how often the foreground window is checked for changes


@dataclass(frozen=True, slots=True)
class _Target:
    window: WindowInfo
    is_focused: bool


@dataclass(slots=True)
class _MonitorState:
    last_attempt: float = float("-inf")
    signature: imaging.Signature | None = None
    window_key: tuple[int, str] | None = None


class ScreenCapture:
    def __init__(  # noqa: PLR0913 - the ports are the dependencies; keyword-only
        self,
        *,
        screen: ScreenGrabber,
        windows: WindowSource,
        uia: UiaSource,
        gate: OcrGate,
        store: FrameStore,
        exclusions: Exclusions,
        browsers: BrowserState,
        games: GameLog,
        settings: Settings | None = None,
        budget: UiaBudget | None = None,
        game_settings: GameSettings | None = None,
        on_game: Callable[[bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._screen = screen
        self._windows = windows
        self._uia = uia
        self._gate = gate
        self._store = store
        self._exclusions = exclusions
        self._browsers = browsers
        self._tick_hwnds: set[int] = set()
        self._settings = settings or Settings()
        self._budget = budget or UiaBudget()
        self._clock = clock
        self._now_ms = now_ms
        self._games = games
        self._on_game = on_game
        self._game_settings = game_settings or GameSettings()
        self._game_mode = GameMode(self._game_settings)
        self._game_id: int | None = None
        self._game_frame_at = float("-inf")
        self._game_heartbeat_at = float("-inf")
        self._debouncer = Debouncer()
        self._last_input: int | None = None
        self._states: dict[int, _MonitorState] = {}
        self._monitors: list[Rect] = []
        self._monitors_at = float("-inf")
        self._focus_key: tuple[int, str] | None = None
        self._focus_polled_at = float("-inf")
        self.stats: Counter[str] = Counter()

    # --- input -------------------------------------------------------------------------------

    def note(self, trigger: Trigger, now: float) -> None:
        self._debouncer.note(trigger, now)

    def reset(self) -> None:
        """Forget pending triggers and what was last seen (used while paused or locked)."""
        self._debouncer.clear()
        self._states.clear()
        self._focus_key = None
        self._last_input = None
        self._browsers.clear()
        self.close()

    def close(self) -> None:
        """End the current game session, if any (pause, lock or shutdown)."""
        if self._game_mode.stop() is not None:
            self._end_game_row()

    # --- loop --------------------------------------------------------------------------------

    def run(self, stop: threading.Event, paused: threading.Event) -> None:
        last_stats = self._clock()
        while not stop.is_set():
            now = self._clock()
            due = self._debouncer.next_due()
            timeout = self._settings.focus_poll_s
            if due is not None:
                timeout = min(timeout, max(due - now, 0.0))
            if stop.wait(timeout):
                break
            if paused.is_set():
                self.reset()  # paused means nothing is read, grabbed or kept
                continue
            now = self._clock()
            self.tick(now)
            if now - last_stats >= STATS_LOG_INTERVAL_S:
                last_stats = now
                log.info("capture stats: %s", dict(sorted(self.stats.items())))

    def tick(self, now: float) -> None:
        """One iteration: poll the foreground window, run due triggers, then idle captures."""
        self._tick_hwnds.clear()  # a window spanning monitors is examined once per tick
        self._poll_focus(now)
        monitors = self._monitor_list(now)
        if not monitors:
            return
        focused = self._windows.focused_window()
        if self._tick_game(focused, monitors, now):
            return
        focus_monitor = _monitor_of(monitors, focused.rect.center) if focused else None
        for trigger in self._debouncer.pop_due(now):
            index = focus_monitor if focus_monitor is not None else 0
            self._capture(trigger, index, monitors, now)
            if trigger == "input":  # e.g. scrolling a window that does not have the focus
                pointed = _monitor_of(monitors, self._windows.cursor_pos())
                if pointed is not None and pointed != index:
                    self._capture(trigger, pointed, monitors, now)
        for index in range(len(monitors)):
            state = self._states.setdefault(index, _MonitorState())
            interval = (
                self._settings.idle_interval_s
                if index == focus_monitor
                else self._settings.other_monitor_interval_s
            )
            if now - state.last_attempt >= interval:
                self._capture("idle", index, monitors, now)

    # --- gaming mode -------------------------------------------------------------------------

    def _tick_game(self, focused: WindowInfo | None, monitors: list[Rect], now: float) -> bool:
        """Gaming mode (AGENTS.md): log the game, one screenshot per interval, no UI Automation,
        no OCR (those frames have no text; the worker can OCR the stored image later), no input
        triggers and no other monitors. Returns True while a game is in the foreground."""
        try:
            transition = self._game_mode.observe(focused, monitors, now)
            if transition.ended is not None:
                self._end_game_row()
            if transition.started is not None:
                self._start_game_row(transition.started.process, transition.started.title, now)
            if self._on_game is not None and (transition.ended or transition.started):
                self._on_game(self._game_mode.active)
            game = self._game_mode.current
            if game is None:
                return False
            self._game_heartbeat(now)
            if (
                focused is not None
                and focused.process == game.process
                and now - self._game_frame_at >= self._game_settings.interval_s
            ):
                self._game_frame_at = now
                self.stats[self._capture_game(focused, monitors)] += 1
        except Exception as exc:  # noqa: BLE001 - gaming mode must never stop the loop
            log.warning("gaming mode failed: %s", type(exc).__name__)
            self.stats["error"] += 1
        return self._game_mode.active

    def _start_game_row(self, process: str, title: str, now: float) -> None:
        self._debouncer.clear()  # input triggers queued before the game started are stale
        self._game_id = self._games.open(
            process=process, window_title=title or None, ts_ms=self._now_ms()
        )
        log.info("game mode started for %s (fullscreen detection)", process)
        self._game_frame_at = float("-inf")
        self._game_heartbeat_at = now
        self.stats["game_started"] += 1

    def _end_game_row(self) -> None:
        if self._game_id is not None:
            self._games.close(self._game_id, self._now_ms())
            log.info("game mode ended")
            self.stats["game_ended"] += 1
        self._game_id = None

    def _game_heartbeat(self, now: float) -> None:
        if (
            self._game_id is not None
            and now - self._game_heartbeat_at >= self._game_settings.heartbeat_s
        ):
            self._game_heartbeat_at = now
            self._games.heartbeat(self._game_id, self._now_ms())

    def _capture_game(self, window: WindowInfo, monitors: list[Rect]) -> str:
        if self._windows.is_locked():
            return "locked"
        reason = self._exclusions.check_window(window.process, window.title)
        if reason is not None:
            return f"excluded:{reason}"
        index = _monitor_of(monitors, window.rect.center)
        if index is None:
            return "no_window"
        ts_ms = self._now_ms()
        image = self._screen.grab_window(window)
        if image is None:
            return "image_unavailable"
        black = imaging.is_black(image)  # exclusive fullscreen often grabs black
        encoded = None if black else imaging.encode_webp(image)
        if not self._consistent(window, focused=True, url=None, read_url=False):
            return "window_changed"
        self._store.add(
            NewFrame(
                ts_ms=ts_ms,
                monitor=index,
                trigger="game",
                process=window.process,
                window_title=window.title or None,
                image_webp=encoded,
            )
        )
        return "game_black" if black else "game_stored"

    # --- helpers -----------------------------------------------------------------------------

    def _monitor_list(self, now: float) -> list[Rect]:
        if now - self._monitors_at >= MONITOR_REFRESH_S:
            self._monitors = self._screen.monitors()
            self._monitors_at = now
        return self._monitors

    def _poll_focus(self, now: float) -> None:
        if now - self._focus_polled_at < self._settings.focus_poll_s:
            return
        self._focus_polled_at = now
        last_input = self._windows.last_input_ms()
        if self._last_input is not None and last_input != self._last_input:
            self._debouncer.note("input", now)
        self._last_input = last_input
        window = self._windows.focused_window()
        if window is None:
            return
        previous = self._focus_key
        self._focus_key = window.key
        if previous is None or previous[0] != window.hwnd:
            self._debouncer.note("focus", now)
        elif previous != window.key:
            self._debouncer.note("title", now)

    def _window_for(self, monitor: Rect) -> tuple[WindowInfo | None, bool]:
        """The window to record for a monitor, and whether it is the focused one."""
        focused = self._windows.focused_window()
        if focused is not None and monitor.contains(*focused.rect.center):
            return focused, True
        window = self._windows.window_at(*monitor.center)
        # A focused window that spans several monitors is the topmost window at their centres
        # too: it is the focused one there as well, so its password check applies.
        return window, window is not None and focused is not None and window.hwnd == focused.hwnd

    def _capture(self, trigger: Trigger, index: int, monitors: list[Rect], now: float) -> None:
        state = self._states.setdefault(index, _MonitorState())
        if now - state.last_attempt < self._settings.min_gap_s:
            if trigger != "idle":
                self._debouncer.defer(trigger, state.last_attempt + self._settings.min_gap_s)
            return
        state.last_attempt = now
        try:
            outcome = self._capture_monitor(trigger, index, monitors[index], state, now)
        except Exception as exc:  # noqa: BLE001 - one bad capture must never stop the loop
            log.warning("capture failed: %s", type(exc).__name__)
            outcome = "error"
        self.stats[outcome] += 1

    def _capture_monitor(  # noqa: PLR0911 - a pipeline of early exits, one per rule
        self, trigger: Trigger, index: int, monitor: Rect, state: _MonitorState, now: float
    ) -> str:
        if self._windows.is_locked():
            return "locked"
        window, is_focused = self._window_for(monitor)
        if window is None:
            return "no_window"
        if window.hwnd in self._tick_hwnds:
            return "duplicate_window"
        self._tick_hwnds.add(window.hwnd)  # before the rules: an excluded window stays excluded
        reason, url = self._capture_allowed(window, focused=is_focused)
        if reason is not None:
            return reason

        ts_ms = self._now_ms()
        image = self._screen.grab_window(window)
        if image is None:
            return "image_unavailable"
        if not self._consistent(window, focused=is_focused, url=url):
            return "window_changed"
        sig = imaging.signature(image)
        if (
            state.signature is not None
            and state.window_key == window.key
            and not imaging.differs(state.signature, sig)
        ):
            return "unchanged"

        black = imaging.is_black(image)
        target = _Target(window, is_focused)
        text, source, gate_result = self._read_text(target, image, black=black, now=now)
        encoded = None if black else imaging.encode_webp(image)
        # UIA/URL and pixels are separate reads. Reject a switch/navigation/lock before writing.
        if not self._consistent(window, focused=is_focused, url=url):
            return "window_changed"
        self._store.add(
            NewFrame(
                ts_ms=ts_ms,
                monitor=index,
                trigger=trigger,
                process=window.process or None,
                window_title=window.title or None,
                url=url,
                focused=is_focused,
                text=text or None,
                text_source=source,
                image_webp=encoded,
            )
        )
        state.signature = sig
        state.window_key = window.key
        if gate_result is not None and source == "ocr":
            self._gate.commit(f"{window.process}:{window.hwnd}", gate_result)
        return "stored_black" if black else "stored"

    def _capture_allowed(
        self, window: WindowInfo, *, focused: bool
    ) -> tuple[str | None, str | None]:
        reason, url = self._exclusion(window, focused=focused)
        self._browsers.publish(window, reason)  # before any media write can rely on it
        return (f"excluded:{reason}" if reason else None), url

    def _exclusion(  # noqa: PLR0911 - one early exit per rule
        self, window: WindowInfo, *, focused: bool
    ) -> tuple[str | None, str | None]:
        """The exclusion category of a window (or None) and its address when it is a browser."""
        reason = self._exclusions.check_window(window.process, window.title)
        if reason is not None:
            return reason, None
        if focused and (reason := self._password_reason()) is not None:
            return reason, None
        family = BROWSERS.get(window.process)
        if family is None:
            return None, None
        caption = self._uia.browser_caption(window, family)
        if caption and self._exclusions.is_private_title(caption):
            return "private_window", None  # Chromium's marker is not in the Win32 title
        if caption is None and window.process in _CAPTION_ONLY:
            # Chrome and Brave keep the marker only in the frame's caption: without it nobody
            # knows whether this window is private, so it is skipped and media is rejected.
            return "caption_unknown", None
        raw = self._read_raw(window)
        url = normalize_url(raw) if raw else None
        if url is None and raw and (typed := self._exclusions.check_typed(raw)) is not None:
            return typed, None  # text typed into the bar still names a host
        return self._exclusions.check_browser(window.title, url), url

    def _password_reason(self) -> str | None:
        """Why the focused element forbids capture: a password field, or an unknown state (UI
        Automation failed, so nobody knows). None when it is not one and when nothing has focus."""
        state = self._uia.focused_is_password()
        if state is None:
            return "password_unknown"
        return "password_field" if state else None

    def _consistent(
        self, window: WindowInfo, *, focused: bool, url: str | None, read_url: bool = True
    ) -> bool:
        if self._windows.is_locked() or self._windows.inspect_window(window.hwnd) != window:
            return False
        if focused and (
            self._windows.focused_window() != window or self._password_reason() is not None
        ):
            return False
        return not read_url or self._read_url(window) == url

    def _read_raw(self, window: WindowInfo) -> str | None:
        family = BROWSERS.get(window.process)
        return self._uia.read_url(window, family) if family is not None else None

    def _read_url(self, window: WindowInfo) -> str | None:
        family = BROWSERS.get(window.process)
        if family is None:
            return None
        raw = self._uia.read_url(window, family)
        return normalize_url(raw) if raw else None

    def _read_text(
        self, target: _Target, image: Image.Image, *, black: bool, now: float
    ) -> tuple[str, TextSource | None, GateResult | None]:
        window = target.window
        text = ""
        source: TextSource | None = None
        if target.is_focused and self._budget.allow(window.process, now):
            started = time.perf_counter()
            parts = self._uia.read_text(window, MAX_UIA_NODES)
            self._budget.record(window.process, (time.perf_counter() - started) * 1000, now)
            text = assemble_text(parts or ())
            source = "uia" if text else None
        if len(text) >= MIN_UIA_CHARS or black:
            return text, source, None
        result = self._gate.read(
            f"{window.process}:{window.hwnd}", image, Rect(0, 0, image.width, image.height)
        )
        ocr_text = result.text.strip()
        if len(ocr_text) > len(text):
            return ocr_text, "ocr", result
        return text, source, None


def _monitor_of(monitors: list[Rect], point: tuple[int, int]) -> int | None:
    for index, rect in enumerate(monitors):
        if rect.contains(*point):
            return index
    return None
