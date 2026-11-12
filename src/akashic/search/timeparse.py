"""Deterministic time-range resolution for French and English expressions.

The LLM never resolves time (AGENTS.md, "Retrieval and answers"): it only copies the words the user
used ("yesterday afternoon") and this module turns them into a range of local time.

Fixed parts of a day, local time (a range is half open: start included, end excluded):

    morning / matin           06:00-12:00
    afternoon / apres-midi    12:00-18:00
    evening / soir            18:00-23:00
    night / nuit              23:00 of that day - 06:00 of the next day

Conventions for the rest:

- A bare weekday ("Monday", "lundi") is its most recent occurrence *before* today, so on a Monday
  "lundi" means last Monday. Say "today"/"aujourd'hui" for today.
- "last night" / "cette nuit" / "la nuit derniere" is yesterday 23:00 to today 06:00.
- Weeks run Monday to Sunday. "this week" ends at the end of today, "last week" is the previous
  full week. Months are calendar months, "this month" ends at the end of today.
- "N days ago" is that whole day; "N hours ago" is a one-hour window centred on that moment;
  "the last N days/hours" runs from N periods before now up to now.
- Dates: ISO ``2026-11-05``, ``5/11`` or ``5/11/2026`` (day first, as in France), ``5 novembre``,
  ``November 5th``. A date without a year is the most recent such date not in the future.
- A clock time ("at 3pm", "a 15h", "15h30") is a one-hour window around it on the resolved day
  (today when no day is named), combined with a day word or date.

Text is compared without accents and ignoring case. ``resolve`` returns None when the text names
no time at all; the caller then searches without a time filter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from akashic.fold import fold

if TYPE_CHECKING:
    from datetime import tzinfo

MORNING: Final = (6, 12)
AFTERNOON: Final = (12, 18)
EVENING: Final = (18, 23)
NIGHT_START: Final = 23
NIGHT_END: Final = 6
CLOCK_WINDOW: Final = timedelta(minutes=30)  # either side of a stated clock time
MONTHS_PER_YEAR: Final = 12
LAST_MONTH_OF_YEAR: Final = 12

_WEEKDAYS: Final = {
    "monday": 0,
    "lundi": 0,
    "tuesday": 1,
    "mardi": 1,
    "wednesday": 2,
    "mercredi": 2,
    "thursday": 3,
    "jeudi": 3,
    "friday": 4,
    "vendredi": 4,
    "saturday": 5,
    "samedi": 5,
    "sunday": 6,
    "dimanche": 6,
}
_MONTHS: Final = {
    "january": 1,
    "janvier": 1,
    "february": 2,
    "fevrier": 2,
    "march": 3,
    "mars": 3,
    "april": 4,
    "avril": 4,
    "may": 5,
    "mai": 5,
    "june": 6,
    "juin": 6,
    "july": 7,
    "juillet": 7,
    "august": 8,
    "aout": 8,
    "september": 9,
    "septembre": 9,
    "october": 10,
    "octobre": 10,
    "november": 11,
    "novembre": 11,
    "december": 12,
    "decembre": 12,
}
_NUMBER_WORDS: Final = {
    "a": 1,
    "an": 1,
    "one": 1,
    "un": 1,
    "une": 1,
    "two": 2,
    "deux": 2,
    "three": 3,
    "trois": 3,
    "four": 4,
    "quatre": 4,
    "five": 5,
    "cinq": 5,
    "six": 6,
    "seven": 7,
    "sept": 7,
    "eight": 8,
    "huit": 8,
    "nine": 9,
    "neuf": 9,
    "ten": 10,
    "dix": 10,
}
_UNITS: Final = {
    "hour": "h",
    "hours": "h",
    "heure": "h",
    "heures": "h",
    "day": "d",
    "days": "d",
    "jour": "d",
    "jours": "d",
    "week": "w",
    "weeks": "w",
    "semaine": "w",
    "semaines": "w",
}

_NUM = r"(\d+|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_UNIT = r"(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")"
_WEEKDAY = r"(" + "|".join(_WEEKDAYS) + r")"
_MONTH = r"(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")"

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?\b")
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:er|st|nd|rd|th)?\s+(?:of\s+|de\s+)?{_MONTH}\b(?:\s+(\d{{4}}))?"
)
_MONTH_DAY = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
_AGO = re.compile(rf"\b(?:{_NUM}\s+{_UNIT}\s+ago|il\s+y\s+a\s+{_NUM}\s+{_UNIT})\b")
_LAST_N = re.compile(
    rf"\b(?:(?:in\s+)?(?:the\s+)?(?:last|past)\s+{_NUM}\s+{_UNIT}"
    rf"|(?:ces|les)\s+{_NUM}\s+(?:derniers?|dernieres?)\s+{_UNIT}"
    rf"|(?:depuis|dans)\s+les\s+{_NUM}\s+(?:derniers?|dernieres?)\s+{_UNIT})\b"
)
_CLOCK = re.compile(
    r"\b(?:(?:at|a|vers|around)\s+)?(\d{1,2})(?:h|:)(\d{2})?\s*(am|pm)?\b"
    r"|\b(?:at|a|vers|around)\s+(\d{1,2})\s*(am|pm)\b"
    r"|\b(\d{1,2})\s*(am|pm)\b"
)
_WORD_TOKEN = re.compile(r"[a-z]+")


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A half-open range of UTC milliseconds, and the words that produced it (for display)."""

    start_ms: int
    end_ms: int
    label: str


def normalize(text: str) -> str:
    """Lower case, no accents, apostrophes and dashes as spaces."""
    plain = re.sub("[\u2019'`]+", " ", fold(text))
    return re.sub(r"(?<=[a-z])-(?=[a-z])", " ", plain)  # "apres-midi"; dates keep their dashes


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class _Clock:
    """Builds local datetimes in one time zone (None: the system's, which knows about DST)."""

    def __init__(self, now_ms: int, tz: tzinfo | None) -> None:
        self.tz = tz
        self.now = datetime.fromtimestamp(now_ms / 1000, tz)
        self.today: date = self.now.date()

    def at(self, day: date, hour: int = 0, minute: int = 0) -> datetime:
        extra_days, hour = divmod(hour, 24)
        naive = datetime.combine(day + timedelta(days=extra_days), time(hour, minute))
        return naive.replace(tzinfo=self.tz) if self.tz is not None else naive.astimezone()

    def day(self, day: date) -> tuple[datetime, datetime]:
        return self.at(day), self.at(day + timedelta(days=1))


def _years_back(clock: _Clock, month: int, day: int) -> date | None:
    """The most recent month/day that is not in the future."""
    for year in (clock.today.year, clock.today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= clock.today:
            return candidate
    return None


def _number(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _explicit_date(text: str, clock: _Clock) -> tuple[date, str] | None:  # noqa: PLR0911
    if m := _ISO.search(text):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), m[0]
        except ValueError:
            return None
    for pattern, day_first in ((_DAY_MONTH, True), (_MONTH_DAY, False)):
        if m := pattern.search(text):
            day, month = (int(m[1]), _MONTHS[m[2]]) if day_first else (int(m[2]), _MONTHS[m[1]])
            if m[3]:
                try:
                    return date(int(m[3]), month, day), m[0]
                except ValueError:
                    return None
            found = _years_back(clock, month, day)
            return (found, m[0]) if found else None
    if m := _SLASH.search(text):
        day, month = int(m[1]), int(m[2])
        if m[3]:
            year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)  # noqa: PLR2004
            try:
                return date(year, month, day), m[0]
            except ValueError:
                return None
        found = _years_back(clock, month, day)
        return (found, m[0]) if found else None
    return None


_TODAY_WORDS: Final = (
    r"\b(?:today|aujourd ?hui|ce matin|cet apres midi|ce soir|this morning|this afternoon"
    r"|this evening|tonight)\b"
)


def _day_word(text: str, clock: _Clock) -> tuple[date, str] | None:
    """A single day named by a word: today, yesterday, a weekday or an explicit date."""
    for pattern, offset in (
        (r"\b(?:day before yesterday|avant hier)\b", -2),
        (r"\b(?:yesterday|hier)\b", -1),
        (_TODAY_WORDS, 0),
    ):
        if m := re.search(pattern, text):
            return clock.today + timedelta(days=offset), m[0]
    if m := re.search(rf"\b{_WEEKDAY}\b", text):
        back = (clock.today.weekday() - _WEEKDAYS[m[1]]) % 7 or 7
        return clock.today - timedelta(days=back), m[0]
    return _explicit_date(text, clock)


_PART_PATTERNS: Final = (
    ("morning", r"\b(?:morning|matin(?:ee)?)\b"),
    ("afternoon", r"\b(?:afternoon|apres midi)\b"),
    ("evening", r"\b(?:evening|soir(?:ee)?|tonight)\b"),
    ("night", r"\b(?:night|nuit)\b"),
)


def _part_of_day(text: str) -> str | None:
    for name, pattern in _PART_PATTERNS:
        if re.search(pattern, text):
            return name
    return None


def _clock_time(text: str) -> tuple[int, int] | None:
    m = _CLOCK.search(text)
    if not m:
        return None
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


def _part_range(clock: _Clock, day: date, part: str) -> tuple[datetime, datetime]:
    if part == "night":
        return clock.at(day, NIGHT_START), clock.at(day + timedelta(days=1), NIGHT_END)
    first, last = {"morning": MORNING, "afternoon": AFTERNOON, "evening": EVENING}[part]
    return clock.at(day, first), clock.at(day, last)


def _month_range(clock: _Clock, year: int, month: int) -> tuple[datetime, datetime]:
    nxt = date(year + (month == LAST_MONTH_OF_YEAR), month % MONTHS_PER_YEAR + 1, 1)
    return clock.at(date(year, month, 1)), clock.at(nxt)


def _relative(text: str, clock: _Clock) -> tuple[datetime, datetime, str] | None:  # noqa: PLR0911
    """Ranges that are not a single day: weeks, months, 'N ago', 'the last N ...'."""
    if m := _AGO.search(text):
        count, unit = (m[1], m[2]) if m[1] else (m[3], m[4])
        n, kind = _number(count), _UNITS[unit]
        if kind == "h":
            centre = clock.now - timedelta(hours=n)
            return centre - CLOCK_WINDOW, centre + CLOCK_WINDOW, m[0]
        days = n * (7 if kind == "w" else 1)
        start, end = clock.day(clock.today - timedelta(days=days))
        return start, end, m[0]
    if m := _LAST_N.search(text):
        groups = [g for g in m.groups() if g]
        n, kind = _number(groups[0]), _UNITS[groups[1]]
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[kind]
        return clock.now - delta, clock.now, m[0]
    if m := re.search(r"\b(?:(?:last|past) hour|derniere heure)\b", text):
        return clock.now - timedelta(hours=1), clock.now, m[0]
    monday = clock.today - timedelta(days=clock.today.weekday())
    if m := re.search(r"\b(?:last week|la semaine derniere|semaine derniere)\b", text):
        return clock.at(monday - timedelta(days=7)), clock.at(monday), m[0]
    if m := re.search(r"\b(?:this week|cette semaine)\b", text):
        return clock.at(monday), clock.at(clock.today + timedelta(days=1)), m[0]
    if m := re.search(r"\b(?:last month|le mois dernier|mois dernier)\b", text):
        year, month = clock.today.year, clock.today.month - 1
        if month == 0:
            year, month = year - 1, MONTHS_PER_YEAR
        start, end = _month_range(clock, year, month)
        return start, end, m[0]
    if m := re.search(r"\b(?:this month|ce mois ci|ce mois)\b", text):
        start, _ = _month_range(clock, clock.today.year, clock.today.month)
        return start, clock.at(clock.today + timedelta(days=1)), m[0]
    return None


def resolve(text: str, now_ms: int, tz: tzinfo | None = None) -> TimeRange | None:
    """The time range ``text`` names, or None when it names none.

    ``now_ms`` is the current time (UTC milliseconds); ``tz`` the time zone to reason in
    (None: the system's local zone).
    """
    plain = normalize(text)
    # These are topics, not dates of capture. Remove the complete noun phrase before
    # looking for day parts, retaining genuine qualifiers elsewhere ("hier soir").
    plain = re.sub(
        r"\b(?:train(?:s)? de nuit|night trains?|night shifts?|morning sickness)\b",
        "",
        plain,
    )
    clock = _Clock(now_ms, tz)

    if re.search(r"\b(?:last night|la nuit derniere|cette nuit)\b", plain):
        yesterday = clock.today - timedelta(days=1)
        start, end = _part_range(clock, yesterday, "night")
        return TimeRange(_ms(start), _ms(end), text.strip())

    relative = _relative(plain, clock)
    if relative is not None:
        start, end, _ = relative
        return TimeRange(_ms(start), _ms(end), text.strip())

    day = _day_word(plain, clock)
    part = _part_of_day(plain)
    clock_time = _clock_time(plain)
    if day is None and part is None and clock_time is None:
        return None
    # "this morning", "tonight" and friends already named today above; a bare part of day
    # ("in the morning", "le soir") with no day word means today.
    when = day[0] if day else clock.today
    if clock_time is not None and part is None:
        centre = clock.at(when, *clock_time)
        return TimeRange(_ms(centre - CLOCK_WINDOW), _ms(centre + CLOCK_WINDOW), text.strip())
    if part is not None:
        start, end = _part_range(clock, when, part)
    else:
        start, end = clock.day(when)
    return TimeRange(_ms(start), _ms(end), text.strip())
