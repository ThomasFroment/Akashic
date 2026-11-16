"""Verdicts: keep, hold or exclude for each visible or focused window, with a fake UIA and clock."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from capture_fakes import SCREEN, FakeUia, top_window

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.ports import TopWindow, WindowChanges, WindowEvent, WindowInfo
from akashic.capture.uia_budget import BudgetedUia
from akashic.capture.verdicts import Judged, Verdicts
from akashic.config import Exclusions as UserExclusions

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from akashic.capture.ports import BrowserFamily

P1 = "P1 - Google Chrome"
P2 = "P2 - Google Chrome"
OWN_UI = "Akashic · local memory"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.step = 0.0  # added by every UIA call, to tell "before the first call" from "after"

    def __call__(self) -> float:
        return self.t


@dataclass
class ClockedUia(FakeUia):
    clock: Clock = dataclasses.field(default_factory=Clock)
    reads: list[int] = dataclasses.field(default_factory=list[int])
    caption_reads: list[int] = dataclasses.field(default_factory=list[int])

    def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        self.reads.append(window.hwnd)
        self.clock.t += self.clock.step
        return super().read_url(window, family)

    def browser_caption(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        self.caption_reads.append(window.hwnd)
        self.clock.t += self.clock.step
        return super().browser_caption(window, family)


class Harness:
    def __init__(self, user: UserExclusions | None = None, step: float = 0.0) -> None:
        self.clock = Clock()
        self.clock.step = step
        self.uia = ClockedUia(clock=self.clock)
        self.browsers = BrowserState(self.clock)
        self.verdicts = Verdicts(
            Exclusions(user or UserExclusions()), BudgetedUia(self.uia), self.browsers, self.clock
        )

    def judge(
        self,
        windows: Sequence[TopWindow],
        at: float = 0.0,
        *,
        visible: Collection[int] | None = None,
        focused: int | None = None,
    ) -> Mapping[int, Judged]:
        self.clock.t = at
        shown = {w.hwnd for w in windows} if visible is None else visible
        return self.verdicts.judge(windows, shown, focused, at).windows

    def verdict(self, window: TopWindow, at: float = 0.0) -> str:
        return self.judge([window], at)[window.hwnd].verdict

    def title_event(self, hwnd: int, at: float) -> None:
        self.verdicts.note_events(WindowChanges((WindowEvent(hwnd, "title", at),), healthy=True))

    def destroyed(self, hwnd: int, at: float) -> None:
        self.verdicts.note_events(
            WindowChanges((WindowEvent(hwnd, "destroyed", at),), healthy=True)
        )

    def lost_events(self, at: float = 10.0) -> None:
        self.clock.t = at
        self.verdicts.note_events(WindowChanges((), healthy=False))

    def rejected(self, process: str = "chrome.exe") -> str | None:
        return self.browsers.media_reason(process, None)


def browser(
    process: str = "chrome.exe", hwnd: int = 1, title: str = P1, **kwargs: object
) -> TopWindow:
    return top_window(hwnd=hwnd, process=process, class_name="Browser", title=title, **kwargs)  # type: ignore[arg-type]


def chrome(hwnd: int = 1, title: str = P1, **kwargs: object) -> TopWindow:
    return browser("chrome.exe", hwnd, title, **kwargs)


def notepad(hwnd: int = 1, **kwargs: object) -> TopWindow:
    return top_window(hwnd=hwnd, **kwargs)  # type: ignore[arg-type]


def excluded_notepad(hwnd: int = 1, **kwargs: object) -> TopWindow:
    return notepad(hwnd, foreign_children=("keepass.exe",), **kwargs)


# --- the result set ----------------------------------------------------------------------------


def test_judge_returns_one_entry_per_visible_or_focused_listed_window_and_never_raises() -> None:
    h = Harness()
    windows = [notepad(1), notepad(2), notepad(3)]
    result = h.judge(windows, visible={1, 77}, focused=3)
    assert set(result) == {1, 3}
    assert set(h.judge(windows, visible=set(), focused=88)) == set()
    assert h.judge([], visible={1}, focused=None) == {}


def test_a_plain_window_is_kept_and_listed_with_no_read() -> None:
    judged = Harness().judge([notepad()])[1]
    assert judged == Judged("keep", url=None, listed=True)


# --- rules of a window by itself ---------------------------------------------------------------


@pytest.mark.parametrize(
    "process", ["", "applicationframehost.exe", "keepass.exe", "consent.exe", "secret.exe"]
)
def test_a_window_the_process_rules_exclude_is_excluded(process: str) -> None:
    h = Harness(UserExclusions(processes=("secret.exe",)))
    assert h.verdict(notepad(process=process)) == "exclude"


@pytest.mark.parametrize("address", ["example.org/page", None])
@pytest.mark.parametrize(
    ("process", "title"),
    [
        ("zen.exe", "Page - Mozilla Firefox Private Browsing"),
        ("zen.exe", ""),
        ("msedge.exe", ""),
    ],
)
def test_a_browser_window_the_title_rules_exclude_is_excluded(
    process: str, title: str, address: str | None
) -> None:
    h = Harness()
    h.uia.urls[1] = address
    assert h.verdict(browser(process, 1, title)) == "exclude"


def test_the_own_ui_title_is_no_reason_to_exclude_a_window() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    assert h.verdict(browser("zen.exe", 1, OWN_UI)) == "keep"


def test_a_blank_title_in_chrome_is_not_a_reason() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    assert h.verdict(chrome(title="")) == "keep"


def test_unreadable_foreign_children_exclude_and_a_foreign_child_with_a_reason_excludes() -> None:
    h = Harness()
    assert h.verdict(notepad(foreign_children=None)) == "exclude"
    assert h.verdict(notepad(foreign_children=("keepass.exe",))) == "exclude"
    assert h.verdict(notepad(foreign_children=("helper.exe",))) == "keep"
    h.uia.urls[1] = "example.org/page"
    assert h.verdict(chrome(foreign_children=None)) == "exclude"


@pytest.mark.parametrize(
    ("process", "class_name"),
    [
        ("explorer.exe", "XamlExplorerHostIslandWindow"),
        ("explorer.exe", "MultitaskingViewFrame"),
        ("explorer.exe", "TaskListThumbnailWnd"),
        ("snippingtool.exe", "XamlWindow"),
    ],
)
def test_shell_copy_surfaces_are_always_excluded(process: str, class_name: str) -> None:
    window = top_window(process=process, class_name=class_name, title="")
    assert Harness().judge([window])[1] == Judged("exclude", None, listed=False)


SHELL_KEPT = ["Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"]


@pytest.mark.parametrize("class_name", SHELL_KEPT)
def test_the_taskbar_and_desktop_are_kept_but_not_listed(class_name: str) -> None:
    window = top_window(process="explorer.exe", class_name=class_name, title="")
    assert Harness().judge([window])[1].verdict == "keep"
    assert Harness().judge([window])[1].listed is False


@pytest.mark.parametrize("class_name", SHELL_KEPT)
def test_the_taskbar_and_desktop_are_excluded_by_another_rule(class_name: str) -> None:
    window = top_window(
        process="explorer.exe", class_name=class_name, title="", foreign_children=("keepass.exe",)
    )
    assert Harness().verdict(window) == "exclude"


# --- ghosts ------------------------------------------------------------------------------------


def test_a_ghost_is_excluded_by_its_own_rules_first() -> None:
    windows = [notepad(1), notepad(2, is_ghost=True, ghost_of=1, foreign_children=None)]
    assert Harness().judge(windows)[2].verdict == "exclude"


def test_a_ghost_takes_the_verdict_of_the_hung_window_even_when_it_is_not_visible() -> None:
    h = Harness()
    kept = [notepad(1), notepad(2, is_ghost=True, ghost_of=1)]
    result = h.judge(kept, visible={2})
    assert set(result) == {2}
    assert result[2].verdict == "keep"
    assert result[2].listed is False
    dropped = [excluded_notepad(1), notepad(2, is_ghost=True, ghost_of=1)]
    assert h.judge(dropped, visible={2})[2].verdict == "exclude"


@pytest.mark.parametrize("ghost_of", [None, 999])
def test_a_ghost_of_nothing_known_is_excluded(ghost_of: int | None) -> None:
    window = notepad(2, is_ghost=True, ghost_of=ghost_of)
    assert Harness().judge([notepad(1), window])[2].verdict == "exclude"


def test_a_ghost_of_a_kept_browser_window_is_kept_without_an_address() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    windows = [chrome(1), chrome(2, is_ghost=True, ghost_of=1)]
    ghost = h.judge(windows)[2]
    assert (ghost.verdict, ghost.url, ghost.listed) == ("keep", None, False)


# --- owners and popups -------------------------------------------------------------------------


def test_a_window_follows_its_owner_chain_and_the_strictest_verdict_wins() -> None:
    h = Harness()
    chain = [excluded_notepad(1), notepad(2, owner=1), notepad(3, owner=2)]
    assert {k: v.verdict for k, v in h.judge(chain).items()} == {
        1: "exclude",
        2: "exclude",
        3: "exclude",
    }
    own_excluded = [notepad(1), excluded_notepad(2, owner=1)]
    assert h.judge(own_excluded)[2].verdict == "exclude"
    assert h.judge([notepad(1), notepad(2, owner=1)])[2].verdict == "keep"


def test_an_owner_loop_is_excluded() -> None:
    result = Harness().judge([notepad(1, owner=2), notepad(2, owner=1)])
    assert result[1].verdict == "exclude"
    assert result[2].verdict == "exclude"


def test_an_owner_missing_from_the_list_changes_nothing() -> None:
    assert Harness().verdict(notepad(1, owner=999)) == "keep"


@pytest.mark.parametrize("owner", [None, 999])
def test_an_ownerless_popup_is_excluded_when_its_app_has_an_excluded_window(
    owner: int | None,
) -> None:
    hidden = excluded_notepad(1, process="app.exe", minimized=True, cloaked=True)
    popup = notepad(2, process="app.exe", popup=True, owner=owner)
    assert Harness().judge([hidden, popup], visible={2})[2].verdict == "exclude"


def test_an_ownerless_popup_keeps_its_own_verdict_otherwise() -> None:
    h = Harness()
    hidden = excluded_notepad(1, process="app.exe", minimized=True)
    other_app = notepad(2, process="other.exe", popup=True)
    assert h.judge([hidden, other_app], visible={2})[2].verdict == "keep"
    assert (
        h.judge([notepad(1, process="app.exe"), notepad(2, process="app.exe", popup=True)])[
            2
        ].verdict
        == "keep"
    )
    not_popup = notepad(2, process="app.exe")
    assert h.judge([hidden, not_popup], visible={2})[2].verdict == "keep"


# --- browser main windows ----------------------------------------------------------------------


def test_a_main_window_is_kept_with_its_normalized_address() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    assert h.judge([chrome()])[1] == Judged("keep", url="https://example.org/page", listed=True)


def test_every_ownerless_browser_window_has_its_address_read() -> None:
    h = Harness()
    h.judge([chrome(1), chrome(2), chrome(3, owner=1)])
    assert sorted(h.uia.reads) == [1, 2]


def test_a_main_window_stays_main_until_destroyed_or_missing_from_a_list() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.judge([chrome()])
    del h.uia.urls[1]
    assert h.verdict(chrome(), 1.0) == "keep"  # the address is unreadable now, but it is main
    h.destroyed(1, 2.0)
    assert h.verdict(chrome(), 3.0) == "exclude"
    h.uia.urls[1] = "example.org/page"
    h.judge([chrome()], 4.0)
    del h.uia.urls[1]
    h.judge([notepad(2)], 5.0)
    assert h.verdict(chrome(), 6.0) == "exclude"


def test_a_devtools_address_does_not_make_a_main_window() -> None:
    h = Harness()
    h.uia.urls[1] = "devtools://devtools/bundled/devtools_app.html"
    assert h.verdict(chrome()) == "exclude"


def test_a_main_window_with_a_private_caption_is_excluded() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.captions[1] = "Page - Google Chrome (Incognito)"
    assert h.verdict(chrome()) == "exclude"


@pytest.mark.parametrize(
    ("process", "expected"),
    [
        ("chrome.exe", "exclude"),
        ("brave.exe", "exclude"),
        ("msedge.exe", "keep"),
        ("zen.exe", "keep"),
    ],
)
def test_an_unreadable_caption_excludes_only_chrome_and_brave(process: str, expected: str) -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.captions[1] = None
    assert h.verdict(browser(process, 1, "Page")) == expected


def test_a_main_window_on_an_excluded_address_is_excluded() -> None:
    h = Harness()
    h.uia.urls[1] = "https://www.paypal.com/myaccount"
    judged = h.judge([chrome()])[1]
    assert (judged.verdict, judged.url) == ("exclude", None)


@pytest.mark.parametrize("address", [None, "half typed words"])
def test_an_unreadable_or_unparseable_address_is_kept_without_a_url(address: str | None) -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.judge([chrome()])
    h.uia.urls[1] = address
    judged = h.judge([chrome()], 1.0)[1]
    assert (judged.verdict, judged.url) == ("keep", None)


@pytest.mark.parametrize(
    "address",
    ["chrome-extension://abc/popup.html", "moz-extension://abc/popup.html", "extension://abc/a"],
)
def test_an_extension_page_is_excluded_with_its_reason(address: str) -> None:
    h = Harness()
    h.uia.urls[1] = address
    assert h.verdict(chrome()) == "exclude"
    assert h.rejected() == "browser_extension"


# --- other browser windows ---------------------------------------------------------------------


def test_a_popup_style_browser_window_that_is_not_main_is_excluded() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    windows = [chrome(1), chrome(2, popup=True), chrome(3, popup=True, owner=1)]
    result = h.judge(windows)
    assert [result[i].verdict for i in (1, 2, 3)] == ["keep", "exclude", "exclude"]


def test_other_browser_windows_take_the_strictest_verdict_of_the_mains() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    pip = chrome(3, "Picture in picture")
    owned = chrome(4, "Dialog", owner=1)
    devtools = chrome(5, "DevTools")
    h.uia.urls[5] = "devtools://devtools/bundled/devtools_app.html"
    windows = [chrome(1), pip, owned, devtools]
    kept = h.judge(windows)
    assert [kept[i].verdict for i in (3, 4, 5)] == ["keep", "keep", "keep"]
    assert [kept[i].url for i in (3, 4, 5)] == [None, None, None]
    h.uia.captions[1] = "Page - Google Chrome (Incognito)"
    private = h.judge(windows, 1.0)
    assert [private[i].verdict for i in (1, 3, 4, 5)] == ["exclude"] * 4


def test_mains_are_read_even_when_covered_minimized_or_cloaked() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.urls[2] = "example.org/other"
    h.uia.captions[2] = "Other - Google Chrome (Incognito)"
    windows = [chrome(1), chrome(2, minimized=True, cloaked=True), chrome(3, "PiP")]
    result = h.judge(windows, visible={1, 3})
    assert result[3].verdict == "exclude"
    assert 2 in h.uia.reads


def test_another_browsers_mains_do_not_count() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.urls[2] = "example.org/other"
    h.uia.captions[2] = "Other - Mozilla Firefox Private Browsing"
    windows = [
        chrome(1),
        browser("zen.exe", 2, "Other - Mozilla Firefox Private Browsing"),
        chrome(3, "PiP"),
    ]
    assert h.judge(windows)[3].verdict == "keep"


def test_another_browser_window_with_no_main_known_is_excluded() -> None:
    h = Harness()
    assert h.verdict(chrome(3, "Picture in picture")) == "exclude"
    assert h.rejected() == "browser_unknown"


def test_chrome_picture_in_picture_with_no_caption_is_excluded() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.captions[3] = None
    assert h.judge([chrome(1), chrome(3, "PiP")])[3].verdict == "exclude"
    assert h.rejected() == "caption_unknown"


@pytest.mark.parametrize("process", ["chrome.exe", "brave.exe"])
def test_a_bar_less_chrome_or_brave_window_with_a_private_caption_is_excluded(
    process: str,
) -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.captions[2] = "Page - Google Chrome (Incognito)"
    windows = [browser(process, 1), browser(process, 2, "Page")]
    assert h.judge(windows)[2].verdict == "exclude"


@pytest.mark.parametrize("process", ["chrome.exe", "brave.exe"])
def test_a_bar_less_chrome_or_brave_window_with_a_clean_caption_follows_the_mains(
    process: str,
) -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    windows = [browser(process, 1), browser(process, 2, "Picture in picture")]
    assert h.judge(windows)[2].verdict == "keep"
    h.uia.captions[1] = "Page - Google Chrome (Incognito)"
    assert h.judge(windows, 1.0)[2].verdict == "exclude"


@pytest.mark.parametrize("process", ["msedge.exe", "zen.exe"])
def test_other_browsers_picture_in_picture_with_no_caption_follows_the_mains(process: str) -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.captions[3] = None
    assert h.judge([browser(process, 1), browser(process, 3, "PiP")])[3].verdict == "keep"


def test_the_caption_of_another_window_is_read_once_per_judge() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    windows = [chrome(1), chrome(3, "PiP")]
    h.judge(windows)
    assert h.uia.caption_reads.count(3) == 1
    h.judge(windows, 1.0)
    assert h.uia.caption_reads.count(3) == 2


def test_another_window_with_a_private_title_is_excluded_by_its_own_rules() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    windows = [
        browser("zen.exe", 1),
        browser("zen.exe", 2, "Video - Mozilla Firefox Private Browsing"),
    ]
    result = h.judge(windows)
    assert [result[1].verdict, result[2].verdict] == ["keep", "exclude"]


# --- holds -------------------------------------------------------------------------------------


def kept_main(h: Harness, address: str = "a.org") -> None:
    h.uia.urls[1] = address
    assert h.judge([chrome(1, P1)], 0.0)[1].verdict == "keep"


def at(h: Harness, now: float, title: str = P2) -> Judged:
    return h.judge([chrome(1, title)], now)[1]


def test_a_title_change_holds_until_three_reads_and_half_a_second_have_passed() -> None:
    h = Harness()
    kept_main(h)
    first = at(h, 10.0)
    assert first == Judged("hold", url=None, listed=False)
    assert at(h, 10.2).verdict == "hold"
    assert at(h, 10.4).verdict == "hold"  # three reads, but not yet 500 ms
    ended = at(h, 10.5)
    assert (ended.verdict, ended.url) == ("keep", "https://a.org")


def test_a_hold_with_fewer_than_three_reads_outlasts_half_a_second() -> None:
    h = Harness()
    kept_main(h)
    assert at(h, 10.0).verdict == "hold"
    assert at(h, 10.6).verdict == "hold"
    assert at(h, 10.7).verdict == "keep"


def test_a_different_address_ends_the_hold_with_the_verdict_of_that_read() -> None:
    h = Harness()
    kept_main(h)
    h.uia.urls[1] = "b.org"
    judged = at(h, 10.0)
    assert (judged.verdict, judged.url) == ("keep", "https://b.org")


def test_a_read_that_excludes_ends_the_hold_at_once() -> None:
    h = Harness()
    kept_main(h)
    assert at(h, 10.0).verdict == "hold"
    h.uia.urls[1] = "https://www.paypal.com/myaccount"
    assert at(h, 10.1).verdict == "exclude"
    h.uia.urls[1] = "a.org"
    assert at(h, 20.0).verdict == "keep"  # an excluded window is never held afterwards


def test_a_title_event_holds_from_the_events_time() -> None:
    h = Harness()
    kept_main(h)
    h.title_event(1, 20.0)
    assert at(h, 19.9, P1).verdict == "hold"
    h.uia.urls[1] = "b.org"
    assert at(h, 19.95, P1).verdict == "hold"  # still read before the hold began
    assert at(h, 20.0, P1).verdict == "keep"


def test_lost_events_hold_a_kept_main_window() -> None:
    h = Harness()
    kept_main(h)
    h.lost_events()
    assert at(h, 10.0, P1).verdict == "hold"
    assert at(h, 10.2, P1).verdict == "hold"
    assert at(h, 10.4, P1).verdict == "hold"
    assert at(h, 10.5, P1).verdict == "keep"
    assert at(h, 11.0, P1).verdict == "keep"  # the lost-events trigger fired once


def test_a_minimized_main_window_with_a_visible_pip_holds_once_after_a_title_event() -> None:
    h = Harness()
    h.uia.urls[1] = "a.org"
    windows = [chrome(1, minimized=True), chrome(2, "PiP")]

    def pip_at(now: float) -> str:
        return h.judge(windows, now, visible={2})[2].verdict

    assert pip_at(0.0) == "keep"
    h.title_event(1, 10.0)
    assert [pip_at(now) for now in (10.0, 10.2, 10.4)] == ["hold"] * 3
    assert pip_at(10.5) == "keep"
    assert pip_at(11.0) == "keep"


def test_lost_events_hold_a_window_that_is_not_judged_until_later() -> None:
    h = Harness()
    kept_main(h)
    h.lost_events()
    h.judge([chrome()], 10.0, visible=set())
    assert at(h, 10.0, P1).verdict == "hold"


def test_a_title_event_before_first_sight_starts_no_later_hold() -> None:
    h = Harness()
    h.uia.urls[1] = "a.org"
    h.title_event(1, 0.5)  # the new window retitles while it loads, before its first judge
    assert at(h, 1.0, P1).verdict == "keep"
    assert at(h, 2.0, P1).verdict == "keep"


def test_an_unreadable_address_counts_toward_the_three_reads_only() -> None:
    h = Harness()
    kept_main(h)
    h.uia.urls[1] = None
    assert at(h, 10.0).verdict == "hold"
    assert at(h, 10.3).verdict == "hold"
    ended = at(h, 10.6)
    assert (ended.verdict, ended.url) == ("keep", None)


def test_an_unreadable_read_never_replaces_the_last_kept_address() -> None:
    h = Harness()
    kept_main(h, "a.org")
    h.uia.urls[1] = None
    assert at(h, 1.0, P1).verdict == "keep"
    h.title_event(1, 2.0)
    h.uia.urls[1] = "a.org"  # the OLD address again: the page has not changed yet
    assert at(h, 2.0, P1).verdict == "hold"


def test_a_page_kept_with_an_unreadable_address_leaves_no_last_kept_address() -> None:
    h = Harness()
    kept_main(h, "a.org")
    h.uia.urls[1] = "https://www.paypal.com/myaccount"
    assert at(h, 1.0, P1).verdict == "exclude"
    h.uia.urls[1] = None
    assert at(h, 2.0, P1).verdict == "keep"
    h.title_event(1, 10.0)
    h.uia.urls[1] = "c.org"  # unknown is not "different": neither A nor C ends the hold
    assert at(h, 10.0, P2).verdict == "hold"


def test_a_hold_ended_by_the_read_rule_on_an_unreadable_address_forgets_the_old_one() -> None:
    h = Harness()
    kept_main(h, "a.org")
    h.uia.urls[1] = None
    assert at(h, 10.0).verdict == "hold"
    assert at(h, 10.3).verdict == "hold"
    assert at(h, 10.6).verdict == "keep"
    h.title_event(1, 20.0)
    h.uia.urls[1] = "a.org"
    assert at(h, 20.0, "P3 - Google Chrome").verdict == "hold"


def test_a_new_trigger_restarts_the_hold() -> None:
    h = Harness()
    kept_main(h)
    assert at(h, 10.0).verdict == "hold"
    assert at(h, 10.2).verdict == "hold"
    h.title_event(1, 10.4)
    assert at(h, 10.4, "P3 - Google Chrome").verdict == "hold"
    assert at(h, 10.6, "P3 - Google Chrome").verdict == "hold"  # unrestarted, this would end
    assert at(h, 10.9, "P3 - Google Chrome").verdict == "keep"


def test_a_window_judged_excluded_is_never_held() -> None:
    h = Harness()
    h.uia.urls[1] = "a.org"
    h.uia.captions[1] = "P1 - Google Chrome (Incognito)"
    assert at(h, 0.0, P1).verdict == "exclude"
    del h.uia.captions[1]
    h.title_event(1, 10.0)
    assert at(h, 10.0, P2).verdict == "keep"


def test_a_window_seen_for_the_first_time_is_never_held() -> None:
    h = Harness()
    kept_main(h)
    h.title_event(2, 5.0)
    h.uia.urls[2] = "b.org"
    result = h.judge([chrome(1, P1), chrome(2, P2)], 10.0)
    assert result[2].verdict == "keep"


def test_windows_that_inherit_a_main_window_are_held_with_it() -> None:
    h = Harness()
    kept_main(h)
    windows = [chrome(1, P2), chrome(3, "PiP"), chrome(4, "Dialog", owner=1), notepad(5, owner=1)]
    result = h.judge(windows, 10.0)
    assert {i: result[i].verdict for i in (1, 3, 4, 5)} == dict.fromkeys((1, 3, 4, 5), "hold")
    assert {result[i].url for i in (1, 3, 4, 5)} == {None}
    assert not any(result[i].listed for i in (1, 3, 4, 5))


# --- listed and what is exposed -------------------------------------------------------


def test_listed_is_true_only_for_a_kept_window_that_is_neither_shell_nor_ghost() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    windows = [
        chrome(1),
        excluded_notepad(2),
        top_window(hwnd=3, process="explorer.exe", class_name="Progman", title=""),
        notepad(4),
        notepad(5, is_ghost=True, ghost_of=4),
    ]
    assert {i: j.listed for i, j in h.judge(windows).items()} == {
        1: True,
        2: False,
        3: False,
        4: True,
        5: False,
    }


def test_only_a_kept_main_window_has_a_url() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    windows = [chrome(1), chrome(2, "PiP"), excluded_notepad(3)]
    result = h.judge(windows)
    assert [result[i].url for i in (1, 2, 3)] == ["https://example.org/page", None, None]


def test_an_excluded_window_exposes_no_title_url_or_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="akashic")
    h = Harness()
    h.uia.urls[1] = "https://www.paypal.com/zz-sentinel-path"
    judged = h.judge([chrome(1, "ZZ-Sentinel-Title - Google Chrome")], 0.0)[1]
    assert dataclasses.astuple(judged) == ("exclude", None, False)
    assert not caplog.records


# --- BrowserState ------------------------------------------------------------------------------


def focus(hwnd: int = 1, title: str = P1) -> WindowInfo:
    return WindowInfo(hwnd, "chrome.exe", title, SCREEN)


def test_a_kept_visible_or_focused_main_window_is_published_as_kept() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.judge([chrome()], visible={1})
    assert h.browsers.media_reason("chrome.exe", focus()) is None
    other = Harness()
    other.uia.urls[1] = "example.org/page"
    other.judge([chrome()], visible=set(), focused=1)
    assert other.browsers.media_reason("chrome.exe", focus()) is None


def test_a_window_neither_visible_nor_focused_is_not_published() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.urls[2] = "example.org/other"
    h.uia.captions[2] = "Other - Google Chrome (Incognito)"
    h.judge([chrome(1), chrome(2, "Other - Google Chrome")], visible={1})
    assert h.rejected() is None
    assert h.browsers.media_reason("chrome.exe", focus(2, "Other - Google Chrome")) == "url_unknown"


def test_a_held_window_is_not_published() -> None:
    h = Harness()
    kept_main(h)
    h.browsers.clear()
    h.lost_events()
    assert at(h, 10.0, P1).verdict == "hold"
    assert h.browsers.media_reason("chrome.exe", focus()) == "url_unknown"


def test_a_browser_popup_is_not_published() -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    assert h.judge([chrome(1), chrome(2, "Popup", popup=True)])[2].verdict == "exclude"
    assert h.rejected() is None


@pytest.mark.parametrize(
    ("title", "address", "reason"),
    [
        ("Page - Google Chrome (Incognito)", "example.org/page", "private_window"),
        ("Bank - Google Chrome", "https://www.paypal.com/myaccount", "finance"),
    ],
)
def test_a_private_or_bank_page_on_a_side_monitor_rejects_media_for_a_minute(
    title: str, address: str, reason: str
) -> None:
    h = Harness()
    h.uia.urls[1] = "example.org/page"
    h.uia.urls[2] = address
    h.uia.captions[2] = title
    h.judge([chrome(1), chrome(2, title)], 0.0, visible={1, 2}, focused=1)
    h.clock.t = 59.0
    assert h.rejected() == reason
    h.clock.t = 61.0
    assert h.rejected() is None
