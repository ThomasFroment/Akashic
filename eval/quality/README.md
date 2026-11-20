# Quality harness

Measures what Akashic records and derives on this PC: a scenario drives the real desktop (browsers,
Discord, a synthetic Direct3D game, Outer Wilds, played audio), an observer logs what the OS saw,
and a scorer compares Akashic's own disposable store with that truth. Design and metric
definitions: `docs/audits/2026-11-18-quality/scope.md`. Frozen research code like the rest of
`eval/`: outside the lint and pyright bar.

## Commands

```powershell
uv run python eval\quality\run.py smoke|standard|day|attended [--seed N] [--machine LABEL]
uv run python eval\quality\run.py score <run-id>
uv run python eval\quality\run.py coverage <run-id>     # exits non-zero, lists cells with n = 0
uv run python eval\quality\run.py compare <run-a> <run-b>
uv run python eval\quality\run.py restore               # after a crashed run
uv run python eval\quality\scenario.py standard --seed 21 --out scenario.json
uv run --with pyarrow==26.0.1 python eval\quality\assets.py fetch|build|verify
uv run python eval\quality\tags.py                      # change-tag self-test
```

Runs take focus, open windows on every monitor and play audio: start them only when the user is
away (the day run overnight, with the monitors dimmed over DDC/CI). `attended` needs the user: a
lock, a sleep (wake the PC), a default-output change and a UAC prompt.

## One-time steps per PC

1. Models home (the run never reads `%LOCALAPPDATA%\Akashic`):
   `uv run python scripts\fetch_models.py --data-dir eval\quality\local\models-home`
   (creates `models\` and `runtime\` there; each run's store links to them with junctions).
2. Assets: `assets.py fetch` then `build` (rebuilds `assets\`, the committed pack, at most 50 MB),
   or just `verify` when the pack is already committed.
3. `run.py smoke --machine <LABEL>` names this PC once; it is remembered in `local\machine.toml`.
4. Confirm the Steam launch arguments for Outer Wilds by hand once: `steam.exe -applaunch 753640
   -window-mode borderless -screen-fullscreen 1 -monitor 1` must open it borderless on the primary
   monitor (the game's own settings may override them: truth uses what the observer saw). The
   harness backs up and restores `graphics.owsett` and the PlayerPrefs key around the run.
5. VB-CABLE must be installed ("CABLE Input" render, "CABLE Output" capture).

## Environment

`uv run` (the repository's `.venv`: numpy, Pillow, av, comtypes, sqlcipher3, and scipy through librosa) plus `pyarrow` for
`assets.py`. Python 3.14, Windows 11, the NVIDIA driver. Browsers: Chrome, Edge, Zen. Also Discord,
Steam, Outer Wilds, VS Code, Windows Terminal. Nothing leaves the machine: the site and the
Akashic API stand-in bind to 127.0.0.1 only; the only downloads are the pinned public corpora.

## Layout

| Path | What |
| --- | --- |
| `profile.toml`, `coverage.toml` | the usage profile; 13 dimensions x 41 cases, core cases, impossible cells, deferred locations |
| `scenario.py` | seeded generator, profile fidelity check, coverage check |
| `assets.py`, `assets\` | corpus fetch, committed pack (`manifest.json`, Opus banks, sentence pool) |
| `site.py`, `site\` | local pages, report endpoint, videos with change tags, the vault |
| `tags.py` | change-tag encode (Python and the JS emitted into pages) and decode |
| `player.py`, `game.py` | audio player and synthetic D3D11 game, run under renamed interpreters |
| `observer.py` | OS log and guardrail sampler (imports nothing from `akashic.capture` or `akashic.platform`) |
| `driver.py`, `termfeed.py` | runs `scenario.json`; terminal line feed |
| `score.py` | metrics M1.1-M13.1, G1-G4, privacy scan, coverage, compare |
| `run.py`, `winapi.py`, `common.py` | CLI and Akashic host; Windows glue; shared helpers |
| `results\<run-id>\` | committed: `scenario.json`, `machine.json`, `truth\*.jsonl`, `scores.json`, `coverage.json`, `summary.md` |
| `local\` | gitignored: models home, stores, DPAPI passphrases, window and brightness backups |

`.gitattributes` keeps `eval/quality/**` byte-exact (`-text`): the scenario records the sha256 of
the profile, coverage and manifest as committed.

## Licences

LibriSpeech (OpenSLR 12) and MLS French (`facebook/multilingual_librispeech`, pinned to a dataset
revision) are CC BY 4.0; the pack holds re-encoded excerpts for evaluation only. SAPI clips are
generated locally from the installed Windows voices; music and game sound are synthesized.

## Limits and choices

- **`profile.toml` numbers.** Every leaf has a source. The gaming block carries the task's
  measurement (source "measured 2026-11-18 (1 h)"); the gaming share, the desk block, the language
  mix and the per-day events are assumed. Focus is by category: gaming has game, voice chat,
  browser and other (Outer Wilds counts as game, a quarter of the game time); desk has browser,
  editor, terminal, assistant, video, voice chat and other. The browser category rotates over
  Chrome, Edge and Zen, and "other" over Explorer, Notepad, Calculator, Steam and the hung window.
  Voice chat sits on the left monitor, the 9 % one of the gaming block. The desk run shape (40
  switches per hour, 30 s median, 600 s p90) is assumed. In a standard run the generator halves the
  desk p90 to 300 s (decision of 2026-11-18: a ~42 min block cannot hold 600 s with the category
  shares); `scenario.json` records the p90 per block kind (`run_p90_s`) and the fidelity check uses it.
- **Run lengths** are not iid: the median and the p90 are pinned by rank and a heavy tail takes the
  rest of the block (`run_lengths`). A short block cannot hold every figure at once, so the
  generator may use up to 13 % fewer switches and a 25 % lower p90 (both inside the tolerances).
  Standard is 65 min, the top of its range, for the same reason.
- **Fidelity** is checked on the generated timeline (retrying seed-derived attempts until the
  tolerances hold) and again by the scorer from the OS and player logs: foreground runs, audible
  spans, the language of the played utterances and the title changes the driver made.
- **Speech language** is drawn per utterance from `shared.language`; mixed turns are a splice of a
  whole English clip and a whole French clip (`mixed1`), so the transcript is exactly the audio.
  The halves come from two voices no other role uses (`spare_en2`, `spare_fr2`); the scorer counts
  each half as its own utterance with its own speaker.
- **Videos** play across the runs of one desk block (one per block; two in a standard run); a
  later run of the same video window only focuses it. Video pages share one look (English, dark,
  16 px); the last video of a standard or day run also enters Picture in Picture.
- **Resolution tags** follow the monitor role (primary 4K, sides 1440p) of the target PC.
- **Hung windows** never exceed 90 s: one `synthetic_window.py hang` process per step.
- **Approximations in the scorer:** M3.1 scores each shown line against its best-matching stored
  line or run of 2-3 consecutive stored lines joined, so a line OCR wrapped still matches (extra
  stored text is not an error); M1.1 decodes change tags from stored frames, with at most 6 000
  changes evaluated (more are sampled evenly up front, and n says how many); the frame decode is
  shared with the M10 pixel scan; M7.1 leakage counts seconds, not energy.
- **Truth the store could hold:** M6 and M11 clip each video play and game session to the first
  stored frame and the drive end, split it at the driver's Pauses, drop pieces under 60 s (video)
  or 120 s (game), and give each stored row to the piece it overlaps most; M6 reports stored rows
  per truth piece and start/end errors. M9.1 leaves out speech during a Pause (reported as
  `speech_paused`) and, when every segment names one speech app (Discord or a browser), scores
  overlapping speech per app; M8.3 takes the truth speaker from the segment's own app when that
  app spoke there. M4.1 scores URL and tab title only for focus steps whose app the OS log shows
  in front for more than half the step; the rest are counted as `steps_not_in_front`.
- **Speed:** edit distances use a bit-parallel Levenshtein (exact) and the best-line search skips
  lines whose length gap already exceeds the best ratio; frames decode on a thread pool.
- **Truth for coverage:** `coverage.json` counts a step only when the logs show it happened
  (focus honoured, play event, player start, event action), so a failed launch fills nothing.
- **Untested here:** the exclusive-fullscreen game, Outer Wilds launch, the attended events, the
  hung window launch and every real-app launch path were written without a desktop run.
- **Voice heard elsewhere** needs a render endpoint that is neither the default, CABLE Input nor
  a Wave Link virtual one; without it the cell is recorded as impossible on that machine.
- Day-run logs record changes only; the 20 MB bound for a day is checked after the first day run.

## Impossible cells

`coverage.toml` lists them with reasons: lock, sleep, device change and the elevated window are
"attended only (user answer 2026-11-18)" for the unattended lengths; pairs that do not apply
(for example 3 x Calculator: "the harness draws no text in this window") have their own reason.
A cell is filled when a truth item of its dimension overlaps a step tagged with its case. The
generator refuses a scenario in which an impossible cell is filled, and one in which a required cell
is empty, so every entry is a pair the harness never fills. By decision of 2026-11-18, two
dimensions are planted events and count only under the tags of their own step (`EVENT_DIMS`): privacy canaries (10: the excluded-page
step carries its browser's tag, so Chrome, Edge and Zen are required; a standard run has four pages,
a day three) and life-event loss (13: the event's own case). Pause, busy disk, canary and the
voice-elsewhere step are placed where they fill the most open cells.

## Deferred location

M10.1 names **export** as a location where canaries must be absent. The app shell, export
included, is a non-goal of this harness, so `scores.json` lists export as not scanned ("app shell
out of scope for this harness"). This is a gap, not a case that does not apply.
