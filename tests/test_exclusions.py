"""Exclusion rules and URL normalisation: pure decisions, no capture loop and no storage."""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from capture_fakes import HOSTED_TITLES
from sink_spy import SECRET

from akashic.capture.exclusions import (
    FINANCE_BRAND_LABELS,
    FINANCE_HOSTS,
    OWN_UI_TITLE,
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
        ("notepad.exe", "My 1Password vault", "password_manager"),
        ("consent.exe", "User Account Control", "system_secret"),
        ("chrome.exe", "Cats - Google Chrome (Incognito)", "private_window"),
        ("brave.exe", "Nouvel onglet - Brave (Navigation privée)", "private_window"),
        ("firefox.exe", "Mozilla Firefox Private Browsing", "private_window"),
        ("zen.exe", "Zen Browser — Navigation privee", "private_window"),
        ("chrome.exe", "Mon compte - Banque en ligne", "finance"),
        ("zen.exe", "Akashic · local memory — Zen Browser", "own_ui"),
        ("notepad.exe", "Akashic · local memory", None),  # not a browser
        ("chrome.exe", "Akashic records - Wikipedia", None),
        ("chrome.exe", "Cats - Google Chrome", None),
        ("notepad.exe", "Notes about incognito mode", None),  # not a browser: no private marker
        ("code.exe", "main.py", None),
    ],
)
def test_check_window(excl: Exclusions, process: str, title: str, reason: str | None) -> None:
    assert excl.check_window(process, title) == reason


def test_own_ui_title_matches_the_page() -> None:
    page = (Path(__file__).resolve().parents[1] / "src/akashic/ui/index.html").read_text("utf-8")
    title = re.search(r"<title>(.*?)</title>", page)
    assert title is not None
    assert title.group(1) == OWN_UI_TITLE


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
    excl = Exclusions(UserExclusions(processes=("Secret*.exe",), window_titles=("*Mon Journal*",)))
    assert excl.check_window("secretapp.exe", "x") == "user_process"
    assert excl.check_window("notepad.exe", "mon journal intime") == "user_title"
    assert excl.check_window("notepad.exe", "autre") is None


def test_fold_and_normalize_url() -> None:
    assert fold("Navigation Privée") == "navigation privee"
    assert normalize_url("https://a.b/c") == "https://a.b/c"
    assert normalize_url("example.com/watch?v=1") == "https://example.com/watch?v=1"
    assert normalize_url("localhost:8080") == "https://localhost:8080"
    assert normalize_url("how to cook rice") is None
    assert normalize_url("   ") is None
    assert normalize_url("justaword") is None


# --- hosted (UWP) password managers: the title is the only identity --------------------------


def test_every_listed_password_manager_has_a_title_fallback() -> None:
    assert set(HOSTED_TITLES) == set(PASSWORD_MANAGER_PROCESSES)  # a new manager needs a title
    exclusions = Exclusions(UserExclusions())
    for process, title in HOSTED_TITLES.items():
        assert exclusions.check_window("applicationframehost.exe", title) == "password_manager", (
            process
        )
        assert exclusions.check_window("", title) == "password_manager", process
        assert exclusions.check_window(process, "anything") == "password_manager", process


@pytest.mark.parametrize("title", ["Calculator", "Goalkeeper Simulator", "Keeper of Secrets"])
def test_innocent_hosted_windows_are_not_excluded(title: str) -> None:
    assert Exclusions(UserExclusions()).check_window("applicationframehost.exe", title) is None


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


def test_user_patterns_stay_globs_over_the_whole_url() -> None:
    exclusions = Exclusions(UserExclusions(urls=("*mybank.example*", "*/private/*")))
    assert exclusions.check_url("https://www.google.com/search?q=mybank.example") == "user_url"
    assert exclusions.check_url("example.org/private/notes") == "user_url"
    assert exclusions.check_url("https://example.org/public") is None


def test_an_address_that_does_not_parse_keeps_the_broad_patterns() -> None:
    """When the host cannot be told, the old whole-URL patterns decide: fail closed."""
    assert Exclusions(UserExclusions()).check_url("http://[broken/paypal.com") == "finance"
    assert Exclusions(UserExclusions()).check_url("http://[broken/innocent") is None


# --- Akashic's own origin is never captured ----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"{ORIGIN}/",
        f"{ORIGIN}/open?code={SECRET}",
        f"{ORIGIN}/static/app.js",
        f"{ORIGIN}/api/segments/3/audio",
        "http://localhost:5123/#/memory",
        "127.0.0.1:5123/media",
        "http://[::1]:5123/",
        "http://127.0.0.1.:5123/",  # fully qualified host name
    ],
)
def test_every_page_of_the_api_origin_is_excluded(url: str) -> None:
    assert Exclusions(UserExclusions(), ORIGIN).check_url(url) == "own_ui"


def test_other_loopback_ports_are_the_users_own_servers() -> None:
    exclusions = Exclusions(UserExclusions(), ORIGIN)
    assert exclusions.check_url("http://127.0.0.1:8000/") is None
    assert exclusions.check_url("http://localhost:3000/app") is None


def test_an_unknown_origin_excludes_every_loopback_address() -> None:
    exclusions = Exclusions(UserExclusions(), None)
    assert exclusions.check_url("http://127.0.0.1:8000/") == "own_ui"
    assert exclusions.check_url("http://localhost/") == "own_ui"


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
    [("", ""), ("notepad.exe", ""), ("", "Notes"), ("café.exe", "Réunion 😀 meeting")],
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
    ],
)
def test_normalize_url_edge_cases(raw: str, expected: str | None) -> None:
    assert normalize_url(raw) == expected
