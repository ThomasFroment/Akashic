"""The Desk: which windows are captured, what each monitor's picture shows, and when it changed.

One ``check`` works out which windows are visible on any monitor, then, in this order:

1. Captures of windows that are missing or no longer visible anywhere are stopped, with their
   RAM copy.
2. For started windows on a due monitor, failed captures are stopped and forgotten, and the
   newest frame is grabbed when its sequence changed. This happens before judging, so a frame
   painted under a verdict was grabbed before the read that verdict rests on.
3. ``Verdicts.judge`` runs once. An ``exclude`` stops the capture and drops the copy in that
   check, so nothing from before the flip can be painted later; a ``keep`` starts a capture.
4. Each due monitor's layers and entries are built, a monitor with a window pending for less
   than ``PENDING_WAIT_S`` is left out unless a capture failed on it, and the picture is
   composed only when it changed. A snapshot is ``incomplete`` while a shown window is held or
   a started one awaits a frame (for at most ``PENDING_RECHECK_S``).

Held and excluded windows are blank rectangles with no title, URL or pixels in any entry.
Nothing is logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture import occlusion
from akashic.capture.compose import Composite, Layer, LayerKind, compose
from akashic.capture.ports import Entry, MonitorInfo

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from akashic.capture.ports import CapturedFrame, Rect, TopWindow, WindowCaptures, WindowInfo
    from akashic.capture.verdicts import Judged, Verdicts

FRAME_INTERVAL_MS: Final = 1000
PENDING_WAIT_S: Final = 0.5
# A started window with no frame (for example capture-excluded) stops speeding up its
# monitor's checks after this long.
PENDING_RECHECK_S: Final = 5.0

type _Signature = tuple[tuple[object, ...], ...]


@dataclass(frozen=True, slots=True)
class Snapshot:
    monitor: MonitorInfo
    changed: bool
    composite: Composite | None  # set only when ``changed``
    entries: tuple[Entry, ...]  # front first
    capture_failures: int
    incomplete: bool  # a shown window is held or awaiting a frame: check this monitor again soon


@dataclass(frozen=True, slots=True)
class Checked:
    snapshots: list[Snapshot]  # of the due monitors that are ready, in ``monitors`` order
    own_address: bool  # a visible main browser window showed Akashic's own address


@dataclass(frozen=True, slots=True)
class _Row:
    window: TopWindow
    judged: Judged
    copy: CapturedFrame | None  # only for a kept window

    @property
    def pending(self) -> bool:
        return self.judged.verdict == "hold" or (
            self.judged.verdict == "keep" and self.copy is None
        )

    @property
    def layer_kind(self) -> LayerKind:
        if self.judged.verdict == "exclude":
            return "excluded"
        return "pending" if self.pending else "frame"


class Desk:
    def __init__(self, captures: WindowCaptures, verdicts: Verdicts) -> None:
        self._captures = captures
        self._verdicts = verdicts
        self._started: set[int] = set()
        self._copies: dict[int, CapturedFrame] = {}
        self._pending_since: dict[int, float] = {}
        self._returned: dict[str, _Signature] = {}  # what each monitor's last snapshot showed

    def check(
        self,
        monitors: Sequence[MonitorInfo],
        due: Collection[str],
        windows: Sequence[TopWindow],
        focused: WindowInfo | None,
        now: float,
    ) -> Checked:
        """Snapshots of the due monitors that are ready, and whether the failsafe tripped."""
        visible = visible_monitors(windows, monitors)
        self._forget_invisible(visible.keys())
        keys = {m.key for m in monitors}
        self._returned = {k: v for k, v in self._returned.items() if k in keys}
        due_monitors = [m for m in monitors if m.key in due]
        due_keys = keys & set(due)
        visible_on_due = {h for h, keys in visible.items() if not keys.isdisjoint(due_keys)}

        failed = self._refresh_copies(visible_on_due)
        focused_hwnd = focused.hwnd or None if focused else None
        judgement = self._verdicts.judge(windows, visible_on_due, focused_hwnd, now)
        judged = judgement.windows
        failed |= self._apply_verdicts(judged, visible_on_due)

        rows = {
            w.hwnd: _Row(
                w,
                judged[w.hwnd],
                self._copies.get(w.hwnd) if judged[w.hwnd].verdict == "keep" else None,
            )
            for w in windows
            if w.hwnd in visible_on_due
        }
        for hwnd, row in rows.items():
            if row.pending:
                self._pending_since.setdefault(hwnd, now)
            else:
                self._pending_since.pop(hwnd, None)

        snapshots: list[Snapshot] = []
        for monitor in due_monitors:
            shown = [rows[w.hwnd] for w in windows if monitor.key in visible.get(w.hwnd, ())]
            failures = sum(1 for row in shown if row.window.hwnd in failed)
            if not failures and any(self._still_waiting(row, now) for row in shown):
                continue
            incomplete = any(self._awaited(row, failed, now) for row in shown)
            snapshots.append(
                self._snapshot(
                    monitor, shown, focused_hwnd, failures=failures, incomplete=incomplete
                )
            )
        return Checked(snapshots, judgement.own_address)

    def retake(self, monitor_key: str) -> None:
        """Forget what was last returned for a monitor: its next snapshot is changed."""
        self._returned.pop(monitor_key, None)

    def drop_all(self) -> None:
        for hwnd in self._started:
            self._captures.stop(hwnd)
        self._started.clear()
        self._copies.clear()
        self._pending_since.clear()
        self._returned.clear()

    def _stop(self, hwnd: int) -> None:
        if hwnd in self._started:
            self._captures.stop(hwnd)
            self._started.discard(hwnd)
        self._copies.pop(hwnd, None)

    def _forget_invisible(self, visible: Collection[int]) -> None:
        for hwnd in self._started - set(visible):
            self._stop(hwnd)
        for hwnd in self._pending_since.keys() - set(visible):
            del self._pending_since[hwnd]

    def _refresh_copies(self, visible_on_due: Collection[int]) -> set[int]:
        """Grab newer frames of started windows on due monitors; returns the failed captures."""
        broken = self._captures.failed()
        failed: set[int] = set()
        for hwnd in self._started & set(visible_on_due):
            if hwnd in broken:
                failed.add(hwnd)
                self._stop(hwnd)
                continue
            seq = self._captures.latest_seq(hwnd)
            copy = self._copies.get(hwnd)
            if seq is None:
                self._copies.pop(hwnd, None)
            elif copy is None or copy.seq != seq:
                grabbed = self._captures.grab(hwnd)
                if grabbed is None:
                    self._copies.pop(hwnd, None)
                else:
                    self._copies[hwnd] = grabbed
        return failed

    def _apply_verdicts(
        self, judged: Mapping[int, Judged], visible_on_due: Collection[int]
    ) -> set[int]:
        """Stop what is excluded, start what is kept; returns the captures that would not start."""
        failed: set[int] = set()
        for hwnd, result in judged.items():
            if result.verdict == "exclude":
                self._stop(hwnd)
            elif result.verdict == "keep" and hwnd in visible_on_due and hwnd not in self._started:
                if self._captures.start(hwnd, FRAME_INTERVAL_MS):
                    self._started.add(hwnd)
                else:
                    failed.add(hwnd)
        return failed

    def _still_waiting(self, row: _Row, now: float) -> bool:
        return row.pending and now - self._pending_since[row.window.hwnd] < PENDING_WAIT_S

    def _awaited(self, row: _Row, failed: Collection[int], now: float) -> bool:
        hwnd = row.window.hwnd
        if hwnd in failed:
            return False
        if row.judged.verdict == "hold":
            return True
        return (
            row.pending
            and hwnd in self._started
            and now - self._pending_since[hwnd] < PENDING_RECHECK_S
        )

    def _snapshot(
        self,
        monitor: MonitorInfo,
        shown: list[_Row],
        focused_hwnd: int | None,
        *,
        failures: int,
        incomplete: bool,
    ) -> Snapshot:
        entries = tuple(_entry(row, z, focused_hwnd) for z, row in enumerate(shown))
        signature = _signature(entries, shown)
        changed = self._returned.get(monitor.key) != signature
        composite = None
        if changed:
            layers = [
                Layer(
                    row.window.rect,
                    row.layer_kind,
                    row.judged.listed,
                    row.copy.image if row.copy else None,
                )
                for row in reversed(shown)
            ]
            composite = compose(monitor.rect, layers)
        self._returned[monitor.key] = signature
        return Snapshot(monitor, changed, composite, entries, failures, incomplete)


def visible_monitors(
    windows: Sequence[TopWindow], monitors: Sequence[MonitorInfo]
) -> dict[int, set[str]]:
    """For each window with a visible part, the keys of the monitors that part touches."""
    visible: dict[int, set[str]] = {}
    covers: list[Rect] = []
    for window in windows:
        if window.minimized or window.cloaked or window.alpha == 0:
            continue
        parts = occlusion.visible_parts(window.rect, covers)
        keys = {
            m.key for m in monitors if any(m.rect.intersect(part) is not None for part in parts)
        }
        if keys:
            visible[window.hwnd] = keys
        covers.append(window.rect)
    return visible


def _entry(row: _Row, z: int, focused_hwnd: int | None) -> Entry:
    window, copy = row.window, row.copy
    if row.judged.verdict != "keep":
        return Entry("hidden", None, window.rect, z)
    if not row.judged.listed:
        return Entry("shell", None, window.rect, z, painted=copy is not None)
    return Entry(
        "app",
        window.hwnd,
        window.rect,
        z,
        process=window.process,
        title=window.title,
        url=row.judged.url,
        focused=window.hwnd == focused_hwnd,
        painted=copy is not None,
        image=copy.image if copy else None,
    )


def _signature(entries: Sequence[Entry], rows: Sequence[_Row]) -> _Signature:
    return tuple(
        (
            e.hwnd,
            e.role,
            row.layer_kind,
            e.rect,
            e.process,
            e.title,
            e.url,
            e.focused,
            e.painted,
            row.copy.seq if row.copy else None,
        )
        for e, row in zip(entries, rows, strict=True)
    )
