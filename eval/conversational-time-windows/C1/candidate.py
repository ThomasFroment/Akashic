"""C1: logical-day phrase table with past preference (rule resolver), dev prototype.

Deterministic, stdlib only. Reuses ``normalize`` and the explicit-date parser from the repo's
``akashic.search.timeparse``; everything else is a standalone port.

- Logical day L = date(now - 5 h): before 05:00 it is still the previous day.
- One main anchor per question: the first anchor in the text, fused with directly adjacent
  anchors (day + part + clock, separated only by filler words), as today's resolver takes one
  day and one part. Multi-anchor choice is a separate candidate (C3).
- Part-of-day table (hours >= 24 are on the next calendar date).
- Past preference: a "this"/implicit window that has not started steps back one day.
- In-progress windows are clipped to now. A window that still starts in the future is dropped.
- Topic titles: the repo's noun-phrase stoplist, plus capitalised mid-sentence runs that
  contain a time word ("the Night King", "Minuit a Paris", "Vendredi tout est permis").
- Model mode: the model's time_text is used only as today (planner.py:275-280): when the
  rules found a window, a resolvable time_text replaces it.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Final

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from akashic.search.timeparse import (  # noqa: E402
    _MONTHS,
    _NUMBER_WORDS,
    _Clock,
    _explicit_date,
    normalize,
)

DAY_START: Final = 5  # logical day boundary, hours
RECENT: Final = timedelta(hours=3)
CLOCK_HALF: Final = timedelta(minutes=30)
MINUTE_HALF: Final = timedelta(minutes=10)

PARTS: Final = {
    "morning": (6, 12),
    "afternoon": (12, 18),
    "noon": (12, 14),
    "evening": (18, 26),
    "night_en": (18, 30),
    "night_fr": (22, 30),
}

_WEEKDAYS: Final = {
    "monday": 0, "lundi": 0, "tuesday": 1, "mardi": 1, "wednesday": 2, "mercredi": 2,
    "thursday": 3, "jeudi": 3, "friday": 4, "vendredi": 4, "saturday": 5, "samedi": 5,
    "sunday": 6, "dimanche": 6,
}  # fmt: skip
_EN_WEEKDAYS: Final = frozenset(k for k in _WEEKDAYS if k.endswith("day"))
_UNITS: Final = {
    "minute": "m", "minutes": "m", "min": "m", "mn": "m",
    "hour": "h", "hours": "h", "heure": "h", "heures": "h",
    "day": "d", "days": "d", "jour": "d", "jours": "d",
    "week": "w", "weeks": "w", "semaine": "w", "semaines": "w",
}  # fmt: skip
_NUM = r"(\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_UNIT = r"(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")"
_WD = r"(" + "|".join(_WEEKDAYS) + r")"

# Words that make a capitalised mid-sentence run a title rather than a time.
_TITLE_TIME_WORDS: Final = frozenset(
    "night nights midnight morning mornings evening evenings afternoon noon tonight today "
    "yesterday weekend lunch minuit matin matinee soir soiree nuit nuits midi hier aujourd "
    "aprem lundi mardi mercredi jeudi vendredi samedi dimanche".split()
)
_TOPIC_STOPLIST = re.compile(
    r"\b(?:train(?:s)? de nuit|night trains?|night shifts?|morning sickness|mode nuit"
    r"|night mode|dark mode)\b"
)
_FILLER: Final = frozenset(
    "a at in the le la l les dans en de d du vers around about on pendant during au".split()
)

# Anchor lexicon, in priority order: an earlier entry wins an overlapping span.
# kind -> payload; see _resolve_group.
_LEX: Final = [
    ("after_midnight", None, r"\b(?:apres minuit|after midnight|past midnight)\b"),
    ("weekend", "last", r"\b(?:last weekend|(?:le |du )?week ?end dernier|previous weekend"
                        r"|the weekend before)\b"),
    ("weekend", "this", r"\bweek ?end\b"),
    ("ago", None, rf"\b(?:{_NUM}\s+{_UNIT}\s+ago|il\s+y\s+a\s+{_NUM}\s+{_UNIT})\b"),
    ("last_n", None, rf"\b(?:(?:in\s+)?(?:the\s+)?(?:last|past)\s+{_NUM}\s+{_UNIT}"
                     rf"|(?:ces|les)\s+{_NUM}\s+(?:derniers?|dernieres?)\s+{_UNIT}"
                     rf"|(?:depuis|dans)\s+les\s+{_NUM}\s+(?:derniers?|dernieres?)\s+{_UNIT})\b"),
    ("last_hour", None, r"\b(?:(?:in\s+)?(?:the\s+)?(?:last|past) hour|derniere heure)\b"),
    ("week", "last", r"\b(?:last week|la semaine derniere|semaine derniere|previous week)\b"),
    ("week", "this", r"\b(?:this week|cette semaine)\b"),
    ("month", "last", r"\b(?:last month|le mois dernier|mois dernier)\b"),
    ("month", "this", r"\b(?:this month|ce mois ci|ce mois)\b"),
    ("recent", None, r"\b(?:tout a l heure|a (?:little )?while ago|il y a (?:un moment|peu)"
                     r"|tantot|earlier|plus tot|not long ago)\b"),
    ("dp", (("rel", -1), "night_fr"), r"\b(?:late last night|la nuit derniere|hier (?:dans la )?nuit)\b"),
    ("dp", (("rel", -1), "night_en"), r"\b(?:last night|yesterday night)\b"),
    ("dp", (("cette_nuit", 0), "night_fr"), r"\b(?:cette nuit|this night)\b"),
    ("dp", (("this", 0), "morning"), r"\b(?:this morning|ce matin|cette matinee)\b"),
    ("dp", (("this", 0), "afternoon"), r"\b(?:this afternoon|cet apres midi|cet aprem)\b"),
    ("dp", (("this", 0), "noon"), r"\b(?:ce midi|this lunch(?:time)?)\b"),
    ("dp", (("this", 0), "evening"), r"\b(?:this evening|tonight|ce soir|cette soiree)\b"),
    ("day", ("rel", -2), r"\b(?:day before yesterday|avant hier)\b"),
    ("day", ("rel", -1), r"\b(?:yesterday|hier)\b"),
    ("day", ("rel", 0), r"\b(?:today|aujourd hui)\b"),
    ("weekday", None, rf"\b(?:last\s+)?{_WD}(?:\s+dernier)?\b"),
    ("part", "afternoon", r"\b(?:afternoon|apres midi|aprem)\b"),
    ("clock", (12, 0), r"\b(?:(?:at|around|vers|about|a)\s+)?(?:noon|midday)\b|\bvers midi\b"),
    ("part", "noon", r"\b(?:(?:at\s+)?lunch(?:time)?|dejeuner|midi)\b"),
    ("part", "morning", r"\b(?:morning|matin(?:ee)?)\b"),
    ("part", "evening", r"\b(?:evening|soir(?:ee)?)\b"),
    ("part", "night_en", r"\bnight\b"),
    ("part", "night_fr", r"\bnuit\b"),
    ("clock", (24, 0), r"\b(?:(?:at|around|vers|about|a)\s+)?(?:minuit|midnight)\b"),
    ("hm", None, r"\b(?:(?:at|a|vers|around)\s+)?(\d{1,2})(?:h|:)(\d{2})?\s*(am|pm)?\b"
                 r"|\b(?:at|a|vers|around)\s+(\d{1,2})\s*(am|pm)\b"
                 r"|\b(\d{1,2})\s*(am|pm)\b"),
]  # fmt: skip
_LEX_RE: Final = [(kind, payload, re.compile(rx)) for kind, payload, rx in _LEX]
_SLOT: Final = {"day": ("day",), "weekday": ("day",), "date": ("day",), "dp": ("day", "part"),
                "part": ("part",), "clock": ("clock",), "hm": ("clock",)}  # fmt: skip


def _strip_titles(text: str) -> str:
    """Blank capitalised mid-sentence runs that contain a time word (titles, names)."""
    tokens = list(re.finditer(r"\w+", text))
    out = list(text)
    i = 0
    while i < len(tokens):
        if not tokens[i].group()[0].isupper():
            i += 1
            continue
        j = i
        while (
            j + 1 < len(tokens)
            and tokens[j + 1].group()[0].isupper()
            and text[tokens[j].end() : tokens[j + 1].start()].isspace()
        ):
            j += 1
        title = False
        for k in range(i, j + 1):
            sep = text[tokens[k - 1].end() : tokens[k].start()] if k else ""
            sentence_start = k == 0 or bool(re.search(r"[.?!:]", sep))
            word = normalize(tokens[k].group()).strip()
            if sentence_start or word not in _TITLE_TIME_WORDS:
                continue
            if word in _EN_WEEKDAYS:
                continue
            title = True
        if title:
            for c in range(tokens[i].start(), tokens[j].end()):
                out[c] = " "
        i = j + 1
    return "".join(out)


def _number(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _lex(plain: str, clock: _Clock) -> list[tuple[int, int, str, object]]:
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, int, str, object]] = []

    def free(s: int, e: int) -> bool:
        return all(e <= a or s >= b for a, b in taken)

    for kind, payload, rx in _LEX_RE:
        for m in rx.finditer(plain):
            s, e = m.span()
            if s == e or not free(s, e):
                continue
            value: object = payload
            if kind == "weekday":
                value = _WEEKDAYS[m[1]]
            elif kind == "hm":
                value = _hm(m)
                if value is None:
                    continue
            elif kind in ("ago", "last_n"):
                g = [x for x in m.groups() if x]
                value = (_number(g[0]), _UNITS[g[1]])
            taken.append((s, e))
            found.append((s, e, kind, value))
    if not found:
        explicit = _explicit_date(plain, clock)
        if explicit is not None:
            s = plain.find(explicit[1])
            found.append((s, s + len(explicit[1]), "date", explicit[0]))
    found.sort()
    return found


def _hm(m: re.Match[str]) -> tuple[int, int] | None:
    if m[1] is not None:
        hour, minute, meridiem = int(m[1]), int(m[2] or 0), m[3]
    elif m[4] is not None:
        hour, minute, meridiem = int(m[4]), 0, m[5]
    else:
        hour, minute, meridiem = int(m[6]), 0, m[7]
    if meridiem == "pm" and hour < 12:  # noqa: PLR2004
        hour += 12
    elif meridiem == "am" and hour == 12:  # noqa: PLR2004
        hour = 0
    if hour > 23 or minute > 59:  # noqa: PLR2004
        return None
    return hour, minute


def _main_group(plain: str, anchors: list[tuple[int, int, str, object]]) -> dict | None:
    """The first anchor, fused with directly adjacent compatible ones."""
    if not anchors:
        return None
    s0, e0, kind0, val0 = anchors[0]
    rest = anchors[1:]
    if kind0 == "recent" and rest:  # "earlier today", "plus tot ce matin"
        nxt = rest[0]
        gap = re.findall(r"[a-z0-9]+", plain[e0 : nxt[0]])
        if nxt[2] in _SLOT and not gap:
            return _main_group(plain, rest)
    if kind0 not in _SLOT:
        return {"special": kind0, "value": val0}
    group: dict = {}

    def add(kind: str, val: object) -> bool:
        slots = _SLOT[kind]
        if any(s in group for s in slots):
            return False
        if kind == "dp":
            group["day"], group["part"] = val  # type: ignore[misc]
        elif kind == "weekday":
            group["day"] = ("wd", val)
        elif kind == "date":
            group["day"] = ("date", val)
        elif kind == "day":
            group["day"] = val
        elif kind == "part":
            group["part"] = val
        else:
            group["clock"] = val
        return True

    add(kind0, val0)
    end = e0
    for s, e, kind, val in rest:
        gap = re.findall(r"[a-z0-9]+", plain[end:s])
        if len(gap) > 3 or any(t not in _FILLER for t in gap) or kind not in _SLOT:  # noqa: PLR2004
            break
        if not add(kind, val):
            break
        end = e
    return group


def _at(day: date, hour: int, minute: int, tz) -> datetime:  # noqa: ANN001
    extra, hour = divmod(hour, 24)
    return datetime.combine(day + timedelta(days=extra), time(hour, minute)).replace(tzinfo=tz)


def _special(kind: str, value, now: datetime, logical: date):  # noqa: ANN001, ANN202, C901, PLR0911
    tz = now.tzinfo
    if kind == "recent":
        return now - RECENT, now
    if kind == "after_midnight":
        return _at(now.date(), 0, 0, tz), _at(now.date(), 6, 0, tz)
    if kind == "weekend":
        wd = logical.weekday()
        if wd >= 5:  # noqa: PLR2004 - inside a weekend
            sat = logical - timedelta(days=wd - 5)
            if value == "this":
                return _at(sat, DAY_START, 0, tz), now
            sat -= timedelta(days=7)
        else:
            sat = logical - timedelta(days=(wd - 5) % 7)
        return _at(sat, DAY_START, 0, tz), _at(sat + timedelta(days=2), DAY_START, 0, tz)
    if kind == "week":
        monday = logical - timedelta(days=logical.weekday())
        if value == "this":
            return _at(monday, DAY_START, 0, tz), now
        return _at(monday - timedelta(days=7), DAY_START, 0, tz), _at(monday, DAY_START, 0, tz)
    if kind == "month":
        first = now.date().replace(day=1)
        if value == "this":
            return _at(first, 0, 0, tz), now
        prev = (first - timedelta(days=1)).replace(day=1)
        return _at(prev, 0, 0, tz), _at(first, 0, 0, tz)
    if kind == "last_hour":
        return now - timedelta(hours=1), now
    n, unit = value
    if kind == "last_n":
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n),
                 "w": timedelta(weeks=n)}[unit]  # fmt: skip
        return now - delta, now
    # ago
    if unit == "m":
        centre = now - timedelta(minutes=n)
        return centre - MINUTE_HALF, centre + MINUTE_HALF
    if unit == "h":
        centre = now - timedelta(hours=n)
        return centre - CLOCK_HALF, centre + CLOCK_HALF
    day = logical - timedelta(days=n * (7 if unit == "w" else 1))
    return _at(day, DAY_START, 0, tz), _at(day + timedelta(days=1), DAY_START, 0, tz)


def _window(group: dict, now: datetime, logical: date):  # noqa: ANN202, C901
    tz = now.tzinfo
    day_spec = group.get("day")
    part = group.get("part")
    clk = group.get("clock")
    flexible = day_spec is None or day_spec[0] in ("this", "cette_nuit")
    today_only = day_spec == ("rel", 0) and part is None and clk is None
    if day_spec is None or day_spec[0] == "this":
        day = logical
    elif day_spec[0] == "rel":
        day = logical + timedelta(days=day_spec[1])
        flexible = flexible or day_spec[1] == 0
    elif day_spec[0] == "cette_nuit":
        day = logical if now.date() != logical else logical - timedelta(days=1)
    elif day_spec[0] == "wd":
        back = (now.date().weekday() - day_spec[1]) % 7 or 7
        day = now.date() - timedelta(days=back)
    else:
        day = day_spec[1]

    hour = minute = 0
    if clk is not None:
        hour, minute = clk
        if part == "evening" and 5 <= hour < 12:  # noqa: PLR2004 - "ce soir a 9h"
            hour += 12
        elif part == "afternoon" and 1 <= hour <= 6:  # noqa: PLR2004
            hour += 12
        if hour < DAY_START:
            hour += 24  # small hours belong to the logical day before

    def build(d: date) -> tuple[datetime, datetime]:
        if clk is not None:
            centre = _at(d, hour, minute, tz)
            return centre - CLOCK_HALF, centre + CLOCK_HALF
        if part is not None:
            first, last = PARTS[part]
            return _at(d, first, 0, tz), _at(d, last, 0, tz)
        return _at(d, DAY_START, 0, tz), _at(d + timedelta(days=1), DAY_START, 0, tz)

    start, end = build(day)
    if flexible and start >= now:  # past preference: the most recent such period
        start, end = build(day - timedelta(days=1))
    if today_only:
        end = now
    return start, end


def resolve_text(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    """The [start, end) local window ``text`` names, or None."""
    plain = normalize(_strip_titles(text))
    plain = _TOPIC_STOPLIST.sub(" ", plain)
    logical = (now - timedelta(hours=DAY_START)).date()
    clock = _Clock(int(now.timestamp() * 1000), now.tzinfo)
    group = _main_group(plain, _lex(plain, clock))
    if group is None:
        return None
    if "special" in group:
        start, end = _special(group["special"], group["value"], now, logical)
    else:
        start, end = _window(group, now, logical)
    end = min(end, now)
    if start >= end:
        return None  # never a future window
    return start, end


def resolve_question(question: str, now: datetime, model_time_text: str | None):  # noqa: ANN201
    rules = resolve_text(question, now)
    if rules is not None and model_time_text:
        return resolve_text(model_time_text, now) or rules
    return rules
