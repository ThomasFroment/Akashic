# Conversational time windows: evaluation reference

Dev-only harness for the invent run "QD FR/EN time windows". Synthetic data only; nothing here
opens the user's Akashic data directory or database, and no repo source is modified.

## Files

| File | What |
|---|---|
| `make_phrases.py` | 234 hand-annotated questions (meaning per question) -> `phrases.jsonl` |
| `phrases.jsonl` | one question per line: `question, lang, category, split, meaning, main_time_text, connector, labels[{ref_date, now, now_label, gold}]` |
| `baseline_today.py` | baseline candidate: the repo's real `planner.heuristic_plan` / `plan_from_model` |
| `harness.py` | scores a candidate (resolver + retrieval), writes `out/<name>/{metrics.txt,details.json,side.png}` |
| `retrieval.py` | synthetic SQLite fixture (repo migrations + real episode builder) and line/episode window evaluation |

Commands (PowerShell or bash, from this folder; `..\.venv\Scripts\python.exe`):

    python make_phrases.py                                  # regenerate phrases.jsonl
    python harness.py                                       # baseline -> out/baseline/
    python harness.py --candidate C1/cand.py --name C1      # candidate vs baseline -> out/C1/
    python retrieval.py                                     # retrieval table, baseline only

## Candidate interface

    resolve_question(question: str, now: datetime, model_time_text: str | None)
        -> tuple[datetime, datetime] | None

`now` is aware (Europe/Paris). Return [start, end) aware or naive-local. Two modes are scored:
**rules** (`model_time_text=None`, the deterministic path, the one that must reach the budget) and
**model** (`model_time_text` = the main anchor's time words copied exactly from the question, as
the current planner prompt instructs; `""` when the question names no time). Two-anchor
questions get the main-anchor phrase only (a best-case model copy).

Optional, for the retrieval part: `select_lines(conn, start_ms, end_ms, plan_kinds, people=())`
-> segment ids, and `select_episodes(...)` (same signature) -> episode ids. `people` are speaker
ids; `plan_kinds` is the tuple from `heuristic_plan`. Missing functions fall back to today's.

## Metrics

Per (question, now) case, rules and model mode separately:

- **covers**: `win.start <= gold.start + 30 min` and `win.end >= gold.end - 30 min`.
- **tight**: `win.end - win.start <= 2 x (gold.end - gold.start)`.
- **pass** = covers and tight; for a null gold, pass = no window.
- **future**: `win.start >= now`. **false filter**: any window when gold is null.
- Timing: `timeit` of `resolve_question(q, now, None)` (number=5, best of 3) per case at the
  Tuesday reference, p50 / p99 in microseconds. Budget: <= 1 ms.

Reported: overall pass, per category, per now, per language, per split, per connector
(two-anchor only), covers/tight rates, future and false-filter counts. `metrics.txt` is one line;
`details.json` holds every case with its window; `side.png` (same layout for every candidate)
shows pass by category against the baseline with the 95 % line, and future/false-filter counts.

Budget (scope): pass >= 95 % with 0 future windows, <= 1 ms per question on the rule path.

## The set

234 questions: 118 FR, 95 EN, 21 mixed. Categories: today 10, morning 10, afternoon 10, noon 10,
evening 12, night 12, yesterday 16, daybefore 6, midnight 8, earlier 9, weekend 10, weekday 12,
clock 13, relative 12, **two_anchor 54** (32 main anchor first, 22 main anchor last; connector
rel 25 / when 6 / none 23), **time_free 30** (topic words such as "the Night King", "Weekend
Update", "Midnight Club", "Le Matin", "Hier encore", "Les 12 coups de midi", "Last Week Tonight").

Every question is labelled at now = 09:00, 14:00, 23:30, 00:40, 03:00 on Tuesday 2026-11-10
(calendar date of "now"; 00:40 and 03:00 are Tuesday morning, i.e. still Monday night for the
user). Questions whose meaning is a weekend are also labelled on Monday 2026-11-09 and Sunday
2026-11-08: 1,290 cases per mode. Split: per category, questions ordered by SHA-1 of the text,
first 60 % dev (140), rest heldout (94).

How labels were made: each question was given a *meaning* by hand from its wording alone
(e.g. `D:yesterday:evening`, `CLK:22:0`, `WEEKEND:last`, `None`), written before and without
running any resolver or reading any candidate. `make_phrases.gold()` turns a meaning into a span
for a "now" with the conventions below. One annotator; no inter-annotator check.

## Labelling conventions (persona: night-owl gamer, Europe/Paris)

1. **Logical day.** The user's day runs 05:00 to 05:00. Before 05:00 it is still the previous
   day: at Tue 00:40, "today / aujourd'hui / ce soir / tonight / ce matin" refer to Monday and
   "hier / yesterday / last night" refer to Sunday (people do not change day until they sleep).
2. **Parts of a logical day D** (hours >= 24 are on D+1):
   morning / matin / matinée 06-12; midi / ce midi / à midi / lunch / at lunch / lunchtime 12-14;
   afternoon / après-midi / aprèm / aprem 12-18; evening / soir / soirée / tonight 18-02 (the
   evening runs past midnight); FR night (nuit, la nuit dernière, cette nuit, "late last night")
   22-06; EN "last night" / "Saturday night" 18-06 (evening and night).
3. **Whole day** ("hier", "yesterday", "avant-hier", "on Friday", "il y a 3 jours"): D 05:00 to
   D+1 05:00. This is the convention with the largest effect: a calendar-midnight window misses
   the late evening and fails covers by up to 5 h.
4. **"Today"**: D 05:00 to now.
5. **In-progress periods are clipped to now** (labels never contain the future): "ce soir" at
   23:30 = 18:00-23:30; "ce soir" at 00:40 = Mon 18:00-Tue 00:40; "ce week-end" on Sunday =
   Saturday 05:00 to now.
6. **"This <part>" not started yet** (past-tense question about a period still to come: "ce
   soir" / "tonight" at 09:00 or 14:00, "cet aprèm" or "ce midi" at 09:00, "ce matin vers 10h"
   at 09:00) means **the most recent such period**, i.e. yesterday's. A memory app can only
   answer about the past; the user is most plausibly referring to the last one (after a late
   night, "tonight" at 09:00 is last night's evening). Not labelled null: a window is wanted.
7. **"Cette nuit"**: before 05:00, the night in progress (Mon 22:00 to now); otherwise the past
   night (Mon 22:00-Tue 06:00), also at 23:30 (in a past-tense question the current evening is
   "ce soir"). **"La nuit dernière" / "last night"**: always the previous logical day's night,
   so at Tue 00:40 they mean Sunday night (Sun 18:00-Mon 06:00 for EN, 22:00-06:00 for FR).
8. **Clock times** ("vers 22h", "at 3pm", "à 14h", "à minuit", "vers midi", "at noon", "around
   noon"): [t-30 min, t+30 min). Without a day word: the most recent occurrence that has started
   (t-30 min < now), clipped to now. With a day word the time is on that logical day, small
   hours (< 05:00) on the next calendar date: "hier soir vers minuit" at Tue 09:00 = Mon
   23:30-Tue 00:30; "last night around 1am" = Tue 00:30-01:30; "cette nuit vers 2h" asked at
   00:40 has not happened yet tonight, so it is the previous night's 2h (Mon 01:30-02:30).
   "à midi" (FR) is the lunch break 12-14; "at noon", "around noon", "vers midi" are clock 12:00.
9. **"Après minuit" / "past midnight"**: 00:00-06:00 of the current calendar date, clipped.
10. **"Tout à l'heure", "earlier", "a while ago", "il y a un moment"**: the last 3 hours
    [now-3h, now). "earlier today" / "plus tôt aujourd'hui" = today (rule 4).
11. **Relative**: "il y a N heures" = [now-N h-30 min, now-N h+30 min); "il y a 20 minutes" =
    [now-30, now-10 min); "in the last hour", "ces 3 dernières heures", "the last 30 minutes",
    "ces trois derniers jours" = [now-N, now); "N days ago" = that whole logical day; "cette
    semaine" = Monday 05:00 to now; "last week" = previous Monday 05:00 to this Monday 05:00.
12. **Weekdays**: a bare weekday is its most recent occurrence strictly before the *calendar*
    date of now ("lundi soir" at Tue 00:40 = Monday evening in progress, clipped; "mardi soir" on
    Tuesday = last week). "last Tuesday" on a Tuesday = a week ago.
13. **Weekend**: Saturday 05:00 to Monday 05:00. On a weekday (logical), "ce week-end", "this
    weekend", "over the weekend", "le week-end dernier" and "last weekend" all mean the weekend
    just past (past-tense questions). Inside a weekend (logical Saturday or Sunday, including
    Monday 00:40/03:00), "this weekend" is the current one clipped to now and "last weekend" /
    "le week-end dernier" the previous one.
14. **Two anchors**: the label is the **main clause's** time (the event the question asks
    about); the time in the relative / "when" clause is a secondary constraint and is not
    labelled. E.g. "What video did I watch yesterday afternoon that Minlo told me about this
    morning?" = yesterday afternoon; "Le jeu dont Sacha a parlé ce matin, j'y ai joué hier
    soir ?" = hier soir; "Qu'est-ce que Léa a dit hier soir quand je lui ai montré la vidéo de ce
    matin ?" = hier soir.
15. **Time-free**: time words inside a title or name ("Night King", "Weekend Update", "Le
    Matin", "Hier encore", "Vendredi tout est permis", "mode nuit") are topics: gold null.

Tolerance (+-30 min) and the 2x length bound absorb fuzzy part boundaries (scope: "may fake
exact boundaries"), not a different reading of the day.

## Retrieval fixture (`retrieval.py`)

In-memory **plain `sqlite3`** database. All 18 repo migrations apply unchanged through
`akashic.storage.db.migrate` (SQLite 3.50.4 has FTS5 with `unicode61 remove_diacritics 2`; no
migration uses a SQLCipher pragma); nothing was skipped or adapted. `akashic.storage.db` imports
`sqlcipher3`, which is installed in the dev venv, so no module stub was needed (the planner
imports only the standard library). Episodes come from the repo's real
`akashic.pipeline.episodes.build_episodes`.

Synthetic timeline (transcripts are "synthetic <session> line <n>"): A Sun 20:30-22:45 Sacha;
E Mon 12:20-13:10 Sacha (lunch); B Mon 17:00-19:30 Léa; C Mon 22:00-Tue 01:30 Minlo with no
speech 23:37-23:43; M Spotify stream Mon 23:00-Tue 03:00 (lyrics on the system stream, music
paused during the video); V YouTube in Zen Tue 00:30-00:45; F fresh Sacha speech Tue
08:52-09:00. One system loopback stream (one segment at a time; `sources` = owner app plus
apps audible underneath), mic stream during calls, segments every 10-30 s. Segments ending in
the last 10 min have no speaker turns and a pending `speakers` job. Built at now = Tue 09:00
(3,518 segments; episodes A, E, B, C split in two by the 6-min gap, M music, V video; F not in
an episode) and Tue 00:40 (2,932 segments; A, E, B and C's first part only; C's second part,
M and V still open, so 357 segments are in no episode).

14 questions (R1-R14), e.g. "hier soir", "last night", "vers 22h", "ce midi", "vers 23h",
"tout à l'heure" at 09:00 and 00:40, each with intended session(s). Plan kinds/people come from
the real `heuristic_plan`. Window sources: gold label, baseline resolver, candidate resolver;
filters: baseline or candidate. Per case: recall (intended session's eligible segments
returned; eligible = passes today's person/kind filters with an unbounded window), recall_win
(eligible segments overlapping the gold span), outside (returned segments of no intended
session), ep_recall / ep_outside (plan-kind episodes built mainly from the intended sessions).

Baseline filters copy hybrid.py: lines `s.start_ms >= start AND s.start_ms < end` plus the
person/kind clauses (hybrid.py:327-373); episodes `start_ms < end AND end_ms > start`
(hybrid.py:197-221).

## Baseline results (2026-11-10)

`out/baseline/metrics.txt`:

    rules_pass=0.1605 (dev 0.1654, heldout 0.1529) model_pass=0.1775 future_rules=163
    future_model=202 false_filter_rules=100 false_filter_model=100 p50=322 us p99=701 us
    (timing is the whole heuristic_plan and varies run to run: 219/440 us on an earlier run)

Rules mode by category: morning 0.60, relative 0.63, time_free 0.33, clock 0.25, evening 0.20,
today 0.20, weekday 0.17, yesterday 0.15, afternoon 0.14, two_anchor 0.08, and 0.00 for
daybefore, earlier, midnight, night, noon, weekend. By now: 09:00 0.16, 14:00 0.16, 23:30 0.29,
00:40 0.10, 03:00 0.10. Retrieval with the baseline window and filters: mean recall 0.13,
910 outside lines (all from R10, "hier soir" at 00:40 resolving to the wrong evening); with the
gold window and today's start-time filter: recall 0.78 (lower only where the window is a moment
inside a longer call), recall_win 0.999, 12 outside lines (open music stream lyrics, R13).

See `out/baseline/run.log` for per-category / per-now / per-language tables and the retrieval
table.
