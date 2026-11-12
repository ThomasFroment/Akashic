"""Capture-side privacy: password, address, hosted-app and local-UI checks come before any write.

Every scenario runs the screen loop and the media tracker over disposable encrypted stores with a
writer spy (``sink_spy``) watching every SQL write, every encrypted file and every log record.
Each requirement also has a negative control: the old behaviour is put back at the narrowest
point and the same scenario must then leak, which proves the assertion can fail.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import pytest
from capture_fakes import (
    AUMID,
    HOSTED_TITLES,
    FakeMedia,
    FakeOcr,
    FakeScreen,
    FakeUia,
    FakeWindows,
    window,
)
from sink_spy import SECRET, SinkSpy
from waits import wait_for

from akashic.capture import browser_state
from akashic.capture import main as capture_main
from akashic.capture import media as media_module
from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.media import MediaTracker
from akashic.capture.ports import CapturePlatform, MediaState, Rect, TextBlock, WindowInfo
from akashic.capture.screen import ScreenCapture
from akashic.child import ChildContext
from akashic.config import Config
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.supervisor import CHILDREN, ChildSpec, Supervisor, spawn_module

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sink_spy import PrivacyRig

    from akashic.config import Paths

SECRET_TEXT = [TextBlock(f"{SECRET} the rest of the form, long enough to count as window text.")]


def row(n: int) -> list[Rect]:
    return [Rect(800 * i, 0, 800 * (i + 1), 600) for i in range(n)]


def chrome(title: str = "Page - Google Chrome", hwnd: int = 1, rect: Rect | None = None):  # noqa: ANN201
    return window(hwnd=hwnd, process="chrome.exe", title=title, rect=rect or Rect(0, 0, 800, 600))


def playing(title: str = f"{SECRET} title", artist: str = f"{SECRET} channel") -> MediaState:
    return MediaState("Chrome", title, artist, "playing")


# --- the spy itself: it must see what a leak looks like --------------------------------------


def test_the_spy_sees_a_stored_frame_in_every_sink(privacy_rig: Callable[..., PrivacyRig]) -> None:
    rig = privacy_rig(focused=window(title=f"{SECRET} notes"), texts={1: SECRET_TEXT})
    rig.tick()
    assert rig.frames() == 1
    assert rig.leaks(SECRET) == ["sql", "tables"]  # the title and the text reached the DB
    assert len(rig.spy.files) == 1  # and the screenshot reached the file writer
    assert not rig.spy.nothing_stored(rig.conn)


# --- #16: the focused window is checked on every monitor it covers ---------------------------


class _AnySet(set[int]):
    """Stand-in for the per-tick window set that never remembers anything (the old behaviour)."""

    def add(self, element: int) -> None:
        del element


def _old_window_for(self: ScreenCapture, monitor: Rect):  # noqa: ANN202
    focused = self._windows.focused_window()
    if focused is not None and monitor.contains(*focused.rect.center):
        return focused, True
    return self._windows.window_at(*monitor.center), False


def spanning(privacy_rig: Callable[..., PrivacyRig], monitors: int, **kwargs: object) -> PrivacyRig:
    """The focused window starts on monitor 0 and covers the centre of every other one."""
    # Centred on monitor 0 (x = 400) and reaching past the centre of the last monitor.
    wide = chrome(
        f"{SECRET} Sign in - Google Chrome",
        rect=Rect(1100 - 800 * monitors, 0, 800 * monitors - 300, 600),
    )
    rig = privacy_rig(monitors=row(monitors), focused=wide, texts={1: SECRET_TEXT}, **kwargs)
    for rect in row(monitors)[1:]:
        rig.windows.at[rect.center] = wide  # the topmost window there is the focused one
    return rig


@pytest.mark.parametrize("monitors", [2, 3, 4])
def test_password_focus_on_a_spanning_window_stores_nothing_on_any_monitor(
    privacy_rig: Callable[..., PrivacyRig], monitors: int
) -> None:
    rig = spanning(privacy_rig, monitors)
    rig.uia.password = True
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.capture.stats["excluded:password_field"] == 1
    assert rig.capture.stats["duplicate_window"] == monitors - 1


def test_the_window_is_the_focused_one_on_every_monitor_it_covers(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = spanning(privacy_rig, 3)
    monitors = row(3)
    assert [rig.capture._window_for(m)[1] for m in monitors] == [True, True, True]
    other = chrome("Other", hwnd=2, rect=Rect(1600, 0, 2400, 600))
    rig.windows.at[monitors[2].center] = other
    assert rig.capture._window_for(monitors[2]) == (other, False)


def test_a_spanning_window_is_captured_once_per_tick(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = spanning(privacy_rig, 3)
    rig.tick()
    assert rig.frames() == 1
    assert len(rig.spy.files) == 1
    assert rig.capture.stats["duplicate_window"] == 2
    rig.tick(20.0)  # idle again on every monitor: examined once more, not three times
    assert rig.capture.stats["duplicate_window"] == 4


def test_negative_control_the_old_behaviour_stores_the_form(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ScreenCapture, "_window_for", _old_window_for)
    rig = spanning(privacy_rig, 2)
    rig.capture._tick_hwnds = _AnySet()
    rig.uia.password = True
    rig.tick()
    assert rig.frames() >= 1  # the second monitor stored the unchecked window
    assert "sql" in rig.leaks(SECRET)


# --- #17: a UI Automation failure is "unknown", never "not a password field" -----------------


def test_unknown_password_state_skips_the_frame_and_is_counted(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=window(title=f"{SECRET} form"), texts={1: SECRET_TEXT})
    rig.uia.password = None
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.capture.stats["excluded:password_unknown"] == 1


def test_no_focused_element_is_neither_a_password_nor_unknown(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=window(title="Notes"), texts={1: SECRET_TEXT})
    rig.uia.password = False  # UI Automation answered: nothing password-like has the focus
    rig.tick()
    assert rig.frames() == 1
    assert "excluded:password_unknown" not in rig.capture.stats
    assert "excluded:password_field" not in rig.capture.stats


def test_a_state_that_becomes_unknown_while_grabbing_discards_the_frame(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=window(title=f"{SECRET} form"), texts={1: SECRET_TEXT})
    answers = iter([False, False, None, None, None])
    rig.uia.focused_is_password = lambda: next(answers)  # type: ignore[method-assign]
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.capture.stats["window_changed"] == 1


def test_negative_control_treating_unknown_as_clear_stores_the_form(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def old_reason(capture: ScreenCapture) -> str | None:
        return "password_field" if capture._uia.focused_is_password() else None

    monkeypatch.setattr(ScreenCapture, "_password_reason", old_reason)
    rig = privacy_rig(focused=window(title=f"{SECRET} form"), texts={1: SECRET_TEXT})
    rig.uia.password = None
    rig.tick()
    assert rig.frames() == 1
    assert "sql" in rig.leaks(SECRET)


# --- #18: the screen loop's verdict on the focused browser gates media writes ----------------


def test_media_on_an_excluded_page_is_never_written(privacy_rig: Callable[..., PrivacyRig]) -> None:
    rig = privacy_rig(focused=chrome(f"{SECRET} Mon espace - Google Chrome"))
    rig.uia.urls[1] = "https://www.paypal.com/myaccount/summary"
    rig.media.states = [playing()]
    rig.tick()
    rig.poll()
    rig.poll()
    assert rig.sessions() == 0
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.tracker.stats["excluded:finance"] == 2


@pytest.mark.parametrize(
    ("user", "url", "reason"),
    [
        (UserExclusions(urls=("*mybank.example*",)), "https://mybank.example/compte", "user_url"),
        (UserExclusions(), "https://vault.bitwarden.com/#/vault", "password_manager"),
        (UserExclusions(urls=("*mybank.example*",)), None, "url_unreadable"),
    ],
    ids=["user-rule", "password-manager", "unreadable-with-user-rule"],
)
def test_other_excluding_verdicts_reject_the_session_too(
    privacy_rig: Callable[..., PrivacyRig], user: UserExclusions, url: str | None, reason: str
) -> None:
    rig = privacy_rig(user=user, focused=chrome(f"{SECRET} Compte - Google Chrome"))
    rig.uia.urls[1] = url
    rig.media.states = [playing()]
    rig.tick()
    rig.poll()
    assert rig.sessions() == 0
    assert rig.leaks(SECRET) == []
    assert rig.tracker.stats[f"excluded:{reason}"] == 1


def test_a_harmless_page_is_logged_so_the_gate_is_not_a_blanket(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=chrome("Video - YouTube - Google Chrome"))
    rig.uia.urls[1] = "https://www.youtube.com/watch?v=abc"
    rig.media.states = [playing("A video", "A channel")]
    rig.tick()
    rig.poll()
    assert rig.sessions() == 1


def test_media_polled_before_the_screen_loop_has_looked_is_rejected(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=chrome(f"{SECRET} Compte - Google Chrome"))
    rig.uia.urls[1] = "https://www.paypal.com/"
    rig.media.states = [playing()]
    rig.poll()  # the screen loop has not published anything yet: unknown state
    assert rig.sessions() == 0
    assert rig.tracker.stats["excluded:url_unknown"] == 1
    rig.tick()
    rig.poll()
    assert rig.sessions() == 0
    assert rig.leaks(SECRET) == []


def test_a_title_change_without_a_new_verdict_is_unknown(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=chrome("Video - YouTube - Google Chrome"))
    rig.uia.urls[1] = "https://www.youtube.com/watch?v=abc"
    rig.media.states = [playing("A video", "A channel")]
    rig.tick()
    rig.poll()
    assert rig.sessions() == 1
    # The user navigates to a bank page; the screen loop has not looked at the new title yet.
    rig.windows.focused = chrome(f"{SECRET} Compte - Google Chrome")
    rig.media.states = [playing()]
    rig.poll()
    rig.poll()
    assert rig.leaks(SECRET) == []
    assert rig.tracker.stats["excluded:url_unknown"] == 2


def test_a_stale_verdict_is_unknown(privacy_rig: Callable[..., PrivacyRig]) -> None:
    rig = privacy_rig(focused=chrome("Video - YouTube - Google Chrome"))
    rig.uia.urls[1] = "https://www.youtube.com/watch?v=abc"
    rig.media.states = [playing("A video", "A channel")]
    rig.tick()
    rig.poll(advance_s=browser_state.FRESH_S + 1)  # the screen loop went quiet
    assert rig.tracker.stats["excluded:url_unknown"] == 1


def test_an_excluding_verdict_outlives_a_focus_change_for_a_while(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=chrome(f"{SECRET} Compte - Google Chrome"))
    rig.uia.urls[1] = "https://www.paypal.com/"
    rig.tick()
    rig.windows.focused = window(hwnd=9, process="notepad.exe", title="Notes")  # focus moved
    rig.media.states = [playing()]
    rig.poll(advance_s=browser_state.HOLD_S - 5)
    assert rig.sessions() == 0
    assert rig.leaks(SECRET) == []
    rig.poll(advance_s=10)  # long after: only the accepted background gap remains
    assert rig.sessions() == 1


def test_negative_control_without_the_gate_the_title_and_channel_are_written(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_gate(state: BrowserState, process: str | None, focus: WindowInfo | None) -> str | None:
        del state, process, focus
        return None

    monkeypatch.setattr(BrowserState, "media_reason", no_gate)
    rig = privacy_rig(focused=chrome(f"{SECRET} Mon espace - Google Chrome"))
    rig.uia.urls[1] = "https://www.paypal.com/myaccount/summary"
    rig.media.states = [playing()]
    rig.tick()
    rig.poll()
    assert rig.sessions() == 1
    assert "sql" in rig.leaks(SECRET)


# --- #19: an unreadable address does not switch the address rules off -----------------------


@pytest.mark.parametrize(
    ("user", "title", "reason"),
    [
        (
            UserExclusions(urls=("*mybank.example*",)),
            "Mon compte - Google Chrome",
            "url_unreadable",
        ),
        (UserExclusions(urls=("*mybank.example*",)), "mybank.example - Google Chrome", "user_url"),
        (UserExclusions(), "PayPal.com - Mon compte - Google Chrome", "finance"),
        (UserExclusions(), "Bitwarden.com - Vault - Google Chrome", "password_manager"),
    ],
    ids=["user-rules-skip", "user-rule-title", "finance-title", "password-manager-title"],
)
def test_unreadable_address_still_applies_the_rules(
    privacy_rig: Callable[..., PrivacyRig], user: UserExclusions, title: str, reason: str
) -> None:
    rig = privacy_rig(user=user, focused=chrome(f"{SECRET} {title}"), texts={1: SECRET_TEXT})
    rig.uia.urls[1] = None
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.capture.stats[f"excluded:{reason}"] == 1


def test_without_user_rules_an_innocent_unreadable_page_is_stored_without_address(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=chrome("Booking.fr - Hotels - Google Chrome"), texts={1: SECRET_TEXT})
    rig.uia.urls[1] = None
    rig.tick()
    assert rig.frames() == 1
    assert rig.conn.execute("SELECT url FROM frames").fetchone() == (None,)


def test_a_title_cannot_vouch_for_a_hidden_address(privacy_rig: Callable[..., PrivacyRig]) -> None:
    """The title looks harmless, but with user address rules the page cannot be told apart."""
    rig = privacy_rig(user=UserExclusions(urls=("*mybank.example*",)), focused=chrome("Weather"))
    rig.uia.urls[1] = "typing a search into the bar"  # not an address: nothing was read
    rig.tick()
    assert rig.frames() == 0
    assert rig.capture.stats["excluded:url_unreadable"] == 1


def test_negative_control_skipping_the_rules_stores_the_page(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Exclusions.check_browser

    def only_readable(ex: Exclusions, title: str, url: str | None) -> str | None:
        return real(ex, title, url) if url is not None else None

    monkeypatch.setattr(Exclusions, "check_browser", only_readable)
    rig = privacy_rig(
        user=UserExclusions(urls=("*mybank.example*",)),
        focused=chrome(f"{SECRET} mybank.example - Google Chrome"),
        texts={1: SECRET_TEXT},
    )
    rig.uia.urls[1] = None
    rig.tick()
    assert rig.frames() == 1
    assert "sql" in rig.leaks(SECRET)


# --- Akashic's own origin is never captured -----------------------------------------------


@pytest.mark.parametrize("path", ["/", f"/open?code={SECRET}", "/api/memory/episodes/4"])
def test_frames_and_media_of_the_local_ui_are_never_written(
    privacy_rig: Callable[..., PrivacyRig], path: str
) -> None:
    rig = privacy_rig(focused=chrome("A page of some kind"), texts={1: SECRET_TEXT})
    rig.uia.urls[1] = f"127.0.0.1:5123{path}"  # Firefox/Zen style, no scheme
    rig.media.states = [playing()]
    rig.tick()
    rig.poll()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.capture.stats["excluded:own_ui"] == 1
    assert rig.tracker.stats["excluded:own_ui"] == 1


def test_negative_control_a_wrong_origin_stores_the_page_and_the_session(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(
        api_origin="http://127.0.0.1:1",  # not where the server listens
        focused=chrome(f"{SECRET} A page"),
        texts={1: SECRET_TEXT},
    )
    rig.uia.urls[1] = "http://127.0.0.1:5123/open?code=x"
    rig.media.states = [playing()]
    rig.tick()
    rig.poll()
    assert rig.frames() == 1
    assert rig.sessions() == 1
    assert "sql" in rig.leaks(SECRET)


def test_the_supervisor_gives_every_child_the_port(tmp_path: Path) -> None:
    key = DataKey.generate()
    specs: list[ChildSpec] = []

    def spawn(spec: ChildSpec, data_dir: Path):  # noqa: ANN202
        del data_dir
        specs.append(spec)
        raise RuntimeError  # stop launching: the spec is all this test needs

    supervisor = Supervisor(tmp_path, key, CHILDREN, spawn=spawn)
    supervisor.set_api_port(5123)
    with pytest.raises(RuntimeError):
        supervisor.start()
    assert [s.api_port for s in specs] == [5123]
    key.wipe()


def test_the_port_reaches_the_child_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def popen(argv: list[str], **_: object) -> None:
        seen.append(argv)

    monkeypatch.setattr("subprocess.Popen", popen)
    spawn_module(ChildSpec("capture", "akashic.capture.main", api_port=5123), tmp_path)
    assert seen[0][-2:] == ["--api-port", "5123"]
    spawn_module(ChildSpec("capture", "akashic.capture.main"), tmp_path)
    assert "--api-port" not in seen[1]


class _CountingMedia(FakeMedia):
    polls = 0

    def sessions(self) -> list[MediaState]:
        self.polls += 1
        return super().sessions()


class _CountingUia(FakeUia):
    url_reads = 0

    def read_url(self, window: WindowInfo, family: str) -> str | None:  # type: ignore[override]  # Literal family
        self.url_reads += 1
        return super().read_url(window, family)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("api_port", "url"),
    [
        (5123, "http://127.0.0.1:5123/media"),
        (None, "http://127.0.0.1:6000/media"),  # the port is unknown: all loopback is Akashic's
    ],
)
def test_the_capture_process_wiring_excludes_the_local_ui(
    paths: Paths, caplog: pytest.LogCaptureFixture, api_port: int | None, url: str
) -> None:
    """run_capture as the supervisor starts it: shared browser state, origin from the port."""
    caplog.set_level(logging.DEBUG)
    key = DataKey.generate()
    uia = _CountingUia(texts={1: SECRET_TEXT}, urls={1: url})
    media = _CountingMedia(states=[playing()], names={"Chrome": "Google Chrome"})
    platform = CapturePlatform(
        screen=FakeScreen(),
        windows=FakeWindows(focused=chrome(f"{SECRET} Akashic")),
        uia=uia,
        ocr=FakeOcr(),
        media=media,
    )
    boot = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(boot)
    ctx_conn = db.connect(paths.db_file, key.subkey("db"))
    ctx = ChildContext("capture", paths, Config(), key, ctx_conn, api_port=api_port)
    thread = threading.Thread(target=capture_main.run_capture, args=(ctx, platform), daemon=True)
    thread.start()
    try:
        seen: list[int] = []

        def polled_after_screen() -> bool:
            if uia.url_reads and not seen:
                seen.append(media.polls)
            return bool(seen) and media.polls >= seen[0] + 2

        reached = wait_for(polled_after_screen, 15)
    finally:
        ctx.stop.set()
        thread.join(timeout=30)
        ctx_conn.close()
    assert reached
    spy = SinkSpy(paths)
    assert spy.table_text(boot).casefold().count(SECRET) == 0
    assert boot.execute("SELECT count(*) FROM frames").fetchone() == (0,)
    assert boot.execute("SELECT count(*) FROM media_sessions").fetchone() == (0,)
    assert SECRET not in caplog.text.casefold()
    assert [p for p in paths.media_dir.rglob("*") if p.is_file()] == []
    boot.close()
    key.wipe()


# --- hosted password managers and finance pages are never stored ----------------------


@pytest.mark.parametrize("title", list(HOSTED_TITLES.values()))
def test_a_hosted_password_manager_is_never_stored(
    privacy_rig: Callable[..., PrivacyRig], title: str
) -> None:
    rig = privacy_rig(focused=window(process="applicationframehost.exe", title=f"{title} {SECRET}"))
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.capture.stats["excluded:password_manager"] == 1


def test_finance_pages_are_not_stored_and_innocent_lookalikes_are(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(
        focused=window(process="chrome.exe", title=f"{SECRET} Mon compte - Google Chrome")
    )
    rig.uia.urls[1] = "https://secure.paypal.com/myaccount"
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    for url in ("https://www.booking.fr/hotel", "https://www.google.com/search?q=paypal.com+frais"):
        rig.uia.urls[1] = url
        rig.windows.focused = window(
            process="chrome.exe", title=f"Resultat {url[-4:]} - Google Chrome"
        )
        rig.tick(30.0)
    assert rig.frames() == 2


def test_negative_control_substring_matching_excludes_booking_fr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import akashic.capture.exclusions as exclusions_module  # noqa: PLC0415

    def substring(host: str, entry: str) -> bool:
        return entry in host  # the old behaviour

    monkeypatch.setattr(exclusions_module, "_host_is", substring)
    assert Exclusions(UserExclusions()).check_url("https://www.booking.fr/") == "finance"


# --- a media session of an unknown app still gets the private-window check -------------


def test_a_session_with_an_unknown_name_still_gets_the_private_window_check(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(
        focused=window(
            process="firefox.exe", title=f"{SECRET} - Navigation privée - Mozilla Firefox"
        )
    )
    rig.media.states = [MediaState(AUMID, f"{SECRET} private video", f"{SECRET} chan", "playing")]
    rig.poll()
    assert rig.sessions() == 0  # the name is unknown, so it may be any browser
    assert rig.leaks(SECRET) == []
    assert rig.tracker.stats["excluded:private_window"] == 1
    rig.media.names[AUMID] = "Firefox"  # the lookup works later: same verdict, now by process
    rig.poll(advance_s=media_module.NAME_RETRY_MAX_MS / 1000 + 1)
    assert rig.sessions() == 0
    assert rig.tracker.stats["excluded:private_window"] == 2


def test_negative_control_a_cached_fallback_lets_a_private_session_through(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def old_app_name(tracker: MediaTracker, app_id: str, now: int) -> tuple[str, bool]:
        del now
        name = tracker._names.get(app_id)
        if name is None:
            name = tracker._source.app_name(app_id) or app_id
            tracker._names[app_id] = name
        return name, name != app_id

    real_reason = MediaTracker._browser_reason

    def old_browser_reason(tracker: MediaTracker, process: str | None) -> str | None:
        # The old tracker only looked for private windows when it knew the process was a browser.
        return real_reason(tracker, process) if process is not None else None

    monkeypatch.setattr(MediaTracker, "_app_name", old_app_name)
    monkeypatch.setattr(MediaTracker, "_browser_reason", old_browser_reason)
    first = window(process="firefox.exe", title="A page")
    rig = privacy_rig(focused=first)
    rig.browsers.publish(first, None)
    rig.media.states = [MediaState(AUMID, "First", "Chan", "playing")]
    rig.poll()  # the lookup missed and was cached
    rig.media.names[AUMID] = "Firefox"
    rig.windows.focused = window(
        process="firefox.exe", title=f"{SECRET} - Navigation privée - Mozilla Firefox"
    )
    rig.media.states = [MediaState(AUMID, f"{SECRET} private", f"{SECRET} chan", "playing")]
    rig.poll(advance_s=900)
    # With the cached fallback the session is not recognised as a browser's: it is logged.
    assert rig.sessions() == 2
