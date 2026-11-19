# Quality audit 2026-11-18: progress

Read this first when continuing on any PC. It records the phase, what is done, where the reports
are and the user's answers that bind the run. Commit and push only the audit files, the harness,
its assets, truth logs, scorer output and baselines at the end of each session.

## Phase

- Phase 0, scope: done 2026-11-18 (`scope.md`)
- Phase 1, harness `eval/quality/`: built 2026-11-18; smoke run twice on `target`, standard twice and
  day once on 2026-11-19 (baseline `docs/baselines/2026-11-18-quality.md`); the standard pair ran
  on different harness versions, so a clean same-seed 5 % check still needs two runs
- Phase 2, audit: done 2026-11-18 (`audit.md`: #187-#219), plus architecture, creep and workspace
  dimensions 2026-11-19 (#220-#240); 0 refuted; all queued in TODO.md `## Audit 2026-11-18`,
  which is not committed: the user's commit rule
- Phase 3, research: done 2026-11-19 (8 reports in `docs/research/`, listed under Report paths)
- Phase 4, invent: started 2026-11-19. User's picks: invent the Phase 3 candidates (#208 small
  screen changes, #196 app time, #190 small-text OCR), games and episodes (#188, #192, game-row
  fragmentation, video episodes) and overlapping speech (overlap WER, #110). Worker GPU
  scheduling is not an invent item. Before any invent, diagnose the day-run bugs with read-only
  spikes on the saved stores: the canary in stored frames first, then capture VRAM growth and
  the Whisper load failure.
- Phase 5, synthesis and refuters: not started
- Phase 6, `quality.md`: not started

## User answers (2026-11-18, binding for this audit)

- Desktop access: runs that take focus, open windows on any monitor or play audio start only
  after the user says they are away (or overnight: black overlay on idle monitors, shut down at
  the end). Building and unit-testing the harness needs no access.
- Real captured data may be used: the harness's own disposable encrypted store may record the
  real desktop. Nothing captured is committed; only synthetic truth logs and number-only scores.
- Speech assets: public corpora with reference transcripts (MLS French, LibriSpeech; speakers
  recur by corpus id). Mixed FR/EN sentences are spliced from FR and EN clips. Eval-only.
- Voice call: corpus speech played by a copy of the harness interpreter renamed `Discord.exe`
  (Akashic sees a discord.exe audio session); the real Discord window may be opened and focused,
  never typed into. Same trick for the synthetic game's sound.
- Games: a synthetic Direct3D 11 game (comtypes) for borderless and exclusive fullscreen and frame
  times, plus the real **Outer Wilds** via Steam (menu or single-player save; its settings file
  is backed up and restored).
- Lock, sleep and audio-device change: attended only. Unattended runs list them impossible; they
  run in a session where the user is present.
- Disposable store: kept encrypted in the run's gitignored `local\`, its random passphrase
  wrapped with Windows DPAPI (user scope), so a run can be re-scored on the same account.
- Elevated window: attended only (a UAC prompt the user approves).
- Overnight day run: monitors dimmed to minimum brightness over DDC/CI and restored after (an
  overlay window would itself be captured); a monitor without DDC/CI stays lit.
- Desk p90 (the user let the agent choose): `profile.toml` keeps the user's 600 s; the
  generator scales p90 per length (standard about 300 s, day 600 s) and records the value used
  in `scenario.json`, because 40 switches/h with a 600 s p90 cannot fit the ~42 min standard
  desk block within the share tolerances.
- Coverage rule (the user let the agent choose): privacy (10) and life-event loss (13) items
  count only under their own step's tags; every other dimension counts by overlap.
- Mic: VB-CABLE is installed (not yet rebooted on 2026-11-18): test it before use. The harness's
  throwaway Akashic config points `mic_device` at the cable and plays the user's side into it.

## Machines

| Id | Monitors | GPU | Audio | Notes |
| --- | --- | --- | --- | --- |
| target (dev PC) | primary 3840x2160 (125%), right 2560x1440 at x=3840, left 2560x1440 at x=-2560 | RTX 4080 SUPER 16 GB | Wave:3 mic, DT-770 PRO default out, Wave Link virtual endpoints, VB-CABLE | Ryzen 7 7800X3D, 32 GB, Win 11 26200; OCR languages en-US and fr-FR (fr-FR installed by the user 2026-11-18, before the first run) |

## Done

- 2026-11-18: guides, TODO, OPEN, baselines read; user answers above.
- 2026-11-18: VB-CABLE checked before reboot: a -15 dBFS 1 kHz tone into "CABLE Input" came back
  on "CABLE Output" at -15.0 dBFS, about 55 ms later (winmm, 48 kHz mono 16-bit). Usable.
- 2026-11-18: Phase 0 `scope.md` written (metrics M1.1-M13.1, guardrails G1-G4, two added
  dimensions: index coverage and delay, life-event loss).

- 2026-11-18: Phase 1 built: `eval/quality/` (README there). Tested without the desktop: the
  scenario generator (all four lengths, byte-identical per seed, fidelity and coverage checks),
  change tags (lossless and WebP), the asset pack (24.7 MB, `assets.py verify`), the site and
  video build, the renamed-exe player (silent), the synthetic D3D11 game (borderless, small
  window), the observer, and the scorer on a hand-made store. Not run: any real drive.

- 2026-11-18: frogg:implement pipeline for Phase 1 (base 7d6d213): plan refuted twice, built,
  one full review (14 MUST), three fix rounds, a final full review (15 MUST), one more fix round.
  The items left open by the review loop (attended timing, call/silence spans, M3.3 dwell,
  player start time, cleanups) were fixed at the user's request and reviewed clean.
  `tests/test_eval_quality.py` (54 tests, no desktop) passes; full suite 3431 passed.
  Note: score.py imports scipy (DER Hungarian assignment); it is installed only through librosa
  and not declared in pyproject.toml.
  Not verified on the desktop: every driver path, exclusive re-entry after a focus loss, popups,
  private windows, Discord and Outer Wilds launch, click hit test, the scorer on a real store.

## Runs and early findings

- 2026-11-18 21:06 first smoke on the target PC: the drive, drain and score all completed
  (0 of 143 required cells missing). Harness fixes it led to (the run itself was discarded, its
  numbers came from the earlier harness):
  - renamed exes share one folder: a DLL already loaded by a running player can't be recopied;
  - side pages (shown beside the focused window, never focused) always loaded English, dark,
    16 px; they now get a look from the profile's language mix, and the scorer reads each page's
    look from the URL it loaded;
  - M7 compared Akashic's lower-case exe names with the observer's image names;
  - M1.1 counted 2 Hz game HUD ticks against Akashic's 60 s game-mode screenshot interval; changes
    now last until their surface changes again, only changes shown for the 5 s dwell count, and
    the game posts one text event every 10 s (assumed).
- Akashic finding (dimension 7, to confirm in Phase 2): the capture process's own loopback stream
  appears as an audible session on the recorded endpoint (observer peak 0.28), so every stored
  system segment lists `python.exe`, Akashic itself, among its sources. M7.1 now reports
  `segments_listing_akashic_itself` and leakage other than self apart.

- 2026-11-18 21:34 and 21:55 smoke pair (seed 11, discarded after the fixes below): 52 values
  differed by more than 5 %, almost all timings (job delays, frame-time max and p99, capture delay)
  or small-n splits (n = 1 media boundary, 3 term hits, 4-5 mixed lines); headline rates stayed
  within 5 %. Smoke is too short for the 5 % rule on small-n values; judge it on standard.
  French transcription WER about 0.6 vs 0.26 English in both runs: to explain before Phase 2.
  Fixes after it: the scorer skips %TEMP% files another program holds open (counted); windows are
  placed in the work area (tags were under the taskbar on the primary); VS Code starts with seeded
  settings (no welcome, walkthrough or AI sign-in prompts, telemetry off); Edge pages open in a
  `--guest` window (a fresh Edge profile signed in with the PC's Microsoft account and synced
  the synthetic test pages; its hidden Default profile still signs in, but holds no harness page).

- 2026-11-18 22:20 and 22:41 definitive smoke pair (seed 11): results committed in
  `eval/quality/results/20261118-smoke-s11-target-2220` and `-2241`, headline table in
  `docs/baselines/2026-11-18-quality.md`. Scorer fixes made while reading them (both runs
  re-scored): WER per overlap group with TTS clips as their own bucket (the midpoint rule turned a
  segment cut into hallucinations and French deletions); M11 video truth from the page's real
  play events.

- 2026-11-19 Phase 3 research, 8 items, each with in-memory spikes in `eval/quality/spikes/`
  (results committed beside each script):
  1. Small-text OCR: 3x Lanczos upscaling takes 9-10 px CER from 0.62-0.73 to about 0.02.
  2. Akashic's loopback capture is its own audible session; a pid check excludes it.
  3. Process loopback works (tree inclusion through the launcher); Communications-category
     streams stay silent.
  4. Game signals: QUNS reads 2 for a real game; GameConfigStore is clean but incomplete; Steam
     `RunningAppID` works.
  5. The 16 s media delay was a Pause artefact, not a defect: the scorer was fixed and the
     baseline corrected.
  6. Language choice was right on 32 of 32 segments; the French WER comes from a cut and from
     overlapping speech.
  7. Small changes: all 40 one-word replies and badges are missed. Candidate: a per-tile span of
     3x3 over four grids. Two refuter rounds narrowed it.
  8. App time: holding each session to the next one, bounded by the monitor's "unchanged"
     confirmations, takes holes from 116 / 110 s to 1.6 / 0.3 s.

  The item 7 WGC dirty-region spike opened a Tk window on the right monitor. It took the
  foreground despite WS_EX_NOACTIVATE + SW_SHOWNOACTIVATE. Do not open spike windows while the
  user is at the PC.

- 2026-11-19 night, standard and day runs (user away). Harness fixes found on the way; first
  real use of the paths smoke never exercised:
  - warm-up refused Akashic's `gaming` state (Outer Wilds is foreground at warm-up): accepted;
  - Zen: `descendants` dropped the launcher once it exited, so no Zen window was ever matched;
    a fresh profile opened Zen's welcome screen (`zen.welcome-screen.seen`, seeded in `user.js`);
    Zen's first window is a startup window replaced ~0.3 s later in the same process, so every
    focus relaunched a new instance (20 in standard 1, never closed): the driver now follows the
    window by pid;
  - Steam's windows belong to `steamwebhelper.exe`; Steam restores its friends chat window, now
    minimized at each layout;
  - Chrome and Edge offered to translate French pages: pages carry `translate="no"` and the
    `notranslate` meta, Chrome/Edge start with `--disable-features=Translate`;
  - `termfeed.py` crashed at start (str path): the terminal case never ran and its window was
    relaunched 14 times;
  - hung windows: the focus check compared against the hung window while Windows had put its
    "(Not Responding)" ghost in front: the ghost now counts as focus;
  - cleanup: Explorer folder windows, Discord, Steam and Calculator were left open; Edge and Zen
    kept 259 processes after their windows closed. The run records which apps ran before and
    closes the rest, then kills every process whose command line names the run's folders
    (Notepad and Explorer excepted);
  - the scorer crashed with exit 139 in the run's own process (native threads still alive);
    rescoring alone succeeded (55 min for standard), so scoring now runs in a fresh process.
  - Notepad: Windows 11 opens a second file as a tab of the running window, so the day run's
    `notepad@primary` and `notepad@right` never matched a new window; every step showing them
    (75 of 297 focus steps) retried the launch and waited 25 s, delaying the drive (lateness p90
    about 10 s, max 78 s at 1 h). Fixed after the day run started (the user asked not to restart):
    the driver takes the Notepad window showing the file.
  - Not fixed: Notepad restores the user's tabs and keeps the harness files as tabs afterwards.
- 2026-11-19 03:15 standard 1 (seed 21, `20261119-standard-s21-target-0315`): drive and drain
  complete, no Akashic restart; ran with the broken terminal feed and the Zen relaunches above,
  so its terminal, Zen and timing-sensitive cells are not comparable with later runs. 32 of 391
  required cells missing (pip, chat_growing, hung_ghost, uwp for dims 8-9, silence for dim 6).

- 2026-11-19 05:47 day run (seed 31, `20261119-day-s31-target-0547`): drive 05:52-12:57 complete;
  drain ended 14:38 without timing out; cleanup left no run process; scored 14:38-17:35; 0 of 391
  required cells missing. Headline numbers and new leads in the baseline.
  **Akashic finding, transcription stops for good:** the worker swapped Whisper and the
  llama-server (embeddings) about every 30 s: 134 Whisper load/unload cycles and 174 llama-server
  starts between 06:22 and 08:16. At 08:16:41 a Whisper load raised `ValueError` and every
  retry (each 10 min) failed the same way; 1,737 transcribe jobs were still pending 45 min into
  the drain, the last one done at 08:16. The same model loads in a fresh process at that moment,
  so the failure is in the worker's state; VRAM sat at 9.9 GB with no job running (2.2 GB before
  the night's runs): a leak across load cycles is the leading guess, unverified. The log keeps
  only the exception type. Phase 4/5 item.
  Why it swaps: `JobQueue.claim` takes the oldest job of any kind (`ORDER BY data_ms, id`), so
  transcribe and embed jobs alternate; `JobRunner._run` force-releases the other GPU model before
  each one (transcribe stops llama-server, embed unloads Whisper), treating the 0.64 GB
  embedding model like the 12B chat model; Whisper large-v3 is 3.1 GB. Candidates to measure:
  drain one kind before switching, keep the embedder resident beside Whisper (evict only for
  chat), restart the worker after repeated load failures.
  13:49: the worker was killed by hand (user's request to restart transcription); the supervisor
  restarted it, Whisper loaded at once and the backlog drained at about 38 jobs/min, still
  swapping. Day scores therefore include transcripts made after one manual worker restart.
  VRAM in use fell from 9.9 GB to 4.7 GB when the old worker died (same models, no game):
  supports the leak guess. No load failure in the first 27 reloads after the restart.
  Harness, during the day run: a game window shown beside another window between two game
  sessions waited 25 s for a game that was not running (fixed after); a popup `select` click and
  a hung-window focus missed (hung: old check, fixed for later runs).

- 2026-11-19 18:18 standard 2 (seed 21, `20261119-standard-s21-target-1818`): drive, drain (no
  worker restart, no Whisper load failure), cleanup (1 run process killed) and scoring complete;
  19 of 391 required cells missing: Calculator was matched to its inner `CalculatorApp.exe`
  window, which cannot take focus (driver fixed after the run), and one exclusive-game focus
  missed. Speech metrics repeat standard 1 within about 5-10 %; screen metrics differ because
  standard 1's harness was broken; the canary hit repeats. Comparison in the baseline.

- 2026-11-19 evening, Phase 4 bug diagnosis (read-only on the saved stores, plus two GPU spikes
  approved by the user; production code unchanged):
  - **Canary hits are a scorer false positive.** Every marker frame in both standard runs holds
    about 880 pixels of the marker colour inside a `steamwebhelper.exe` window (Steam's library
    art), 6-50 s before the vault page opened; none is the vault page or the vault window. The
    scorer now requires a solid square (100 monitor px; the canaries draw 300) instead of 400
    scattered pixels: the four standard-2 Steam frames no longer count, a half-size swatch still
    does. Day run: all 74 old-rule frames are the same Steam art (some also inside a harness Chrome
    page behind it), and 0 pass the solid-square rule. Privacy held in all three runs; the
    stored scores still say 1 hit until rescored.
  - **Whisper reloads leak about 8 MiB of VRAM each** (`spikes/whisper_reload_cycles.py`): 160
    load/transcribe/unload cycles in one process, as `pipeline/whisper.py` does them, never failed,
    but VRAM after each unload rose linearly from 237 to 1,543 MiB. The day worker's 134 reloads
    alone leak about 1.1 GB; the `ValueError` itself did not reproduce (it likely needs the
    other models and processes resident too). The swap thrash is the multiplier.
  - **Capture VRAM: each WGC capture start/stop leaks one frame-sized buffer**
    (`spikes/wgc_reopen_vram.py`, `WgcCaptures` on the desktop window, 8960x2160): 81.8 MiB per
    cycle once a frame has arrived, 0 when stopped before any frame; the same with frames never
    copied to a SoftwareBitmap, with the session closed before the pool, and with gc.collect();
    `WgcCaptures.close()` (device closed) frees only 85 of 829 MiB. In the day run, growth was
    10 times faster within 30 s of the 100 game-mode switches (2.4 GB in 45 min) than elsewhere
    (2.0 GB in 381 min), when per-window captures are stopped and restarted.

- 2026-11-19 Phase 4 invent 1 of 5, small screen changes (#208): done.
  `docs/solutions/2026-11-19 - Small Screen Change Detection/report.md`, artifact
  https://claude.ai/artifact/2CFUBV51hgt32UUJkikDvt. Best fit C5: today's test OR exact clusters
  of the moved points, minus regions that ticked at the previous checks, exact toggles capped,
  taskbar band as today. Synthetic: 0 of 442,368 word placements and 0 badges missed (today
  misses all), bar carets never fire, +0.03 ms. Real desktop 10 min (GDI grab, counts only):
  +5 primary / +9 right stores over today, unlabelled. C1, C4, C6 refuted; C2, C3 work but store
  spinners and block carets at every check. Not done: quality-harness M1.1/M1.2 with C5
  (needs a production change). Harness and candidates in `eval/small-screen-change-detection/`.

## Harness commands

```powershell
uv run python eval\quality\run.py smoke|standard|day|attended [--seed N] [--machine LABEL]
uv run python eval\quality\run.py score|coverage <run-id>
uv run python eval\quality\run.py compare <run-a> <run-b>
uv run python eval\quality\run.py restore          # after a crashed run
uv run --with pyarrow==26.0.1 python eval\quality\assets.py fetch|build|verify
```

Results go to `eval\quality\results\<run-id>\`: `scenario.json`, `machine.json`, `truth\*.jsonl`,
`scores.json`, `coverage.json`, `summary.md` (committed). Stores, DPAPI passphrases and backups
stay in `eval\quality\local\` (gitignored).

## One-time steps per PC (before the first run)

1. `uv run python scripts\fetch_models.py --data-dir eval\quality\local\models-home`
2. `assets.py fetch` and `build` (or `verify` when the pack is committed).
3. `run.py smoke --machine <LABEL>` (the label is remembered in `local\machine.toml`).
4. Confirm by hand that `steam.exe -applaunch 753640 -window-mode borderless -screen-fullscreen 1
   -monitor 1` opens Outer Wilds borderless on the primary monitor (the harness backs up and
   restores its graphics settings and PlayerPrefs key).

## Report paths

- `scope.md`: Phase 0 scope and metric definitions.
- `audit.md`: Phase 2 findings #187-#219.
- `docs/baselines/2026-11-18-quality.md`: smoke, standard 1 and day baseline.
- `docs/research/conversational-speech-datasets-fr-en.md`: written by an agent during the Phase 1
  planning; public conversational corpora (SUMM-RE FR, AMI EN) as a later replacement for the
  read-speech bank. Input for Phase 3.
- Phase 3 reports in `docs/research/`:
  - `windows-ocr-small-text-upscaling.md` (#190)
  - `wasapi-loopback-own-session.md` (#187)
  - `windows-process-loopback-capture.md` (A5 #110)
  - `windows-game-detection-signals.md` (#188, #192)
  - `browser-media-session-timing.md` (#189)
  - `whisper-language-choice-fr-en.md` (#139)
  - `screen-change-detection-small-text.md` (#208)
  - `app-usage-time-from-samples.md` (#196)

## Deferred

- Export as an M10.1 location: not scanned (the app shell is a non-goal of the harness). Listed in
  `scores.json` and `coverage.toml` as a gap, not as "does not apply".

## Next

- Phase 4 (`/frogg:invent` full, one per problem picked, `eval/quality/` named as the harness in
  Context). Phase 3 measured candidates for #190, #196 and #208. #187 needs only a pid check. #188,
  #110 and the overlap WER are open method problems.
- Optional: a clean standard pair (two `run.py standard` on the current harness, then
  `run.py compare`) for the 5 % rule; each needs the desktop about 1 h 25 min, then about 1 h of
  scoring.
- New Phase 4/5 items from the day run (baseline section "Standard and day"): transcription stops
  after repeated model swaps, capture VRAM growth, a canary in stored frames, game-row
  fragmentation, video-episode F1; harness: scorer speed, Notepad tabs left in the user's session.
- Untested until standard: exclusive-fullscreen re-entry, the Outer Wilds launch (confirm the Steam
  launch arguments once by hand), real-app launches beyond smoke's.
- Attended run with the user present (lock, sleep, default-output change, UAC).
