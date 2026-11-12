"""Time resolution: fixed ranges, French and English, in a fixed time zone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from akashic.search.timeparse import normalize, resolve

TZ = timezone(timedelta(hours=1))
# Thursday 2026-11-05, 14:20 local.
NOW = datetime(2026, 11, 5, 14, 20, tzinfo=TZ)
NOW_MS = int(NOW.timestamp() * 1000)


def local(day: int, hour: int = 0, minute: int = 0, month: int = 11, year: int = 2026) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp() * 1000)


def span(text: str) -> tuple[int, int] | None:
    found = resolve(text, NOW_MS, TZ)
    return None if found is None else (found.start_ms, found.end_ms)


def test_normalize_drops_accents_and_punctuation() -> None:
    assert normalize("Après-midi d\u2019Hier") == "apres midi d hier"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yesterday afternoon", (local(4, 12), local(4, 18))),
        ("hier après-midi", (local(4, 12), local(4, 18))),
        ("this morning", (local(5, 6), local(5, 12))),
        ("ce matin", (local(5, 6), local(5, 12))),
        ("hier soir", (local(4, 18), local(4, 23))),
        ("ce soir", (local(5, 18), local(5, 23))),
        ("tonight", (local(5, 18), local(5, 23))),
        ("cet après-midi", (local(5, 12), local(5, 18))),
        ("yesterday", (local(4), local(5))),
        ("aujourd'hui", (local(5), local(6))),
        ("avant-hier", (local(3), local(4))),
        ("the day before yesterday in the morning", (local(3, 6), local(3, 12))),
        ("last night", (local(4, 23), local(5, 6))),
        ("la nuit dernière", (local(4, 23), local(5, 6))),
    ],
)
def test_days_and_parts_of_day(text: str, expected: tuple[int, int]) -> None:
    assert span(text) == expected


def test_a_part_of_day_alone_means_today() -> None:
    assert span("in the evening") == (local(5, 18), local(5, 23))
    assert span("le matin") == (local(5, 6), local(5, 12))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on Monday", (local(2), local(3))),  # the most recent one before today
        ("lundi matin", (local(2, 6), local(2, 12))),
        ("jeudi", (local(5) - 7 * 86_400_000, local(6) - 7 * 86_400_000)),  # today is Thursday
        ("samedi soir", (local(31, 18, month=10), local(31, 23, month=10) + 0)),
    ],
)
def test_weekdays_are_the_last_occurrence_before_today(
    text: str, expected: tuple[int, int]
) -> None:
    got = span(text)
    assert got is not None
    if text.startswith("samedi"):  # Saturday 2026-10-31
        assert got == (local(31, 18, month=10), local(31, 23, month=10))
    else:
        assert got == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("last week", (local(26, month=10), local(2))),
        ("la semaine dernière", (local(26, month=10), local(2))),
        ("this week", (local(2), local(6))),
        ("cette semaine", (local(2), local(6))),
        ("last month", (local(1, month=10), local(1))),
        ("le mois dernier", (local(1, month=10), local(1))),
        ("this month", (local(1), local(6))),
    ],
)
def test_weeks_and_months(text: str, expected: tuple[int, int]) -> None:
    assert span(text) == expected


def test_last_month_in_january_goes_back_a_year() -> None:
    now = datetime(2027, 1, 10, 9, tzinfo=TZ)
    found = resolve("le mois dernier", int(now.timestamp() * 1000), TZ)
    assert found is not None
    assert found.start_ms == local(1, month=12, year=2026)
    assert found.end_ms == local(1, month=1, year=2027)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2 days ago", (local(3), local(4))),
        ("il y a trois jours", (local(2), local(3))),
        ("a week ago", (local(29, month=10), local(30, month=10))),
        ("2 hours ago", (local(5, 11, 50), local(5, 12, 50))),
        ("il y a 2 heures", (local(5, 11, 50), local(5, 12, 50))),
        ("in the last 3 days", (local(2, 14, 20), local(5, 14, 20))),
        ("ces 2 derniers jours", (local(3, 14, 20), local(5, 14, 20))),
        ("the past hour", (local(5, 13, 20), local(5, 14, 20))),
    ],
)
def test_relative_ranges(text: str, expected: tuple[int, int]) -> None:
    assert span(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-02", (local(2), local(3))),
        ("on 2 November", (local(2), local(3))),
        ("November 2nd", (local(2), local(3))),
        ("le 2 novembre", (local(2), local(3))),
        ("le 1er novembre", (local(1), local(2))),
        ("2/11", (local(2), local(3))),
        ("02/11/2026", (local(2), local(3))),
        ("14 juillet", (local(14, month=7), local(15, month=7))),
        ("24 décembre", (local(24, month=12, year=2025), local(25, month=12, year=2025))),
        ("le 3 août 2025", (local(3, month=8, year=2025), local(4, month=8, year=2025))),
    ],
)
def test_explicit_dates(text: str, expected: tuple[int, int]) -> None:
    assert span(text) == expected


@pytest.mark.parametrize("text", ["31/02/2026", "2026-13-40", "30 février"])
def test_impossible_dates_name_nothing(text: str) -> None:
    assert span(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("at 3pm", (local(5, 14, 30), local(5, 15, 30))),
        ("à 15h", (local(5, 14, 30), local(5, 15, 30))),
        ("hier à 15h30", (local(4, 15), local(4, 16))),
        ("yesterday at 9am", (local(4, 8, 30), local(4, 9, 30))),
        ("vers 12am", (local(5, 0) - 30 * 60_000, local(5, 0) + 30 * 60_000)),
    ],
)
def test_clock_times(text: str, expected: tuple[int, int]) -> None:
    assert span(text) == expected


@pytest.mark.parametrize(
    "text", ["", "what video did I watch", "Minlo parlait d'un jeu", "à 25h", "at 14:75"]
)
def test_no_time_in_the_text(text: str) -> None:
    assert span(text) is None


def test_label_keeps_the_original_words() -> None:
    found = resolve("  Hier après-midi ", NOW_MS, TZ)
    assert found is not None
    assert found.label == "Hier après-midi"


def test_system_time_zone_is_the_default() -> None:
    found = resolve("yesterday", NOW_MS)
    assert found is not None
    assert found.end_ms - found.start_ms in {23 * 3_600_000, 24 * 3_600_000, 25 * 3_600_000}


@pytest.mark.parametrize(
    "text", ["train de nuit pour Bergen", "night train to Bergen", "night shift"]
)
def test_topic_day_parts_do_not_restrict_capture_time(text: str) -> None:
    assert span(text) is None


def test_topic_can_still_have_an_explicit_capture_time() -> None:
    assert span("Minlo parlait du train de nuit hier soir") == (local(4, 18), local(4, 23))
