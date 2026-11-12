# Thread follow-up field inheritance: reference and harness

Scope QC (follow-up detection). In an open chat thread, `planner.follow_up` merges every
earlier plan field into each new question. A self-contained question must plan exactly as if
asked alone, while an elliptical follow-up must inherit only the fields it leaves out.

## Files

- `build_pairs.py`: the labelled pairs as a readable table. It writes `pairs.jsonl`.
- `pairs.jsonl`: 170 pairs (107 dev, 63 held out). Each pair has `{id, lang, split,
  prev_question, question, category, gold: {people, kinds, keywords, time}}`.
- `harness.py`: the scorer, the two built-in baselines and the chart (see its docstring).
- `check.txt`: output of `harness.py --check`. It lists each pair's standalone plan (S) and
  earlier plan (E) under the repo's `heuristic_plan`. It has 0 flags: no gold `own` field has
  an empty standalone value.
- `baselines/today/` and `baselines/never/`: `metrics.txt`, `details.json` and `side.png`.

Run with `eval\.venv\Scripts\python.exe`:

    python harness.py <candidate_dir>          # candidate_dir/candidate.py, outputs in candidate_dir
    python harness.py path\to\candidate.py     # outputs next to the file
    python harness.py --baseline today|never   # outputs in baselines/<name>/
    python harness.py --check                  # S and E per pair, label sanity flags

Fixed clock: 2026-11-10 14:00 Europe/Paris. Known names: Minlo, Sacha, Léa.

## Categories (each in EN, FR and mixed unless noted)

| category | n | what it tests |
|---|---|---|
| target | 2 (EN) | the two scope questions in both orders |
| repo_tests | 3 (EN) | `and when was that?`, `and then?` and `tell me more` after `what did Minlo say about a game?` (tests/test_ask_job.py) |
| topic_switch | 31 | self-contained questions with full anchors, including 6 with no time after a dated question |
| short_selfcontained | 27 | short questions that look elliptical but are complete (`What did I watch yesterday?`, `Résume ma journée d'aujourd'hui.`), often after a question of the same kind |
| pronoun | 16 | he / she / il / elle / lui |
| deictic | 15 | that / it / ça / celui-là / cette vidéo |
| time_link | 14 | then / after that / ensuite / le même jour / juste après |
| conjunction | 18 | `And on Tuesday?`, `Et mardi ?`, `What about Sacha?`, `Et sur Minecraft ?` |
| continuation | 16 | tell me more / dis-m'en plus / pourquoi ? / go on |
| exclusion | 14 | what else / quoi d'autre / rien d'autre |
| partial_shift | 14 | one field replaced and the others inherited (`And what video did I watch then?`, `Et Sacha, il a dit quoi ?`) |

## Labelling conventions (what a human means, chosen for ambiguous cases)

1. **Self-contained versus elliptical.** A question is self-contained when it could open a new
   thread and mean the same thing. It has no anaphora (he, she, it, that, then, ça, lui, en,
   là-dessus), no leading `and` or `et`, and no `else`, `more` or `autre`. Every field of a
   self-contained question is `own` (stated) or `none` (not stated). Nothing is inherited,
   whatever its length or the kind of the earlier question.
2. **No time in a self-contained question after a dated question** means `time = none`. The
   question searches all history (`What did Sacha say about Elden Ring?` after a question about
   yesterday).
3. **Elliptical follow-ups inherit every field they do not state.** A field they state is
   `own`, even when its value equals the earlier one. A field with an empty earlier value is
   still labelled `inherit`, so the expected value is empty.
4. **Implied kinds.** A kind implied by the anaphora but not stated by a cue word is `inherit`.
   Examples: `What did he think of the new boss?`, `Je lui ai répondu quoi ?`,
   `Elle l'a vue où ?` and `What else did she mention?`. In the last two, `vue` is about seeing
   a trailer and `mention` is not a cue word. A kind word used as the topic is also
   `inherit`, as in `Dis-m'en plus sur cette vidéo.` after a video question.
5. **Continuation requests inherit all four fields.** Examples are `tell me more`,
   `dis-m'en plus`, `pourquoi ?`, `go on`, `and then?` and `et après ?`. Their `tell` and `dis`
   are not a request for conversations.
6. **Exclusion drops the topic.** `What else did he say?` and `quoi d'autre ?` give
   `keywords = none`, inherit people and time, and keep a stated kind as `own`.
7. **Time links inherit the earlier window.** This covers then, after that, ensuite, juste
   after, le même jour, pendant ce temps-là, at the same time and à ce moment-là. The planner has
   no "after X" operator, and time phrase resolution is a non-goal.
8. **Changing the kind after an earlier question drops that question's people and topic.**
   Examples are `And what video did I watch then?` and `Et j'ai écouté quelle musique ?`. The
   person and topic belonged to the earlier kind, so they are `none`. A link word or a leading
   `and` still makes time `inherit`. A deictic reference to the topic, as in
   `Did I watch a video about that the same day?`, makes keywords `inherit`.
9. **A different named person with a leading `and`/`et`/`what about` is a same-kind shift.**
   `What about Sacha?` and `Et Sacha, il a dit quoi ?` give `people = own`; the other fields
   are inherited.
10. **A pronoun plus a new topic is a topic shift.** `What did he say about the DLC?` and
    `Il a dit quoi about the loot ?` give `people = inherit`, `keywords = own` and
    `time = inherit`.
11. **`and when was that?` inherits all four fields**, as the repo test expects. Time is
    inherited even though the earlier question had none.

## Scoring rule (from harness.py)

S is the standalone `heuristic_plan` of the new question, E is the earlier question's plan
(also planned alone) and C is the candidate's value. Sets are compared for people, kinds and
keywords, and `(start_ms, end_ms)` for time. A field is correct when:

- gold `inherit`: C == E;
- gold `own`: C == S;
- gold `none`: C is empty or C == S. "Plan as if alone" is accepted, and so is "drop it".

A field where S == E is correct either way. The reported decision is `none` if C is empty,
`own` if C == S (including S == E), `inherit` if C == E, and `other` otherwise. Because a
`none` field may keep S, the baseline passes exclusion items whose standalone keywords are a
leftover word such as `else`. The exclusion category only tests that the earlier topic is not
inherited.

The accuracy budget (`accuracy_budget_ok`) requires every per-field accuracy to be at least
0.95 on all pairs and on held-out pairs, with 0 target errors and 0 exceptions. The rule cost
(`rule_cost_ok`) requires gate p99 at most 5 ms. Gate timing covers only `plan_thread`; the
harness computes both `heuristic_plan` calls before timing starts.
