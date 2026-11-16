"""Private markers and process rules, with no title list (#148).

A private marker only counts as the browser's own title suffix and no title names a password
manager, so a video called "Bitwarden vs 1Password" or "Incognito" is kept. Real private windows
and password-manager processes stay excluded before anything is written; a window with no known
process is skipped. Writer spy and negative controls as in test_capture_privacy.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from capture_fakes import FakeUia, window
from sink_spy import SECRET

from akashic.capture import exclusions as exclusions_module
from akashic.capture import verdicts as verdicts_module
from akashic.capture.exclusions import Exclusions
from akashic.capture.ports import BROWSERS, MediaState, Rect, WindowInfo
from akashic.config import Exclusions as UserExclusions

if TYPE_CHECKING:
    from collections.abc import Callable

    from sink_spy import PrivacyRig


def excl() -> Exclusions:
    return Exclusions(UserExclusions())


# --- the private marker is the browser's own suffix ------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Page - Google Chrome (Incognito)",
        "Nouvel onglet - Google Chrome (navigation privée)",
        "Page — Mozilla Firefox Private Browsing",
        "Page — Mozilla Firefox (Private Browsing)",
        "Nouvel onglet — Navigation privée — Mozilla Firefox",
        "Accueil - [InPrivate] - Microsoft Edge",
        "Page - [InPrivate] - Microsoft\u200b Edge",  # as Edge 1xx writes it
        "Page - (Incognito) - Chromium",
        "Nouvel onglet - Brave (Private)",
        "Nouvel onglet - Brave (Private with Tor)",
        "NOUVEL ONGLET - FENÊTRE PRIVÉE",
        "Zen Browser — Navigation privee",
        "x" * 20_000 + " - Google Chrome (Incognito)",
        "Incognito",  # nothing but the marker: cannot be a page
    ],
)
def test_real_private_windows_are_excluded(title: str) -> None:
    assert excl().check_window("chrome.exe", title) == "private_window"
    assert excl().check_window("firefox.exe", title) == "private_window"


@pytest.mark.parametrize(
    "title",
    [
        "Incognito - Official Trailer - YouTube - Google Chrome",
        "Incognito - Google Chrome",  # a page called Incognito; Chrome adds no marker of its own
        "InPrivate - Microsoft Edge",
        "InPrivate - Profile 1 - Microsoft Edge",
        "Les dangers de la navigation privée - YouTube - Mozilla Firefox",
        "InPrivate (2024) - Film - Microsoft Edge",
        "Private - Netflix - Brave",
        "Private Browsing explained - YouTube - Google Chrome",
        "How to use Incognito mode - Google Chrome",
        "Movie (Incognito) - YouTube - Google Chrome",  # a title that ends in the marker
        "Mode InPrivate : tout savoir - Microsoft Edge",
        "Cats - Google Chrome",
    ],
)
def test_pages_that_only_mention_private_modes_are_kept(title: str) -> None:
    assert excl().check_window("chrome.exe", title) is None
    assert excl().check_window("msedge.exe", title) is None


# --- password-manager title patterns are for non-browser processes ---------------------------


@pytest.mark.parametrize(
    "process", ["chrome.exe", "msedge.exe", "firefox.exe", "zen.exe", "brave.exe"]
)
@pytest.mark.parametrize(
    "title",
    [
        "Bitwarden vs 1Password : lequel choisir ? - YouTube - Google Chrome",
        "1Password review - YouTube",
        "KeePass tutorial - YouTube",
    ],
)
def test_a_browser_window_about_a_password_manager_is_not_one(process: str, title: str) -> None:
    assert excl().check_window(process, title) is None


def test_a_password_manager_is_judged_by_its_process_not_its_title() -> None:
    assert excl().check_window("bitwarden.exe", "anything") == "password_manager"
    for process in ("notepad.exe", "code.exe", "vlc.exe"):
        assert excl().check_window(process, "My 1Password vault") is None


def test_a_password_manager_in_a_browser_is_excluded_by_the_address_only() -> None:
    exclusions = excl()
    assert exclusions.check_browser("https://vault.bitwarden.com/") == "password_manager"
    # A video that mentions the name has an ordinary address.
    assert exclusions.check_browser("https://www.youtube.com/watch") is None


# --- unknown or unreadable metadata keeps the exclusions -------------------------------------


def test_a_window_of_an_unknown_process_is_skipped_whatever_the_title() -> None:
    for title in ("Notes", "", "Bitwarden vs 1Password", "Page - Incognito mode explained"):
        assert excl().check_window("", title) == "window_unknown"
        assert excl().check_window("applicationframehost.exe", title) == "window_unknown"


def test_a_media_title_of_an_unknown_process_meets_only_the_process_rules() -> None:
    for title in ("Akashic · local memory", "Bitwarden vs 1Password", "Page - Incognito"):
        assert excl().check_window("", title) == "window_unknown"
    assert excl().check_process("") is None


def test_a_blank_title_is_unreadable_for_most_browsers_not_for_chrome_or_brave() -> None:
    for process in ("msedge.exe", "firefox.exe", "zen.exe"):
        assert excl().check_window(process, "") == "window_unreadable"
        assert excl().check_window(process, "   ") == "window_unreadable"
    for process in ("chrome.exe", "brave.exe"):
        assert excl().check_window(process, "") is None
        assert excl().check_window(process, "   ") is None
    assert excl().check_window("notepad.exe", "") is None


def test_media_is_judged_by_process_rules_only() -> None:
    assert excl().check_process("vlc.exe") is None
    assert excl().check_process("bitwarden.exe") == "password_manager"
    user = Exclusions(UserExclusions(processes=("zen.exe",)))
    assert user.check_process("vlc.exe") is None
    assert user.check_process("zen.exe") == "user_process"


# --- through the capture path, with the writer spy -------------------------------------------

VIDEO_URL = "https://www.youtube.com/watch?v=abc"


@pytest.mark.parametrize("name", ["Bitwarden vs 1Password : lequel choisir ?", "Incognito"])
def test_a_video_named_like_a_password_manager_or_private_mode_is_logged_with_its_frames(
    privacy_rig: Callable[..., PrivacyRig], name: str
) -> None:
    rig = privacy_rig(
        focused=window(process="chrome.exe", title=f"{name} - YouTube - Google Chrome")
    )
    rig.uia.urls[1] = VIDEO_URL
    rig.media.states = [MediaState("Chrome", name, "A channel", "playing")]
    rig.tick()
    rig.poll()
    assert rig.frames() == 1
    assert rig.sessions() == 1
    assert rig.tracker.stats == {"opened": 1}


def test_a_video_in_a_media_app_with_such_a_name_is_logged(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=window(process="notepad.exe", title="Notes"))
    rig.media.names["VLC"] = "VLC media player"
    rig.media.states = [MediaState("VLC", "Bitwarden vs 1Password.mp4", "", "playing")]
    rig.poll()
    assert rig.sessions() == 1


@pytest.mark.parametrize("url", [VIDEO_URL, None], ids=["readable-address", "unreadable-address"])
def test_a_real_private_window_stays_excluded_with_or_without_an_address(
    privacy_rig: Callable[..., PrivacyRig], url: str | None
) -> None:
    rig = privacy_rig(
        focused=window(
            process="chrome.exe", title=f"{SECRET} A video - YouTube - Google Chrome (Incognito)"
        )
    )
    rig.uia.urls[1] = url
    rig.media.states = [MediaState("Chrome", f"{SECRET} A video", f"{SECRET} chan", "playing")]
    rig.tick()
    rig.poll()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.rig.last_note() == "hidden"
    assert rig.tracker.stats["excluded:private_window"] == 1


@pytest.mark.parametrize(
    ("process", "title", "url"),
    [
        ("bitwarden.exe", "Bitwarden", None),
        ("keepassxc.exe", "Database", None),
        ("chrome.exe", "Mon coffre", "https://my.1password.com/vaults"),
    ],
)
def test_real_password_manager_windows_stay_excluded_before_anything_is_written(
    privacy_rig: Callable[..., PrivacyRig], process: str, title: str, url: str | None
) -> None:
    rig = privacy_rig(focused=window(process=process, title=f"{SECRET} {title}"))
    rig.uia.urls[1] = url
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.rig.last_note() == "hidden"


def test_a_browser_window_whose_address_was_never_readable_is_not_stored(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    """Without a readable address the window is not known to be a main window: it is excluded
    as unknown, whatever its title (a titled extension popup is such a window)."""
    rig = privacy_rig(focused=window(process="chrome.exe", title=f"{SECRET} Bitwarden"))
    rig.uia.urls[1] = None
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []


@pytest.mark.parametrize("process", ["applicationframehost.exe", ""])
def test_a_password_manager_title_in_an_unidentified_window_is_skipped_as_unknown(
    privacy_rig: Callable[..., PrivacyRig], process: str
) -> None:
    rig = privacy_rig(focused=window(process=process, title=f"{SECRET} Bitwarden"))
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.rig.last_note() == "hidden"


def test_a_media_session_of_an_unresolved_app_is_judged_by_its_process_only(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=window(process="notepad.exe", title="Notes"))
    rig.media.states = [MediaState("0123UNKNOWN", "Bitwarden vs 1Password", "", "playing")]
    rig.poll()
    assert rig.sessions() == 1


def test_a_media_session_titled_like_the_own_ui_is_not_excluded_by_its_title(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    own = privacy_rig(focused=window(process="notepad.exe", title="Notes"))
    own.media.states = [MediaState("0123UNKNOWN", "Akashic · local memory", "", "playing")]
    own.poll()
    assert own.sessions() == 1  # recording is halted while the UI is open, not by a title rule


# --- negative controls: the assertions above can fail in both directions ---------------------


def _legacy_check_window(
    self: Exclusions, process: str, title: str, *, media: bool = False
) -> str | None:
    """The pre-D2 rules: a private marker anywhere in the title, media titles included."""
    del self, media
    if process in BROWSERS and "incognito" in title.casefold():
        return "private_window"
    return None


def test_negative_control_the_old_rules_drop_the_video(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Exclusions, "check_window", _legacy_check_window)
    rig = privacy_rig(
        focused=window(
            process="chrome.exe", title="Incognito - Official Trailer - YouTube - Google Chrome"
        )
    )
    rig.uia.urls[1] = VIDEO_URL
    rig.media.states = [
        MediaState("Chrome", "Incognito - Official Trailer", "A channel", "playing")
    ]
    rig.tick()
    rig.poll()
    assert rig.frames() == 0  # the video's frames were dropped: what D2 repairs
    assert rig.sessions() == 0


def test_negative_control_without_the_suffix_rule_a_private_window_is_stored(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def never_private(title: str) -> bool:
        del title
        return False

    monkeypatch.setattr(Exclusions, "is_private_title", staticmethod(never_private))
    rig = privacy_rig(
        focused=window(
            process="chrome.exe", title=f"{SECRET} A video - YouTube - Google Chrome (Incognito)"
        )
    )
    rig.uia.urls[1] = VIDEO_URL
    rig.tick()
    assert rig.frames() == 1
    assert "sql" in rig.leaks(SECRET)


# --- Chromium writes its private-mode marker into its accessible name, not the Win32 title ----
# Found natively (Chrome 157: title "Page - Google Chrome", UI Automation frame name "Page -
# Google Chrome (Incognito)"), so the screen loop also reads the frame's accessible name.


def test_a_chromium_private_window_without_a_title_marker_is_excluded_and_not_logged(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(
        focused=window(process="chrome.exe", title=f"{SECRET} A video - Google Chrome")
    )
    rig.uia.urls[1] = VIDEO_URL
    rig.uia.captions[1] = f"{SECRET} A video - Google Chrome (Incognito)"
    rig.media.states = [MediaState("Chrome", f"{SECRET} A video", f"{SECRET} chan", "playing")]
    rig.tick()
    rig.poll()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.rig.last_note() == "hidden"
    assert rig.tracker.stats["excluded:private_window"] == 1


@pytest.mark.parametrize(
    "caption", ["Page - Google Chrome", "Incognito - Google Chrome", "Incognito mode - Brave"]
)
def test_an_ordinary_chromium_caption_does_not_exclude(
    privacy_rig: Callable[..., PrivacyRig], caption: str | None
) -> None:
    rig = privacy_rig(focused=window(process="chrome.exe", title="Page - Google Chrome"))
    rig.uia.urls[1] = VIDEO_URL
    rig.uia.captions[1] = caption
    rig.tick()
    assert rig.frames() == 1


def test_negative_control_without_the_caption_probe_a_chromium_private_window_is_stored(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def blind(uia: FakeUia, window: WindowInfo, family: str) -> str | None:
        del uia, window, family
        return None

    monkeypatch.setattr(FakeUia, "browser_caption", blind)
    monkeypatch.setattr(verdicts_module, "CAPTION_ONLY_BROWSERS", frozenset[str]())
    monkeypatch.setattr(exclusions_module, "CAPTION_ONLY_BROWSERS", frozenset[str]())
    rig = privacy_rig(
        focused=window(process="chrome.exe", title=f"{SECRET} A video - Google Chrome")
    )
    rig.uia.urls[1] = VIDEO_URL
    rig.uia.captions[1] = f"{SECRET} A video - Google Chrome (Incognito)"
    rig.tick()
    assert rig.frames() == 1
    assert "sql" in rig.leaks(SECRET)


@pytest.mark.parametrize("process", ["chrome.exe", "brave.exe"])
def test_an_unreadable_caption_on_chrome_or_brave_is_unknown_so_nothing_is_stored(
    privacy_rig: Callable[..., PrivacyRig], process: str
) -> None:
    rig = privacy_rig(focused=window(process=process, title=f"{SECRET} A video - Brave"))
    rig.uia.urls[1] = VIDEO_URL  # readable, harmless address
    rig.uia.captions[1] = None  # the frame caption could not be read
    rig.media.states = [MediaState("Chrome", f"{SECRET} A video", f"{SECRET} chan", "playing")]
    rig.media.names["Chrome"] = "Brave" if process == "brave.exe" else "Google Chrome"
    rig.tick()
    rig.poll()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.rig.last_note() == "hidden"
    assert rig.tracker.stats["excluded:caption_unknown"] == 1


def test_negative_control_without_the_unknown_caption_rule_the_window_is_stored(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verdicts_module, "CAPTION_ONLY_BROWSERS", frozenset[str]())
    monkeypatch.setattr(exclusions_module, "CAPTION_ONLY_BROWSERS", frozenset[str]())
    rig = privacy_rig(
        focused=window(process="chrome.exe", title=f"{SECRET} A video - Google Chrome")
    )
    rig.uia.urls[1] = VIDEO_URL
    rig.uia.captions[1] = None
    rig.tick()
    assert rig.frames() == 1
    assert "sql" in rig.leaks(SECRET)


def test_an_unreadable_caption_on_edge_keeps_the_title_rule(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    """Edge writes its marker into the Win32 title too, so no caption is needed."""
    rig = privacy_rig(focused=window(process="msedge.exe", title="Page - Microsoft Edge"))
    rig.uia.urls[1] = VIDEO_URL
    rig.uia.captions[1] = None
    rig.tick()
    assert rig.frames() == 1


# --- text in the address bar: an address only with a scheme or a host and a slash --------------


@pytest.mark.parametrize(
    ("typed", "reason"),
    [
        ("paypal.com/signin x", "finance"),
        ("secure.paypal.com/mon compte", "finance"),
        ("my.1password.com/vault x", "password_manager"),
        ("MYBANK.EXAMPLE/solde x", "user_url"),
    ],
)
def test_a_spaced_address_is_checked_whole(
    privacy_rig: Callable[..., PrivacyRig], typed: str, reason: str
) -> None:
    rig = privacy_rig(
        user=UserExclusions(urls=("*mybank.example*",)),
        focused=window(process="firefox.exe", title=f"{SECRET} Nouvel onglet - Mozilla Firefox"),
        texts={1: [SECRET]},
    )
    rig.uia.urls[1] = typed
    rig.tick()
    assert rig.spy.nothing_stored(rig.conn)
    assert rig.leaks(SECRET) == []
    assert rig.rig.last_note() == "hidden"
    assert rig.browsers.media_reason("firefox.exe", None) == reason


@pytest.mark.parametrize(
    "typed",
    ["booking.fr hotels paris", "notpaypal.com login", "paypal.com login", "paypal frais"],
)
def test_typed_text_that_is_not_an_address_is_kept_without_address(
    privacy_rig: Callable[..., PrivacyRig], typed: str
) -> None:
    rig = privacy_rig(
        focused=window(process="firefox.exe", title="Nouvel onglet - Mozilla Firefox")
    )
    rig.uia.urls[1] = typed
    rig.tick()
    assert rig.frames() == 1
    assert rig.conn.execute("SELECT url FROM frame_windows WHERE focused").fetchone() == (None,)


# --- a private window on a side monitor is checked at the side rate, and keeps rejecting -------


@pytest.mark.parametrize(
    ("process", "title", "caption", "app_id"),
    [
        ("zen.exe", f"{SECRET} Page — Mozilla Firefox Private Browsing", None, "Z1"),
        (
            "chrome.exe",
            f"{SECRET} Page - Google Chrome",
            f"{SECRET} Page - Google Chrome (Incognito)",
            "Chrome",
        ),
    ],
    ids=["zen-private", "chrome-incognito"],
)
def test_a_private_window_on_a_side_monitor_rejects_the_media_at_every_side_check(
    privacy_rig: Callable[..., PrivacyRig],
    process: str,
    title: str,
    caption: str | None,
    app_id: str,
) -> None:
    left, right = Rect(0, 0, 800, 600), Rect(800, 0, 1600, 600)
    rig = privacy_rig(monitors=[left, right], focused=window(title="Notes"))
    rig.rig.show(5, process, title, right, focus=False, url=VIDEO_URL)
    if caption is not None:
        rig.uia.captions[5] = caption
    rig.media.states = [MediaState(app_id, f"{SECRET} video", f"{SECRET} chan", "playing")]
    rig.tick()  # the first pass checks every monitor
    for _ in range(5):  # 5 x 15 s is longer than a verdict is held without a new check
        rig.poll()
        assert rig.sessions() == 0
        rig.tick(15.0)
    assert rig.leaks(SECRET) == []
    assert rig.tracker.stats["excluded:private_window"] >= 5
