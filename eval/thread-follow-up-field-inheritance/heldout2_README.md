# heldout2: a second held-out set for thread follow-up field inheritance

`heldout2.jsonl` holds 110 new labelled pairs, all with `split = "heldout2"`. It was written
after the candidates existed and without reading them. Its author did not read the rows of
`pairs.jsonl`; only the schema was copied. No question text matches a question in `pairs.jsonl`
(checked after normalising case and punctuation). The labels follow the 11 conventions in
`README.md` exactly. Cases that the conventions leave ambiguous were left out (see below).

## Schema

The schema matches `pairs.jsonl`: `{id, lang, split, prev_question, question, category, gold:
{people, kinds, keywords, time}}`, where each gold value is `own`, `inherit` or `none`. Each row
also has `names`: `["Minlo", "Sacha", "Léa", "Karim", "Inès", "Tom"]`. The harness passes a
pair's own `names` to `heuristic_plan` (for S and E) and to the candidate's `plan_thread`.
Pairs without `names` (all of `pairs.jsonl`) still use the default three names. The new names
therefore test whether a candidate reads `names` or hardcodes Minlo, Sacha and Léa.

## Counts

| category | en | fr | mixed | total |
|---|---|---|---|---|
| topic_switch (4 with no time after a dated question) | 6 | 7 | 4 | 17 |
| short_selfcontained | 6 | 5 | 2 | 13 |
| pronoun | 5 | 4 | 2 | 11 |
| deictic | 5 | 4 | 2 | 11 |
| time_link | 4 | 4 | 3 | 11 |
| conjunction | 5 | 4 | 3 | 12 |
| continuation | 5 | 4 | 3 | 12 |
| exclusion | 5 | 4 | 2 | 11 |
| partial_shift | 5 | 5 | 2 | 12 |
| **total** | **46** | **41** | **23** | **110** |

There are no `target` or `repo_tests` pairs, so `target_errors` is always 0 on this file.
`harness.py --check --pairs heldout2.jsonl` reports 0 flags: every gold `own` field has a
non-empty standalone value.

## What is new compared with pairs.jsonl

- **Names:** Karim, Inès and Tom are added to the three existing names.
- **Games, apps and topics:** Balatro, Factorio, Hades II, Helldivers, Satisfactory, Lethal Company,
  Hollow Knight, Godot, Spotify Wrapped, Netflix, Notion, the Steam Deck, raclette, belote,
  rent and an internship.
- **Syntax:**
  - questions without a question mark;
  - informal French (`chui`, `kestion`, `ptet`, `stp`);
  - English typos and shorthand (`waht`, `pls`, `didnt`);
  - one long self-contained question of about 30 words;
  - one-word questions (`Développe.`);
  - discourse openers (`ok so`, `bon`, `du coup`, `et sinon`, `btw`, `petite kestion :`).
- **Hard cases:**
  - Continuation phrasings: `can you elaborate?`, `go deeper`, `how come`, `Développe.`,
    `plus de détails stp`, `bon, vas-y, continue`, `Explique-moi mieux stp`,
    `C'est-à-dire ? tell me more`.
  - Conjunctions with a preposition: `and on Friday?`, `Et pour jeudi ?`,
    `Et pour la semaine dernière alors ?`. The standalone keywords then hold the weekday
    (`friday`, `jeudi`), and the gold is still `inherit`.
  - A new named person with a pronoun clause: `Et Karim, il en pensait quoi ?`,
    `And Léa, did she say anything about it?` and `Et Tom, what did he think ?`.
  - A deictic reference with a new person: `Did Sacha mention that too?` gives
    `people = own` and inherits the rest.
  - Kind-change partial shifts (convention 8): `And did I play it afterwards?` keeps the
    topic through `it`, while `Et du coup, j'ai joué à quoi juste après ?` drops it.
  - Deictic questions with predicates: `Ça coûtait combien, ça ?`, `did it actually work in
    the end`, `Did I buy that in the end?` and `was it any good`.
  - `Ça parlait de quoi exactement, cette vidéo ?`: `parlait` is a conversation cue, so S kinds
    are `{video, conversation}`, but gold kinds are `inherit` (`{video}`).
  - Exclusion with a named person: `What else did Karim say?` and `Sacha a parlé d'autre
    chose ?` give `people = own`. In `besides that, what did Tom and Sacha talk about`,
    people are `own` (`{Tom, Sacha}`) and the topic is dropped.
  - `Et sinon, qu'est-ce que Karim a dit sur le resto hier ?` has a leading `et`, so it counts
    as elliptical under convention 1. It states every field, so all four are `own`.

## Labelling choices within the conventions

- When a field is equal in S and E, either label scores the same. In those cases, labels follow
  the wording of the convention. Convention 9 gives `kinds = inherit` for
  `Et Minlo, il a dit quoi là-dessus ?`, even though `dit` is a cue word.
- On 2026-11-10, a Tuesday, `lundi` or `Monday` resolves to yesterday. Earlier questions were
  chosen so that S and E time differ wherever the time field is under test.

## Cases left out as ambiguous under the conventions

These cases were left out:

- Bare day parts in a follow-up (`And in the evening?`, `that evening`). They resolve to
  today, while the user means the earlier day.
- Unparsed time words (`ce week-end`, `cet aprem`, `tout à l'heure`). A gold `own` time
  would have an empty S.
- A new person after a time link (`Pendant ce temps-là, Inès disait quoi ?`). It is unclear
  whether the topic carries over.
- `What was he doing at the same time?`.
- `anything else` addressed to a different person.
- Questions where a predicate such as `angry` could be read as the topic.

## Scoring

The commands below are run from this folder with `..\.venv\Scripts\python.exe`.

    python harness.py <candidate_dir> --pairs heldout2.jsonl
    python harness.py --baseline today --pairs heldout2.jsonl
    python harness.py --check --pairs heldout2.jsonl

Outputs go to `<candidate_dir>/heldout2/` (or `baselines/<name>/heldout2/`): `metrics.txt`,
`details.json` and `side.png`. The metrics line gives `all_*`, `heldout2_min`, the minimum per
language and `cat_<category>_min`. Here `accuracy_budget_ok` requires every per-field accuracy
to be at least 0.95 over the file, with 0 exceptions. Without `--pairs`, the harness behaves as
before.

## Baselines (2026-11-10)

| baseline | people | kinds | keywords | time | min |
|---|---|---|---|---|---|
| today (`follow_up`) | 0.900 | 0.964 | 0.555 | 0.955 | 0.555 |
| never inherit | 0.609 | 0.600 | 0.545 | 0.327 | 0.327 |

`today` fails mostly on keywords, scoring 0.08 on continuation, 0.09 on deictic and 0.36 on
time_link. Leftover standalone words such as `elaborate`, `details`, `friday` and `good` block
inheritance of the topic. It also fails on people and time for topic switches without a time
or a person (0.71 each), and on people for kind-change shifts (0.67). `never` passes
topic_switch and short_selfcontained only.
