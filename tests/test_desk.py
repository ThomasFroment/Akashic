"""Desk: visibility, capture lifecycle, verdict handling, layers, entries and change detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from capture_fakes import SCREEN, FakeWindowCaptures, blank, monitor, top_window

from akashic.capture import desk as desk_module
from akashic.capture.compose import Layer, compose
from akashic.capture.desk import Desk, Snapshot
from akashic.capture.ports import CapturedFrame, Rect, WindowInfo
from akashic.capture.verdicts import Judged, Judgement

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from akashic.capture.compose import Composite
    from akashic.capture.ports import MonitorInfo, TopWindow

M1 = monitor(key="m1", rect=Rect(0, 0, 800, 600))
M2 = monitor(key="m2", rect=Rect(800, 0, 1600, 600), primary=False)
RIGHT = Rect(800, 0, 1600, 600)

KEEP = Judged("keep", None, listed=True)
HOLD = Judged("hold", None, listed=True)
EXCLUDE = Judged("exclude", None, listed=True)
SHELL = Judged("keep", None, listed=False)


@dataclass
class RecordingCaptures(FakeWindowCaptures):
    log: list[tuple[str, int]] = field(default_factory=list[tuple[str, int]])
    on_start: CapturedFrame | None = None  # a frame that arrives as soon as a capture starts
    grab_none: bool = False

    def start(self, hwnd: int, min_interval_ms: int) -> bool:
        started = super().start(hwnd, min_interval_ms)
        if started and self.on_start is not None:
            self.push(hwnd, self.on_start.image, self.on_start.at)
        return started

    def latest_seq(self, hwnd: int) -> int | None:
        self.log.append(("latest_seq", hwnd))
        return super().latest_seq(hwnd)

    def grab(self, hwnd: int) -> CapturedFrame | None:
        self.log.append(("grab", hwnd))
        return None if self.grab_none else super().grab(hwnd)


class StubVerdicts:
    def __init__(self, log: list[tuple[str, int]]) -> None:
        self.table: dict[int, Judged] = {}
        self.calls: list[tuple[tuple[TopWindow, ...], frozenset[int], int | None, float]] = []
        self._log = log

    def judge(
        self,
        windows: Sequence[TopWindow],
        visible: Collection[int],
        focused: int | None,
        now: float,
    ) -> Judgement:
        self._log.append(("judge", 0))
        self.calls.append((tuple(windows), frozenset(visible), focused, now))
        listed = {w.hwnd for w in windows}
        wanted = set(visible)
        if focused is not None:
            wanted.add(focused)
        wanted &= listed
        return Judgement({h: self.table.get(h, KEEP) for h in wanted}, own_address=False)


class Rig:
    def __init__(self, monitors: Sequence[MonitorInfo] = (M1,)) -> None:
        self.captures = RecordingCaptures()
        self.verdicts = StubVerdicts(self.captures.log)
        self.desk = Desk(self.captures, self.verdicts)  # type: ignore[arg-type]
        self.monitors = list(monitors)
        self.windows: list[TopWindow] = []
        self.now = 0.0

    def check(
        self,
        *,
        due: Collection[str] = ("m1",),
        focused: int | None = None,
        step: float = 0.0,
        windows: Sequence[TopWindow] | None = None,
    ) -> list[Snapshot]:
        self.now += step
        info = WindowInfo(focused, "", "", SCREEN) if focused is not None else None
        shown = self.windows if windows is None else windows
        return self.desk.check(self.monitors, due, shown, info, self.now).snapshots

    def push(self, hwnd: int, color: str = "red") -> None:
        self.captures.push(hwnd, blank(color), self.now)

    def paint(self, *hwnds: int, color: str = "red") -> Snapshot:
        """Start the captures, deliver a frame to each, and return the first snapshot."""
        self.check()
        for hwnd in hwnds:
            self.push(hwnd, color)
        return self.check()[0]


def win(hwnd: int, rect: Rect = SCREEN, **kwargs: object) -> TopWindow:
    return top_window(hwnd=hwnd, rect=rect, **kwargs)  # type: ignore[arg-type]


def composite_of(snapshot: Snapshot) -> Composite:
    assert snapshot.composite is not None
    return snapshot.composite


def pixel(snapshot: Snapshot, xy: tuple[int, int] = (10, 10)) -> object:
    image = composite_of(snapshot).image
    assert image is not None
    return image.getpixel(xy)


# --- visibility --------------------------------------------------------------------------------


@pytest.mark.parametrize("why", ["minimized", "cloaked", "alpha0", "offscreen", "covered", "gone"])
def test_a_started_window_that_is_no_longer_visible_is_stopped_in_that_check(why: str) -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.windows = {
        "minimized": [win(1, minimized=True)],
        "cloaked": [win(1, cloaked=True)],
        "alpha0": [win(1, alpha=0)],
        "offscreen": [win(1, rect=Rect(2000, 0, 2800, 600))],
        "covered": [win(2), win(1)],
        "gone": [],
    }[why]
    rig.check(step=1)
    assert rig.captures.stops.count(1) == 1
    assert 1 not in rig.captures.intervals


def test_a_window_visible_again_is_pending_until_a_new_verdict_and_frame() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.check(windows=[], step=1)
    assert rig.check(step=1) == []
    assert rig.captures.starts == [1, 1]
    rig.push(1, "blue")
    [snap] = rig.check(step=0.1)
    assert snap.entries[0].painted
    assert pixel(snap) == (0, 0, 255)


def test_a_partly_covered_window_is_visible() -> None:
    rig = Rig()
    rig.windows = [win(2, Rect(0, 0, 400, 600)), win(1)]
    rig.check()
    assert sorted(rig.captures.starts) == [1, 2]


def test_an_alpha_zero_window_neither_counts_nor_covers() -> None:
    rig = Rig()
    rig.windows = [win(2, alpha=0), win(1)]
    rig.check()
    assert rig.captures.starts == [1]


def test_an_excluded_window_under_an_alpha_zero_one_still_shows_as_excluded() -> None:
    rig = Rig()
    rig.windows = [win(2, alpha=0), win(1)]
    rig.verdicts.table[1] = EXCLUDE
    [snap] = rig.check()
    assert composite_of(snap).has_hidden
    assert [e.role for e in snap.entries] == ["hidden"]


def test_visibility_is_worked_out_over_all_monitors_not_only_due_ones() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1, RIGHT)]
    rig.check(due=("m2",))
    assert rig.captures.starts == [1]
    rig.check(due=("m1",), step=1)
    assert rig.captures.stops == []
    rig.check(due=("m1",), windows=[], step=1)
    assert rig.captures.stops == [1]


def test_judge_gets_the_full_window_list_and_only_windows_visible_on_a_due_monitor() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1), win(2, RIGHT), win(3)]  # 3 is covered by 1
    rig.check(due=("m1",), focused=2, step=1.5)
    windows, visible, focused, now = rig.verdicts.calls[0]
    assert windows == tuple(rig.windows)
    assert visible == {1}
    assert focused == 2
    assert now == 1.5
    assert len(rig.verdicts.calls) == 1
    rig.check(due=("m1",))
    assert rig.verdicts.calls[1][2] is None


def test_a_window_judged_only_because_it_is_focused_is_not_started() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1), win(2, RIGHT)]
    rig.check(due=("m1",), focused=2)
    assert rig.captures.starts == [1]


# --- grab before judging -----------------------------------------------------------------------


def test_a_frame_is_grabbed_before_judging_and_only_when_its_seq_changed() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.check()
    rig.push(1)
    rig.captures.log.clear()
    rig.check()
    assert rig.captures.log == [("latest_seq", 1), ("grab", 1), ("judge", 0)]
    rig.captures.log.clear()
    rig.check()
    assert rig.captures.log == [("latest_seq", 1), ("judge", 0)]
    rig.push(1)
    rig.captures.log.clear()
    rig.check()
    assert ("grab", 1) in rig.captures.log


def test_no_seq_drops_the_copy_so_the_window_is_pending() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.captures.frames.clear()
    assert rig.check(step=1) == []
    [snap] = rig.check(step=0.5)
    assert not snap.entries[0].painted
    assert snap.entries[0].image is None


def test_a_failed_grab_drops_the_copy_so_the_window_is_pending() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.captures.grab_none = True
    rig.push(1, "blue")
    assert rig.check(step=1) == []
    [snap] = rig.check(step=0.5)
    assert not snap.entries[0].painted


# --- verdicts ----------------------------------------------------------------------------------


def test_exclude_stops_the_capture_and_no_old_frame_is_painted_after_a_flip_back() -> None:
    rig = Rig()
    rig.windows = [win(1, title="Secret")]
    rig.paint(1, color="red")
    rig.verdicts.table[1] = EXCLUDE
    [snap] = rig.check(step=1)
    assert rig.captures.stops == [1]
    assert snap.entries[0].role == "hidden"
    rig.verdicts.table[1] = KEEP
    assert rig.check(step=1) == []  # pending, restarted, no frame yet
    assert rig.captures.starts == [1, 1]
    rig.push(1, "blue")
    [snap] = rig.check(step=0.1)
    assert pixel(snap) == (0, 0, 255)
    assert snap.entries[0].image is not None
    assert snap.entries[0].image.getpixel((10, 10)) == (0, 0, 255)


def test_the_focused_window_is_stopped_on_exclude_too() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.verdicts.table[1] = EXCLUDE
    rig.check(focused=1, step=1)
    assert rig.captures.stops == [1]
    assert 1 not in rig.captures.intervals


def test_nothing_is_started_on_hold_or_exclude() -> None:
    rig = Rig()
    rig.windows = [win(1), win(2)]
    rig.verdicts.table = {1: HOLD, 2: EXCLUDE}
    rig.check()
    assert rig.captures.starts == []


def test_a_held_window_keeps_its_capture_and_a_later_keep_paints_the_latest_frame() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1, color="red")
    rig.verdicts.table[1] = HOLD
    rig.check(step=1)
    rig.push(1, "blue")
    [snap] = rig.check(step=0.5)
    assert rig.captures.stops == []
    assert ("grab", 1) in rig.captures.log
    assert not snap.entries[0].painted
    assert pixel(snap) == (0, 0, 0)
    rig.verdicts.table[1] = KEEP
    [snap] = rig.check(step=0.1)
    assert pixel(snap) == (0, 0, 255)


def test_a_held_window_is_laid_out_as_a_pending_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    layers: list[Layer] = []

    def spy(rect: Rect, given: Sequence[Layer]) -> Composite:
        layers[:] = given
        return compose(rect, given)

    monkeypatch.setattr(desk_module, "compose", spy)
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.verdicts.table[1] = HOLD
    rig.check(step=1)
    rig.check(step=0.5)
    assert [layer.kind for layer in layers] == ["pending"]


# --- capture failures --------------------------------------------------------------------------


def test_a_capture_that_will_not_start_counts_as_a_failure_and_stays_pending() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.captures.refuse.add(1)
    [snap] = rig.check()
    assert snap.capture_failures == 1
    assert not snap.entries[0].painted
    rig.captures.refuse.clear()
    rig.check(step=0.1)
    assert rig.captures.starts == [1]


def test_a_failed_capture_is_stopped_and_restarted_in_the_same_check() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.captures.failures.add(1)
    [snap] = rig.check(step=1)
    assert snap.capture_failures == 1
    assert rig.captures.stops == [1]
    assert rig.captures.starts == [1, 1]
    assert not snap.entries[0].painted
    assert rig.check(step=0.1) == []  # restarted, pending, no failure left to report


def test_a_failure_is_reported_even_when_the_restart_delivers_a_frame_at_once() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.paint(1)
    rig.captures.on_start = CapturedFrame(0, 0.0, blank("blue"))
    rig.captures.failures.add(1)
    snaps = rig.check(step=0.1)
    assert [s.capture_failures for s in snaps] == [1]


def test_a_failure_counts_on_every_monitor_that_shows_the_window() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1, Rect(400, 0, 1200, 600)), win(2)]
    rig.captures.refuse.add(1)
    snaps = rig.check(due=("m1", "m2"))
    assert [s.capture_failures for s in snaps] == [1, 1]


def test_a_failed_capture_of_a_window_on_a_monitor_that_is_not_due_is_ignored() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1, RIGHT)]
    rig.check(due=("m2",))
    rig.captures.failures.add(1)
    [snap] = rig.check(due=("m1",))
    assert snap.capture_failures == 0
    assert rig.captures.starts == [1]
    assert rig.captures.stops == []


# --- layers and entries ------------------------------------------------------------------------


def test_entries_are_front_first_with_roles_and_no_content_for_hidden_or_shell() -> None:
    rig = Rig()
    rig.windows = [
        win(1, Rect(0, 0, 200, 500), process="app.exe", title="App"),
        win(2, Rect(200, 0, 400, 500), title="Hold"),
        win(3, Rect(400, 0, 800, 500), title="Secret"),
        win(4, Rect(0, 500, 800, 600), title="Taskbar"),
    ]
    rig.verdicts.table = {
        1: Judged("keep", "https://a.example", listed=True),
        2: HOLD,
        3: EXCLUDE,
        4: SHELL,
    }
    rig.check()
    rig.push(1)
    rig.push(4)
    [snap] = rig.check(step=0.5)
    app, held, hidden, shell = snap.entries
    assert [e.z for e in snap.entries] == [0, 1, 2, 3]
    assert (app.role, app.hwnd, app.process, app.title, app.url) == (
        "app",
        1,
        "app.exe",
        "App",
        "https://a.example",
    )
    assert app.painted
    assert app.image is not None
    assert app.image.size == (800, 600)
    assert (shell.role, shell.hwnd, shell.title, shell.url, shell.painted) == (
        "shell",
        None,
        None,
        None,
        True,
    )
    for entry in (held, hidden):
        assert (entry.role, entry.hwnd, entry.title, entry.url, entry.image) == (
            "hidden",
            None,
            None,
            None,
            None,
        )
        assert not entry.painted


def test_an_app_entry_marks_the_focused_window() -> None:
    rig = Rig()
    rig.windows = [win(1, Rect(0, 0, 400, 600)), win(2, Rect(400, 0, 800, 600))]
    rig.check()
    rig.push(1)
    rig.push(2)
    [snap] = rig.check(focused=2, step=0.1)
    assert [e.focused for e in snap.entries] == [False, True]


def test_layers_go_bottom_to_top_with_listed_and_the_monitor_rect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[Rect, list[Layer]]] = []

    def spy(rect: Rect, given: Sequence[Layer]) -> Composite:
        seen.append((rect, list(given)))
        return compose(rect, given)

    monkeypatch.setattr(desk_module, "compose", spy)
    rig = Rig([M2])
    a, b, c = Rect(800, 0, 1000, 600), Rect(1000, 0, 1200, 600), Rect(1200, 0, 1600, 600)
    rig.windows = [win(1, a), win(2, b), win(3, c)]
    rig.verdicts.table = {1: EXCLUDE, 2: SHELL, 3: KEEP}
    rig.check(due=("m2",))
    rig.push(2)
    rig.push(3)
    rig.check(due=("m2",), step=0.1)
    rect, layers = seen[-1]
    assert rect == RIGHT
    assert [(layer.rect, layer.kind, layer.listed) for layer in layers] == [
        (c, "frame", True),
        (b, "frame", False),
        (a, "excluded", True),
    ]
    assert layers[0].image is not None


# --- a new frame is a change ---------------------------------------------------------------------


def test_a_new_frame_is_a_change_once() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    snap = rig.paint(1)
    assert snap.changed
    assert not rig.check()[0].changed
    rig.push(1)
    assert rig.check()[0].changed


def test_a_window_spanning_two_monitors_changes_each_once() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1, Rect(400, 0, 1200, 600))]
    rig.check(due=("m1", "m2"))
    rig.push(1)
    first = rig.check(due=("m1", "m2"))
    assert [s.changed for s in first] == [True, True]
    second = rig.check(due=("m1", "m2"))
    assert [s.changed for s in second] == [False, False]


def test_a_new_frame_is_tracked_per_monitor() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1, Rect(400, 0, 1200, 600))]
    rig.check(due=("m1",))
    rig.push(1)
    assert rig.check(due=("m1",))[0].changed
    assert rig.check(due=("m2",))[0].changed
    assert not rig.check(due=("m1",))[0].changed


# --- changed -----------------------------------------------------------------------------------


def test_changed_is_true_first_and_false_when_nothing_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def spy(rect: Rect, given: Sequence[Layer]) -> Composite:
        calls.append(1)
        return compose(rect, given)

    monkeypatch.setattr(desk_module, "compose", spy)
    rig = Rig()
    rig.windows = [win(1)]
    first = rig.paint(1)
    assert first.changed
    assert first.composite is not None
    assert len(calls) == 1
    [again] = rig.check()
    assert not again.changed
    assert again.composite is None
    assert len(calls) == 1
    assert len(again.entries) == 1


@pytest.mark.parametrize("what", ["title", "focus", "frame", "rect"])
def test_changed_when_the_signature_differs(what: str) -> None:
    rig = Rig()
    rig.windows = [win(1, Rect(0, 0, 700, 600), title="A")]
    rig.paint(1)
    focused = None
    if what == "title":
        rig.windows = [win(1, Rect(0, 0, 700, 600), title="B")]
    elif what == "focus":
        focused = 1
    elif what == "frame":
        rig.push(1, "blue")
    else:
        rig.windows = [win(1, Rect(0, 0, 600, 600), title="A")]
    [snap] = rig.check(focused=focused)
    assert snap.changed
    assert snap.composite is not None


def test_held_to_excluded_sets_changed() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.verdicts.table[1] = HOLD
    rig.check()
    [held] = rig.check(step=0.5)
    assert held.changed
    assert composite_of(held).image is not None
    rig.verdicts.table[1] = EXCLUDE
    [snap] = rig.check(step=0.1)
    assert snap.changed
    assert composite_of(snap).image is None
    assert composite_of(snap).has_hidden


# --- pending delay -----------------------------------------------------------------------------


def test_a_monitor_with_a_pending_window_waits_half_a_second_then_shows_it_blank() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.now = 10.0
    assert rig.check() == []
    assert rig.check(step=0.4) == []
    [snap] = rig.check(step=0.1)
    assert not snap.entries[0].painted
    assert pixel(snap) == (0, 0, 0)


def test_the_wait_ends_as_soon_as_the_frame_arrives() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    assert rig.check() == []
    rig.push(1)
    [snap] = rig.check(step=0.1)
    assert snap.entries[0].painted


def test_a_held_window_waits_too() -> None:
    rig = Rig()
    rig.windows = [win(1)]
    rig.verdicts.table[1] = HOLD
    assert rig.check() == []
    assert len(rig.check(step=0.5)) == 1


def test_snapshots_come_in_monitor_order_and_only_for_due_monitors() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1, SCREEN), win(2, RIGHT)]
    rig.check(due=("m1", "m2"))
    rig.push(1)
    rig.push(2)
    both = rig.check(due=("m2", "m1"))
    assert [s.monitor.key for s in both] == ["m1", "m2"]
    only = rig.check(due=("m2",))
    assert [s.monitor.key for s in only] == ["m2"]


# --- drop_all ----------------------------------------------------------------------------------


def test_drop_all_stops_everything_and_the_next_check_restarts_and_changes_every_monitor() -> None:
    rig = Rig([M1, M2])
    rig.windows = [win(1), win(2, RIGHT)]
    rig.check(due=("m1", "m2"))
    rig.push(1)
    rig.push(2)
    rig.check(due=("m1", "m2"))
    assert not any(s.changed for s in rig.check(due=("m1", "m2")))
    rig.desk.drop_all()
    assert sorted(rig.captures.stops) == [1, 2]
    assert rig.check(due=("m1", "m2")) == []  # pending again, copies gone
    assert rig.captures.starts == [1, 2, 1, 2]
    rig.push(1)
    rig.push(2)
    snaps = rig.check(due=("m1", "m2"))
    assert [s.changed for s in snaps] == [True, True]
