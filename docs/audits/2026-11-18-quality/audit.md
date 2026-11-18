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
| Architecture (2026-11-19) | 1 | 6 |
| Creep and redundancy (2026-11-19) | 6 | 8 |
| **Total** | **16** | **38** |

Refuted: 0 of 33 checked; 17 narrowed (fixes below are the narrowed ones). Workspace: a separate
table, not numbered (`/frogg:create-workspace`).

The architecture, creep and workspace dimensions were added on 2026-11-19 at the user's request,
under the user's rule: "Treat the app as a fresh one, no migration, no data to keep, no
compatibility code."

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

## Architecture (2026-11-19)

| # | Sev | Where | Problem | Fix |
| :- | :- | :- | :- | :- |
| 225 | MUST | `src/akashic/api/settings.py:502-516`, `storage/retention.py:419-445` | `_purge_category` rebuilds retention's per-category sequence, and the two copies have diverged: the API path never calls `expire_monitor_history`, against "monitor history follows the frame rows" (STORAGE.md). A per-kind `purge()` cannot hold the spent-row steps: `apply_retention` runs them once after the loop, only when both paired kinds have limits, at the smaller cutoff. | One helper both call, e.g. `delete_spent_screen(conn, cutoff)` = `delete_spent_frames` + `expire_monitor_history`; or add the monitor-history call to the API path. |
| 226 | SHOULD | `src/akashic/storage/debuginfo.py:21-26, 110-124` | Storage owns the app-shell verdict on what the tray and sidebar show (`TRAY_CAPTURE_RUNS`, `SIDEBAR_CAPTURE_RUNS`, `processing_attention`). Process-state strings have no single owner: `ipc.State` lacks "stopped" and "crashed", `supervisor._Child.state` is a plain `str`, more sets live in `api/server.py:90`, `app.py:62-79`, `tray.py:38-59`. | A typed `ProcessState` beside `ipc.State`; move `Processing`, `processing_status`, `processing_attention` and the run sets into an app-shell module (e.g. `akashic/status.py`); `debuginfo` keeps `ProcessInfo`. |
| 227 | SHOULD | `src/akashic/platform/win/ocr.py:17, 63-76`, `capture/ports.py:296-297` | The OCR adapter imports core capture logic (`ocr_gate.pick_language`, `text.render_blocks`), decides word grouping and returns rendered text (business rules in Windows glue). | `OcrEngine.recognize` returns `list[TextBlock]` and the installed languages; language choice, grouping and rendering move into `capture/`; `pipeline/ocr_windows.py` renders through the same helper; a `test_layout.py` rule: platform imports only `capture.ports`. Pairs with #190 and #214. |
| 228 | SHOULD | `src/akashic/api/server.py:105-111, 146-159, 406-428` | `Services` takes six optional collaborators and `Controls` optional hooks "for tests on other systems" (multi-OS, out of scope); production always passes them all (`app.py:231-264`). Routers mount conditionally, `None` guards follow (`settings.py:540-541, 638-647`, `debug.py:183-184`, `server.py:366-367`), handlers rebuild `Paths` from the config file (`stt.py:112-115`, `settings.py:614`). Also a creep finding. | Make them required, pass `Paths` once, register every router; tests build `Services` from one fixture (27 constructions in 20 files move); delete the `None` branches and `available` (`ui/settings.js:311`, `test_settings.py:375`). |
| 229 | SHOULD | `src/akashic/app.py:97-165`, `api/settings.py:433-447`, `api/stt.py:257` | Four shell helpers each call `load_config` with a different fallback; Settings wraps `parse_config` again; `api/stt.py` calls `load_config`, which writes a default file as a side effect. | One reader (`config.read(paths) -> Config | ConfigError`, no write) passed through `Services`; `load_config` only at process start. |
| 230 | SHOULD | `src/akashic/api/settings.py:488`, `capture/audio.py:835` | Settings reads its device list from capture's Debug payload (`process_info.capture.audio.available`), which `debuginfo` documents as a Debug aid. | Publish devices under a named key read by both, or document `process_info` in ARCHITECTURE.md as the capture-to-shell status channel with its keys. |
| 231 | SHOULD | `docs/ARCHITECTURE.md:24-25, 38` | Says `search/` is shared below the processes, but it imports `pipeline` (recurring #2); silent on api → pipeline, platform → capture (`ocr.py`), platform → storage (`audio.py` → `storage.opus`); omits `config_edit.py`, `fold.py`, `jsonish.py`; presents the storage-resident `processing_attention` as intended. | After #226-#229, list the real package edges and top-level helpers. |

## Creep and redundancy (2026-11-19)

| # | Sev | Where | Problem | Fix |
| :- | :- | :- | :- | :- |
| 220 | MUST | `src/akashic/config.py:339-346, 367-368` | `disk_warning_free_gb` and `[exclusions] window_titles` are accepted and ignored only so old config.toml files load, against the file's own rule (unknown keys are errors). A fresh folder gets `DEFAULT_CONFIG_TOML`, which has neither. | Remove both keys and comments; delete only the "accepted" and "survives a save" checks (`test_config.py:68-73, 82-87`, `test_settings.py:206-211, 221-226`) or turn them into "unknown key is an error"; keep the checks that neither is offered as a setting (`test_settings.py:203-205, 217-220`) and the default checks. Also drops TODO.md:605's "old configs still load". |
| 221 | MUST | `src/akashic/storage/db.py:172-192`, `app.py:337`, `docs/STORAGE.md:13-16`, `tests/test_db.py:115-155, 174-179`, `tests/test_export.py:181` | `StaleSchemaError` and `_refuse_stale_schema` refuse data folders of older schema edits: code kept for old data. The user confirmed (2026-11-19) that the old data folder was moved aside. | Delete the class, the check, the `except`, the STORAGE.md sentences and those tests. |
| 222 | MUST | `src/akashic/storage/db.py:30-42, 130-173`; `tests/test_db.py:75-79, 94-113, 206-208, 224-230`; `schema vN` displays (`app.py:224`, `child.py:218`, `api/debug.py:65`, `tests/test_processes.py:63`, `tests/test_debug.py:123`) | A full migration runner (filename regex, gap-free sequence, "newer than this app" refusal, injectable list) serves one schema file edited in place. Part C plans schema changes once real data exists (#129, #134, #137, #164). | User decision 2026-11-19: drop it now. One create step that runs the script and sets `user_version = 1` when it is 0, refusing any other version; keep the name `db.migrate` (about 60 call sites in tests and scripts); move the file to `storage/schema.sql`; keep `user_version` where `storage/export.py:292-295` copies it. Bring numbered migrations back with the first schema change after real recording starts. |
| 223 | MUST | `src/akashic/storage/migrations/0001_schema.sql:1`, `docs/STORAGE.md:12-13` | Line 1 says later changes go in new numbered files, with no time condition. | With #222: say "The whole schema; edited in place until real data exists, then numbered migrations come back" and align STORAGE.md. |
| 224 | MUST | `src/akashic/config.py:33, 117, 203, 383, 417`, `api/settings.py:154, 590`, `pipeline/main.py:207, 240`, `pipeline/models.py:521` | The speech-model key is `[audio] whisper_model`, commented as a "legacy key", though it also selects Qwen3-ASR and Canary. | Rename to `speech_model` (and `DEFAULT_SPEECH_MODEL`) everywhere, no alias; update `test_config.py:159-189`, `test_settings.py:235-263`, `test_models.py:350-359`, `tests/hardware/test_real_hardware.py`. |
| 232 | MUST | `TODO.md` #49, #51, #56, #73, #84, #85, #87, #89 | Done or obsolete items still unticked: #49 (`db.py:83-95`), #51 (`0001_schema.sql:80`), #56 (`retention.py:103-105`), #73 (`api/settings.py:531-532`), #84 (`conftest.py:75-80`), #85 (`test_network_guard.py:31`), #87 (`test_whisper_and_worker_body.py:265`); #89's targets no longer exist. | Tick them (#89 as obsolete); no OPEN.md leftovers. |
| 233 | SHOULD | `TODO.md` epics (lines 29-116), A2, A6 | Every box ticked; TODO.md's own rule says to drop them; their open points are already in OPEN.md. A8 is the gate to Part C (its last line and the references "before A8", "after A8"). | Drop the three epics, A2 and A6; keep A8's heading and its gate sentence, or move that sentence into the Part A intro and reword the references. |
| 234 | SHOULD | `docs/DECISIONS.md:46` | The #47 decision says "old segments lose meaning-search until re-embedded", which assumes data to keep. `TODO.md:648` (#108, "existing keyfiles must unlock first") stays: real recording and its export will create keyfiles before B11 is done. | Change the DECISIONS line with a dated edit, on the user's word only (DECISIONS.md rules). |
| 235 | SHOULD (recurring #210) | `tests/test_settings.py:332-333`, `tests/test_timeline_api.py:390-391`, `tests/test_db.py:82-91`, `tests/test_platform_win.py:46-50`, `tests/test_frames_storage.py:407-441`, `tests/test_ask_job.py:122` | More guards of removed routes, columns and methods, an "old format paths still read" test with no current writer, an "old plans remain readable" line. | Delete them with #210; at `test_db.py` and `test_ask_job.py:122` strip only the lines inside behaviour tests. |
| 236 | SHOULD | `src/akashic/capture/imaging.py:3`, `capture/ocr_gate.py:1`, `capture/media.py:1`, `capture/screen.py:78` | Comments cite "research §3.2", "§5", "§3.1" by number. | Drop the references or name the file and topic. |
| 237 | SHOULD | `src/akashic/pipeline/main.py:225-226`, `scripts/selftest.ps1:1` | Comments narrate a change ("no longer swaps them twice", "used to be manual"). | State the why only. |
| 238 | SHOULD (recurring #97) | `src/akashic/pipeline/main.py:205, 210` | `build_runner` builds two identical `MediaStore`s (as `app.py:225-226`). | One, shared, with #97. |
| 239 | SHOULD | `src/akashic/storage/retention.py:21-45` | Bullets for episodes, embeddings, gaps and monitor history sit after "Safety rules:" and read as rules. | Move them into the per-type list above it. |
| 240 | SHOULD | `pyproject.toml:70-72` | `TD002`, `TD003`, `FIX002` ignored, but `src`, `tests` and `scripts` hold no TODO/FIXME comment. | Drop the three ignores (any future TODO comment then fails the check, which fits tracking work in TODO.md). |

## Workspace (2026-11-19)

Read-only survey (`/frogg:create-workspace` § Survey and § Checklist). Not numbered and not
queued: apply with `/frogg:create-workspace`. The survey ran `uv audit --frozen`, which sends the
project's package names and versions to OSV (no captured data): no known vulnerability in 134
packages. TODO.md B12 asks for the user's OK before such an audit; it was not asked first.

| Item | State | Action |
| :- | :- | :- |
| GitHub visibility | public | AGENTS.md says the code holds screenpipe logic ported under a personal, non-commercial licence: decide whether the repo stays public. |
| Remote branch `claude/akashic-data-collection-audit-e91ad3` | unmerged | Holds `docs/audits/2026-11-17-data-collection.md` and `eval/data-collection-2026-11-17/`, not on main: merge (`/frogg:merge`) or delete. |
| Local branches and worktrees | stale | Five local branches fully merged, four with worktrees under `.claude/worktrees/` (one holds untracked `.epic-notes/`): remove on the user's yes. |
| `.gitignore` | missing groups | OS files, secrets, `.claude/settings.local.json` and `.claude/worktrees/` (ignored only in `.git/info/exclude`), `CLAUDE.local.md`, logs, `*.egg-info/` (TODO B12). |
| `.gitattributes` | missing | `* text=auto eol=lf` on top (the `-text` lines still win) and `binary` for `*.npy`, `*.npz`, `*.png`, `*.opus`, `*.pdf`, `*.webp`; renormalize in its own commit (TODO B12). |
| AGENTS.md | bloat, missing | 132 lines, 9 KB: move "What this is" product intent to a DESIGN.md or keep the split with DECISIONS.md on purpose; add `## Commands` / `## Dev loop` (check, selftest, hardware tests, test store, `eval/quality/run.py`); add `docs/research/` to the guides list; the commit rule repeats `frogg:commit`. |
| README.md, LICENSE | missing | License line and an all-rights-reserved LICENSE (TODO B12). |
| `.claude/settings.json` | missing | Shared `permissions.allow` for the check and test commands (TODO B12). |
| Dev loop | missing | No headless start/stop/status (`uv run akashic` needs the passphrase dialog and tray); document what exists before building one. |
| ISSUES.md | missing | No tracker; OPEN.md and TODO.md stand in: mark it deliberate or add ISSUES.md. |
| Machine tools | missing | `pdftoppm` (Poppler) not installed. |

#190, #196 and #201 are also Approaches findings (upscaling before OCR, zero-order hold, lossless
OCR source); they are listed once, in their bug dimension.

## Refuted

0. Narrowed by the refuters (fix rewritten above): #188 (keep game mode, write content-free),
#196 (three bounds), #204 (unmeasured alone), #195 (exact-equivalent reduction), #207 (keep the
lower-case invariant), #209 (import rule not enforced elsewhere), #210 (guard lines inside
behaviour tests), and the zero-order-hold Approaches item merged into #196.
2026-11-19 dimensions: 17 checked, 0 refuted, 9 narrowed: #220 (keep the not-offered and default
checks), #221 (only once the old data folder is moved: the user confirmed it), #222 (Part C needs
migrations again once real data exists: the user chose drop now, re-add later), #223 (STORAGE.md
already says it right), #225 (a per-kind purge cannot hold the spent-row steps), #233 (A8 is the
Part C gate), #234 (#108's keyfile check stays; the DECISIONS line needs the user's dated edit),
#235 (strip lines inside behaviour tests).

User answers 2026-11-19 (architect question): the old data folder was moved aside; the migration
runner is dropped now and numbered migrations come back with the first schema change after real
recording starts.

## Queued

All 33 findings (#187-#219), at the user's choice (2026-11-18): TODO.md `## Audit 2026-11-18`,
#195 and #204 as `/frogg:invent` items. All 21 findings of 2026-11-19 (#220-#240), at the user's
choice, in the same section. Workspace rows: `/frogg:create-workspace`.

## Open questions

- None from the reviewers. Whether a 82 s gap between two calls should give one conversation
  (`CLOSE_GAP_MS`, 5 min) is a Phase 4/5 question, not a defect.
