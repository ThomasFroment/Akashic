# Held-out set 3

A third evaluation set, written fresh and independently. The author did not read the earlier
held-out set, any candidate or any result. The only exception was `phrases.jsonl`: its
question texts were read for the collision check alone. Labels follow the README "Labelling
conventions" exactly. The meaning-to-span arithmetic is reused from `make_phrases.py` without
changes, by import.

    python heldout3_phrases.py                                        # regenerate heldout3.jsonl
    python harness.py --phrases heldout3.jsonl --no-retrieval         # baseline -> out/baseline_heldout3/
    python harness.py --phrases heldout3.jsonl --candidate X/cand.py --name X --no-retrieval

## Contents

There are 198 questions: 73 FR, 69 EN and 56 mixed. Labelled at the 5 nows (09:00, 14:00, 23:30,
00:40, 03:00) of Tue 2026-11-10, they give 1,070 cases per mode. WEEKEND meanings are also
labelled on Mon 2026-11-09 and Sun 2026-11-08. `split` is `heldout3` for every row.

| Category | n | | Category | n |
|---|---|---|---|---|
| today | 7 | | midnight | 5 |
| morning | 7 | | earlier | 6 |
| afternoon | 6 | | weekend | 7 |
| noon | 5 | | weekday | 8 |
| evening | 8 | | clock | 10 |
| night | 8 | | relative | 10 |
| yesterday | 10 | | **two_anchor** | **45** |
| daybefore | 4 | | **time_free** | **52** |

- **two_anchor (45):** the main anchor comes first in 29 and last in 16 (`anchor_order` field).
  By connector: rel 26, when 4, none 15. The connectors cover commas, reduced relatives, "celui
  que", "ce que", "dont", "quand/when", and final clauses without a time (", c'était lequel ?",
  ", which one was it?", ", whats it called"). Main anchors include clock times (one at "cte nuit
  vers 3h"), "y a 2h", "20 mins ago", "après minuit", weekdays, a weekend, "earlier" and
  Sunday night.
- **time_free (52):** every label is null. The `time_free_kind` field splits them into two kinds:
  - `title`, 34 questions. These are shows, songs, games, channels, films, books and newspapers:
    Mercredi / wednesday, Ce soir ou jamais, telematin, Nightreign, Nightcall, Midnight City,
    Midnight Mass, Vivement dimanche, Vendredi 13, Le Soir, Soir 3, Tonight We Riot, After
    Hours, Days Gone, Yesterday, Un après-midi de chien, and others. Titles appear both
    capitalised and lowercase, and some are a single word.
  - `nontemporal`, 18 questions. Time-like words are used for something other than a time:
    fichier midi, le Midi (the region), 3am challenge, vidéo 24h, table de nuit, night light,
    day one patch, morning routine vid, cycle jour nuit, last minute, night vision, weekly
    reset, robe de soirée, night shift, menu du midi à 12 balles, late night snack, weekend
    pass.
- **Style:** about half the questions are ordinary and half informal. The informal ones use
  apocopes and abbreviations (auj, ajd, ce mat, aprem, ce we, cte nuit, tt à l'heure, pdt la
  pause dej, y a 2h, yday, wkend), missing accents (apres minuit, la nuit derniere, tout a
  l'heure), SMS spellings (c quoi, g regardé) and EN typos or slang (tonite, last nite, this
  mornin, this afternon, wat, abt, 2 hrs). Code-switching happens inside the question, in both
  the time words and the rest of the sentence.
- **Collisions:** none. Each question is normalised (lowercase, accents and punctuation
  stripped) and checked against every `phrases.jsonl` question by the generator, which asserts
  there is no match. The same check asserts there are no duplicates and that each
  `main_time_text` is a substring of its question.

## Interpretive choices, all within the conventions

- "pause dej" is lunch, so 12-14 (rule 2).
- "A midi" without an accent is the FR "à midi": lunch, not clock 12:00 (rule 8).
- "In the morning call" means this morning. `phrases.jsonl` sets the precedent with "in the
  afternoon call".
- "on sat night" and "Sunday night" follow the EN "Saturday night" rule: 18-06.
- "late last night" uses the FR night span, 22-06 (rule 2).
- "jeudi dernier", "last Wednesday" and "Friday evening" are treated as bare weekdays (rule 12).
- "20 mins ago" and "y a genre 20 min" use AGO_MIN(20), the README's "il y a 20 minutes".
- An "A, B" structure where A holds the main event is labelled by A's time (rule 14). In "...,
  le podcast que Minlo m'a conseillé hier", the "hier" is secondary.

## Left out (the conventions do not settle them)

- yesterday night, hier dans la nuit, dans la nuit de samedi à dimanche
- tantôt, which is regional and can mean either "this afternoon" or "earlier"
- just now, à l'instant, a bit ago, recently, récemment, y a pas longtemps, the other day,
  l'autre jour, la veille
- a few hours ago, y a quelques heures, past few days
- since this morning, depuis ce matin, early this morning, tôt ce matin, late afternoon, en fin
  d'aprèm, before / after lunch, after dinner, avant de dormir, au réveil
- Ambiguous clock times without am/pm or a day part: "at 9", "vers 9h", "around 10"
- Habitual or generic uses: le soir, le matin, in the evenings
- Future words: demain, tomorrow, ce week-end said on a Friday
- A single time anchor that sits only in a subordinate clause, e.g. "What did Minlo say about
  the video I watched this morning?". Rule 14 says secondary times are not labelled, but it
  does not say whether the right answer is then null or the subordinate time.
- Titles that read as a time when lowercase and nothing disambiguates them, e.g. "did I finish
  the movie yesterday", "le dernier ep de mercredi"
- Identity questions where neither clause is clearly the main one, e.g. "Le build que j'ai
  testé cette nuit, c'est celui que Léa a posté avant-hier ?"

## Baseline (2026-11-10)

`out/baseline_heldout3/metrics.txt`:

    rules_pass=0.1897 model_pass=0.2037 future_rules=172 future_model=201
    false_filter_rules=145 false_filter_model=145 errors=0 p50_us=466.8 p99_us=1492.9 cases=1070

The two modes give identical per-category results except for two_anchor:

| Category | rules = model | | Category | rules = model |
|---|---|---|---|---|
| relative | 0.46 | | today | 0.14 |
| time_free | 0.44 | | afternoon | 0.13 |
| morning | 0.43 | | weekday | 0.13 |
| clock | 0.28 | | yesterday | 0.12 |
| evening | 0.18 | | daybefore, earlier, midnight, night, noon, weekend | 0.00 |

- **two_anchor:** 0.04 in rules mode and 0.10 in model mode.
- **time_free by kind:** title 0.38 and nontemporal 0.56, the same in both modes.
- **By now (rules):** 09:00 0.19, 14:00 0.18, 23:30 0.29, 00:40 0.14, 03:00 0.14.
- **By language (rules):** en 0.24, fr 0.15, mixed 0.19.
- **Timed cases (rules):** covers 0.17 and tight 0.54.

Timing covers the whole `heuristic_plan` and varies from run to run.
