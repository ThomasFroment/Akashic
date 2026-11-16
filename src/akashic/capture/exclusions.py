"""What is never captured. Decisions are made in memory before anything is written.

Every check returns the *category* of the rule that matched (never the matched content), so the
caller can count and log drops without logging what was dropped.

No rule matches a window title for content: a title is free text (a video called "Bitwarden vs
1Password" is not a password manager). Windows are judged by process, by a browser's private-mode
decoration; a browser page by its address. Akashic's own address is not excluded: it halts
capture (the failsafe in ``capture/screen.py``). A window whose process
cannot be told is skipped (``window_unknown``), a browser whose title cannot be read too
(``window_unreadable``), except Chrome and Brave, whose caption decides; an unreadable address
is kept. A browser extension page (``browser_extension``) is never captured. Shell windows that
copy other windows' pixels are told by their class, which ``capture/verdicts.py`` checks.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from akashic.capture.ports import BROWSERS, FRAME_HOST
from akashic.fold import fold

if TYPE_CHECKING:
    from akashic.config import Exclusions as UserExclusions

PASSWORD_MANAGER_PROCESSES: Final = (
    "keepass.exe",
    "keepassxc.exe",
    "bitwarden.exe",
    "1password.exe",
    "lastpass.exe",
    "dashlane.exe",
    "enpass.exe",
    "nordpass.exe",
    "proton pass.exe",
    "protonpass.exe",
    "roboform.exe",
    "keeper.exe",
)
# Windows credential and sign-in surfaces.
SYSTEM_SECRET_PROCESSES: Final = ("consent.exe", "credentialuibroker.exe", "logonui.exe")
# Built-in address rules are matched against the parsed host, never as substrings of the URL, so
# "booking.fr" is not "ing.fr" and a search for "paypal.com" is not PayPal. A host matches when it
# is the entry or a subdomain of it.
PASSWORD_MANAGER_HOSTS: Final = (
    "bitwarden.com",
    "1password.com",
    "lastpass.com",
    "dashlane.com",
    "keepersecurity.com",
)
PASSWORD_MANAGER_PATHS: Final = (("proton.me", "/pass"),)  # (host, path prefix)
# Banking and money (French and international).
FINANCE_HOSTS: Final = (
    "paypal.com",
    "boursobank.com",
    "boursorama.com",
    "credit-agricole.fr",
    "labanquepostale.fr",
    "caisse-epargne.fr",
    "creditmutuel.fr",
    "cic.fr",
    "lcl.fr",
    "ing.fr",
    "hellobank.fr",
    "revolut.com",
    "n26.com",
    "wise.com",
    "binance.com",
    "coinbase.com",
    "impots.gouv.fr",
    "ameli.fr",
)
# Brands that appear as a DNS label with any ending: bnpparibas.fr, mabanque.bnpparibas (a
# top-level domain of its own), societegenerale.com.
FINANCE_BRAND_LABELS: Final = ("bnpparibas", "societegenerale")
# Chrome and Brave put their private marker only in the accessibility caption, not the title.
CAPTION_ONLY_BROWSERS: Final = frozenset({"chrome.exe", "brave.exe"})
# Shell windows that show copies of other windows (Alt-Tab, Task View, taskbar previews, the
# snipping overlay): (executable, window class).
SHELL_COPY_SURFACES: Final = frozenset(
    {
        ("explorer.exe", "XamlExplorerHostIslandWindow"),
        ("explorer.exe", "MultitaskingViewFrame"),
        ("explorer.exe", "TaskListThumbnailWnd"),
        ("snippingtool.exe", "XamlWindow"),
    }
)
# Pages of browser extensions (autofill, password popups, wallets).
EXTENSION_SCHEMES: Final = ("chrome-extension://", "moz-extension://", "extension://")
# The broad whole-URL patterns: used only when a URL does not parse, where the host cannot
# be told and the broad match is the safe one.
_UNPARSED_URL_GLOBS: Final = (
    *(f"*{h}*" for h in PASSWORD_MANAGER_HOSTS),
    "*proton.me/pass*",
    *(f"*{h}*" for h in FINANCE_HOSTS),
    *(f"*{label}*" for label in FINANCE_BRAND_LABELS),
)

# Private-browsing markers. They are the browser's own decoration at the END of a window title
# ("Page - Google Chrome (Incognito)", "Page - InPrivate - Microsoft Edge", "Page - Mozilla
# Firefox Private Browsing"), so they are only looked for there: a page or video that merely
# mentions "Incognito" is not private. Compared lower-case and without diacritics.
PRIVATE_MARKERS: Final = (
    "incognito",
    "inprivate",
    "private browsing",
    "private window",
    "private with tor",
    "navigation privee",
    "fenetre privee",
)
_PRIVATE_PAREN_ONLY: Final = ("(private)", "[private]")  # a bare "private" is an ordinary word
# Single words that a page can be titled with. Between the page title and the browser name they
# only count in brackets, the way Edge writes them ("Page - [InPrivate] - Microsoft Edge"):
# "Incognito - Google Chrome" is a page that is called Incognito.
_PRIVATE_BRACKETED_ONLY: Final = ("incognito", "inprivate")
_BROWSER_WORDS: Final = (
    "chrome",
    "chromium",
    "firefox",
    "edge",
    "brave",
    "zen",
    "opera",
    "vivaldi",
    "tor browser",
    "mozilla",
)
_TITLE_TAIL_CHARS: Final = 90
_TITLE_SEPARATOR = re.compile(r"\s[-\u2013\u2014|\u00b7]\s")  # hyphen, en/em dash, bar, dot
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
_SCHEME_GLOB = re.compile(r"^[\w+.*-]*://")  # "https://" or "*://"
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})


def _matches(value: str, patterns: tuple[str, ...]) -> bool:
    v = fold(value)
    return any(fnmatchcase(v, fold(p)) for p in patterns)


def _url_forms(url: str) -> tuple[str, str]:
    raw = url.strip()
    return raw, _SCHEME.sub("", raw.casefold())


def _address(url: str) -> tuple[str, int | None, str] | None:
    """(host, port, path) of a URL with or without a scheme; None when it does not parse."""
    raw = url.strip()
    try:
        parts = urlsplit(raw if _SCHEME.match(raw.casefold()) else "//" + raw)
        host = (parts.hostname or "").rstrip(".").casefold()
        port = parts.port
    except ValueError:
        return None
    return (host, port, parts.path) if host else None


def _path_rule_sites(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """The host part of every rule that names a path: a page can repaint before its address
    changes, so such a rule excludes its whole site."""
    sites: list[str] = []
    for pattern in patterns:
        site, slash, path = _SCHEME_GLOB.sub("", pattern.strip()).partition("/")
        if slash and path:
            sites.append(site)
    return tuple(sites)


def _host_is(host: str, entry: str) -> bool:
    return host == entry or host.endswith("." + entry)


def _private_marker_ends(segment: str) -> bool:
    if any(segment.endswith(m) for m in _PRIVATE_PAREN_ONLY):
        return True
    return any(segment.rstrip(" )]").endswith(m) for m in PRIVATE_MARKERS)


class Exclusions:
    """``api_origin`` is where Akashic's own server listens ("http://127.0.0.1:5000"). While it
    is unknown, every loopback address counts as Akashic's own (``is_own_address``)."""

    def __init__(self, user: UserExclusions, api_origin: str | None = None) -> None:
        self._user = user
        self._user_sites = _path_rule_sites(user.urls)
        self._own_port: int | None = None
        if api_origin is not None:
            address = _address(api_origin)
            self._own_port = address[1] if address is not None else None

    def check_window(self, process: str, title: str) -> str | None:
        """Reason to drop this window, or None. Cheap: no UI Automation involved.

        Judged by process and by a browser title's private-mode decoration. A window with no
        known process is ``window_unknown``; a browser's address decides the rest
        (``check_browser``).
        """
        proc = process.casefold()
        browser = proc in BROWSERS
        rules = (
            ("password_manager", proc in PASSWORD_MANAGER_PROCESSES),
            ("system_secret", proc in SYSTEM_SECRET_PROCESSES),
            ("window_unknown", not proc or proc == FRAME_HOST),
            ("private_window", browser and self.is_private_title(title)),
            (
                "window_unreadable",
                browser and proc not in CAPTION_ONLY_BROWSERS and not title.strip(),
            ),
            ("user_process", _matches(proc, self._user.processes)),
        )
        return next((reason for reason, hit in rules if hit), None)

    def check_process(self, process: str) -> str | None:
        """Reason to drop sound made by this process (audio has no window to look at)."""
        proc = process.casefold()
        rules = (
            ("password_manager", proc in PASSWORD_MANAGER_PROCESSES),
            ("system_secret", proc in SYSTEM_SECRET_PROCESSES),
            ("user_process", _matches(proc, self._user.processes)),
        )
        return next((reason for reason, hit in rules if hit), None)

    @staticmethod
    def is_private_title(title: str) -> bool:
        """True when a browser window title ends in its private-mode decoration."""
        tail = fold(title)[-_TITLE_TAIL_CHARS:].strip()
        segments = [s.strip() for s in _TITLE_SEPARATOR.split(tail)]
        if _private_marker_ends(segments[-1]):
            return True
        if len(segments) < 2 or not any(word in segments[-1] for word in _BROWSER_WORDS):  # noqa: PLR2004
            return False
        inner = segments[-2]
        core = inner.strip(" ()[]")
        return core in PRIVATE_MARKERS and (inner != core or core not in _PRIVATE_BRACKETED_ONLY)

    def check_url(self, url: str) -> str | None:
        """Reason to drop a page by its address. A user rule that names a path excludes every
        page of its host; host-only user rules and the built-in rules match as written."""
        if url.strip().casefold().startswith(EXTENSION_SCHEMES):
            return "browser_extension"
        address = _address(url)
        if address is None:  # the host cannot be told: the broad whole-URL patterns decide
            forms = _url_forms(url)
            if any(_matches(f, _UNPARSED_URL_GLOBS) for f in forms):
                return "finance"
            return "user_url" if any(_matches(f, self._user.urls) for f in forms) else None
        reason = self._check_address(address[0], address[2])
        if reason is None and (
            any(_matches(f, self._user.urls) for f in _url_forms(url))
            or self._on_user_site(address[0], address[1])
        ):
            return "user_url"
        return reason

    def _on_user_site(self, host: str, port: int | None) -> bool:
        """True when this host (or host:port) matches the host glob of a user rule with a path."""
        hosts = (host,) if port is None else (host, f"{host}:{port}")
        return any(_matches(h, self._user_sites) for h in hosts)

    def is_own_address(self, url: str) -> bool:
        """True for Akashic's own server address, with or without a scheme. This is not an
        exclusion: it triggers the own-address failsafe (``capture/screen.py``)."""
        address = _address(url)
        if address is None:
            return False
        host, port, _ = address
        return host in _LOOPBACK_HOSTS and (self._own_port is None or self._own_port == port)

    def _check_address(self, host: str, path: str) -> str | None:
        if any(_host_is(host, h) for h in PASSWORD_MANAGER_HOSTS) or any(
            _host_is(host, h) and path.casefold().startswith(p) for h, p in PASSWORD_MANAGER_PATHS
        ):
            return "password_manager"
        if any(_host_is(host, h) for h in FINANCE_HOSTS) or any(
            label in FINANCE_BRAND_LABELS for label in host.split(".")
        ):
            return "finance"
        return None

    def check_browser(self, url: str | None) -> str | None:
        """Verdict on a browser page given its address; ``url`` None means "could not be read"
        and is kept."""
        return None if url is None else self.check_url(url)


def _is_hostlike(text: str) -> bool:
    return "." in text or text.startswith("localhost")


def normalize_url(raw: str | None) -> str | None:
    """Firefox and Zen show URLs without ``https://``. Returns None for text that isn't a URL
    (a half-typed search, an empty bar, no text). Text with spaces is an address when it starts
    with a scheme or a host and a "/" (Zen shows decoded addresses); it is kept whole."""
    value = (raw or "").strip()
    if not value:
        return None
    first, spaced, _ = value.partition(" ")
    if _SCHEME.match(first.casefold()):
        return value
    if not spaced:
        return "https://" + value if _is_hostlike(value) else None
    host, slash, _ = first.partition("/")
    return "https://" + value if slash and _is_hostlike(host) else None
