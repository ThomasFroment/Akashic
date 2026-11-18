# Quality audit 2026-11-18: progress

Read this first when continuing on any PC. It records the phase, what is done, where the reports
are and the user's answers that bind the run. Commit and push only the audit files, the harness,
its assets, truth logs, scorer output and baselines at the end of each session.

## Phase

- Phase 0, scope: done 2026-11-18 (`scope.md`)
- Phase 1, harness `eval/quality/`: built 2026-11-18; smoke run twice on `target` (baseline
  `docs/baselines/2026-11-18-quality.md`); standard and day pending (overnight)
- Phase 2, audit: in progress (started 2026-11-18 after the smoke baseline)
- Phase 3, research: not started
- Phase 4, invent: not started
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
- `docs/research/conversational-speech-datasets-fr-en.md`: written by an agent during the Phase 1
  planning; public conversational corpora (SUMM-RE FR, AMI EN) as a later replacement for the
  read-speech bank. Input for Phase 3.

## Deferred

- Export as an M10.1 location: not scanned (the app shell is a non-goal of the harness). Listed in
  `scores.json` and `coverage.toml` as a gap, not as "does not apply".

## Next

- Phase 2 (`/frogg:audit`), with the smoke findings as leads.
- Overnight: `run.py standard` twice (same seed, 5 % check) and `run.py day`; add them to the baseline.

- Run smoke, then standard, with the user away; confirm the untested paths (exclusive game, Outer
  Wilds launch, hung window, real-app launches, wt.exe feed) and fix what a real drive shows.
- Day run overnight (monitors dimmed, shutdown after, by the user); attended run with the user
  present (lock, sleep, default-output change, UAC).
- Then write `docs\baselines\<date>-quality.md` and start Phase 2.
