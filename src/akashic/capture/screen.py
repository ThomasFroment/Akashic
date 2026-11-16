"""The event-driven screen capture loop, one schedule per monitor.

Each monitor is checked when a trigger asks for it (focus, title, input, window events) or when its
rate says so: the focus monitor every few seconds, the others less often. All monitors due in one
tick share one window list and one ``Desk.check``; the desk decides what each monitor shows and the
screen writer stores it off this loop. A picture is submitted only when the monitor changed. The
focused window's UI Automation text rides along with the picture, and is dropped when the window or
its address changed while it was read.

A check that finds a visible browser window on Akashic's own address keeps nothing of it and halts
capture (the failsafe); while halted by it, the addresses of visible browser windows are re-read
about once a second until none shows it.

Logs here contain counts and durations only, never window titles, URLs or text.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final

from akashic.capture import imaging
from akashic.capture.debounce import Debouncer
from akashic.capture.desk import Desk, visible_monitors
from akashic.capture.exclusions import normalize_url
from akashic.capture.gaming import GameMode, GameSettings
from akashic.capture.ports import BROWSERS
from akashic.capture.screen_writer import Picture
from akashic.capture.text import assemble_text
from akashic.capture.uia_budget import BudgetedUia
from akashic.capture.verdicts import Verdicts

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Collection, Sequence

    from akashic.capture.browser_state import BrowserState
    from akashic.capture.desk import Snapshot
    from akashic.capture.exclusions import Exclusions
    from akashic.capture.gaming import Transition
    from akashic.capture.ports import (
        Displays,
        Entry,
        MonitorInfo,
        Rect,
        TopWindow,
        Trigger,
        WindowCaptures,
        WindowChanges,
        WindowEvents,
        WindowFacts,
        WindowInfo,
        WindowSource,
    )
    from akashic.capture.ports import TextSource as UiaSource
    from akashic.capture.screen_writer import ScreenWriter
    from akashic.capture.uia_budget import UiaBudget
    from akashic.storage.monitors import MonitorState
    from akashic.storage.sessions import GameLog

log = logging.getLogger(__name__)

STATS_LOG_INTERVAL_S: Final = 300.0
MONITOR_REFRESH_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class Settings:
    """Starting values from screenpipe (research §3.1); tune by measurement."""

    focus_interval_s: float = 5.0  # the monitor with the focused window: checked this often
    side_interval_s: float = 15.0  # every other monitor
    min_gap_s: float = 1.0  # minimum time between triggered checks of one monitor
    poll_s: float = 0.1  # one loop pass; also how often the foreground window is polled


@dataclass(slots=True)
class _Monitor:
    last_check: float = float("-inf")
    due_at: float | None = None  # when a trigger's check may run
    trigger: Trigger | None = None
    pending: bool = False  # due, but the desk had no snapshot yet
    noted: MonitorState = "hidden"
    signature: imaging.Signature | None = None  # of the last picture submitted
    entries: tuple[Entry, ...] | None = None  # of the last picture submitted


@dataclass(slots=True)
class _Tick:
    now: float
    ts_ms: int
    focused: WindowInfo | None
    focus_key: str | None  # the monitor holding the centre of the focused window
    game_focused: bool
    game_active: bool
    text_read: bool = False
    text: str | None = None


class ScreenCapture:
    def __init__(  # noqa: PLR0913 - the ports are the dependencies; keyword-only
        self,
        *,
        displays: Displays,
        windows: WindowSource,
        facts: WindowFacts,
        events: WindowEvents,
        captures: WindowCaptures,
        uia: UiaSource,
        writer: ScreenWriter,
        exclusions: Exclusions,
        browsers: BrowserState,
        games: GameLog,
        on_own_address: Callable[[bool], None],
        settings: Settings | None = None,
        game_settings: GameSettings | None = None,
        budget: UiaBudget | None = None,
        on_game: Callable[[bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self._displays = displays
        self._windows = windows
        self._facts = facts
        self._events = events
        self._captures = captures
        self._writer = writer
        self._browsers = browsers
        self._uia = BudgetedUia(uia, budget)
        self._verdicts = Verdicts(exclusions, self._uia, browsers, clock)
        self._desk = Desk(captures, self._verdicts)
        self._settings = settings or Settings()
        self._clock = clock
        self._now_ms = now_ms
        self._games = games
        self._on_own_address = on_own_address
        self._on_game = on_game
        self._game_settings = game_settings or GameSettings()
        self._game_mode = GameMode(self._game_settings)
        self._game_id: int | None = None
        self._game_heartbeat_at = float("-inf")
        self._debouncer = Debouncer()
        self._event_hwnds: set[int] = set()
        self._last_input: int | None = None
        self._focus_window_key: tuple[int, str] | None = None
        self._states: dict[str, _Monitor] = {}
        self._monitors: list[MonitorInfo] | None = None
        self._monitors_at = float("-inf")
        self._own_address = False  # the failsafe holds: a browser window showed Akashic
        self._watched_at = float("-inf")
        self._rects: dict[int, Rect] = {}  # window rects of the last enumeration
        self._stats: Counter[str] = Counter()

    def stats_snapshot(self) -> dict[str, int]:
        """Loop and UI Automation counters, merged into a copy that is safe to read elsewhere."""
        return dict(self._stats) | dict(self._uia.stats)

    # --- lifecycle ---------------------------------------------------------------------------

    def reset(self) -> None:
        """Forget everything seen and held (used while halted or locked)."""
        self._desk.drop_all()
        self._events.drain()
        self._debouncer.clear()
        self._event_hwnds.clear()
        self._states.clear()
        self._rects = {}
        self._focus_window_key = None
        self._last_input = None
        self._browsers.clear()
        self._end_game(notify=True)

    def close(self) -> None:
        self._end_game()
        self._desk.drop_all()
        self._captures.close()
        self._events.close()

    def _end_game(self, *, notify: bool = False) -> None:
        """End the current game session, if any (pause, lock or shutdown). ``notify`` reports the
        end through ``on_game`` even when writing the end row fails."""
        if self._game_mode.stop() is None:
            return
        try:
            self._end_game_row()
        except Exception as exc:  # noqa: BLE001 - e.g. a busy database
            self._note_game_log_fault(exc)
        finally:
            if notify and self._on_game is not None:
                self._on_game(False)  # noqa: FBT003 - the callback takes the game flag

    # --- loop --------------------------------------------------------------------------------

    def run(self, stop: threading.Event, halt: threading.Event, paused: threading.Event) -> None:
        """Check until ``stop``. While ``halt`` is set nothing is grabbed or kept; only the
        failsafe watch reads browser addresses. The user's ``paused`` ends that watch too."""
        self._events.start()
        last_stats = self._clock()
        last_logged: dict[str, float] = {}
        while not stop.wait(self._settings.poll_s):
            try:
                if paused.is_set():
                    self._end_own_address()
                    self.reset()
                    continue
                if self._own_address:
                    self.reset()
                    self._watch_own_address()
                    continue
                if halt.is_set():
                    self.reset()
                    continue
                now = self._clock()
                self.tick(now)
                if now - last_stats >= STATS_LOG_INTERVAL_S:
                    last_stats = now
                    log.info("capture stats: %s", dict(sorted(self.stats_snapshot().items())))
            except Exception as exc:  # noqa: BLE001 - the screen loop must outlive any one fault
                self._stats["loop_error"] += 1
                self._log_loop_fault(type(exc).__name__, last_logged)
                for key, state in self._states.items():
                    state.noted = "failed"  # the next check composes and submits again
                    self._writer.capture_failures(key, 1)

    def _log_loop_fault(self, name: str, last_logged: dict[str, float]) -> None:
        now = self._clock()
        if now - last_logged.get(name, float("-inf")) >= STATS_LOG_INTERVAL_S:
            last_logged[name] = now
            log.warning("screen loop iteration failed: %s", name)

    def _trip_own_address(self) -> None:
        """A visible browser window shows Akashic's own address: keep nothing of this check,
        stop every capture and halt capture until the address is gone."""
        self.reset()
        self._own_address = True
        self._on_own_address(True)  # noqa: FBT003 - the callback takes the flag
        self._stats["own_address"] += 1

    def _end_own_address(self) -> None:
        if self._own_address:
            self._own_address = False
            self._on_own_address(False)  # noqa: FBT003 - the callback takes the flag

    def _watch_own_address(self) -> None:
        """While the failsafe holds: at most once per ``min_gap_s``, read only the addresses of
        visible browser windows. Capture resumes when none shows Akashic's own."""
        now = self._clock()
        if now - self._watched_at < self._settings.min_gap_s:
            return
        self._watched_at = now
        if self._windows.is_locked():
            self._end_own_address()
            return
        monitors = self._displays.monitors()
        windows = self._facts.windows()
        if monitors is None or windows is None:
            return  # unknown: stay halted
        visible = visible_monitors(windows, monitors)
        if not self._verdicts.shows_own_address(windows, visible.keys(), now):
            self._end_own_address()

    def tick(self, now: float) -> None:
        """One pass: poll, turn triggers and rates into due monitors, check them together."""
        if self._windows.is_locked():
            self.reset()
            return
        monitors = self._refresh_monitors(now)
        changes = self._events.drain()
        self._verdicts.note_events(changes)
        if not monitors:
            self._debouncer.clear()
            return
        self._sync_states(monitors)
        tick = self._begin(monitors, now)
        window_due = self._collect_requests(monitors, changes, tick)
        due = self._due_keys(tick)
        if not due and not window_due:
            return
        windows = self._facts.windows()
        if windows is None:
            self._fail_checks(due, tick)
            return
        rects = {window.hwnd: window.rect for window in windows}
        if window_due:
            self._request_event_monitors(monitors, rects, now)
            due = self._due_keys(tick)
        self._rects = rects
        if due:
            self._check(monitors, due, windows, tick)

    # --- scheduling --------------------------------------------------------------------------

    def _begin(self, monitors: Sequence[MonitorInfo], now: float) -> _Tick:
        """Poll the foreground window and the game state, once for the whole tick."""
        focused = self._windows.focused_window()
        self._record_transition(
            self._game_mode.observe(focused, [m.rect for m in monitors], now), now
        )
        game = self._game_mode.current
        if game is not None:
            self._game_heartbeat(now)
        self._poll_focus_and_input(focused, now)
        return _Tick(
            now=now,
            ts_ms=self._now_ms(),
            focused=focused,
            focus_key=_monitor_key_at(monitors, focused.rect.center) if focused else None,
            game_focused=game is not None
            and focused is not None
            and focused.process == game.process,
            game_active=game is not None,
        )

    def _collect_requests(
        self, monitors: Sequence[MonitorInfo], changes: WindowChanges, tick: _Tick
    ) -> bool:
        """Turn triggers and window events into monitor requests; True when events are due."""
        if tick.game_active:
            self._clear_triggers()
            return False
        self._event_hwnds.update(event.hwnd for event in changes.events)
        if changes.events:
            self._debouncer.note("window", tick.now)
        if not changes.healthy:
            for key in self._states:
                self._request(key, "window", tick.now)
        return self._request_triggers(monitors, tick.now, tick.focus_key)

    def _refresh_monitors(self, now: float) -> list[MonitorInfo]:
        if now - self._monitors_at >= MONITOR_REFRESH_S:
            self._monitors_at = now
            listed = self._displays.monitors()
            if listed is not None and listed != self._monitors:
                self._monitors = listed
                self._writer.layout(listed, self._now_ms())
        return self._monitors or []

    def _sync_states(self, monitors: Sequence[MonitorInfo]) -> None:
        keys = {monitor.key for monitor in monitors}
        for key in keys - self._states.keys():
            self._states[key] = _Monitor()
            self._desk.retake(key)
        for key in self._states.keys() - keys:
            del self._states[key]

    def _poll_focus_and_input(self, focused: WindowInfo | None, now: float) -> None:
        last_input = self._windows.last_input_ms()
        if self._last_input is not None and last_input != self._last_input:
            self._debouncer.note("input", now)
        self._last_input = last_input
        if focused is None:
            return
        previous = self._focus_window_key
        self._focus_window_key = focused.key
        if previous is None or previous[0] != focused.hwnd:
            self._debouncer.note("focus", now)
        elif previous != focused.key:
            self._debouncer.note("title", now)

    def _clear_triggers(self) -> None:
        self._debouncer.clear()
        self._event_hwnds.clear()
        for state in self._states.values():
            state.due_at = None
            state.trigger = None

    def _request_triggers(
        self, monitors: Sequence[MonitorInfo], now: float, focus_key: str | None
    ) -> bool:
        """Turn the debounced triggers into monitor requests; True when window events are due."""
        window_due = False
        for trigger in self._debouncer.pop_due(now):
            if trigger == "window":
                window_due = True
                continue
            self._request(focus_key, trigger, now)
            if trigger == "input":
                self._request(_monitor_key_at(monitors, self._windows.cursor_pos()), trigger, now)
        return window_due

    def _request_event_monitors(
        self, monitors: Sequence[MonitorInfo], rects: dict[int, Rect], now: float
    ) -> None:
        """Request the monitors that each event's window touched, before or after the event."""
        touched = [
            rect
            for hwnd in self._event_hwnds
            for rect in (self._rects.get(hwnd), rects.get(hwnd))
            if rect is not None
        ]
        for monitor in monitors:
            if any(monitor.rect.intersect(rect) is not None for rect in touched):
                self._request(monitor.key, "window", now)
        self._event_hwnds.clear()

    def _request(self, key: str | None, trigger: Trigger, now: float) -> None:
        state = None if key is None else self._states.get(key)
        if state is not None and state.trigger is None:
            state.trigger = trigger
            state.due_at = max(now, state.last_check + self._settings.min_gap_s)

    def _due_keys(self, tick: _Tick) -> set[str]:
        return {
            key
            for key, state in self._states.items()
            if state.pending
            or (state.due_at is not None and tick.now >= state.due_at)
            or tick.now - state.last_check >= self._interval(key, tick)
        }

    def _interval(self, key: str, tick: _Tick) -> float:
        if key != tick.focus_key:
            return self._settings.side_interval_s
        if tick.game_focused:
            return self._game_settings.interval_s
        return (
            self._settings.side_interval_s if tick.game_active else self._settings.focus_interval_s
        )

    # --- checking ----------------------------------------------------------------------------

    def _fail_checks(self, due: Collection[str], tick: _Tick) -> None:
        self._stats["enumeration_failed"] += 1
        for key in due:
            state = self._states[key]
            state.noted = "failed"
            state.pending = False
            state.trigger = None
            state.due_at = None
            state.last_check = tick.now
            self._writer.note(key, "failed", tick.ts_ms)
            self._writer.capture_failures(key, 1)

    def _check(
        self,
        monitors: Sequence[MonitorInfo],
        due: set[str],
        windows: Sequence[TopWindow],
        tick: _Tick,
    ) -> None:
        for key in due:
            state = self._states[key]
            state.due_at = None
            if state.noted == "failed" or self._writer.lost(key):
                self._desk.retake(key)
                state.signature = None
                state.entries = None
        checked = self._desk.check(monitors, due, windows, tick.focused, tick.now)
        if checked.own_address:
            self._trip_own_address()
            return
        returned = {snapshot.monitor.key for snapshot in checked.snapshots}
        for key in due - returned:
            self._states[key].pending = True
            self._stats["pending"] += 1
        for snapshot in checked.snapshots:
            self._handle(snapshot, tick)

    def _handle(self, snapshot: Snapshot, tick: _Tick) -> None:
        key = snapshot.monitor.key
        state = self._states[key]
        trigger: Trigger = "game" if tick.game_focused else state.trigger or "idle"
        state.trigger = None
        state.pending = False
        state.last_check = tick.now
        self._writer.capture_failures(key, snapshot.capture_failures)
        composite = snapshot.composite
        if not snapshot.changed:
            self._note(key, state, state.noted, tick)
        elif composite is None or composite.image is None:
            state.signature = None
            self._note(key, state, "hidden", tick)
        else:
            signature = imaging.signature(composite.image)
            if (
                state.signature is not None
                and not imaging.differs(state.signature, signature)
                and state.entries == snapshot.entries
            ):
                self._note(key, state, "shown", tick)
                return
            entries = snapshot.entries
            if composite.black:
                entries = tuple(replace(entry, image=None) for entry in entries)
            self._writer.submit(
                Picture(
                    monitor_key=key,
                    ts_ms=tick.ts_ms,
                    trigger=trigger,
                    image=None if composite.black else composite.image,
                    entries=entries,
                    uia_text=self._focused_text(snapshot.entries, tick),
                    defer_ocr=tick.game_active,
                    side=key != tick.focus_key,
                )
            )
            state.signature = signature
            state.entries = tuple(replace(entry, image=None) for entry in snapshot.entries)
            state.noted = "shown"
            self._stats["picture"] += 1

    def _note(self, key: str, state: _Monitor, noted: MonitorState, tick: _Tick) -> None:
        state.noted = noted
        self._writer.note(key, noted, tick.ts_ms)
        self._stats["unchanged" if noted == "shown" else noted] += 1

    # --- focused window text -----------------------------------------------------------------

    def _focused_text(self, entries: Sequence[Entry], tick: _Tick) -> str | None:
        """The focused window's UIA text, read once per tick, only for a picture that shows it."""
        entry = next((e for e in entries if e.role == "app" and e.focused), None)
        if entry is None or tick.focused is None or tick.game_active:
            return None
        if not tick.text_read:
            tick.text_read = True
            tick.text = self._read_text(tick.focused, entry, tick.now)
        return tick.text

    def _read_text(self, window: WindowInfo, entry: Entry, now: float) -> str | None:
        text = assemble_text(self._uia.text(window, now) or ())
        fresh = self._windows.focused_window()
        if fresh is None or fresh.hwnd != entry.hwnd or fresh.title != entry.title:
            return None
        family = BROWSERS.get(fresh.process)
        if family is not None:
            address = self._uia.address(fresh, family, now)
            if normalize_url(address) != entry.url:
                return None
        return text or None

    # --- gaming mode -------------------------------------------------------------------------

    def _record_transition(self, transition: Transition, now: float) -> None:
        """Write the game rows and report the flag; a failed write is counted, not a grab fault."""
        try:
            if transition.ended is not None:
                self._end_game_row()
            if transition.started is not None:
                self._start_game_row(transition.started.process, transition.started.title, now)
        except Exception as exc:  # noqa: BLE001 - e.g. a busy database
            self._note_game_log_fault(exc)
        finally:
            if transition.started is not None:
                for state in self._states.values():
                    state.last_check = float("-inf")  # the first game picture comes at once
            if self._on_game is not None and (transition.ended or transition.started):
                self._on_game(self._game_mode.active)

    def _note_game_log_fault(self, exc: Exception) -> None:
        log.warning("game log write failed: %s", type(exc).__name__)
        self._stats["game_log_error"] += 1

    def _start_game_row(self, process: str, title: str, now: float) -> None:
        self._game_id = self._games.open(
            process=process, window_title=title or None, ts_ms=self._now_ms()
        )
        log.info("game mode started for %s (fullscreen detection)", process)
        self._game_heartbeat_at = now
        self._stats["game_started"] += 1

    def _end_game_row(self) -> None:
        # Cleared first: a failed close leaves the open row to close_stale, never to a later end.
        game_id, self._game_id = self._game_id, None
        if game_id is not None:
            self._games.close(game_id, self._now_ms())
            log.info("game mode ended")
            self._stats["game_ended"] += 1

    def _game_heartbeat(self, now: float) -> None:
        if (
            self._game_id is not None
            and now - self._game_heartbeat_at >= self._game_settings.heartbeat_s
        ):
            self._game_heartbeat_at = now
            try:
                self._games.heartbeat(self._game_id, self._now_ms())
            except Exception as exc:  # noqa: BLE001 - e.g. a busy database
                self._note_game_log_fault(exc)


def _monitor_key_at(monitors: Sequence[MonitorInfo], point: tuple[int, int]) -> str | None:
    return next((m.key for m in monitors if m.rect.contains(*point)), None)
