# Planner question sets

Six labelled question sets for the question planner (`src/akashic/search/planner.py` and
`timeparse.py`). They contain no user data: every question, name and topic is invented.
`scripts/eval_planner.py` scores today's code on them, and `tests/test_planner_baseline.py`
checks their hashes and holds today's accuracy as a ratchet floor.

## Provenance

The sets come from two method studies of 2026-11-10 (`frogg:invent`). Their reports are in
`docs/solutions/`: "Thread Follow-up Field Inheritance" (audit #175) and "Conversational Time
Windows" (audit #179-#181). Each record is copied unchanged from the studies' working folders
(only the CRLF line endings became LF); no record carried a field that pointed into those
folders, so nothing was dropped.

| file | study file | records | split(s) | written |
|---|---|---|---|---|
| `follow_ups_dev.jsonl` | `pairs.jsonl` | 170 pairs | `dev` 107, `heldout` 63 | by the study itself, as its development set |
| `follow_ups_heldout2.jsonl` | `heldout2.jsonl` | 110 pairs | `heldout2` | by an independent agent, after the candidates, without reading them or `pairs.jsonl` |
| `follow_ups_heldout3.jsonl` | `heldout3.jsonl` | 165 pairs | `heldout3` | by an independent agent, without reading any candidate, result or earlier row |
| `time_windows_dev.jsonl` | `phrases.jsonl` | 234 questions, 1,290 cases | `dev` 140, `heldout` 94 | meanings fixed by hand before running any resolver |
| `time_windows_heldout2.jsonl` | `heldout2.jsonl` | 145 questions, 795 cases | `heldout2` | by an independent agent, without reading any candidate or result |
| `time_windows_heldout3.jsonl` | `heldout3.jsonl` | 198 questions, 1,070 cases | `heldout3` | by an independent agent, without reading the earlier held-out set, any candidate or result |

Each set has one annotator and no inter-annotator check. The questions are synthetic; they
measure the conventions below on unseen wording, not daily-use accuracy. Each held-out set was
checked for collisions with the earlier sets after normalising case, accents and punctuation.

## Split discipline

- **Dev** (`split = dev` in the first file of each study) is what the study's candidates were
  tuned against.
- **Held-out** sets must stay untouched. Score them; never tune on them, and never edit a record
  to make a method pass. The studies scored each fresh set once, and that is what exposed the
  generalisation gap (a candidate at 1.000 on seen data fell to 0.90 on a fresh set).
- A new fresh set is a new file with a new split name, written without reading candidates or
  results. Do not move questions between files.
- `tests/test_planner_baseline.py` fails if a file's sha256 differs from the table below. A
  deliberate change to a set needs a new row here and a stated reason in the commit.

## Follow-up sets: what is labelled

Fixed clock 2026-11-10 14:00 Europe/Paris (a Tuesday). Each record is
`{id, lang, split, prev_question, question, category, gold: {people, kinds, keywords, time}}`;
the held-out files add `names`, the known speakers for that pair (default: Minlo, Sacha, Léa).
`lang` is the new question's language (`en`, `fr`, `mixed`). Each gold value is `own` (the
question states it), `inherit` (it comes from the earlier question) or `none` (not stated).

Scoring: S is `heuristic_plan` of the new question alone, E of the earlier question alone, C the
planner's answer. `inherit` needs C == E, `own` needs C == S, `none` needs C empty or C == S.
People, kinds and keywords compare as sets; time as `(start_ms, end_ms)`.

Labelling conventions, condensed:

1. A question is **self-contained** when it could open a new thread and mean the same thing: no
   anaphora (he, she, it, that, then, ça, lui, en, là-dessus), no leading `and`/`et`, no
   `else`/`more`/`autre`. Every field is then `own` or `none`; nothing is inherited.
2. A self-contained question with no time after a dated question has `time = none` (it
   searches all history).
3. An **elliptical** follow-up inherits every field it does not state; a stated field is `own`
   even when it equals the earlier value.
4. A kind implied by the anaphora but not stated by a cue word is `inherit`.
5. Continuations (`tell me more`, `dis-m'en plus`, `pourquoi ?`, `go on`, `and then?`)
   inherit all four fields; their `tell`/`dis` is not a request for conversations.
6. Exclusion (`what else`, `quoi d'autre`) drops the topic (`keywords = none`), inherits people
   and time, and keeps a stated kind.
7. Time links (then, after that, ensuite, le même jour, at the same time) inherit the window.
8. Changing the kind (`And what video did I watch then?`) drops the earlier people and topic;
   a link word or leading `and` still inherits time, a deictic topic still inherits keywords.
9. A different named person after `and`/`et`/`what about` is a same-kind shift: people `own`,
   the rest inherited.
10. A pronoun with a new topic (`What did he say about the DLC?`) inherits people and time and
    owns the keywords.
11. `and when was that?` inherits all four fields.

Cases the conventions leave ambiguous were left out of the held-out sets (their study READMEs
list them: bare day parts in a follow-up, a new person after a time link, `ben` as filler, and
others).

## Time-window sets: what is labelled

Each record is `{id, question, lang, category, split, meaning, main_time_text, connector,
labels: [{ref_date, now, now_label, gold}]}`; the held-out files add descriptive fields
(`connector_word`, `main_pos`, `final_no_time`, `title_case`, `anchor_order`,
`time_free_kind`). Every question is labelled at five "now" times of Tue 2026-11-10 (09:00,
14:00, 23:30, 00:40, 03:00); weekend meanings also on Mon 2026-11-09 and Sun 2026-11-08. `gold`
is `[start, end)` with a UTC+1 offset, or `null` when the question names no time.
`main_time_text` is the main anchor's time words copied exactly from the question (`""` when
there are none): the best case of what the model planner is asked to copy.

Scoring per case: pass when the window covers the gold span within 30 minutes on each edge and
is at most twice its length; for a null gold, pass when there is no window. A window starting at
or after now is a future window; a window for a null gold is a false filter.

Labelling conventions (persona: a night-owl gamer in Europe/Paris), condensed:

1. **Logical day** 05:00 to 05:00. Before 05:00 it is still the previous day: at Tue 00:40,
   "today/ce soir/tonight" mean Monday and "hier/yesterday/last night" mean Sunday.
2. **Parts of day**: morning 06-12; midi/lunch 12-14; afternoon/aprèm 12-18; evening/soir/
   tonight 18-02 (past midnight); FR night (nuit, la nuit dernière) 22-06; EN "last night" and
   "Saturday night" 18-06.
3. **Whole day** (hier, yesterday, avant-hier, a weekday, N days ago): D 05:00 to D+1 05:00.
4. **Today**: D 05:00 to now. In-progress periods are clipped to now; labels never hold the
   future.
5. "This <part>" not started yet in a past-tense question means the most recent such part.
6. **Clock times** ("vers 22h", "at 3pm", "à minuit", "at noon"): t ± 30 min, the most recent
   occurrence without a day word, on that logical day with one. FR "à midi" is lunch 12-14.
7. "Après minuit"/"past midnight": 00:00-06:00 of the current date. "Tout à l'heure"/"earlier":
   the last three hours.
8. **Relative**: "il y a N heures" = now - N h ± 30 min; "in the last hour" = [now - 1 h, now);
   "cette semaine" = Monday 05:00 to now; "last week" = the previous Monday-to-Monday.
9. **Weekdays**: the most recent occurrence strictly before the calendar date of now.
   **Weekend**: Saturday 05:00 to Monday 05:00; on a weekday, "ce week-end" means the one just
   past.
10. **Two anchors**: the label is the main clause's time; the time of the relative or "when"
    clause is not labelled.
11. **Time-free**: time words inside titles or names ("Night King", "Le Soir", "Midnight City",
    "fichier midi") are topics: gold `null`.

## Today's baseline (pre-audit code)

`scripts/eval_planner.py` reproduces both studies' "today" numbers exactly.

| set | measure | today |
|---|---|---|
| follow_ups_dev | people / kinds / keywords / time (lowest) | .818 / .929 / .700 / .965 (.700) |
| follow_ups_heldout2 | people / kinds / keywords / time (lowest) | .900 / .964 / .555 / .955 (.555) |
| follow_ups_heldout3 | people / kinds / keywords / time (lowest) | .830 / .952 / .618 / .927 (.618) |
| time_windows_dev | rules / model pass | .160 / .178 |
| time_windows_heldout2 | rules / model pass | .179 / .205 |
| time_windows_heldout3 | rules / model pass | .190 / .204 |

## Hashes

sha256 of each file's bytes with line endings normalised to `\n`:

| file | records | sha256 |
|---|---|---|
| `follow_ups_dev.jsonl` | 170 | `b96dd9f6b11ba554ff883de23be4e2a2c85273e1c726b6b7e3162b45a38683eb` |
| `follow_ups_heldout2.jsonl` | 110 | `6b2968ad5aa88826ab986d1509d4f073a89093e73b0dbd03a26ad67ab71e04b5` |
| `follow_ups_heldout3.jsonl` | 165 | `449df1b84dd8f12c710d75f66f461053e99414800ee46d8c45b49301b9c1b188` |
| `time_windows_dev.jsonl` | 234 | `1d597c37a58ce99c3d7b97edf7d67f85c0acd4533e139fefd1757580e9405a78` |
| `time_windows_heldout2.jsonl` | 145 | `4c041c8d5b9c866bd9e1ee2f732ac83e3b94663c973715413cc86aa85601db15` |
| `time_windows_heldout3.jsonl` | 198 | `4b9c10a2d614e9ff1f225b27348647e8997adc7762043aa14b23ea582a425f99` |
