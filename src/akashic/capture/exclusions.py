"""What is never captured. Decisions are made in memory before anything is written.

Every check returns the *category* of the rule that matched (never the matched content), so the
caller can count and log drops without logging what was dropped.

Unknown or unreadable metadata keeps exclusions in force (fail closed): a window of an unknown
process is judged by every title rule, a browser whose address cannot be read is judged by its
title and, when the user has address rules, skipped. Narrower rules (D2) apply only where the
process is known.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from akashic.capture.ports import BROWSERS
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
# Window titles of password managers, for windows whose process cannot be told (a UWP app behind
# applicationframehost.exe, an unreadable process) and for non-browser apps. Never applied to a
# browser window's title: a video called "Bitwarden vs 1Password" is not a password manager (D2).
PASSWORD_MANAGER_TITLES: Final = (
    "*keepass*",  # also KeePassXC
    "*bitwarden*",
    "*1password*",
    "*lastpass*",
    "*dashlane*",
    "*enpass*",
    "*nordpass*",
    "*proton pass*",
    "*protonpass*",
    "*roboform*",
    "keeper",  # a bare "*keeper*" would match games, so only the exact title and the product names
    "*keeper password*",
    "*keeper security*",
    "*keeper vault*",
)
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
FINANCE_TITLES: Final = ("*banque en ligne*", "*online banking*", "*internet banking*")
# The pre-D2/#26 whole-URL patterns: used only when a URL does not parse, where the host cannot
# be told and the broad match is the safe one.
_UNPARSED_URL_GLOBS: Final = (
    *(f"*{h}*" for h in PASSWORD_MANAGER_HOSTS),
    "*proton.me/pass*",
    *(f"*{h}*" for h in FINANCE_HOSTS),
    *(f"*{label}*" for label in FINANCE_BRAND_LABELS),
)

# Private-browsing markers. They are the browser's own decoration at the END of a window title
# ("Page - Google Chrome (Incognito)", "Page - InPrivate - Microsoft Edge", "Page - Mozilla
# Firefox Private Browsing"), so they are only looked for there (D2): a page or video that merely
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
# The <title> of Akashic's own web UI (ui/index.html). Its pages show transcripts and
# screenshots, so capturing them would only record the memory inside itself.
OWN_UI_TITLE: Final = "Akashic · local memory"
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
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


def _host_is(host: str, entry: str) -> bool:
    return host == entry or host.endswith("." + entry)


def _title_mentions(title: str, token: str) -> bool:
    """The token as a whole host-like word in the title ("booking.fr" does not contain "ing.fr")."""
    return re.search(rf"(?<![a-z0-9.-]){re.escape(token)}(?![a-z0-9-])", title) is not None


def _private_marker_ends(segment: str) -> bool:
    if any(segment.endswith(m) for m in _PRIVATE_PAREN_ONLY):
        return True
    return any(segment.rstrip(" )]").endswith(m) for m in PRIVATE_MARKERS)


def _private_marker_anywhere(title: str) -> bool:
    """The pre-D2 rule (marker anywhere in the last 90 characters), kept for a window whose
    process is unknown, where the browser-suffix rule cannot be trusted."""
    tail = fold(title)[-_TITLE_TAIL_CHARS:]
    return any(m in tail for m in (*PRIVATE_MARKERS, *_PRIVATE_PAREN_ONLY))


_OWN_UI_FOLDED: Final = fold(OWN_UI_TITLE)


class Exclusions:
    """``api_origin`` is where Akashic's own server listens ("http://127.0.0.1:5000"). Its
    pages and media players are never captured. While it is unknown, every loopback address is
    treated as Akashic's own."""

    def __init__(self, user: UserExclusions, api_origin: str | None = None) -> None:
        self._user = user
        self._own_port: int | None = None
        if api_origin is not None:
            address = _address(api_origin)
            self._own_port = address[1] if address is not None else None

    def check_window(self, process: str, title: str, *, media: bool = False) -> str | None:
        """Reason to drop this window, or None. Cheap: no UI Automation involved.

        A browser's title is checked for private-mode decoration only; its address decides the
        rest (``check_browser``). Password-manager title patterns apply to every other process.
        When the process is unknown (empty) the title cannot be trusted either way, so every
        title rule applies. ``media`` marks a media session's title, which is not a window
        title: for a known process only the process rules and the user's own rules apply to it.
        """
        proc = process.casefold()
        browser = proc in BROWSERS
        unknown = not proc
        titled = not media or unknown  # the window-title rules apply
        rules = (
            ("password_manager", proc in PASSWORD_MANAGER_PROCESSES),
            (
                "password_manager",
                titled and not browser and _matches(title, PASSWORD_MANAGER_TITLES),
            ),
            ("system_secret", proc in SYSTEM_SECRET_PROCESSES),
            ("private_window", titled and browser and self.is_private_title(title)),
            ("private_window", titled and unknown and _private_marker_anywhere(title)),
            ("window_unreadable", titled and browser and not title.strip()),
            (
                "own_ui",
                titled and (browser or unknown) and fold(title).startswith(_OWN_UI_FOLDED),
            ),
            ("finance", _matches(title, FINANCE_TITLES)),
            ("user_process", _matches(proc, self._user.processes)),
            ("user_title", _matches(title, self._user.window_titles)),
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
        address = _address(url)
        if address is None:  # the host cannot be told: the broad whole-URL patterns decide
            forms = _url_forms(url)
            if any(_matches(f, _UNPARSED_URL_GLOBS) for f in forms):
                return "finance"
            return "user_url" if any(_matches(f, self._user.urls) for f in forms) else None
        reason = self._check_address(*address)
        if reason is None and any(_matches(f, self._user.urls) for f in _url_forms(url)):
            return "user_url"
        return reason

    def _check_address(self, host: str, port: int | None, path: str) -> str | None:
        if host in _LOOPBACK_HOSTS and (self._own_port is None or self._own_port == port):
            return "own_ui"
        if any(_host_is(host, h) for h in PASSWORD_MANAGER_HOSTS) or any(
            _host_is(host, h) and path.casefold().startswith(p) for h, p in PASSWORD_MANAGER_PATHS
        ):
            return "password_manager"
        if any(_host_is(host, h) for h in FINANCE_HOSTS) or any(
            label in FINANCE_BRAND_LABELS for label in host.split(".")
        ):
            return "finance"
        return None

    def check_typed(self, text: str) -> str | None:
        """Verdict on text typed into the address bar (not an address yet, e.g. "paypal.com
        login"): its first word is checked as an address and the user's rules as globs."""
        words = text.split()
        candidate = normalize_url(words[0]) if words else None
        reason = self.check_url(candidate) if candidate else None
        if reason is None and any(_matches(f, self._user.urls) for f in _url_forms(text)):
            return "user_url"
        return reason

    def check_browser(self, title: str, url: str | None) -> str | None:
        """Verdict on a browser window given its address; ``url`` None means "could not be read".

        With no address the title cannot prove the page is harmless: it is matched against the
        built-in and the user's address rules, and when the user has address rules at all the
        window is skipped (``url_unreadable``). A readable address is checked as such.
        """
        if url is not None:
            return self.check_url(url)
        folded = fold(title)
        if _matches(title, PASSWORD_MANAGER_TITLES) or any(
            _title_mentions(folded, h) for h in PASSWORD_MANAGER_HOSTS
        ):
            return "password_manager"
        if any(_title_mentions(folded, h) for h in FINANCE_HOSTS) or any(
            _title_mentions(folded, label) for label in FINANCE_BRAND_LABELS
        ):
            return "finance"
        if _matches(title, self._user.urls):
            return "user_url"
        return "url_unreadable" if self._user.urls else None


def normalize_url(raw: str) -> str | None:
    """Firefox and Zen show URLs without ``https://``. Returns None for text that isn't a URL
    (a half-typed search, an empty bar)."""
    value = raw.strip()
    if not value or " " in value:
        return None
    if _SCHEME.match(value.casefold()):
        return value
    if "." in value or value.startswith("localhost"):
        return "https://" + value
    return None
