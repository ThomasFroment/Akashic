# heldout3: a third held-out set for thread follow-up field inheritance

`heldout3.jsonl` holds 165 new labelled pairs, all with `split = "heldout3"`. It was written
without reading any candidate, scratchpad, metrics or `details.json` file. Only the schema was
taken from `pairs.jsonl` and `heldout2.jsonl`; their rows were not read. No question text matches
a question or an earlier question in either file, and no earlier question matches either, after
case folding, accent removal and punctuation stripping. No question repeats inside the file. The
labels follow the 11 conventions in `README.md` exactly. Cases that the conventions leave
ambiguous were left out (see below).

## Schema

The schema matches `heldout2.jsonl`: `{id, lang, split, prev_question, question, category,
names, gold: {people, kinds, keywords, time}}`, where each gold value is `own`, `inherit` or
`none`. `lang` is the language of the new question. `mixed` means that the question itself
switches between French and English. Each row has
`names = ["Minlo", "Sacha", "Léa", "Yanis", "Chloé", "Ben", "Nora"]`. The harness passes them to
`heuristic_plan` (for S and E) and to the candidate's `plan_thread`.

## Counts

| category | en | fr | mixed | total |
|---|---|---|---|---|
| topic_switch (8 with no time after a dated question) | 7 | 7 | 8 | 22 |
| short_selfcontained | 7 | 7 | 6 | 20 |
| pronoun | 6 | 6 | 6 | 18 |
| deictic | 6 | 6 | 6 | 18 |
| time_link | 7 | 6 | 4 | 17 |
| conjunction | 6 | 6 | 6 | 18 |
| continuation | 6 | 6 | 5 | 17 |
| exclusion | 6 | 6 | 5 | 17 |
| partial_shift | 6 | 6 | 6 | 18 |
| **total** | **57** | **56** | **52** | **165** |

Gold labels, per field (own / inherit / none): people 44 / 82 / 39, kinds 81 / 77 / 7,
keywords 42 / 74 / 49, time 38 / 115 / 12. There are no `target` or `repo_tests` pairs, so
`target_errors` is always 0. `harness.py --check --pairs heldout3.jsonl` reports 0 flags: every
gold `own` field has a non-empty standalone value.

## What is new compared with pairs.jsonl and heldout2.jsonl

- **Names:** Yanis, Chloé, Ben and Nora are added to Minlo, Sacha and Léa. Some questions type the
  names without the accent or in lower case (`et chloe`, `what did yanis say`).
- **Topics:** Discord, Twitch and YouTube; Valorant, Deadlock, Arc Raiders, Marvel Rivals, Path
  of Exile 2, Last Epoch, Clair Obscur, Elden Ring Nightreign, Peak, Hytale, Rust wipes, Rocket
  League, Smash, Among Us, Pokémon TCG, Frieren, Arcane, Kurzgesagt, Veritasium, Kameto, Stromae,
  Damso, a Ghibli film, Black Friday, Secret Santa, a move to Lyon, a deposit, taxes and a vet.
- **Syntax:**
  - informal French (`jsp`, `chui`, `ptet`, `kestion :`, `wsh`, `tkt`, `bah`, `du coup`,
    `genre`, `stp`) and French typed without accents (`j'ai regarde`, `il etait`);
  - English typos and shorthand (`waht`, `wat`, `abt`, `Whats`);
  - questions without a question mark, one-word questions (`raconte`), two-word questions (`and Nora`,
    `et chloe`, `say more`) and one self-contained question of about 40 words;
  - 52 code-switched questions (`Elle a dit quoi about the new heroes`,
    `later that day elle en a reparlé ?`, `j'ai watch quoi d'autre ce jour-là`).
- **Hard cases:**
  - Self-contained questions with cue-like words used non-referentially. They must not inherit:
    - the determiner `that` or `cette` with a restrictive clause: `What was that song I listened
      to on Sunday`, `C'était quoi cette chanson que j'ai écoutée samedi`, `c'était quoi that
      clip where the guy falls off the map, sur Twitch`;
    - the English noun `son`: `Did Chloé say anything about my son's birthday party`, with no
      time after a dated question;
    - `the other day`, which resolves to no time: `What was the Kurzgesagt video I watched the
      other day` and `Ben a dit quoi sur Arc Raiders the other day` give `time = none`;
    - `pendant` without `ce temps-là`: `what did I browse on Chrome pendant mon call avec Léa ce
      matin`.
  - Possessives bound by a name in the same question are self-contained: `Qu'est-ce que Ben a
    dit about his new keyboard la semaine dernière` and `Yanis m'a déjà parlé de son PC gamer`.
  - New continuation phrasings: `keep going`, `ok and? what happened next`, `Can you expand on
    that`, `say more`, `in what way`, `go on, i'm listening`, `raconte`, `Tu peux préciser ?`,
    `Approfondis stp`, `genre ?`, `et du coup ?`, `dis m'en un peu plus wsh`,
    `ok mais pourquoi exactly`, `explain stp`, `et après, what happened`,
    `tell me everything, genre tout` and `c'est-à-dire, elaborate un peu`. `say`, `tell`,
    `raconte`, `dis` and `listening` are kind cues, and the gold kinds are still `inherit`
    (convention 5).
  - Pronoun plus the same topic versus pronoun plus a new topic: `elle a gagné ou pas`,
    `Elle l'a fini ou pas`, `he liked it ou pas` (keywords `inherit`) against `Did he mention
    the loot drops`, `Il en pensait quoi, du prix ?`, `Je lui ai dit quoi about the deposit`
    (keywords `own`, convention 10).
  - Deictic predicates: `how long did that one last`, `did he end up buying it`, `how many
    viewers did that have`, `was it sponsored`, `Il l'a payé combien, ce casque`, `Ça sortait
    quand, ce truc`, `C'était bien au moins`, `is it worth it`, `ça a duré combien de temps, that
    wipe`.
  - A new person with a deictic reference: `Did Yanis say anything about that too`, `Léa était
    au courant de ça ?` and `Nora en a parlé aussi ?` give `people = own` and inherit the topic.
  - Time links: `later that day`, `right after`, `after that`, `at the same time`, `the same
    day`, `juste après`, `dans la foulée`, `le même jour`, `ensuite`, `pendant ce temps-là` and
    `à ce moment-là`. They inherit time (convention 7). With a kind change, as in `j'ai joué à
    quoi dans la foulée` or `at the same time, what was I listening to`, people and topic are
    `none` (convention 8).
  - Kind-change partial shifts, with or without a deictic topic: `Did I play it right after`,
    `and did i listen to it`, `Et j'y ai joué ensuite ?`, `et j'ai watch it après ?` and
    `and did I talk about it with anyone` keep keywords (`inherit`). `And what did I play
    afterwards`, `and which sites did I browse then` and `et du coup j'ai écouté quoi` drop
    them (`none`).
  - A kind change with a new person and a deictic topic: `Et Chloé, she talked about it ?` gives
    people `own`, kinds `own`, keywords `inherit` and time `inherit`.
  - Conjunctions whose standalone keywords hold leftover words: `and on saturday`, `how about
    three days ago` (`three`, `days`), `Et dimanche, rien ?`, `And vendredi?` and
    `et avant-hier du coup`. The gold keywords are still `inherit`.
  - Exclusion with names: `what else did Yanis talk about`, `Yanis a dit quoi d'autre`, `Chloé
    et Léa ont parlé de quoi d'autre`, `Apart from that, did Ben say anything` (people `own` =
    `{Ben}` after `Ben and Nora`) and `à part ça, did Ben say anything`.
  - Overview questions after a topical question: `Summarize my Monday`, `Fais-moi un bilan de la
    semaine dernière` and `recap de mon dimanche please` give kinds, people and keywords `none`.

## Labelling choices within the conventions

- A self-contained field is `own` when the question states it and S holds it, and `none` when
  the question does not state it. A `none` field may still have leftover standalone words, such
  as a weekday, `please`, `wsh` or `waht`. The scorer accepts S for `none`.
- When a field is equal in S and E, either label scores the same. Labels follow the wording of
  the convention. Convention 9 rows (`and Yanis, what did he tell me`, `Et Minlo il disait quoi
  la-dessus`) have `kinds = inherit` even when the cue is repeated. Exclusion and pronoun rows
  label a stated cue as `own` (conventions 6 and 10).
- `what else did I watch that day` and `j'ai regardé quoi d'autre ce jour-là` are exclusions
  whose `that day` / `ce jour-là` is a time link. Their time is `inherit`.
- `kestion : il y avait autre chose ?` uses an impersonal `il`. People are still `inherit` under
  convention 6.
- `On y est allées le même jour ?` inherits people: `on` is the user plus Chloé.
- `le même jour, j'ai joué à Minecraft ou pas` is a kind change that states its own topic, so
  keywords are `own` and people `none`.
- `and right after did she mention the switch 2` and `et dans la foulée, il a dit quoi sur la
  voiture` combine conventions 7 and 10. People and time are `inherit`, and keywords are `own`.
- On 2026-11-10, a Tuesday, `mardi` resolves to the previous Tuesday and `Monday` or `lundi` to
  yesterday. Earlier questions were chosen so that S and E differ wherever a field is under test.

## Cases left out as ambiguous under the conventions

- French filler `ben` (`ben du coup...`), which matches the known name Ben.
- `l'autre jour` and non-referential `more`. Convention 1 lists `autre` and `more` as elliptical
  markers, but these words do not refer back.
- Pronouns bound inside a long self-contained question (`what did she say it was called`). Read
  literally, convention 1 treats `she` and `it` as anaphora.
- Same-kind time links that ask for the next item (`What did I watch right after that?` after
  a topical video question, `Et juste après, il a parlé de quoi ?`). Convention 3 would inherit
  the topic, but the user wants a new one.
- `What did I do right after?` with no kind, and `Who did I talk to after that?`.
- A new person after a time link (`and what did Léa say at the same time`).
- `Other than the tournament, what did Ben and Nora talk about` with no time. Convention 1 makes it
  self-contained (`time = none`), but the user probably means the same day. The same applies to
  `à part Diablo, il a parlé de quoi`, an exclusion that names the topic.
- A conversation cue that describes media content (`il disait quoi là-dessus, le mec` about a
  podcast). It could be a kind change or the media's content.
- A pronoun with a game cue (`il a lancé une partie ?`), and another person's viewing
  (`did Minlo watch it too`).
- Implicit definite references after a kind change (`Juste après, did I watch the replay`).
- Topic words that the time parser reads as a day part (`Night City`, `morning routine`). The
  same applies to bare day parts and `ce soir-là` in follow-ups, as in heldout2.
- Predicates that could be read as the topic (`Was she nervous at the time`).
- `called` (not a cue word) in a self-contained `Who called me on Saturday`. The gold `none`
  would contradict the meaning.

## Scoring

The commands below are run from this folder with `..\.venv\Scripts\python.exe`.

    python harness.py <candidate_dir> --pairs heldout3.jsonl
    python harness.py --baseline today --pairs heldout3.jsonl
    python harness.py --baseline never --pairs heldout3.jsonl
    python harness.py --check --pairs heldout3.jsonl

Outputs go to `<candidate_dir>/heldout3/` (or `baselines/<name>/heldout3/`): `metrics.txt`,
`details.json` and `side.png`. `accuracy_budget_ok` requires every per-field accuracy to be at
least 0.95 over the file, with 0 exceptions.

## Baselines (2026-11-10)

| baseline | people | kinds | keywords | time | min |
|---|---|---|---|---|---|
| today (`follow_up`) | 0.830 | 0.952 | 0.618 | 0.927 | 0.618 |
| never inherit | 0.594 | 0.588 | 0.552 | 0.303 | 0.303 |

`today`, per category (people / kinds / keywords / time):

| category | people | kinds | keywords | time |
|---|---|---|---|---|
| topic_switch | 0.73 | 0.86 | 0.95 | 0.64 |
| short_selfcontained | 0.70 | 0.85 | 0.75 | 0.80 |
| pronoun | 1.00 | 1.00 | 0.67 | 1.00 |
| deictic | 1.00 | 1.00 | 0.06 | 1.00 |
| time_link | 0.59 | 1.00 | 0.65 | 1.00 |
| conjunction | 1.00 | 1.00 | 0.72 | 1.00 |
| continuation | 1.00 | 0.88 | 0.12 | 1.00 |
| exclusion | 1.00 | 1.00 | 1.00 | 1.00 |
| partial_shift | 0.50 | 1.00 | 0.56 | 1.00 |

Per-language minima for `today` are en 0.649, fr 0.536 and mixed 0.673. `today` fails mostly on
keywords, because leftover standalone words block inheritance in deictic, continuation and
time-link rows. It also fails on people and time for self-contained questions that leave a field
unstated, and on people for kind changes. `today` scores 1.00 on exclusion, as expected from the
scoring rule: leftover `else` or `autre` counts as `none`. `never` passes topic_switch and
short_selfcontained only (1.00 each) and scores 0.00 on time in every elliptical category.
