"""What may be captured of each window: keep, hold or exclude. No pixel is touched here.

``Verdicts.judge`` turns the OS window list into one ``Judged`` per visible or focused window.
Each window is read at most once per call and judged by, in order: shell copy surfaces, process
and title, child windows of other apps, ghost windows (the hung window's verdict), then its
kind. A browser's main window is the one whose address bar can be read; its verdict comes from
that address and its caption. Other browser windows (picture-in-picture, DevTools) take the
strictest verdict of their browser's main windows; in Chrome and Brave such a window also needs a
readable caption without a private marker. Other apps' windows follow their owner, and an
ownerless popup is excluded when its app has an excluded window.

A kept main window whose title changes (or after lost window events) is held until its address
changes or a few reads have passed (docs/baselines/2026-11-16-title-change-hold.md), so the
pixels of a new page are never painted under the old page's verdict.

A visible main browser window that shows Akashic's own address sets ``Judgement.own_address``; the
screen loop then halts capture (the failsafe) and ``shows_own_address`` watches for it to go.

A UIA read can come back unfinished or late, started before this check (``capture/uia_threads.py``).
An unfinished address never makes a window a main window; a late one may recognize a main window
but never keeps it. Either holds a known main window, or excludes it by its rules, without reading
its caption; an unfinished caption counts as None; either keeps the failsafe halted.

Nothing here logs or exposes a title, an address or the reason of an exclusion, except to
``BrowserState``, which needs the reason to reject that browser's media.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, Literal

from akashic.capture.exclusions import CAPTION_ONLY_BROWSERS, SHELL_COPY_SURFACES, normalize_url
from akashic.capture.ports import BROWSERS, WindowInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence

    from akashic.capture.browser_state import BrowserState
    from akashic.capture.exclusions import Exclusions
    from akashic.capture.ports import TopWindow, WindowChanges
    from akashic.capture.uia_budget import BudgetedUia
    from akashic.capture.uia_threads import UiaRead

type Verdict = Literal["keep", "hold", "exclude"]

# Bound of a hold: both this long and this many reads started after it began, measured with
# 22 runs per case in Chrome and Zen: the address trails a title event by at most 106 ms.
HOLD_MIN_S: Final = 0.5
HOLD_MIN_READS: Final = 3
DEVTOOLS_PREFIX: Final = "devtools://"
# The taskbar and the desktop: kept, but not windows of the user's work.
SHELL_KEPT: Final = frozenset(
    {
        ("explorer.exe", "Shell_TrayWnd"),
        ("explorer.exe", "Shell_SecondaryTrayWnd"),
        ("explorer.exe", "Progman"),
        ("explorer.exe", "WorkerW"),
    }
)
_STRICTNESS: Final[dict[Verdict, int]] = {"keep": 0, "hold": 1, "exclude": 2}


@dataclass(frozen=True, slots=True)
class Judged:
    verdict: Verdict
    url: str | None  # a kept main browser window's address
    listed: bool  # kept and a window the user sees as one (not the shell, not a ghost)


@dataclass(frozen=True, slots=True)
class Judgement:
    windows: Mapping[int, Judged]
    own_address: bool  # a visible main browser window showed Akashic's own address


@dataclass(frozen=True, slots=True)
class _Result:
    verdict: Verdict
    reason: str | None = None  # exclusion category; BrowserState is the only reader
    url: str | None = None
    publishes: bool = False


@dataclass(slots=True)
class _Hold:
    start: float
    reads: int = 0


@dataclass(slots=True)
class _Seen:
    title: str
    kept: bool
    address: str | None  # raw address of the page now kept; None when unknown
    hold: _Hold | None = None

    def remember(self, address: str | None) -> None:
        if address is not None:
            self.address = address


@dataclass(slots=True)
class _State:
    mains: set[int] = field(default_factory=set[int])
    seen: dict[int, _Seen] = field(default_factory=dict[int, _Seen])
    title_at: dict[int, float] = field(default_factory=dict[int, float])


_KEEP: Final = _Result("keep")
_LOOP: Final = _Result("exclude", "window_loop")


def _excluded(reason: str) -> _Result:
    return _Result("exclude", reason)


def _strictest(*results: _Result) -> _Result:
    worst = max(results, key=lambda r: _STRICTNESS[r.verdict])
    return _Result(worst.verdict, worst.reason)


def _is_shell_kept(window: TopWindow) -> bool:
    return (window.process, window.class_name) in SHELL_KEPT


def _info(window: TopWindow) -> WindowInfo:
    return WindowInfo(
        window.hwnd, window.process, window.title, window.rect, window.has_frame, pid=window.pid
    )


class Verdicts:
    def __init__(
        self,
        exclusions: Exclusions,
        uia: BudgetedUia,
        browsers: BrowserState,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exclusions = exclusions
        self._uia = uia
        self._browsers = browsers
        self._clock = clock
        self._state = _State()
        # Per judge call: the window list, its clock and the memoized verdicts and reads.
        self._windows: Sequence[TopWindow] = ()
        self._by_hwnd: dict[int, TopWindow] = {}
        self._now = 0.0
        self._results: dict[tuple[str, int], _Result] = {}
        self._active: set[tuple[str, int]] = set()
        self._addresses: dict[int, UiaRead[str]] = {}
        self._visible: Collection[int] = ()
        self._saw_own_address = False

    def note_events(self, changes: WindowChanges) -> None:
        """Take in a drain of window events: title changes start holds, destroyed windows are
        forgotten, lost events hold every kept main window."""
        state = self._state
        if not changes.healthy:
            state.title_at.update(dict.fromkeys(state.seen, self._clock()))
        for event in changes.events:
            if event.change == "title":
                state.title_at[event.hwnd] = event.at
            elif event.change == "destroyed":
                state.mains.discard(event.hwnd)
                state.seen.pop(event.hwnd, None)
                state.title_at.pop(event.hwnd, None)

    def judge(
        self,
        windows: Sequence[TopWindow],
        visible: Collection[int],
        focused: int | None,
        now: float,
    ) -> Judgement:
        """One verdict for each listed window that is visible or focused, by window handle."""
        state = self._state
        self._windows = windows
        self._by_hwnd = {w.hwnd: w for w in windows}
        self._now = now
        self._visible = visible
        self._saw_own_address = False
        self._results.clear()
        self._active.clear()
        self._addresses.clear()
        state.mains &= self._by_hwnd.keys()
        state.seen = {hwnd: seen for hwnd, seen in state.seen.items() if hwnd in self._by_hwnd}
        state.title_at = {h: at for h, at in state.title_at.items() if h in self._by_hwnd}
        wanted = {*visible, focused}
        judged = {w.hwnd: self._judged(w) for w in windows if w.hwnd in wanted}
        return Judgement(judged, self._saw_own_address)

    def shows_own_address(
        self, windows: Sequence[TopWindow], visible: Collection[int], now: float
    ) -> bool:
        """Whether a visible, ownerless browser window shows Akashic's own address. Reads only
        addresses (the failsafe watch): no caption, no text, no verdict. A window whose address
        is not really read (unfinished, late, or None while backed off) counts as showing it.
        The backoff never skips the read here: the halted screen loop has no other work."""
        for window in windows:
            if (
                window.hwnd not in visible
                or window.process not in BROWSERS
                or window.owner is not None
                or window.is_ghost
            ):
                continue
            backed_off = self._uia.backed_off(window.hwnd, now)
            read = self._uia.address(
                _info(window), BROWSERS[window.process], now, ignore_backoff=True
            )
            if read.started_since(now) is None or (backed_off and read.value is None):
                return True
            url = normalize_url(read.value)
            if url is not None and self._exclusions.is_own_address(url):
                return True
        return False

    def _judged(self, window: TopWindow) -> Judged:
        result = self._verdict(window.hwnd)
        if result.publishes:
            self._browsers.publish(window, result.reason)
        kept = result.verdict == "keep"
        return Judged(
            result.verdict,
            url=result.url if kept else None,
            listed=kept and not window.is_ghost and not _is_shell_kept(window),
        )

    def _verdict(self, hwnd: int) -> _Result:
        return self._memo("verdict", self._by_hwnd[hwnd], self._compute)

    def _memo(
        self, kind: str, window: TopWindow, compute: Callable[[TopWindow], _Result]
    ) -> _Result:
        key = (kind, window.hwnd)
        if key in self._results:
            return self._results[key]
        if key in self._active:  # an owner or ghost loop: fail closed
            return _LOOP
        self._active.add(key)
        result = compute(window)
        self._active.discard(key)
        self._results[key] = result
        return result

    def _compute(self, window: TopWindow) -> _Result:
        if window.process in BROWSERS and not window.is_ghost:
            return self._browser(window)
        own = self._own(window)
        if own.verdict != "keep" or window.is_ghost or _is_shell_kept(window):
            return own
        return self._follow_owner(window, own)

    # --- rules of a window by itself ---------------------------------------------------------

    def _own(self, window: TopWindow) -> _Result:
        return self._memo("own", window, self._own_rules)

    def _own_rules(self, window: TopWindow) -> _Result:
        if (window.process, window.class_name) in SHELL_COPY_SURFACES:
            return _excluded("shell_copy_surface")
        reason = self._rules_reason(window)
        if reason is not None:
            return _excluded(reason)
        return self._ghost(window) if window.is_ghost else _KEEP

    def _ghost(self, window: TopWindow) -> _Result:
        if window.ghost_of is None or window.ghost_of not in self._by_hwnd:
            return _excluded("ghost_unknown")
        return replace(self._verdict(window.ghost_of), url=None, publishes=False)

    def _rules_reason(self, window: TopWindow) -> str | None:
        reason = self._exclusions.check_window(window.process, window.title)
        if reason is not None:
            return reason
        if window.foreign_children is None:
            return "children_unknown"
        return next(
            (
                r
                for child in window.foreign_children
                if (r := self._exclusions.check_process(child))
            ),
            None,
        )

    def _follow_owner(self, window: TopWindow, own: _Result) -> _Result:
        if window.owner is not None and window.owner in self._by_hwnd:
            return _strictest(own, self._verdict(window.owner))
        if window.popup and self._app_has_excluded_window(window.process):
            return _excluded("app_excluded")
        return own

    def _app_has_excluded_window(self, process: str) -> bool:
        return any(
            w.process == process and self._own(w).verdict == "exclude" for w in self._windows
        )

    # --- browsers ----------------------------------------------------------------------------

    def _browser(self, window: TopWindow) -> _Result:
        if window.owner is None:
            self._recognize(window)
        is_main = window.hwnd in self._state.mains
        rules = self._rules_reason(window)
        if is_main:
            result = self._main(window, rules)
        elif rules is not None:
            result = _excluded(rules)
        elif window.popup:
            result = _excluded("browser_popup")
        else:
            result = self._other(window)
        return replace(result, publishes=result.verdict != "hold" and (is_main or not window.popup))

    def _address(self, window: TopWindow) -> UiaRead[str]:
        """The address bar read; read once per call."""
        if window.hwnd not in self._addresses:
            self._addresses[window.hwnd] = self._uia.address(
                _info(window), BROWSERS[window.process], self._now
            )
        return self._addresses[window.hwnd]

    def _recognize(self, window: TopWindow) -> None:
        """A readable address that is not DevTools' makes this window a main window for good."""
        address = self._address(window).value
        if address is not None and not address.casefold().startswith(DEVTOOLS_PREFIX):
            self._state.mains.add(window.hwnd)

    def _other(self, window: TopWindow) -> _Result:
        """Picture-in-picture, DevTools, bar-less windows: their browser's main windows decide.
        Chrome and Brave also need a caption that reads without a private marker."""
        if window.process in CAPTION_ONLY_BROWSERS:
            caption = self._uia.caption(_info(window), BROWSERS[window.process], self._now).value
            if caption is None:
                return _excluded("caption_unknown")
            if self._exclusions.is_private_title(caption):
                return _excluded("private_window")
        candidates = [
            w
            for w in self._windows
            if w.process == window.process and w.owner is None and not w.is_ghost
        ]
        for candidate in candidates:
            self._recognize(candidate)
        mains = [w.hwnd for w in candidates if w.hwnd in self._state.mains]
        if not mains:
            return _excluded("browser_unknown")
        return _strictest(*(self._verdict(hwnd) for hwnd in mains))

    def _main(self, window: TopWindow, rules: str | None) -> _Result:
        read = self._address(window)
        url = normalize_url(read.value)
        if (
            url is not None
            and window.hwnd in self._visible
            and self._exclusions.is_own_address(url)
        ):
            self._saw_own_address = True
        event_at = self._state.title_at.get(window.hwnd)
        started_at = read.started_since(self._now)
        if started_at is None or (event_at is not None and started_at < event_at):
            return _Result("hold") if rules is None else _excluded(rules)
        return self._held(window, self._fresh(window, rules, url), read.value, started_at)

    def _fresh(self, window: TopWindow, rules: str | None, url: str | None) -> _Result:
        if rules is not None:
            return _excluded(rules)
        caption = self._uia.caption(_info(window), BROWSERS[window.process], self._now).value
        if caption and self._exclusions.is_private_title(caption):
            return _excluded("private_window")
        if caption is None and window.process in CAPTION_ONLY_BROWSERS:
            return _excluded("caption_unknown")  # the private marker may be hidden
        reason = self._exclusions.check_browser(url)
        if reason is not None:
            return _excluded(reason)
        return _Result("keep", url=url)

    # --- title-change hold -------------------------------------------------------------------

    def _held(self, window: TopWindow, fresh: _Result, address: str | None, at: float) -> _Result:
        # Consumed even on first sight: the fresh read below already started after that event.
        event_at = self._state.title_at.pop(window.hwnd, None)
        seen = self._state.seen.get(window.hwnd)
        if seen is None:
            self._state.seen[window.hwnd] = _Seen(window.title, fresh.verdict == "keep", address)
            return fresh
        started = self._hold_start(window, seen, event_at)
        seen.title = window.title
        was_kept = seen.kept
        seen.kept = fresh.verdict == "keep"
        if not (was_kept and seen.kept):
            seen.hold = None
            seen.address = address
            return fresh
        if started is not None:
            seen.hold = _Hold(started)
        if seen.hold is None:
            seen.remember(address)
        elif self._hold_ends(seen.hold, address, seen.address, at):
            seen.hold = None
            seen.address = address
        else:
            return _Result("hold")
        return fresh

    def _hold_start(self, window: TopWindow, seen: _Seen, event_at: float | None) -> float | None:
        """When a hold triggered by this check begins, or None when nothing triggers one."""
        if not seen.kept:
            return None
        if event_at is None and window.title == seen.title:
            return None
        return self._now if event_at is None else event_at

    def _hold_ends(
        self, hold: _Hold, address: str | None, kept_address: str | None, read_at: float
    ) -> bool:
        if read_at < hold.start:
            return False
        if address is not None and kept_address is not None and address != kept_address:
            return True
        hold.reads += 1
        return read_at - hold.start >= HOLD_MIN_S and hold.reads >= HOLD_MIN_READS
