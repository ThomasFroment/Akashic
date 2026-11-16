"""Exclusion rules and URL normalisation: pure decisions, no capture loop and no storage."""

from __future__ import annotations

import time

import pytest
from sink_spy import SECRET

from akashic.capture.exclusions import (
    FINANCE_BRAND_LABELS,
    FINANCE_HOSTS,
    PASSWORD_MANAGER_PROCESSES,
    Exclusions,
    fold,
    normalize_url,
)
from akashic.config import Exclusions as UserExclusions

ORIGIN = "http://127.0.0.1:5123"

# --- windows and addresses ---------------------------------------------------------------------


@pytest.fixture
def excl() -> Exclusions:
    return Exclusions(UserExclusions())


@pytest.mark.parametrize(
    ("process", "title", "reason"),
    [
        ("KeePassXC.exe", "Database", "password_manager"),
        ("notepad.exe", "My 1Password vault", None),  # no title rule
        ("", "Notes", "window_unknown"),
        ("applicationframehost.exe", "Notes", "window_unknown"),
        ("consent.exe", "User Account Control", "system_secret"),
        ("chrome.exe", "Cats - Google Chrome (Incognito)", "private_window"),
        ("brave.exe", "Nouvel onglet - Brave (Navigation privée)", "private_window"),
        ("firefox.exe", "Mozilla Firefox Private Browsing", "private_window"),
        ("zen.exe", "Zen Browser — Navigation privee", "private_window"),
        ("chrome.exe", "Mon compte - Banque en ligne", None),
        ("msedge.exe", "", "window_unreadable"),
        ("firefox.exe", "   ", "window_unreadable"),
        ("zen.exe", "", "window_unreadable"),
        ("chrome.exe", "", None),
        ("brave.exe", "  ", None),
        ("zen.exe", "Akashic · local memory — Zen Browser", None),  # the failsafe decides
        ("notepad.exe", "Akashic · local memory", None),  # not a browser
        ("chrome.exe", "Akashic records - Wikipedia", None),
        ("chrome.exe", "Cats - Google Chrome", None),
        ("notepad.exe", "Notes about incognito mode", None),  # not a browser: no private marker
        ("code.exe", "main.py", None),
    ],
)
def test_check_window(excl: Exclusions, process: str, title: str, reason: str | None) -> None:
    assert excl.check_window(process, title) == reason


def test_private_marker_only_looked_for_at_the_end_of_a_title(excl: Exclusions) -> None:
    title = "Incognito mode explained " + "x" * 200 + " - Google Chrome"
    assert excl.check_window("chrome.exe", title) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.paypal.com/myaccount", "finance"),
        ("https://mabanque.bnpparibas.net/login", "finance"),
        ("bitwarden.com/vault", "password_manager"),
        ("https://youtube.com/watch?v=abc", None),
        ("https://secret.example.org/page", "user_url"),
    ],
)
def test_check_url(url: str, reason: str | None) -> None:
    excl = Exclusions(UserExclusions(urls=("*secret.example.org*",)))
    assert excl.check_url(url) == reason


def test_user_exclusions() -> None:
    excl = Exclusions(UserExclusions(processes=("Secret*.exe",)))
    assert excl.check_window("secretapp.exe", "x") == "user_process"
    assert excl.check_window("notepad.exe", "mon journal intime") is None
    assert excl.check_window("notepad.exe", "autre") is None


@pytest.mark.parametrize("process", ["notepad.exe", "code.exe", "vlc.exe", "chrome.exe"])
@pytest.mark.parametrize(
    "title", ["Bitwarden", "KeePass", "banque en ligne", "online banking", "Bitwarden vs 1Password"]
)
def test_no_title_list_matches_a_known_process(excl: Exclusions, process: str, title: str) -> None:
    assert excl.check_window(process, title) is None


@pytest.mark.parametrize("process", ["", "applicationframehost.exe"])
def test_an_unidentified_window_is_unknown_whatever_the_title(
    excl: Exclusions, process: str
) -> None:
    for title in ("", "Notes", "Bitwarden", "Page - Incognito mode explained"):
        assert excl.check_window(process, title) == "window_unknown"
        assert excl.check_process(process) is None  # media of an unresolved app: process rules only


@pytest.mark.parametrize("process", ["msedge.exe", "firefox.exe", "zen.exe"])
def test_a_blank_browser_title_is_unreadable(excl: Exclusions, process: str) -> None:
    assert excl.check_window(process, " 	 ") == "window_unreadable"


def test_an_unknown_process_has_no_private_marker_rule(excl: Exclusions) -> None:
    assert excl.check_window("notepad.exe", "Page - Incognito mode - YouTube") is None
    assert excl.check_process("vlc.exe") is None


def test_the_own_ui_title_is_no_exclusion(excl: Exclusions) -> None:
    for process in ("", "zen.exe", "vlc.exe", "chrome.exe"):
        reason = excl.check_window(process, "Akashic · local memory")
        assert reason in (None, "window_unknown"), process
        assert reason != "own_ui"
    assert excl.check_window("zen.exe", "Akashic · local memory — Zen Browser") is None
    assert excl.check_process("") is None


def test_check_browser_takes_only_the_address() -> None:
    plain = Exclusions(UserExclusions())
    assert plain.check_browser(None) is None
    assert plain.check_browser("https://www.paypal.com/x") == "finance"
    assert plain.check_browser("https://youtube.com/watch") is None
    assert not hasattr(plain, "check_typed")
    ruled = Exclusions(UserExclusions(urls=("*mybank*",), processes=("x.exe",)))
    assert ruled.check_browser(None) is None
    assert ruled.check_browser("https://mybank.example/a") == "user_url"


def test_fold_and_normalize_url() -> None:
    assert fold("Navigation Privée") == "navigation privee"
    assert normalize_url("https://a.b/c") == "https://a.b/c"
    assert normalize_url("example.com/watch?v=1") == "https://example.com/watch?v=1"
    assert normalize_url("localhost:8080") == "https://localhost:8080"
    assert normalize_url("how to cook rice") is None
    assert normalize_url("   ") is None
    assert normalize_url("justaword") is None


def test_every_listed_password_manager_process_is_excluded(excl: Exclusions) -> None:
    for process in PASSWORD_MANAGER_PROCESSES:
        assert excl.check_window(process, "anything") == "password_manager", process
        assert excl.check_process(process) == "password_manager", process


# --- built-in address rules are host rules ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.booking.fr/hotel/fr/paris.html",
        "https://www.camping.fr/",
        "https://www.gaming.fr/",
        "https://otherwise.com/blog",
        "https://www.google.com/search?q=paypal.com+frais",
        "https://fr.wikipedia.org/wiki/Paypal.com",
        "https://paypal.com.evil.example/login",
        "https://notpaypal.com/",
        "https://bnpparibas-fake.example/",
        "https://www.bing.com/search?q=mabanque.bnpparibas",
        "https://proton.me/mail",
        "https://example.org/page#societegenerale",
    ],
)
def test_unrelated_hosts_are_not_finance(url: str) -> None:
    assert Exclusions(UserExclusions()).check_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.paypal.com/myaccount",
        "https://paypal.com:443/",
        "https://user:pw@www.paypal.com/",
        "https://WWW.PAYPAL.COM./x",
        "HTTPS://WWW.PAYPAL.COM/",
        "paypal.com/signin",  # Firefox/Zen: no scheme
        "https://espace-client.ing.fr/login",
        "https://www.bnpparibas.fr/",
        "https://mabanque.bnpparibas/",  # the .bnpparibas top-level domain
        "https://mabanque.bnpparibas",
        "https://particuliers.societegenerale.fr/",
        "https://www.societegenerale.com/en",
        "https://www.impots.gouv.fr/accueil",
        "https://secure.credit-agricole.fr/",
        "https://app.n26.com/",
    ],
)
def test_real_finance_hosts_and_subdomains_are_excluded(url: str) -> None:
    assert Exclusions(UserExclusions()).check_url(url) == "finance"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://my.1password.com/vaults", "password_manager"),
        ("https://vault.bitwarden.com/#/vault", "password_manager"),
        ("https://proton.me/pass/vault", "password_manager"),
        ("https://www.lastpass.com/", "password_manager"),
    ],
)
def test_password_manager_hosts_use_the_same_host_rules(url: str, reason: str) -> None:
    assert Exclusions(UserExclusions()).check_url(url) == reason


def test_every_built_in_host_is_excluded_with_a_subdomain() -> None:
    exclusions = Exclusions(UserExclusions())
    for host in FINANCE_HOSTS:
        assert exclusions.check_url(f"https://www.{host}/x") == "finance", host
        assert exclusions.check_url(f"https://x{host}/") is None, host  # not a subdomain
    for label in FINANCE_BRAND_LABELS:
        assert exclusions.check_url(f"https://shop.{label}.example/") == "finance", label


def test_spaced_addresses_are_checked_whole() -> None:
    plain = Exclusions(UserExclusions())
    assert plain.check_url(normalize_url("paypal.com/signin x") or "") == "finance"
    assert normalize_url("paypal.com login") is None
    user = Exclusions(UserExclusions(urls=("*mybank*",)))
    assert user.check_url(normalize_url("mybank.example/a b") or "") == "user_url"


def test_user_patterns_stay_globs_over_the_whole_url() -> None:
    exclusions = Exclusions(UserExclusions(urls=("*mybank.example*", "example.org/private/*")))
    assert exclusions.check_url("https://www.google.com/search?q=mybank.example") == "user_url"
    assert exclusions.check_url("example.org/private/notes") == "user_url"
    assert (
        exclusions.check_url("https://example.org/public") == "user_url"
    )  # the path rule: its site
    assert exclusions.check_url("https://example.net/public") is None


def test_an_address_that_does_not_parse_keeps_the_broad_patterns() -> None:
    """When the host cannot be told, the old whole-URL patterns decide: fail closed."""
    assert Exclusions(UserExclusions()).check_url("http://[broken/paypal.com") == "finance"
    assert Exclusions(UserExclusions()).check_url("http://[broken/innocent") is None


# --- Akashic's own address is not an exclusion: it is the failsafe's trigger -------------------

OWN_URLS = [
    f"{ORIGIN}/",
    f"{ORIGIN}/open?code={SECRET}",
    f"{ORIGIN}/static/app.js",
    "http://localhost:5123/#/memory",
    "127.0.0.1:5123/media",
    "http://[::1]:5123/",
    "http://127.0.0.1.:5123/",  # fully qualified host name
]


@pytest.mark.parametrize("url", OWN_URLS)
def test_every_page_of_the_api_origin_is_the_own_address_and_not_excluded(url: str) -> None:
    exclusions = Exclusions(UserExclusions(), ORIGIN)
    assert exclusions.is_own_address(url)
    assert exclusions.check_url(url) is None
    assert exclusions.check_browser(url) is None


def test_other_loopback_ports_are_the_users_own_servers() -> None:
    exclusions = Exclusions(UserExclusions(), ORIGIN)
    assert not exclusions.is_own_address("http://127.0.0.1:8000/")
    assert not exclusions.is_own_address("http://localhost:3000/app")


def test_an_unknown_port_makes_every_loopback_address_the_own_address() -> None:
    exclusions = Exclusions(UserExclusions(), None)
    assert exclusions.is_own_address("http://127.0.0.1:8000/")
    assert exclusions.is_own_address("localhost")
    assert exclusions.is_own_address("[::1]:9/x")
    assert not exclusions.is_own_address("http://example.org/")


# --- a user rule that names a path excludes its whole site -------------------------------------


@pytest.mark.parametrize(
    ("rule", "url", "reason"),
    [
        ("example.com/private/*", "https://example.com/anything", "user_url"),
        ("example.com/private/*", "https://example.com/", "user_url"),
        ("example.com/private/*", "example.com/other", "user_url"),
        ("https://example.com/private/*", "https://example.com/home", "user_url"),
        ("*.example.com/*", "https://a.example.com/anything", "user_url"),
        ("*.example.com/*", "https://example.com/anything", None),
        ("example.com/private/*", "https://other.org/private/x", None),
        ("example.com/private/*", "https://notexample.com/home", None),
        ("example.com", "https://example.com/anything", None),  # host-only rules: as before
        ("*mybank.example*", "https://mybank.example/x", "user_url"),
        ("*/private/*", "https://example.org/private/x", "user_url"),
    ],
)
def test_a_user_path_rule_excludes_every_page_of_its_host(
    rule: str, url: str, reason: str | None
) -> None:
    exclusions = Exclusions(UserExclusions(urls=(rule,)))
    assert exclusions.check_url(url) == reason
    assert exclusions.check_browser(url) == reason


def test_a_path_rule_does_not_change_the_built_in_rules() -> None:
    exclusions = Exclusions(UserExclusions(urls=("example.com/private/*",)))
    assert exclusions.check_url("https://www.paypal.com/x") == "finance"
    assert exclusions.check_url("https://youtube.com/watch") is None


# --- edge cases: private titles, unicode, empty input, very long addresses --------------------


@pytest.mark.parametrize(
    "title",
    ["Nouvel onglet - Fenetre privee", "Accueil — [InPrivate] — Microsoft Edge"],
    ids=["fr-ascii", "edge"],
)
def test_private_browser_windows_without_diacritics_or_with_edge_marker_are_excluded(
    title: str,
) -> None:
    assert Exclusions(UserExclusions()).check_window("firefox.exe", title) == "private_window"


@pytest.mark.parametrize(
    ("process", "title"),
    [("notepad.exe", ""), ("café.exe", "Réunion 😀 meeting")],
)
def test_empty_or_unicode_window_identity_is_not_excluded(process: str, title: str) -> None:
    assert Exclusions(UserExclusions()).check_window(process, title) is None


def test_very_long_urls_are_checked_quickly() -> None:
    exclusions = Exclusions(UserExclusions(urls=("*mybank.example*",)))
    url = "https://example.org/" + "a/" * 50_000
    started = time.perf_counter()
    assert exclusions.check_url(url) is None
    assert exclusions.check_url(url + "?next=mybank.example") == "user_url"
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("   ", None),
        ("rechercher quelque chose", None),
        ("example.org/page", "https://example.org/page"),
        ("localhost:8080", "https://localhost:8080"),
        ("HTTP://Example.org", "HTTP://Example.org"),
        ("café.fr/menu", "https://café.fr/menu"),
        ("nas/index.html", "https://nas/index.html"),
        ("https://a.com", "https://a.com"),
        ("justaword", None),
        ("paypal.com login", None),
        ("how to cook rice", None),
        ("localhost login", None),
        (
            "youtube.com/results?search_query=hello world",
            "https://youtube.com/results?search_query=hello world",
        ),
        ("https://fr.wikipedia.org/wiki/Café crème", "https://fr.wikipedia.org/wiki/Café crème"),
        ("localhost/a b", "https://localhost/a b"),
    ],
)
def test_normalize_url_edge_cases(raw: str, expected: str | None) -> None:
    assert normalize_url(raw) == expected
