# Audit 2026-11-18: quality of recorded and derived data (base 1987a91)

Phase 2 of the [2026-11-18 quality audit](progress.md). Scope: what Akashic records and derives
([scope.md](scope.md)); Ask and answer quality are out. Reviewers worked from the code, the
guides and the measured smoke baseline ([2026-11-18-quality](../../baselines/2026-11-18-quality.md));
every MUST, every Approaches finding and every test deletion went to a refuter. Numbering continues
the earlier audits (#1-#186). Dimensions run: bugs per area (capture and its Windows glue,
pipeline, storage and evidence readers), tests, approaches. Architecture, creep and workspace were
not part of this request.

## Summary

| Dimension | MUST | SHOULD |
| :- | -: | -: |
| Capture and platform | 3 | 5 |
| Pipeline | 3 | 5 |
| Storage | 1 | 1 |
| Tests | 2 | 11 |
| Approaches | 0 | 2 |
| **Total** | **9** | **24** |

Refuted: 0 of 16 checked; 8 narrowed (fixes below are the narrowed ones).

Top 5 by impact on the target questions:

1. **#187 Akashic lists itself as the source of every system segment.** Its own loopback session is
   audible on the recorded endpoint, so attribution is never exact, music-only segments are never
   dropped (D7), and conversation titles can fall back to "Python conversation". Smoke: 17 of 17.
2. **#196 The timeline loses most of the time between stored frames.** An app session ends at its
   last stored frame, not where the next app starts: 110-116 s of 300 s are untyped holes, duration
   error 0.57; a game in front for minutes shows as instants (60 s game-mode frames).
3. **#197 Games split on every voice-chat visit, then vanish.** Each piece must reach 2 min alone;
   with 54 switches/h and short Discord visits, "what solo game did I play" loses real sessions.
4. **#189 A background video is split on routine navigation in the same browser.** A title-change
   hold makes the browser `url_unknown` for 0.5-3 s; a media poll in that window closes the row,
   and pieces under 60 s never become videos.
5. **#188 An excluded process that a monitor covers fullscreen is written to disk** as a
   `game_sessions` row with its process and window title, and its exe name reaches the log.

## Capture and platform

| # | Sev | Where | Problem | Fix |
| :- | :- | :- | :- | :- |
| 187 | MUST | `src/akashic/platform/win/audio.py:544` | `_audible` lists every active session above -60 dBFS, including capture's own loopback session on the recorded endpoint (its meter mirrors the endpoint: peak 0.281 against the game's 0.261 in smoke run 2220). Every system segment lists `python.exe`; `sources_exact` 0; `set(sources) <= MUSIC_PROCESSES` (`capture/audio.py:511`) can never hold, so songs are stored and transcribed against D7; the no-voice-app conversation title can become "Python conversation" (`pipeline/episodes.py:294-305`, a new trigger of #170). | Skip sessions whose `GetProcessId()` equals `os.getpid()` (`WindowsAudioSessions` runs in the capture process, `platform/win/__init__.py:50`). Add a unit test of `_audible` with fake session controls (#216). |
| 188 | MUST | `src/akashic/capture/screen.py:334-341, 603-605`, `capture/gaming.py:93` | Game detection runs on the raw focused window and never consults exclusions: a user-excluded process covering a monitor (borderless or D3D fullscreen, not in `not_games`) gets a `game_sessions` row with its process and free-text `window_title`, and its exe name goes to the plaintext log at :605. Not among SECURITY.md's accepted exceptions. | Keep game mode and the row (the scheduler holds GPU jobs on `gaming_now`, `storage/sessions.py:134-141`; game mode lowers capture rates), but write it content-free: a placeholder process and NULL `window_title` (`process` is NOT NULL, `0001_schema.sql:211`); `build_games` and `heard_tag` skip it; drop the name from the log line. Update `tests/test_gaming_and_sessions.py:313`, which asserts today's behaviour. |
| 189 | MUST | `src/akashic/capture/media.py:241-255` | A live browser media row survives `url_unknown` only after a lock or a verdict clear. Any title change of a kept browser window (tab switch, navigation, "(1) " prefix) holds it; `Verdicts._judged` publishes nothing while held (`verdicts.py:317`); `media_reason` returns `url_unknown` (`browser_state.py:92-94`); a 2 s poll in that 0.5-3 s window closes the background video's row and the next poll opens a new one. Pieces under `MIN_PLAYED_MS` (60 s) never become videos. | Treat `url_unknown` on a session with a live row as held whatever the lock or generation state (keep the row, open none), bounded by `FRESH_S`; any excluding verdict still closes it. Update `tests/test_capture_privacy.py:208` (its `excluded:url_unknown` count changes). |
| 190 | SHOULD | `src/akashic/platform/win/ocr.py:48-52`, `capture/ocr_gate.py:58-66` | Text reaches Windows OCR at native pixel size; the adapter only ever shrinks. Smoke CER 0.41-0.45 at ≤ 11 device px and 0.40-0.43 on 1440p monitors, against 0.02-0.04 at 4K (125 %). Text-Grab, a mature Windows.Media.Ocr app, scales every image to a ~40 px word height capped at `MaxImageDimension`. Also an Approaches finding. | Upscale (Lanczos or bicubic) by a factor from the monitor scale or estimated glyph height, capped at `max_image_dimension`, for small-text or low-DPI crops only. Measure with M3.1 split live vs deferred (the 1440p cells mix both). |
| 191 | SHOULD | `src/akashic/capture/screen_writer.py:350-352` | For the focused window, any UIA read of 40 characters or more is stored and OCR never runs; CAPTURE.md says the longer of UIA and OCR is kept. Interface-only Chromium trees, virtualized Electron lists and placeholder strings suppress the page text. | When the entry has an image, run the gate for the focused window too and keep the longer text (the existing `use_ocr` comparison without the early return). |
| 192 | SHOULD | `src/akashic/capture/gaming.py:25-62` | The borderless deny-list misses common caption-less fullscreen non-games: Steam Big Picture (`steamwebhelper.exe`), Windows Terminal and VS Code in F11, Photos, frameless Electron apps maximized over an auto-hidden taskbar. Each opens a game row, drops input triggers and moves its monitor to the 60 s rate with no UIA text. | Add them to `DEFAULT_NOT_GAMES`. |
| 193 | SHOULD | `src/akashic/capture/audio.py:76, 726-734` | The default output is re-checked every 5 s (`DEVICE_CHECK_S`): after a switch (headset plugged in for a call), up to 5 s of system audio plays on the new endpoint while the old, silent loopback is recorded. | 1 s; `find` is one enumerator call and a property read. |
| 194 | SHOULD | `src/akashic/platform/win/media.py:51-55` | The GSMTC session manager is requested once and cached; if `get_sessions()` starts raising, media logging stays dead until capture restarts. | Reset `self._manager = None` on any exception from `get_sessions()` before re-raising. |

## Pipeline

| # | Sev | Where | Problem | Fix |
| :- | :- | :- | :- | :- |
| 196 | MUST | `src/akashic/pipeline/sessions.py:53-75` | An app session ends at its own last stored frame; frames are stored only on change and every 60 s in game mode, so the time from that frame to the next app's first frame belongs to no app. Smoke: 110-116 of 300 s untyped holes, M5.3 error 0.57-0.59 (Code 0.39, Discord 0.40, Chrome 0.43, the game 1.0). No test covers a session across a gap (`tests/test_gaming_and_sessions.py:437`). Also an Approaches finding (zero-order hold). | Hold each focused observation until the next focused frame, bounded three ways: (a) at the start of a gap of kind `all` or `screen` only (`SCREEN_STOPPING_GAPS`, `storage/monitors.py:27`; audio drop rows must not cut sessions); (b) at the first frame of the focus monitor whose focus is on a hidden or shell window (they get no `focused=1` row, `capture/desk.py:265-268`), or a focus-left marker, so an excluded window's time is not given to the previous app; (c) `IDLE_GAP_MS`, also for the open session "until now". Add a test of a session across a lock gap. |
| 197 | MUST | `src/akashic/pipeline/episodes.py:219-243` | `build_games` judges each `game_sessions` row alone. Alt-tabbing to Discord for more than `end_grace_s` (5 s, `capture/gaming.py:68`) ends a row, so one play session becomes several rows; each must reach `MIN_GAME_MS` (2 min) alone, short pieces vanish, longer ones become duplicate episodes. (Smoke's game lasted 105 s, under 2 min, so smoke M11.1 alone cannot show the gain.) | Chain rows of the same process separated by at most `CLOSE_GAP_MS` into one episode (first start, last end, every row in `items`), apply `MIN_GAME_MS` to the chain, wait while the last end + `CLOSE_GAP_MS` > horizon, as `build_app_runs` does. Raising `end_grace_s` instead would keep game-mode throttling during Discord visits. |
| 198 | MUST | `src/akashic/pipeline/episodes.py:254-262, 124-129` | `_conversation_clusters` filters `end_ms > ?`, but `audio_segments` is indexed on `start_ms` only (`0001_schema.sql:251-255`): every 30 s housekeeping pass reads and decrypts the whole segment table, retention is forever by default, and the comment at :342-345 claims the opposite. `_segments` (`start_ms < ?`) reads every older segment for each media episode. | `CREATE INDEX audio_segments_end ON audio_segments (end_ms)` (schema edited in place per STORAGE.md until real data), or bound both queries on `start_ms > since - max_segment_ms`. |
| 199 | SHOULD | `src/akashic/pipeline/sessions.py:32` | `MAX(last_frame_id)` scans all of `app_sessions` every 30 s; the newest row already holds the watermark. | `SELECT last_frame_id FROM app_sessions ORDER BY id DESC LIMIT 1`. |
| 200 | SHOULD | `src/akashic/pipeline/episodes.py:268-276` | Game-tagged system speech counts as a call, so voiced dialogue in a solo game becomes "<Game> conversation" episodes with NPC voices as people (M11.4), and makes a solo game look multiplayer. | Drop a final cluster whose system segments are all tagged `game` and that holds no mic segment. |
| 201 | SHOULD | `src/akashic/capture/imaging.py:70`, `capture/screen_writer.py:356-357`, `pipeline/ocr_windows.py:69-70` | The whole-window capture kept for deferred OCR goes through lossy WebP q70 (4:2:0 chroma, quantized): smoke deferred CER 0.47-0.51 against live 0.19-0.22; `baselines/2026-11-15-window-composition.md:195-203` measured OCR recall 0.89-0.99 for WebP q70 against 1.00 for PNG level 1, which also encodes faster (41-70 ms vs 119-149 ms at 1440p). Confounder: deferred reads the whole window, live the crop. Also an Approaches finding. | Store the OCR-pending capture losslessly (PNG level 1, about 28x larger at 4K, temporary until the job runs); keep the stored screenshot as is. Separate the confounder by OCR-ing one capture raw and after decoding. |
| 202 | SHOULD (recurring #170) | `src/akashic/pipeline/episodes.py:294-305` | With no voice app the conversation app is `seg.sources[0]`; #187 makes that Akashic's own `python.exe`. | #170's fix (the overlapping game's process) after #187. |
| 203 | SHOULD (recurring #34, #37) | `src/akashic/pipeline/embed.py:59-91` | One 32-item embed batch per 30 s housekeeping pass: smoke embed delay p50 71-76 s, p90 405-412 s, max 785-813 s; unchanged windows are embedded again on every frame (M3.2 0.84, #101). | #37/#34: drain embed batches while no transcription can run, watermark per kind and model. |

## Storage

| # | Sev | Where | Problem | Fix |
| :- | :- | :- | :- | :- |
| 205 | MUST | `src/akashic/storage/retention.py:177-189, 227-239` | `expire_frame_text` and `expire_transcripts` batch `UPDATE … WHERE id IN (SELECT … WHERE ts < ? AND text IS NOT NULL LIMIT 500)` under `BEGIN IMMEDIATE`; cleared rows stay, so each batch re-walks from the oldest frame: O(N²/500) under the write lock, past the 5 s `busy_timeout` capture's writes rely on, so capture drops data. Trigger: a `frame_text` limit with screenshots kept forever. | Page by id as `_batches` does for media (`retention.py:76-90`): read the next 500 ids (`id > :last ORDER BY id LIMIT 500`) outside the lock, update them in a short transaction. |
| 206 | SHOULD | `src/akashic/storage/sessions.py:93-98` | `tab_frames` lets SQLite pick the process index as the outer loop (no ANALYZE statistics), so every call reads the browser's whole window history to keep the last ~3 min; `capture/media.py:_find_url` calls it every 5 s for 2 min per browser media session. | Force the time side outer (`FROM frames f CROSS JOIN frame_windows w …`), the #48 remedy; confirm with `EXPLAIN QUERY PLAN`. |

## Tests

| # | Sev | Where | Problem | Fix |
| :- | :- | :- | :- | :- |
| 207 | MUST | `tests/test_capture_ports.py` | The file checks the fakes and value shapes (field order, frozenness, a removed `Literal` member, `len(VOICE_APPS) == 15`, 8 `test_fake_*`), none of which a capture bug can fail; pyright strict already checks tests (`pyproject.toml:89`). One assertion guards real behaviour: every `VOICE_APPS` entry is lower-case `.exe` (line 227; process names are lower-cased and matched by set membership). | Delete the file except that invariant; move the four `_x: Port = Fake()` lines into `capture_fakes.py`. |
| 208 | MUST | `tests/test_capture_screen.py:625` | `imaging.differs` decides whether a monitor is stored again and whether OCR text is reused, but is only tested on a 300×100 block on 800×600. Thresholds need > 23 changed quarter-res pixels at 1440p and > 51 at 4K; a short static text line (a one-word reply) may never be stored (M1.1). | Parametrized test at 2560×1440 and 3840×2160: a short dark text line on white must differ, a 1-px caret blink must not; mark `known_defect` and fix the threshold if the line fails. |
| 209 | SHOULD | `tests/test_screen_writer.py:214, 255`, `tests/test_platform_win.py:791` | Shape and `is not None` tests add nothing pyright or behaviour tests do not cover. The "writer does not import the desk" check is not enforced anywhere else (`test_layout.py:69-84` covers only files outside `capture/` and `platform/`). | Delete the shape tests; move the import check into `test_layout.py` as an AST rule, or drop it on purpose. |
| 210 | SHOULD | `tests/test_layout.py:113, 142`, `tests/test_crypto.py:272`, `tests/test_config.py:63-65, 76-79`, `tests/test_capture_units.py:30, 40`, `tests/test_exclusions.py:128` | Guards of finished moves and removed names (#1). At `test_capture_units.py:30, 40` and `test_exclusions.py:128` the guard is one line inside a real behaviour test. | Delete whole tests only at `test_layout.py:113, 142`, `test_crypto.py:272`, `test_config.py:63-65, 76-79`; remove just the guard lines elsewhere. |
| 211 | SHOULD | `tests/test_capture_stop.py:305` | Sleeps 0.4 s once, then snapshots thread names; later threads start asynchronously. | `wait_for` the expected thread set (10 s), then record the daemon flags. |
| 212 | SHOULD | `tests/test_stop_request.py:200` | `BUDGET_S <= elapsed < BUDGET_S + 0.4` times a whole capture child with real threads and a held lock. | `< BUDGET_S + LOOP_DELAY_S`, which still separates the 2.5 s fresh budget. |
| 213 | SHOULD | `tests/test_capture_health_flows.py:129`, `tests/test_write_retry.py:682` | Each sleeps, then asserts a state that also holds if the code under test never ran. | Count open attempts in `FakeDevices.open` before raising and `wait_for(attempts >= 3)`; `wait_for` two `("stale", ())` calls. |
| 214 | SHOULD | `tests/hardware/test_real_hardware.py:161` | `WindowsOcr.recognize` block splitting and `render_blocks` order (sidebars, two columns) are untested. | A unit test with a fake recognize result carrying word rectangles, or a hardware two-column render. |
| 215 | SHOULD | `tests/test_platform_win.py:448` | `platform/win/media.py` `_read` (status mapping, `None` properties, a session closing mid-read) has no test; media boundaries (M6.1) start there. | A fake `Manager` covering each status, `None` properties and a raising session. |
| 216 | SHOULD | `tests/test_platform_win.py:345`, `tests/hardware/test_real_hardware.py:106` | Every audio-session test replaces `_audible`, so the rule behind M7 never runs (#187 went unseen). | Unit-test `_audible` with fake controls (state, peak, pid, own pid); in the hardware test play a clip and assert the player, and not capture itself, is audible. |
| 217 | SHOULD | `tests/test_capture_uia_backoff.py:320` | Only the critical-cost skip is tested; the 2/6/20 s tiers, interval expiry and recovery after cheap walks are not. | Table-test `UiaBudget.allow` at each threshold edge, at expiry and after 8 cheap records. |
| 218 | SHOULD | `tests/test_audio_wiring.py:127` | The real Silero tests check only that silence and noise score low; a VAD always returning 0 passes. | Add a few seconds of public-corpus speech under `tests/data` and assert most windows score above 0.5. |
| 219 | SHOULD | `tests/test_recording_readiness_e2e.py:13` and 7 others | Test modules import rigs and fixtures from other test modules. | Move them into helper modules beside `capture_fakes.py` and `screen_rig.py`. |

## Approaches

| # | Sev | Where | Problem | Fix |
| :- | :- | :- | :- | :- |
| 195 | SHOULD | `src/akashic/capture/compose.py:69`, `capture/imaging.py:47, 67` | Each changed canvas is shrunk by 4 twice per frame (RGB for `is_black`, again for the signature), on the capture thread (G1 p95 33 % of a core); a few ms per composite (signature 2.7-2.8 ms on a 41 % 4K crop). A gray-max shortcut would change the black verdict on dim saturated frames. | invent: one reduction per frame feeding both the black check (max over RGB) and the signature (`convert("L")` of the same reduced image), byte-identical verdicts. |
| 204 | SHOULD (recurring, TODO C4) | `src/akashic/pipeline/speakers.py:227-236` | Segmentation windows are laid end to end and the last is zero-padded; a 10.5 s segment gets a 0.5 s tail whose unidentified turn clears the author (report: authorship 0.464 at 0.5-2 s past a boundary vs 0.701). Nearest-centre ownership was measured only inside C7, never alone: alone it can split a speaker into two halves under `MIN_IDENTIFY_MS`. | invent: evenly spread full-length windows with frame ownership, measured alone with `eval/segment-speaker-attribution` before it ships outside C2/C7 (TODO C4 "Spread windows evenly"). |

#190, #196 and #201 are also Approaches findings (upscaling before OCR, zero-order hold, lossless
OCR source); they are listed once, in their bug dimension.

## Refuted

0. Narrowed by the refuters (fix rewritten above): #188 (keep game mode, write content-free),
#196 (three bounds), #204 (unmeasured alone), #195 (exact-equivalent reduction), #207 (keep the
lower-case invariant), #209 (import rule not enforced elsewhere), #210 (guard lines inside
behaviour tests), and the zero-order-hold Approaches item merged into #196.

## Queued

All 33 findings (#187-#219), at the user's choice (2026-11-18): TODO.md `## Audit 2026-11-18`,
#195 and #204 as `/frogg:invent` items.

## Open questions

- None from the reviewers. Whether a 82 s gap between two calls should give one conversation
  (`CLOSE_GAP_MS`, 5 min) is a Phase 4/5 question, not a defect.
