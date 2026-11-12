# C1: repo test expectations that change

Produced by `C1/check_tests.py` (C1's `resolve_text` / `resolve_question` against the cases and
expected values copied from the tests). Test NOW: Thursday 2026-11-05 14:20, UTC+1 (timeparse,
memory_search); 2026-11-08 12:00 UTC in the system zone (planner_model, answer, ask_job).

**36 expectations change**, all for one of six reasons. Everything else in scope passes unchanged.

## 1. Logical day: whole days run 05:00 to 05:00 (19)

The user's day ends at 05:00, not midnight, so a whole-day window starts and ends at 05:00.

| Test | Text | Today | C1 |
|---|---|---|---|
| test_days_and_parts_of_day | yesterday | 11-04 00:00 - 11-05 00:00 | 11-04 05:00 - 11-05 05:00 |
| test_days_and_parts_of_day | avant-hier | 11-03 00:00 - 11-04 00:00 | 11-03 05:00 - 11-04 05:00 |
| test_weekdays_are_the_last_occurrence_before_today | on Monday | 11-02 00:00 - 11-03 00:00 | 11-02 05:00 - 11-03 05:00 |
| test_weekdays_are_the_last_occurrence_before_today | jeudi | 10-29 00:00 - 10-30 00:00 | 10-29 05:00 - 10-30 05:00 |
| test_weeks_and_months | last week | 10-26 00:00 - 11-02 00:00 | 10-26 05:00 - 11-02 05:00 |
| test_weeks_and_months | la semaine dernière | 10-26 00:00 - 11-02 00:00 | 10-26 05:00 - 11-02 05:00 |
| test_relative_ranges | 2 days ago | 11-03 00:00 - 11-04 00:00 | 11-03 05:00 - 11-04 05:00 |
| test_relative_ranges | il y a trois jours | 11-02 00:00 - 11-03 00:00 | 11-02 05:00 - 11-03 05:00 |
| test_relative_ranges | a week ago | 10-29 00:00 - 10-30 00:00 | 10-29 05:00 - 10-30 05:00 |
| test_explicit_dates | 2026-11-02, on 2 November, November 2nd, le 2 novembre, 2/11, 02/11/2026 (6) | 11-02 00:00 - 11-03 00:00 | 11-02 05:00 - 11-03 05:00 |
| test_explicit_dates | le 1er novembre | 11-01 00:00 - 11-02 00:00 | 11-01 05:00 - 11-02 05:00 |
| test_explicit_dates | 14 juillet | 07-14 00:00 - 07-15 00:00 | 07-14 05:00 - 07-15 05:00 |
| test_explicit_dates | 24 décembre | 12-24 00:00 - 12-25 00:00 | 12-24 05:00 - 12-25 05:00 |
| test_explicit_dates | le 3 août 2025 | 08-03 00:00 - 08-04 00:00 | 08-03 05:00 - 08-04 05:00 |

Explicit dates are outside the scope's goals; the prototype applies the 05:00 day to them for
consistency. Keeping calendar midnight for explicit dates only would leave those 10 unchanged.

## 2. The evening runs past midnight: soir/evening ends at 02:00 the next day (4)

| Test | Text | Today | C1 |
|---|---|---|---|
| test_days_and_parts_of_day | hier soir | 11-04 18:00 - 11-04 23:00 | 11-04 18:00 - 11-05 02:00 |
| test_weekdays_are_the_last_occurrence_before_today | samedi soir | 10-31 18:00 - 10-31 23:00 | 10-31 18:00 - 11-01 02:00 |
| test_topic_can_still_have_an_explicit_capture_time | Minlo parlait du train de nuit hier soir | 11-04 18:00 - 11-04 23:00 | 11-04 18:00 - 11-05 02:00 |
| test_memory_search.py:77 | De quoi parlait Zoé hier soir à propos d'un jeu ? | 11-04 18:00 - 11-04 23:00 | 11-04 18:00 - 11-05 02:00 |

The topic stoplist still removes "train de nuit" (the window comes from "hier soir").

## 3. Past preference: a "this"/implicit period that has not started is the previous one (5)

At 14:20 the evening and 15:00 have not happened yet, so a past-tense question means the
most recent ones. This is also the 0-future-window guarantee.

| Test | Text | Today | C1 |
|---|---|---|---|
| test_days_and_parts_of_day | ce soir | 11-05 18:00 - 11-05 23:00 | 11-04 18:00 - 11-05 02:00 |
| test_days_and_parts_of_day | tonight | 11-05 18:00 - 11-05 23:00 | 11-04 18:00 - 11-05 02:00 |
| test_a_part_of_day_alone_means_today | in the evening | 11-05 18:00 - 11-05 23:00 | 11-04 18:00 - 11-05 02:00 |
| test_clock_times | at 3pm | 11-05 14:30 - 11-05 15:30 | 11-04 14:30 - 11-04 15:30 |
| test_clock_times | à 15h | 11-05 14:30 - 11-05 15:30 | 11-04 14:30 - 11-04 15:30 |

Known cost: future intent ("what's on tonight?") gets yesterday evening instead of no window.

## 4. In-progress windows are clipped to now; "today" starts at 05:00 (5)

| Test | Text | Today | C1 |
|---|---|---|---|
| test_days_and_parts_of_day | cet après-midi | 11-05 12:00 - 11-05 18:00 | 11-05 12:00 - 11-05 14:20 |
| test_days_and_parts_of_day | aujourd'hui | 11-05 00:00 - 11-06 00:00 | 11-05 05:00 - 11-05 14:20 |
| test_weeks_and_months | this week | 11-02 00:00 - 11-06 00:00 | 11-02 05:00 - 11-05 14:20 |
| test_weeks_and_months | cette semaine | 11-02 00:00 - 11-06 00:00 | 11-02 05:00 - 11-05 14:20 |
| test_weeks_and_months | this month | 11-01 00:00 - 11-06 00:00 | 11-01 00:00 - 11-05 14:20 |

(Months keep calendar midnight as their start; only the end is clipped.)

## 5. Night spans: EN "last night" = evening + night, FR "la nuit dernière" = 22:00-06:00 (2)

| Test | Text | Today | C1 |
|---|---|---|---|
| test_days_and_parts_of_day | last night | 11-04 23:00 - 11-05 06:00 | 11-04 18:00 - 11-05 06:00 |
| test_days_and_parts_of_day | la nuit dernière | 11-04 23:00 - 11-05 06:00 | 11-04 22:00 - 11-05 06:00 |

Reason: a 20:30-22:45 call asked about as "last night" was missed (scope finding).

## 6. Fixture with data after "now" (1)

| Test | Case | Today | C1 |
|---|---|---|---|
| test_ask_job.py:347 | "Summarize what I did today." with time_text "today"; 13 work episodes at [NOW + i min, NOW + (i+1) min) | all 13 inside 00:00-24:00 | 0 of 13 inside 05:00-13:00 (now) |

The episodes are in the future relative to the handler's clock (`lambda: NOW`). Under clipping
to now they fall outside "today". The assertion ("10 of 13 matching episodes") is still the
intended behaviour; the fixture would move them before NOW (`NOW - (i + 1) * MINUTE`).

## Unchanged (checked)

- test_timeparse: "this morning", "ce matin", "yesterday afternoon", "hier après-midi",
  "the day before yesterday in the morning", "le matin", "lundi matin", "last month",
  "le mois dernier", "2 hours ago", "il y a 2 heures", "in the last 3 days",
  "ces 2 derniers jours", "the past hour", "hier à 15h30", "yesterday at 9am", "vers 12am",
  the impossible dates, the no-time texts, the topic texts (train de nuit / night train /
  night shift), and test_last_month_in_january_goes_back_a_year.
- test_memory_search.py:53 (yesterday afternoon), :68 (this morning), :521 (latest questions:
  no time), :532 (day summaries still have a window).
- test_planner_model.py:38 (yesterday afternoon is 6 h), :72 ("nuit" time_text on a
  night-train question: the rules find nothing, so the model text is not used).
- test_answer.py:158 ("yesterday" still resolves; the test does not check the bounds).

## Not applicable to the prototype API

`test_normalize_drops_accents_and_punctuation` (normalize is reused unchanged),
`test_label_keeps_the_original_words` and `test_system_time_zone_is_the_default`: the
prototype returns `(start, end)` datetimes for an aware `now`. An integration would keep
`TimeRange`, its label, and the `tz=None` system-zone path.
