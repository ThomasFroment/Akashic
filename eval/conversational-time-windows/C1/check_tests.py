"""Run the repo's time-window test expectations against C1's resolver; print every difference.

Cases are copied from tests/test_timeparse.py, tests/test_memory_search.py, tests/test_planner_model.py,
tests/test_answer.py:158 and tests/test_ask_job.py:347 (expected values as the tests state them).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from candidate import resolve_question, resolve_text  # noqa: E402

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 5, 14, 20, tzinfo=TZ)  # Thursday


def local(day: int, hour: int = 0, minute: int = 0, month: int = 11, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def ms(x: int) -> timedelta:
    return timedelta(milliseconds=x)


W = timedelta(days=7)
TIMEPARSE = [
    ("test_days_and_parts_of_day", "yesterday afternoon", (local(4, 12), local(4, 18))),
    ("test_days_and_parts_of_day", "hier après-midi", (local(4, 12), local(4, 18))),
    ("test_days_and_parts_of_day", "this morning", (local(5, 6), local(5, 12))),
    ("test_days_and_parts_of_day", "ce matin", (local(5, 6), local(5, 12))),
    ("test_days_and_parts_of_day", "hier soir", (local(4, 18), local(4, 23))),
    ("test_days_and_parts_of_day", "ce soir", (local(5, 18), local(5, 23))),
    ("test_days_and_parts_of_day", "tonight", (local(5, 18), local(5, 23))),
    ("test_days_and_parts_of_day", "cet après-midi", (local(5, 12), local(5, 18))),
    ("test_days_and_parts_of_day", "yesterday", (local(4), local(5))),
    ("test_days_and_parts_of_day", "aujourd'hui", (local(5), local(6))),
    ("test_days_and_parts_of_day", "avant-hier", (local(3), local(4))),
    ("test_days_and_parts_of_day", "the day before yesterday in the morning", (local(3, 6), local(3, 12))),
    ("test_days_and_parts_of_day", "last night", (local(4, 23), local(5, 6))),
    ("test_days_and_parts_of_day", "la nuit dernière", (local(4, 23), local(5, 6))),
    ("test_a_part_of_day_alone_means_today", "in the evening", (local(5, 18), local(5, 23))),
    ("test_a_part_of_day_alone_means_today", "le matin", (local(5, 6), local(5, 12))),
    ("test_weekdays_are_the_last_occurrence_before_today", "on Monday", (local(2), local(3))),
    ("test_weekdays_are_the_last_occurrence_before_today", "lundi matin", (local(2, 6), local(2, 12))),
    ("test_weekdays_are_the_last_occurrence_before_today", "jeudi", (local(5) - W, local(6) - W)),
    ("test_weekdays_are_the_last_occurrence_before_today", "samedi soir", (local(31, 18, month=10), local(31, 23, month=10))),
    ("test_weeks_and_months", "last week", (local(26, month=10), local(2))),
    ("test_weeks_and_months", "la semaine dernière", (local(26, month=10), local(2))),
    ("test_weeks_and_months", "this week", (local(2), local(6))),
    ("test_weeks_and_months", "cette semaine", (local(2), local(6))),
    ("test_weeks_and_months", "last month", (local(1, month=10), local(1))),
    ("test_weeks_and_months", "le mois dernier", (local(1, month=10), local(1))),
    ("test_weeks_and_months", "this month", (local(1), local(6))),
    ("test_relative_ranges", "2 days ago", (local(3), local(4))),
    ("test_relative_ranges", "il y a trois jours", (local(2), local(3))),
    ("test_relative_ranges", "a week ago", (local(29, month=10), local(30, month=10))),
    ("test_relative_ranges", "2 hours ago", (local(5, 11, 50), local(5, 12, 50))),
    ("test_relative_ranges", "il y a 2 heures", (local(5, 11, 50), local(5, 12, 50))),
    ("test_relative_ranges", "in the last 3 days", (local(2, 14, 20), local(5, 14, 20))),
    ("test_relative_ranges", "ces 2 derniers jours", (local(3, 14, 20), local(5, 14, 20))),
    ("test_relative_ranges", "the past hour", (local(5, 13, 20), local(5, 14, 20))),
    ("test_explicit_dates", "2026-11-02", (local(2), local(3))),
    ("test_explicit_dates", "on 2 November", (local(2), local(3))),
    ("test_explicit_dates", "November 2nd", (local(2), local(3))),
    ("test_explicit_dates", "le 2 novembre", (local(2), local(3))),
    ("test_explicit_dates", "le 1er novembre", (local(1), local(2))),
    ("test_explicit_dates", "2/11", (local(2), local(3))),
    ("test_explicit_dates", "02/11/2026", (local(2), local(3))),
    ("test_explicit_dates", "14 juillet", (local(14, month=7), local(15, month=7))),
    ("test_explicit_dates", "24 décembre", (local(24, month=12, year=2025), local(25, month=12, year=2025))),
    ("test_explicit_dates", "le 3 août 2025", (local(3, month=8, year=2025), local(4, month=8, year=2025))),
    ("test_impossible_dates_name_nothing", "31/02/2026", None),
    ("test_impossible_dates_name_nothing", "2026-13-40", None),
    ("test_impossible_dates_name_nothing", "30 février", None),
    ("test_clock_times", "at 3pm", (local(5, 14, 30), local(5, 15, 30))),
    ("test_clock_times", "à 15h", (local(5, 14, 30), local(5, 15, 30))),
    ("test_clock_times", "hier à 15h30", (local(4, 15), local(4, 16))),
    ("test_clock_times", "yesterday at 9am", (local(4, 8, 30), local(4, 9, 30))),
    ("test_clock_times", "vers 12am", (local(5, 0) - ms(1_800_000), local(5, 0) + ms(1_800_000))),
    ("test_no_time_in_the_text", "", None),
    ("test_no_time_in_the_text", "what video did I watch", None),
    ("test_no_time_in_the_text", "Minlo parlait d'un jeu", None),
    ("test_no_time_in_the_text", "à 25h", None),
    ("test_no_time_in_the_text", "at 14:75", None),
    ("test_topic_day_parts_do_not_restrict_capture_time", "train de nuit pour Bergen", None),
    ("test_topic_day_parts_do_not_restrict_capture_time", "night train to Bergen", None),
    ("test_topic_day_parts_do_not_restrict_capture_time", "night shift", None),
    ("test_topic_can_still_have_an_explicit_capture_time", "Minlo parlait du train de nuit hier soir", (local(4, 18), local(4, 23))),
    # tests/test_memory_search.py (same NOW and TZ, via heuristic_plan = resolve(question))
    ("test_memory_search:53", "What video did I watch yesterday afternoon?", (local(4, 12), local(4, 18))),
    ("test_memory_search:68", "What did Minlo say during our conversation this morning, something about a game?", (local(5, 6), local(5, 12))),
    ("test_memory_search:77", "De quoi parlait Zoé hier soir à propos d'un jeu ?", (local(4, 18), local(4, 23))),
    ("test_memory_search:521", "What is Minlo's latest arrangement for band rehearsal?", None),
    ("test_memory_search:521", "Quelle est la dernière proposition de Minlo pour la répétition ?", None),
    ("test_memory_search:521", "What is the most recent plan?", None),
    ("test_memory_search:532 (not None)", "Summarize what I did today.", "any"),
    ("test_memory_search:532 (not None)", "Résume ma journée d'hier.", "any"),
]


def fmt(w) -> str:  # noqa: ANN001
    if w is None or w == "any":
        return str(w)
    return " - ".join(x.strftime("%m-%d %H:%M") for x in w)


def main() -> None:
    changed = []
    for test, text, expected in TIMEPARSE:
        got = resolve_text(text, NOW)
        ok = (got is not None) if expected == "any" else got == expected
        if not ok:
            changed.append((test, text, fmt(expected), fmt(got)))
    # test_last_month_in_january_goes_back_a_year
    jan = datetime(2027, 1, 10, 9, tzinfo=TZ)
    got = resolve_text("le mois dernier", jan)
    if got != (local(1, month=12, year=2026), local(1, month=1, year=2027)):
        changed.append(("test_last_month_in_january_goes_back_a_year", "le mois dernier", "12-01 - 01-01", fmt(got)))
    # tests/test_planner_model.py: NOW = 2026-11-08 12:00 UTC in the system zone.
    pm_now = datetime.fromtimestamp(1_794_139_200, timezone.utc).astimezone()
    got = resolve_question("yesterday afternoon", pm_now, "yesterday afternoon")
    if got is None or got[1] - got[0] != timedelta(hours=6):
        changed.append(("test_planner_model:38", "yesterday afternoon", "6 h", fmt(got)))
    got = resolve_question("Que disait Minlo du train de nuit pour Bergen ?", pm_now, "nuit")
    if got is not None:
        changed.append(("test_planner_model:72", "train de nuit + time_text nuit", "None", fmt(got)))
    # tests/test_answer.py:158 only needs a resolvable "yesterday".
    if resolve_text("yesterday", pm_now) is None:
        changed.append(("test_answer:158", "yesterday", "a window", "None"))
    # tests/test_ask_job.py:347: 13 work episodes at [NOW + i min, NOW + (i+1) min), i < 13,
    # question "Summarize what I did today." with model time_text "today"; the test expects
    # all 13 to match (episode overlap filter start_ms < end AND end_ms > start).
    win = resolve_question("Summarize what I did today.", pm_now, "today")
    n = 0
    if win is not None:
        for i in range(13):
            s, e = pm_now + timedelta(minutes=i), pm_now + timedelta(minutes=i + 1)
            n += s < win[1] and e > win[0]
    if n != 13:  # noqa: PLR2004
        changed.append(("test_ask_job:347", "Summarize what I did today. (time_text today)",
                        "13 of 13 episodes in window", f"{n} of 13 (window {fmt(win)})"))
    print(f"{len(changed)} changed expectations")
    for c in changed:
        print(" | ".join(c))


if __name__ == "__main__":
    main()
