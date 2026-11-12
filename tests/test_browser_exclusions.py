"""Private markers and password-manager patterns, narrowed with fail-closed edges (#148).

A private marker only counts as the browser's own title suffix and a password-manager title
pattern only applies to a non-browser process, so a video called "Bitwarden vs 1Password" or
"Incognito" is kept. Everything unknown or unreadable keeps the old, broader rules, and real
private windows and password managers stay excluded before anything is written, even when their
address or title cannot be read. Writer spy and negative controls as in test_capture_privacy.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from capture_fakes import FakeUia, window
from sink_spy import SECRET

from akashic.capture import screen as screen_module
from akashic.capture.exclusions import Exclusions
from akashic.capture.ports import MediaState, WindowInfo
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


@pytest.mark.parametrize(
    "process", ["notepad.exe", "applicationframehost.exe", "code.exe", "bitwarden.exe"]
)
def test_password_manager_titles_still_exclude_every_other_process(process: str) -> None:
    assert excl().check_window(process, "My 1Password vault") == "password_manager"


def test_a_password_manager_in_a_browser_with_a_readable_address_is_excluded_by_the_address() -> (
    None
):
    exclusions = excl()
    assert exclusions.check_browser("Bitwarden Web Vault", "https://vault.bitwarden.com/") == (
        "password_manager"
    )
    # A video that mentions the name has an ordinary address.
    assert (
        exclusions.check_browser("Bitwarden vs 1Password", "https://www.youtube.com/watch") is None
    )


# --- unknown or unreadable metadata keeps the exclusions -------------------------------------


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Bitwarden vs 1Password", "password_manager"),
        ("Page - Incognito mode explained - YouTube", "private_window"),
        ("Akashic · local memory", "own_ui"),
    ],
)
def test_a_window_of_an_unknown_process_is_judged_by_every_rule(title: str, reason: str) -> None:
    assert excl().check_window("", title) == reason
    assert excl().check_window("", title, media=True) == reason


def test_an_unknown_process_with_an_innocent_title_is_kept() -> None:
    assert excl().check_window("", "Notes") is None
    assert excl().check_window("", "") is None


def test_a_browser_window_with_no_readable_title_is_excluded() -> None:
    assert excl().check_window("chrome.exe", "") == "window_unreadable"
    assert excl().check_window("chrome.exe", "   ") == "window_unreadable"
    assert excl().check_window("notepad.exe", "") is None


@pytest.mark.parametrize(
    "title", ["Bitwarden", "Dashlane - Extension", "KeePassXC-Browser", "NordPass popup"]
)
def test_a_browser_window_without_a_readable_address_and_a_manager_title_is_excluded(
    title: str,
) -> None:
    """An extension popup has no omnibox: its title is all there is."""
    assert excl().check_browser(title, None) == "password_manager"


def test_media_titles_are_not_window_titles_for_a_known_process() -> None:
    assert excl().check_window("vlc.exe", "Bitwarden vs 1Password.mp4", media=True) is None
    assert excl().check_window("zen.exe", "Incognito", media=True) is None
    assert excl().check_window("bitwarden.exe", "anything", media=True) == "password_manager"
    assert excl().check_window("vlc.exe", "x", media=True) is None
    user = Exclusions(UserExclusions(window_titles=("*secret*",), processes=("zen.exe",)))
    assert user.check_window("vlc.exe", "My secret clip", media=True) == "user_title"
    assert user.check_window("zen.exe", "Fine", media=True) == "user_process"


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
    assert rig.capture.stats["excluded:private_window"] == 1
    assert rig.tracker.stats["excluded:private_window"] == 1


@pytest.mark.parametrize(
    ("process", "title", "url"),
    [
        ("bitwarden.exe", "Bitwarden", None),
        ("applicationframehost.exe", "Dashlane", None),
        ("keepassxc.exe", "Database", None),
        ("chrome.exe", "Bitwarden", None),  # an extension popup: no omnibox
        ("msedge.exe", "NordPass", None),
        ("chrome.exe", "Mon coffre", "https://my.1password.com/vaults"),
        ("", "Bitwarden", None),  # a process that could not be read
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
    assert rig.capture.stats["excluded:password_manager"] == 1


def test_a_media_session_of_an_app_that_could_not_be_resolved_is_judged_by_every_rule(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=window(process="notepad.exe", title="Notes"))
    rig.media.states = [
        MediaState("0123UNKNOWN", f"{SECRET} Bitwarden vs 1Password", "", "playing")
    ]
    rig.poll()
    assert rig.sessions() == 0  # fail closed: the process is unknown, so the old rule applies
    assert rig.leaks(SECRET) == []
    assert rig.tracker.stats["excluded:password_manager"] == 1


# --- negative controls: the assertions above can fail in both directions ---------------------


def _legacy_check_window(
    self: Exclusions, process: str, title: str, *, media: bool = False
) -> str | None:
    """The pre-D2 rules: password-manager titles for everyone, a private marker anywhere in the
    last 90 characters, the media title judged as a window title."""
    from akashic.capture import exclusions as module  # noqa: PLC0415

    del self, media
    if module._matches(title, module.PASSWORD_MANAGER_TITLES):
        return "password_manager"
    if process in module.BROWSERS and module._private_marker_anywhere(title):
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


def test_negative_control_without_the_unreadable_address_rule_a_manager_popup_is_stored(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Exclusions.check_browser

    def only_readable(ex: Exclusions, title: str, url: str | None) -> str | None:
        return real(ex, title, url) if url is not None else None

    monkeypatch.setattr(Exclusions, "check_browser", only_readable)
    rig = privacy_rig(focused=window(process="chrome.exe", title=f"{SECRET} Bitwarden"))
    rig.uia.urls[1] = None
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
    assert rig.capture.stats["excluded:private_window"] == 1
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
    monkeypatch.setattr(
        screen_module, "_CAPTION_ONLY", frozenset[str]()
    )  # and the unknown rule off
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
    assert rig.capture.stats["excluded:caption_unknown"] == 1
    assert rig.tracker.stats["excluded:caption_unknown"] == 1


def test_negative_control_without_the_unknown_caption_rule_the_window_is_stored(
    privacy_rig: Callable[..., PrivacyRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screen_module, "_CAPTION_ONLY", frozenset[str]())
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


# --- text typed into the address bar still names a host --------------------------------------


@pytest.mark.parametrize(
    ("typed", "reason"),
    [
        ("paypal.com login", "finance"),
        ("secure.paypal.com mon compte", "finance"),
        ("mabanque.bnpparibas virement", "finance"),
        ("my.1password.com vault", "password_manager"),
        ("127.0.0.1:5123 open", "own_ui"),
        ("MYBANK.EXAMPLE solde", "user_url"),
    ],
)
def test_typed_text_whose_first_word_is_an_excluded_host_is_excluded(
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
    assert rig.capture.stats[f"excluded:{reason}"] == 1


@pytest.mark.parametrize(
    "typed", ["booking.fr hotels paris", "notpaypal.com login", "camping.fr", "paypal frais"]
)
def test_typed_text_about_look_alike_hosts_is_not_excluded(
    privacy_rig: Callable[..., PrivacyRig], typed: str
) -> None:
    rig = privacy_rig(
        focused=window(process="firefox.exe", title="Nouvel onglet - Mozilla Firefox")
    )
    rig.uia.urls[1] = typed
    rig.tick()
    assert rig.frames() == 1
