# Held-out set 2 (`heldout2.jsonl`)

A second, fresh evaluation set for `harness.py`. It contains 145 new questions, with no
collision with `phrases.jsonl` after lowercasing and stripping punctuation. The labels follow the
"Labelling conventions" in `README.md` exactly. The set is weighted toward the hard shapes:
two-anchor questions, time-like titles, informal spellings, weekdays, clock times and relative
ranges. It was written without reading any candidate (C1, C3, C5), any `out\` result or any
scratchpad candidate file, and without running a resolver before the meanings were fixed.

## Files and commands

| File | What |
|---|---|
| `heldout2_phrases.py` | the questions and their meanings -> `heldout2.jsonl`. Imports `make_phrases` (meaning constructors, `gold`, `NOWS`, refs) unchanged. Refuses to write on a collision and prints the closest existing questions for a paraphrase review. |
| `heldout2.jsonl` | same schema as `phrases.jsonl`, ids `h2_000`..., `split` = `heldout2` |
| `harness.py --phrases` | scores any question file. Without `--phrases`, the output is identical to the previous version. With it, the results go to `out/<name>_<stem>/`, so `out/<name>/` is never overwritten. |

From this folder (`..\.venv\Scripts\python.exe`):

    python heldout2_phrases.py                                                  # regenerate
    python harness.py --phrases heldout2.jsonl --no-retrieval                  # baseline -> out/baseline_heldout2/
    python harness.py --phrases heldout2.jsonl --candidate C1/cand.py --name C1 --no-retrieval   # -> out/C1_heldout2/

`--no-retrieval` is recommended: the retrieval fixture (R1-R14) does not depend on the question
file. Timing (p50/p99) runs over the heldout2 questions at the Tuesday reference.

## The set

145 questions: FR 59, EN 53, mixed 33. There are 795 cases per mode: 5 nows (09:00, 14:00, 23:30,
00:40, 03:00 on Tue 2026-11-10), plus Mon 2026-11-09 and Sun 2026-11-08 for the 7 questions whose
main meaning is a weekend.

| Category | n | | Category | n |
|---|---|---|---|---|
| today | 4 | | midnight | 4 |
| morning | 4 | | earlier | 4 |
| afternoon | 4 | | weekend | 5 |
| noon | 4 | | weekday | 8 |
| evening | 4 | | clock | 8 |
| night | 7 | | relative | 9 |
| yesterday | 6 | | **two_anchor** | **52** |
| daybefore | 3 | | **time_free** | **19** |

By language:

| Category | FR | EN | mixed |
|---|---|---|---|
| single-anchor (14 categories) | 30 | 28 | 16 |
| two_anchor | 22 | 18 | 12 |
| time_free | 7 | 7 | 5 |

**Two-anchor (52).**

- Main anchor position: 32 first, 20 last.
- `connector`: rel 24 (que, qui, dont, that, which), when 14 (quand, when, pendant que, après
  que, while, after), none 14.
- Extra fields, which the harness ignores: `connector_word`, `main_pos` and `final_no_time`.
- 9 questions end in a clause that asks without a time ("c'était quoi ?", "c'était lequel ?",
  "which one was it?", "what was it called?").
- 7 are comma dislocations with no opener, where the main anchor comes last ("La vidéo d'hier
  soir, je l'ai finie ce matin ?").

**Time-free (19).** These are titles containing time words, 9 written in lower case and 10 in
upper case (field `title_case`). Examples: "les 12 coups de midi", "minuit chrétiens" / "Minuit,
Chrétiens", "demain nous appartient", "Le Soir", "Vendredi 13", "les Matins de France Culture",
"the tonight show", "weekend at bernie's", "SZA's The Weekend", "the day after tomorrow movie",
"Wednesday" / "wednesday", "Nightreign", "After Hours" / "after hours", "the morning show",
"Saturday Night Fever", "Before Midnight".

**Informal and variant forms:** ajd, ce mat', hier mat', cette aprem, dans l'aprem, pendant le
déj, hier soir tard, tard hier soir, dans la nuit, vers 1h du mat, vers 3h du matin, passé minuit,
ce WE, le weekend dernier, en matinée, il y a 3h, il y a une demi-heure, so far today, around
lunchtime, during lunch, late last night, the night before last, over the weekend, earlier this
week, over the past two days, last Friday night, dans la nuit de samedi à dimanche.

## How the README conventions were applied

Most forms are listed in the conventions directly. The following were labelled by the closest
listed form. Each one is a spelling variant or a direct translation, never a new reading:

| Form | Label | Basis |
|---|---|---|
| `ce mat'`, `hier mat'` | morning | spelling of matin (rule 2) |
| `ajd` | today | abbreviation of aujourd'hui (rule 4) |
| `cette aprem`, `dans l'aprem` | today's afternoon | aprem (rule 2); "dans l'après-midi" is labelled today in phrases.jsonl |
| `en matinée` | today's morning | matinée (rule 2), as "dans la matinée" |
| `pendant le déj`, `around lunchtime` | noon part 12-14 | lunch / lunchtime (rule 2). This is not clock 12:00, which only "at noon", "around noon" and "vers midi" mean. |
| `hier soir tard`, `tard hier soir` | FR night 22-06 of yesterday | French for "late last night", which rule 2 lists under FR night |
| `dans la nuit` (no day word) | `cette_nuit` (rule 7) | "dans la matinée" / "dans la soirée" are labelled as the current day's part |
| `passé minuit`, `after midnight` | 00:00-06:00 (rule 9) | "après minuit" / "past midnight" |
| `the night before last` | EN night 18-06 of the day before yesterday | "last night" (rule 2), one day earlier |
| `last Friday night`, `On Sunday night` | EN night of that weekday | "Saturday night" (rule 2); "last <weekday>" follows rule 12 like a bare weekday |
| `dans la nuit de samedi à dimanche` | FR night 22-06 of Saturday | rules 2 and 12 |
| `so far today` | today (rule 4) | |
| `earlier this week` | this week (rule 11) | "earlier today" = today (rule 10) |
| `il y a une demi-heure`, `45 minutes ago` | ago-minutes, +-10 min (rule 11) | |
| `over the past two days` | `LAST_D(2)` (rule 11) | |
| `ce WE` | this weekend (rule 13) | WE = week-end |

**Two-anchor labels (rule 14)** take the main clause's time, using the same reading as the
two-anchor examples in `phrases.jsonl`:

- When a question ends in a clause that asks with a time (for example "..., je l'ai finie ce
  matin ?" or "..., what did Sacha say about it this afternoon?"), the main anchor is that
  clause.
- When the final clause asks with no time ("c'était quoi ?"), the main anchor is the user's own
  activity, or the noun phrase that names the thing asked about ("La musique de cette nuit, Sacha
  l'a citée ce midi, c'était quoi ?" = cette nuit). This follows "Le stream de ce soir, Léa l'a
  mentionné hier, c'était quoi ?" = ce soir.

`main_time_text` is the main anchor copied exactly, as in `phrases.jsonl`.

## Left out: the conventions do not settle these

| Case | Why it is not labelled |
|---|---|
| `2main` / `demain`, "tomorrow" | Future. There is no convention for a past-tense question about tomorrow. |
| `tôt ce matin`, `early this morning` | "Early" narrows the morning, but there is no span for it (06-09? the small hours for a night owl?). A narrow window fails covers if gold is 06-12; a wide one fails tight if gold is narrow. |
| `en fin d'aprèm`, `en début de soirée`, `hier en fin de soirée` | Sub-parts of a part of the day are not defined. |
| `this arvo` | Australian slang. It is not common for this user, and the conventions do not list it. |
| `le soir du réveillon` | It could be a title, but it is more naturally a date (Christmas Eve evening). The conventions also have no holiday dates. |
| `le 20h` / `au 20h` (the TV news) | It is both a title and a clock time (20:00). It is not clearly time-free. |
| `lundi matin` (the media outlet lundimatin) | Read as a time unless spelled as one word. It is not clearly a title. |
| `à midi pile` | FR "à midi" is the lunch break (rule 8), while "pile" asks for the clock 12:00. |
| `at half past ten`, `at 10` (EN, no am/pm) | A 12-hour ambiguity that the conventions do not resolve. |
| `yesterday night` | Non-native English. It could mean "last night" (18-06) or yesterday's evening (18-02), and it is not listed. |
| `depuis ce matin`, `since lunch` | An open-ended span from a part of the day to now is not defined. |
| `hier dans la nuit`, `la nuit d'avant-hier` | In French, these can mean the night from yesterday to today or the night before. |
| `juste après minuit`, `just now`, `à l'instant`, `tantôt` | Not listed. They need a span narrower than "after midnight" or "earlier", or they are regional. |
| `dans la soirée` after a clause about another day ("After Sacha sent me the link yesterday, j'ai regardé quoi dans la soirée ?") | It could be today's evening or yesterday's evening. |
| A two-anchor question whose two clauses are declaratives by different people, followed by "c'était quoi ?" ("Minlo m'a parlé d'un jeu hier soir, j'y ai joué ce matin, c'était quoi ?") | Either clause can be read as the main one. Rule 14 and the phrases.jsonl examples do not pick one. |

## Baseline (today's planner, 2026-11-10)

`out/baseline_heldout2/metrics.txt`:

    rules_pass=0.1786 model_pass=0.2050 future_rules=112 future_model=128
    false_filter_rules=35 false_filter_model=35 errors=0 cases=795
    (p50 497 us / p99 1277 us: timing of the whole heuristic_plan; it varies run to run)

For comparison, the baseline scores rules 0.1605 and model 0.1775 on `phrases.jsonl`.

Rules mode by category:

| Pass rate | Categories |
|---|---|
| 0.63 | time_free |
| 0.45 | morning |
| 0.40 | clock |
| 0.29 | relative |
| 0.20 | evening, daybefore |
| 0.15 | today |
| 0.13 | weekday |
| 0.10 | yesterday |
| 0.09 | two_anchor |
| 0.05 | afternoon |
| 0.00 | noon, night, midnight, earlier, weekend |

Other breakdowns, rules mode:

- **By now:** 09:00 0.18, 14:00 0.18, 23:30 0.28, 00:40 0.13, 03:00 0.14.
- **By language:** en 0.23, fr 0.14, mixed 0.17.
- **Two-anchor by connector:** rel 0.05, when 0.09, none 0.17. In model mode: rel 0.13, when
  0.11, none 0.29, and two_anchor overall 0.16.
- **Two-anchor by shape (rules / model):** main first 0.09 / 0.17, main last 0.08 / 0.15, final
  clause without a time 0.07 / 0.09, comma dislocation with the main anchor last 0.26 / 0.46.
- **Time-free by case:** lower 0.67, upper 0.60.
- **False filters:** 7 time-free titles give windows: "the morning show", "the tonight show",
  "wednesday", "Wednesday", "Vendredi 13", "Le Soir" and "Saturday Night Fever".

The analysis lists above (by shape, by `title_case`) were computed from `harness.evaluate` and
the extra fields; the harness itself prints category, now, language, split and connector.

## Limits

- There is one annotator, the same method as `phrases.jsonl`, and no inter-annotator check.
- The author had read `make_phrases.py`, so the question style is not independent of the first
  set. It is new text and new anchors, not a new population of users.
- Questions are synthetic. The set measures the same conventions on unseen wording; it does not
  show that daily use is accurate.
