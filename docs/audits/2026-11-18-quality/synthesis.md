# Quality audit 2026-11-18: Phase 5 synthesis

Draft for Phase 6 (`quality.md`). It joins the Phase 2 findings ([audit.md](audit.md)), the Phase 3
research (`docs/research/`), the six Phase 4 invent studies (`docs/solutions/2026-11-19*`,
`2026-11-20*`) and the measured runs, rescored on 2026-11-20 with the fixed scorer
([baseline](../../baselines/2026-11-18-quality.md), last section). Production code is still at the
audit's base: every "after" number below comes from a replay, a spike or a synthetic set, never from
a run of changed Akashic code. Runs: smoke ×2 (5 min), standard ×2 (1.1 h), day ×1 (6.9 h), one PC.
Verified by two refuters and two fact-checkers (last section).

## Where the target questions stand

| Question shape | Limiting measurement today (rescored) | Main cause | Recommendation |
| :- | :- | :- | :- |
| Anything spoken ("what did Minlo say") on a long day | Day run: transcription stopped at 08:16 for 5.5 h; transcribe delay p50 9,950 s (standard 16-17 s); capture VRAM grows about 0.6 GB/h | Model swap on every job-kind change, VRAM leaks in Whisper reloads and capture restarts | R1, R2 |
| "What solo game did I play" | Game rows per truth piece 1 (standard) to 3.67 (day), Outer Wilds 1.5-7; game end error max 516-703 s; Outer Wilds missed after a Pause; game episode F1 0.29-0.30 | Rows close on focus loss and reopen (#197); covering-window rule (#188, #192); NPC voices make a solo game a call (#200) | R3 |
| "What video did I watch", "all the series" | Video episode F1 day 0.22; 15 media-row splits in the replays (11 from #189); title accuracy 0.52-0.67 | No viewing chains; title-change hold closes background video rows (#189) | R3 |
| Speech in a call while a video or game plays | Overlap WER 0.53-0.58 standard, 0.50 day, 0.64 smoke (clean 0.05-0.12 on standard and day) | One endpoint loopback mixes every app (#110) | R6 |
| Which app was on screen at a time | App-time holes 571-582 s (standard), 5,240 s (day); duration error 0.18-0.48 | Sessions end at their last stored frame (#196) | R5 |
| "Who said it", the people of a conversation | Episode people 14 / 16 / 27 distinct against 4 truth speakers; authors wrong / unknown 46 / 293 of 1,811 (day); DER 0.16-0.25; useless episodes 25-37 % | Speaker profiles fragment; segmentation merges a short second voice; window tail and 30 s seam leave turns unidentified | R9 |
| Names seen or heard ("the Minecraft Rust project", "Minlo") | Term hits in transcripts 0.50-0.75; screen CER ≤ 11 device px 0.23-0.52, 1440p 0.15-0.23; FTS recall of shown lines 0.68-0.83 | Unstudied vocabulary; native-size OCR of small and subpixel text (#190) | R8, R11 |
| A one-word chat reply or badge | Every one-word reply missed on synthetic frames; 131 of 356 missed at the first check on real windows | Global changed-pixel threshold (#208) | R7 |
| Search results for something seen often | 73-78 % of top-5 slots repeat one window | Text stored and indexed per frame (#101) | R10 |

Holding well: app label accuracy 0.98-0.99, focused-row accuracy 1.00, UIA text CER about 0.01,
clean transcription WER EN 0.05-0.11 and FR 0.08-0.11 on standard and day (FR 0.27-0.29 on smoke:
a cut utterance and overlap, `docs/research/whisper-language-choice-fr-en.md`), speech-tag accuracy
0.99, privacy (0 canary hits, 0 plaintext, 0 outbound connections on every run).

## Recommendations, by impact on the target questions

Each item: what to change, the evidence for it, its cost, and what is not shown yet. R1 and R2 come
first because without them a day of speech and capture does not survive; R3 serves three of the five
example questions.

### R1. Keep transcription alive through a long day (day-run defect, not studied in Phase 4)

- **Change, in this order:**
  1. Give the embedding server a small context (`ctx-size`; `EMBEDDING_FLAGS`,
     `src/akashic/pipeline/llm.py:60-65`, sets none, so llama-server takes the model's full
     context). It held 4.69 GB at p95 on every run with only the 0.6 B embedder loaded; a 32k-token
     KV cache for Qwen3-Embedding-0.6B is about 3.5 GiB, which fits. Passages are 400 characters.
  2. Stop evicting a model on every job-kind change: `JobQueue.claim` takes the oldest job of any
     kind (`src/akashic/storage/jobs.py:99-108`, `ORDER BY data_ms, id`) and `JobRunner._run`
     force-releases the other GPU model before each job (`src/akashic/pipeline/runner.py:114-126`).
     Drain one kind before switching, keeping query embeddings first (`data_ms` 0), or keep the
     shrunk embedder resident beside Whisper and evict only for chat.
  3. Restart the worker after repeated model-load failures.
- **Evidence:** day run, 134 Whisper loads and 174 llama-server starts in about 2 h, then a load
  raised `ValueError` at 08:16 and every retry failed; 1,737 transcribe jobs waited until a manual
  worker restart at 13:49; VRAM fell from 9.9 GB to 4.7 GB when the old worker died. A spike
  measured about 8 MiB of VRAM leaked per Whisper load/unload cycle (237 → 1,543 MiB over 160
  cycles) without reproducing the failure (`eval/quality/spikes/whisper_reload_cycles.py`).
- **Not shown:** the cause of the `ValueError` (the log keeps only the type); the embedder's VRAM at
  a small context (inferred from the model config, not measured). Acceptance: a day run with no
  manual restart and transcribe delay p50 near the standard runs' 16-17 s.

### R2. Bound capture VRAM growth (day-run defect, not studied in Phase 4)

- **Change:** keep per-window captures alive across game-mode switches instead of stopping them,
  and let the capture child exit past a VRAM or capture-restart threshold so the supervisor
  restarts it (it already restarts children on any exit, `src/akashic/supervisor.py`). Captures
  also restart when a window stops being visible or becomes excluded
  (`src/akashic/capture/desk.py`), so the switch fix alone does not stop the growth.
- **Evidence:** a spike leaks 81.8 MiB per Windows.Graphics.Capture start/stop once a frame has
  arrived (8960x2160 desktop window), 0 when stopped before any frame
  (`eval/quality/spikes/wgc_reopen_vram.py`). Capture VRAM p95 1.0 GB (standard) and 4.4 GB (day),
  about 0.6 GB/h; day growth 10x faster within 30 s of the 100 game-mode switches (2.4 GB in
  45 min) than elsewhere (2.0 GB in 381 min). Next to about 4.7 GB each for the worker and
  llama-server on a 16 GB card, this ends a long day.
- **Contradicted alternative:** recreating the D3D device. `WgcCaptures.close()` already closes it
  (`src/akashic/platform/win/wgc.py:226-237`) and frees only 85 of 829 MiB; a matching open issue
  in Microsoft's capture sample suspects the driver (https://github.com/robmikh/win32capturesample/issues/77).
- **Not shown:** the leak's cause; that restarting the child returns the memory (expected, as with
  the worker in R1).

### R3. Games and video episodes (#188, #192, #197, #200, #189): C1 of "Game Sessions And Episodes"

- **Change:** one game row per process instance, from creation to exit through a held
  `SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION` handle, time in front kept as a separate
  ledger, a provenance verdict that checks exclusions first (an excluded fullscreen app keeps game
  mode with a content-free row), video episodes from chains of same-title media rows, and a Pause
  splitting rows, game episodes and chains. When the handle is refused (protected or anti-cheat
  processes), C2: focus rows with identity reopen and an `IsWindow` liveness end. With it: #200
  (drop a conversation cluster whose system speech is all game-tagged and holds no mic segment), so
  a solo game's NPC voices are not people. The viewing chains (+0.4 µs) are independent and can
  ship first.
- **Evidence (replays of standard ×2 and day, truth clipped and split at Pauses as the fixed scorer
  now does):** rows per real session 1.25 (max 2), start and end error 0.0 s, 0 false games, 0
  missed, 0 excluded names written, episode F1 video 1.00 and game 1.00, titles 0.86; +21.8 µs per
  check at first sight of a process, 71.1 ms per day of changed derivation. On the report's older
  unclipped scoring the same rows read 1.54, end 698.9 s, game F1 0.57, titles 0.75. The rescored
  runs confirm today's problem: the synthetic game's row closes while the game runs (standard
  1818: end error 518 s), and Outer Wilds has no row after the Pause on both standard runs.
- **#189 inside R3:** 11 of the replays' 15 media-row splits are #189 close-and-reopen within 10 s
  on the same title; chains join them (gaps ≤ 180 s, the 60 s minimum on summed play), giving video
  F1 1.00. The audit's #189 fix (hold `url_unknown` on a live row, bounded by `FRESH_S`) is then
  only row continuity, which no target question needs and the timeline does not show (no media
  lanes): optional.
- **Not shown:** launchers the deny rule and the handle do not cover (Store and Electron games,
  rare per the user); a live run of the changed code.

### R4. Drop Akashic's own audio session from every source list (#187)

- **Change:** skip sessions whose `GetProcessId()` equals `os.getpid()` in `_audible`
  (`src/akashic/platform/win/audio.py:544-557`), with a unit test of `_audible` (#216).
- **Evidence:** Phase 3 spike: the capture process's loopback session is listed Active on the
  recorded endpoint with a peak equal to the mix; screenpipe filters by own pid
  (`docs/research/wasapi-loopback-own-session.md`). Today 257 (standard) and 2,074 (day) stored
  segments list Akashic itself, so music-only segments are never dropped (D7,
  `capture/audio.py:511`), attribution is never exact, and a conversation can be titled after
  `python.exe` (#202). The R6 prototypes already filter by pid.
- **Cost:** none measurable; first in line on cost, smaller in effect than R3. **Not shown:** the
  M7.1 rerun with the fix.

### R5. App time between frames (#196): C3 switch rows with C4's presence channels

- **Change:** store foreground switches as rows and use them as band edges (C3 of "App Time Between
  Frames"; capture already polls the foreground window every 0.1 s), and cut bands by C4's away
  span: every presence channel quiet for 180 s, back-dated, with keyboard/mouse idle stored as a
  new back-dated record, the band's own audible process with a changing picture, paired speech, or
  an open game session as presence.
- **Evidence (replays of all five runs):** C4 alone takes holes from 15-39 % to 0.3-4.0 % of app
  time and overlap from 55.5 s to 0, but barely moves the duration error (day 0.426 against 0.481);
  C3 reaches duration error 0.063 / 0.053 / 0.123 (standard ×2, day) with holes ≤ 0.18 %, and ≤ 0.37
  % with 10 % of switch rows dropped, at 10.9-12.9 ms per day and 2.4-3 KB/h. C3's only failure is
  away handling, which C4's channels fix (nine synthetic timelines: 0.5 s counted while away, 9.9 s
  present missed).
- **Not shown:** the combination; C3's figures are an upper bound (switch rows simulated from the
  truth source); away on real runs (the driver gives no input between focus steps).

### R6. Overlapping speech (#110): C1 + language split of "Overlapping Speech"

- **Change:** per-app process-loopback stems for up to three audible non-voice apps, the call as
  the endpoint loopback minus those stems (128-tap FIR), each stream segmented, transcribed and
  attributed on its own, system rows overlapping an excluded app's sound dropped; and a system row
  of 5 s or more with an unclear FR/EN choice cut at an energy dip and decoded per side (the user
  ruled the split in scope, 2026-11-20). Add the report's overlap-gated separation (C6) only if a
  real same-app need appears: it equals C1 on both runs (the gate fired 0 times), its gain is
  synthetic only (same-app cpWER 0.247 against 0.575), it adds 79-89 s of CPU per recorded hour,
  and its model is approved for measurement only.
- **Evidence (simulated per-process streams of the standard and day runs, measured for C6 + split):**
  overlap WER 0.639 → 0.223 (standard) and 0.497 → 0.156 (day); with Pause speech left out, as the
  fixed scorer does, 0.583 → 0.107 and 0.501 → 0.156; mixed clean 0.151 → 0.096 (day); DER 0.249 →
  0.148 and 0.161 → 0.089; excluded audio 0 s; GPU ×1.13-1.23 as a daily average (model), worst
  clock hour ×1.26; VRAM +0; capture CPU about 2 % of a core. Without the split, C1 fails the mixed
  day must (0.202 against 0.151).
- **Costs to watch:** wrong authors rise 6 → 19 (standard) and 46 → 71 (day) while DER and correct
  authors improve.
- **Not shown, and the first checks:** a real Discord call (Communications-category streams come
  back silent in process loopback on this PC, -96.7 dBFS; the design needs the call to stay in the
  residual; the harness call is category Other), then C1 + split run on its own (its equality with
  C6 + split is inferred from the gate counts).

### R7. Small screen changes (#208): C5fix of "Small Screen Change Detection"

- **Change:** today's test OR exact clusters of the moved pixels (≥ 3 wide and ≥ 2 high), with the
  previous check as a second reference that holds back regions changing at every check and caps
  exact toggles, and stores through the dense gate refreshing the last-move map (C5fix; plain C5
  adds 54 useless stores per hour in a busy phase, C5fix 8). The OCR gate uses the same test.
- **Evidence:** synthetic, 0 of 442,368 word placements and 0 badges missed (today misses all), a
  1-2 px bar caret never fires; real Edge, Chrome and Terminal windows at 1440p: 1 of 356 replies
  missed at the first check (today 131), 0 caret, clock, spinner or cursor stores per hour. Compare
  0.35-0.79 ms against today's 0.52-1.32 ms; 2.6 MB per 4K monitor.
- **Not shown:** harness M1.1 with it. Side monitors are checked every 15 s
  (`src/akashic/capture/screen.py:81`) and the live check used 5 s checks on one window, so M1.1
  all (0.59-0.72) will rise less than the reply numbers; accept on one-word reply and badge recall.

### R8. Small text OCR (#190): C2 for live ClearType strips, C1's scale for the rest

- **Change:** one OCR call on a mosaic of the window's line strips; strips under 16 device px are
  enlarged; ClearType strips are rebuilt from their R, G and B subpixels (C2); strips without
  subpixel phases (grayscale text, deferred OCR of stored WebP) use C1's budgeted scale instead of
  C2's luma fallback.
- **Evidence:** live WGC captures of real Edge, Chrome and Zen windows, ≤ 11 px at scale 1.0 0.53
  → 0.005-0.007 (Zen 0.517 → 0.002), 12-15 px ≤ 0.002; 1.44x OCR CPU, +50 ms p95 per call overall
  (at the budget limit). On the stored-WebP replays C2 reads ≤ 11 px at 0.485 / 0.398, C1's scale
  at 0.095 / 0.101 (standard / day).
- **Limits:** the live gain is almost all 9-10 CSS px text on the 100 % 1440p monitors (today 11 px
  reads 0.10, 12 px 0.03, ≤ 11 px on 4K 0.044); how much real text is that small is unmeasured;
  no gain in FTS recall is shown. #201 (lossy deferred OCR source) is not the 1440p cause: OCR of
  the stored WebP matches the live text within 0.01.

### R9. People and authorship (not studied in Phase 4)

- **Problem:** episodes list 14 / 16 / 27 distinct people against 4 truth speakers (standard ×2,
  day), 25-37 % of episodes match no truth activity or duplicate one, and the day run has 46 wrong
  and 293 unknown authors of 1,811 segments. This bears directly on "what did Minlo say".
- **Known causes:** wrong authors come mostly from segmentation merging a short second voice into
  the main one (24 of 38 in that study's C2); unknown authors from the zero-padded window tail
  (#204: authorship 0.464 at 0.5-2 s past a 10 s boundary, against 0.701 overall) and the 30 s
  continuation seam (0.422), `docs/solutions/2026-11-10 - Segment Speaker Attribution`. That
  report's C7 reaches authorship 0.927 but still fails its wrong-author limit (3.17 % against 1 %).
- **Recommendation:** an invent study of speaker-profile fragmentation and episode people on the
  quality runs, with #204's window change measured alone inside it. How many of the day's
  unknowns sit past a 10 s boundary was never measured.

### R10. Repeated screen text in search: merge per window run at query time first

- **Change:** merge keyword hits per window run (same hwnd, title and URL) at query time;
  `search/keyword.py:37-40` merges a run today only when the text is byte-identical. Then, if
  index size or the repeated embedding of unchanged windows (#203) needs it, C1 of "Screen Text
  Duplication And Noise" (lossless line store, one FTS document per window run, chrome and junk
  columns).
- **Evidence:** the study measured top-5 slots repeating a window 0.73-0.78 today against 0-0.06
  with C1, through today's keyword search; FTS recall and CER equal to today; misreads returnable
  538 / 7,654 / 44,363 → 89 / 448 / 3,121 while the misread share per document stays 0.92-1.00x;
  FTS index 0.05-0.22x (about 12 MB per day saved against 4.2-4.9 GB per day of store growth);
  write path +0, deferred 0.03 % of a core.
- **Not shown:** the query-time merge (inferred from the study's search path, not measured); Ask
  on either.

### R11. Names and terms in transcripts (not studied)

Term hits 0.50-0.75 (rescored: 0.60 / 0.50 standard, 0.75 day): the names in "Minlo" and "the
Minecraft Rust project" questions. No study is queued; the vocabulary path (`docs/SPEECH.md`) is
the place to start.

## Smaller fixes from the audit, no study needed

- #194 (reset the media manager after an exception: when it triggers, media logging stays dead
  until capture restarts), #191 (focused window: keep the longer of UIA and OCR), #193
  (default-output check 5 s → 1 s), #199, #202 (#170 after R4), #203 (embed batches).
- Storage: #198 (an index on `audio_segments.end_ms` for the 30 s conversation pass; the store
  keeps everything forever by default) and #206 (time side outer in `tab_frames`), both one-line;
  #205 bites only with a non-default text or transcript limit (every retention type defaults to
  forever, `src/akashic/config.py`).
- The tests, architecture and creep findings (#207-#240), all queued in TODO.md
  `## Audit 2026-11-18`.

## Measured, not yet studied

- **Store growth** 4.2-4.9 GB per day kept forever by default (about 1.5-1.8 TB a year); one real
  desktop hour stores 389 MB (9.3 GB per 24 h of activity). Studied 2026-11-20
  (`docs/solutions/2026-11-20 - Screenshot Store Growth`): key-relative tile deltas 0.54x on the
  real hour within every must, deferred SVT-AV1 compaction 0.36x at 954 s of worker CPU per hour.
- **URL identity** M4.1 0.69-0.92 on the first scoring; 0.86-0.96 once steps the browser never held
  are left out (2026-11-20, baseline last section): no wrong URL remains, only visits under 5 s.
- **Game borderless change recall** M1.1 0.04-0.12 (game mode stores one frame per 60 s by design).
- **Embedding delay** p50 209-319 s on standard, p90 1,417-1,589 s (#203).

## Checks still open

- A real Discord call with a video playing (R6's main condition).
- An attended run: lock, sleep, default-output change, UAC (M5.4 and M13.1 have only the Pause).
- A clean same-seed standard pair on the current harness for the 5 % rule.
- Exclusive-fullscreen re-entry and the real-app launch paths beyond smoke's.
- After each R item ships: the matching harness rerun (standard, then day).

## Scorer state

Fixed 2026-11-20: M3.1 wrap tolerance, M6/M11 truth clipped to the recorded span and split at
Pauses with one row per piece, M9.1 Pause speech left out and per-app overlap groups, M8.3 truth
speaker by app, M10.1 solid-square canary rule; scoring time standard 75 s, day 412 s. Remaining
approximations are listed in `eval/quality/README.md`.

## Verification (2026-11-20)

- **Fact-checkers:** 18 claims, 18 supported (code paths, run and spike figures, the six invent
  reports, #204, the media-delay artefact). Notes folded in: R3's figures are on clipped truth (the
  report's unclipped scoring is now quoted beside them); C7 fails its wrong-author limit; the
  "166 s" row end of standard 1818 is in the baseline's prose, consistent with the stored 518 s
  end error.
- **Refuters:** 13 items, 0 refuted, 2 stand (#187 own pid; games C1), 11 narrowed:
  - R1: the embedder holds 4.69 GB, not 0.64 GB (no `ctx-size`): shrink its context first; drain
    keeps query embeddings first.
  - R2 (was R10): moved up beside R1; device recreation is contradicted by `close()`; a capture
    restart past a threshold is the bounded fix.
  - R3: ranked first among the derivation fixes (three of five example questions); #189 and #200
    folded in.
  - R5: C4 alone barely moves the duration error; C3's switch rows plus C4's presence channels.
  - R6: C1 + split instead of C6 + split; separation only on a real same-app need; wrong authors
    rise.
  - R7: C5fix, not C5; M1.1 will rise less (side monitors at 15 s).
  - R8: C1's scale for non-ClearType and deferred strips; the gain is narrow.
  - R9 (new): episode people and useless episodes were missing; #204's tail explains unknown
    authors only.
  - R10: query-time merge per run first; "all the series" comes from media rows, not screen text.
  - R11 (new): term hits rescored 0.50-0.75.
  - Storage at scale moved to the smaller fixes (#205 never runs under the defaults).
