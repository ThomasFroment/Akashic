# TODO

Upcoming work. Rules:
- One task per line, ≤ ~100 characters. Longer tasks split into sub-items.
- Complex features nest their steps as sub-checkboxes (2-space indent).
- List order inside a milestone = work order. `(after X)` names a dependency in another milestone.
- Tick a box when the work is done and verified, in the same commit as the work.
- Drop a milestone once every box in it is ticked and verified.
- Anything a ticked item leaves open (unrun checks, missing tests, deferred findings) goes in
  [OPEN.md](OPEN.md) under its milestone, in the same commit as the tick.

Sources: `#1`–`#112` are findings of [audit 2026-11-10](docs/audits/2026-11-10.md), `#113`–`#186` of
[audit 2026-11-10-2](docs/audits/2026-11-10-2.md). The [synthesis](docs/audits/2026-11-11-synthesis/synthesis.md)
explains them and their order (§n below); `docs/solutions/` holds the method reports (C1, C7… are
candidates of the named study). Read the finding's full row before starting it.
User decisions (D1–D9 and dated ones) are in [DECISIONS.md](docs/DECISIONS.md).

Every item keeps synthesis §7.7: exclusions before disk, everything encrypted, new tables and
columns covered by retention and "Delete all", no captured text in logs, no dependency without asking.

Three parts:
- **Part A, before the real dataset:** changes what is recorded (screen, audio, media, exclusions)
  or whether recording keeps going. Finish A, record daily use, then export it (A8).
- **Part B, any time:** needs no real data and does not change what is recorded. Deterministic
  defects, measurement tools, structure and speed, verified on synthetic fixtures.
- **Part C, needs the real dataset:** transcription, speaker identity and episode rules whose right
  behaviour depends on the user's own audio, screens and habits. Starts on the A8 export.

## Part A: collection quality, before the real dataset

### A1. Exclusions before disk

- [x] Password checks fail closed on every monitor
  - [x] #16 Treat the focused HWND as focused on every monitor; capture each HWND once per tick
  - [x] #17 Skip the frame when UIA errors in `focused_is_password`; count it
- [x] URL and site exclusions reach every path
  - [x] #18 Reject a browser's media session while its focused tab is URL-excluded
  - [x] #19 Unreadable address bar: match finance and user URL rules on the title, fail closed (D2)
  - [x] #20 Read the URL from the omnibox only (`OmniboxViewViews`), never an in-page input
  - [x] #26 Match built-in finance rules as host suffixes; keep globs for user rules
  - [x] #27 Exclude Akashic's own API origin in `check_url`
- [x] Process and title exclusions
  - [x] #24 Audio process lookup returns unknown on COM error; drop or flag uncovered segments
  - [x] #25 Resolve UWP CoreWindow PIDs; title patterns for every listed password manager
  - [x] #28 Reload AUMID names with backoff instead of caching a failed load for the process life
  - [x] #148 Private markers only as browser title suffix; manager globs only off-browser (D2)
- [x] Live check on Zen, Chrome, Edge (playing tabs, private windows, user URL rule) and Proton Pass
- [x] Unexamined background browser media is logged: accepted and documented (DECISIONS, 2026-11-12)
- [ ] Follow-ups found during A1
  - [ ] Ask the user: drop browser audio while a private or excluded page is open? (after A9)

### A2. Capture stops when asked

- [x] #60 Stop the API before the supervisor; a terminal `shutdown()` makes restart and poll no-ops
- [x] #163 Keep the paused flag across lock/unlock and app restart

### A3. Capture keeps running and keeps what it recorded

- [x] Screen loop survives faults
  - [x] #15 Game mode: clear due input triggers each tick; no busy-spin with no monitors
  - [x] #29 Per-iteration guard in screen `run`: log the type, count, continue
  - [x] #22 Short UIA connection and transaction timeouts (`IUIAutomation2`)
    - [x] `CUIAutomation8` + `IUIAutomation2`; 1 s connection, 2 s transaction
    - [x] Spike: time the unbudgeted readers on real browsers; set the transaction timeout from it
  - [x] #158 Skip only a hung HWND; store text-only rows on a None image; downscale > 16 MP
- [x] Audio loop survives faults
  - [x] #23 Catch any exception per recorder iteration: log the type, count, release, retry
  - [x] #160 Flush the open utterance on stop and device change; drop it only on pause
- [x] Writes survive a busy database
  - [x] #161 Retry capture writes with backoff; pop a media session only after its write
  - [x] #44 Index `audio_segments(continues_id)` so deletions don't block capture inserts
  - [x] #52 Name media files by UTC `ts_ms` so the DST fall-back can't overwrite a capture
- [x] #162 Capture reports `degraded` (audio off, every grab failing); UI shows Stopped/Restarting
- [x] #79 Test that an unpaused capture run writes a frame row and an `.enc` file
- [ ] Follow-ups found during A3
  - [ ] Guard `capture.close()` in `run_capture`'s finally so a DB error can't skip the joins
  - [ ] Clear `_game_id` when the game end-row write raises; leave the row to `close_stale`
  - [x] Per-HWND UIA backoff after a timeout; time-box the readers without a `UiaBudget`
  - [ ] Shorter `busy_timeout` once stop is set, so a busy database at stop fits the 10 s kill
  - [ ] One rate-limited, type-only fault log shared by the screen, audio, media and gap loops
  - [ ] Report dropped audio segments and failed frame writes as capture problems (with #143)
  - [ ] Keep UIA text on very large pages (filtered `read_text` on the 30k-element page: worst 1.9 s Edge, 1.8 s Chrome, against the 2 s timeout; target <= 666 ms)

### A4. Screen coverage

- [x] #144 Scroll capture uses the window under the cursor, skipping the focused HWND
- [x] #147 Game mode: idle captures of other monitors without UIA, OCR deferred; measure the cost
- [x] #150 Game detection: add common players, new Teams and RDP/Hyper-V clients; D3D exclusive fullscreen on a covered monitor marks a game
- [x] #21 UIA FindAll condition filters `IsOffscreen == False` before the 3000-node cut
- [x] #82 `FakeUia` returns `TextBlock`s; test the controls-only OCR fallback and `MAX_TEXT_CHARS`
- [ ] Lead: spike Chrome 138+ native UIA as the cause of the blank Edge probe (§9)
  - [x] Locked PC (2026-11-13): occlusion, not lazy UIA, blanks page text; URL and caption still read
  - [ ] Rerun unlocked, window foreground (scratchpad `chrome-uia/run.py`, not kept)

### A5. Audio recording

- [ ] #131 Keep a seeded continuation's short fresh speech after a 30 s cut; keep the quiet check
- [ ] #151 Attribute only the recorded endpoint's processes; log a gap when voice plays elsewhere
- [ ] #152 Music-app audio: drop songs, keep segments with evidence of real speech (D7)
- [ ] #110 Spike per-process loopback for the Discord process tree before building it
- [ ] Explore per-app audio streams (process loopback, include and exclude modes) (after #110)
  - [ ] Drop only an excluded app's sound, not the overlapping speech of other apps
  - [ ] Browser as its own stream, dropped while a private or excluded page is open (after A9)
  - [ ] Per-stream source for attribution (voice-chat stream = other people, browser = video)

### A6. Media sessions

- [ ] #167 Freeze `end_ms` while paused and close at `paused_since` (tracker side)
- [ ] #142 After a poll gap (suspend), close at `last_poll_ms`; close games when last seen
- [ ] #146 Close media rows only on Akashic pause, not on lock or UAC prompts
- [x] #145 `find_url`: same process, title prefix, skip search engines, no lock-in within 120 s
- [ ] Lead: spike several media sessions per browser (§9)

### A7. Capture gaps

- [x] #143 Record outages in a content-free `capture_gaps(kind, reason, start_ms, end_ms)`
  - [x] Pause, lock, missing or failed audio device, gated drops (screen drops: A9 states)
  - [x] OCR unavailable, capture process down
  - [x] Covered by retention and "Delete all" (after B4 #10)
- [ ] Shared overlapping-gaps reader (open rows end at heartbeat or now), with its first consumer
- [ ] Own retention key for gaps if "longest limit, only when every type has one" proves wrong

### A9. Monitor capture and timeline (before A8)

Design: [docs/designs/2026-11-14-monitor-capture.md](docs/designs/2026-11-14-monitor-capture.md).
Decisions of 2026-11-14 and 2026-11-15 in DECISIONS.md. Supersedes #78 (selected-window capture
test), #144 (window under the cursor), #153 (full-res copy for game OCR), #92 (`GdiScreen.grab`)
and the A4 rate-state follow-up; the A4 owned-popups follow-up is covered by the popup rules.
Spike results go to a dated report in `docs/baselines/`; guides keep only the rules. Spikes keep
captured pixels and titles in memory; reports hold numbers only.

- [x] Spike per-window composition and storage (baseline 2026-11-15 `window-composition.md`)
- [x] Ask the user: the WGC packages (`winrt` Graphics.Capture and DirectX projections: what they
  do, network calls, alternative) before building on them (approved 2026-11-15, TOOLING.md)
- [ ] Remaining spikes on the real PC
  - [ ] Popups not yet seen: password-manager extension popup, autofill and save-password
    bubbles, IME and emoji panels, credential prompts, WebView2 popups
  - [ ] Shell surfaces: taskbar-preview redaction, the snipping overlay outliving its window,
    notifications, elevated windows, the 1,292 marker pixels seen with PowerToys Command Palette
  - [ ] Title-change hold: same-title and in-page navigation, switching to a loaded tab, late
    titles, back/forward cache, Zen (20+ runs, title-change event hook)
  - [ ] Game frame times below the frame cap, exclusive fullscreen, DRM video, HDR output
  - [ ] A normal day: number of windows, capture VRAM, frame-copy CPU, composition time
  - [ ] Disk per normal day at native resolution (WebP vs AV1 NVENC), OCR of stored frames
  - [ ] OCR time of window areas on 1440p and 4K; decode time of several frames per moment
  - [ ] Caption and address reads for every visible browser; a hung side browser
  - [ ] Monitor identity across reboot and replug, incl. two identical monitors
- [ ] Ask the user: resolution, format and store drive, with disk per day
  - [ ] If the store leaves the system drive: Settings chooses the data folder; documented move
  - [ ] If resolution is capped: restore #153 (full-resolution copy for deferred game OCR)
- [ ] Ask the user: acceptable game frame-rate loss, with the spike's numbers
- [ ] Platform adapters (after the WGC package is approved)
  - [ ] Monitors with stable identity and layout
  - [ ] Per-window WGC captures: started after a kept verdict, stopped on exclusion or close,
    border off, minimum update interval
  - [ ] Visible windows with stacking order, cloaking, owner, constant alpha (skip alpha-0
    overlays), UWP hosted app, child windows of other processes
  - [ ] Window events (show, hide, move, reorder, title change) instead of polling
- [ ] Verdicts: process, private mode and readable address only (incl. Akashic's localhost port)
  - [ ] Remove all title exclusions (built-in, user list, media, own UI) and the password check
  - [ ] Zen decoded addresses with spaces parse as addresses, so address rules apply
  - [ ] Unreadable address kept; Settings and config lose the title list
  - [ ] Unknown app, unresolved UWP host or unreadable private state counts as excluded;
    re-judged at every check
  - [ ] PiP and DevTools take their browser window's verdict
  - [ ] Popups follow their owner; ownerless: excluded if its app has an excluded or unknown
    window, minimized or on another desktop included
  - [ ] Ownerless browser popups always excluded
  - [ ] Browser extension, autofill and save-password popups always excluded
  - [ ] Ghost windows take the hung window's verdict (`HungWindowFromGhostWindow`), else excluded
  - [ ] A child window of an excluded or unknown app excludes the window that contains it
  - [ ] Shell copy surfaces (Alt-Tab, Snap Assist, Task View, taskbar previews, snipping overlay)
    always painted over
  - [ ] Media: per-browser rule unchanged (excluding, unknown or stale), fed by every window
- [ ] Composition
  - [ ] Kept windows' latest frames in stacking order; excluded areas solid; pending until the
    first frame
  - [ ] Turning excluded: stop the capture, discard the last frame; back to kept: nothing older
    than the change is painted
  - [ ] Browser title change: pending until address and private state are read again (timeout:
    unknown)
  - [ ] Verdicts and UIA reads outside the composition lock
- [ ] What a frame keeps
  - [ ] Excluded windows: no text, absent from the window list; only "an area was hidden"
  - [ ] All-excluded monitor stores no frame, state hidden
  - [ ] Writer-spy tests: an excluded window's title, URL, text never reach SQL, files, logs
- [ ] Storage, schema edited in place, store recreated
  - [ ] Monitors and their layout over time
  - [ ] Windows per frame; the frame row keeps a copy of the focused window's app, title, URL
  - [ ] Per-window text rows
  - [ ] Per-window text keeps D3: expiry clears copied excerpts and answers that used it (#57)
  - [ ] States shown/hidden/dropped/failed, no reason; "not recording" from gaps (after A7 #143)
  - [ ] Retention and "Delete all" cover them (after B4 #10)
- [ ] Capture loop per monitor (after A3 per-HWND UIA backoff)
  - [ ] Rate and change detection kept per monitor identity
  - [ ] Compose only when a window on the monitor sent a frame or the window list changed
  - [ ] Change measured against the last stored picture; lost frames mark dropped or failed
  - [ ] Side monitors checked <= 30 s; every check refreshes media verdicts, even if dropped
  - [ ] Input also captures the monitor under the cursor
  - [ ] Game mode: game monitor at the game rate, others at the side rate, no UIA, OCR deferred
  - [ ] If one timed-out UIA call per HWND per 30 s still delays other monitors: per-HWND UIA thread
    - [ ] Spike first: comtypes UIA calls from several MTA threads (`WindowsUia` is one-thread today)
  - [ ] A slow or hung window on one monitor never delays another
  - [ ] Black monitor stores no image, only its window list and any focused-window UIA text
  - [ ] Encoding off the loop, bounded queue; full: drop the oldest side-monitor frame
  - [ ] Health per monitor: failure and drop runs; full disk reported as a problem
- [ ] Text: UIA for the focused window, OCR per visible window area, unchanged windows reuse text
  - [ ] Never OCR painted-over or pending areas, or the desktop
  - [ ] Deferred OCR (game mode) follows the same per-window rules
- [ ] Downstream reads the window list and per-window text
  - [ ] App sessions follow the focused window
  - [ ] Search, its app filter and passages per window
  - [ ] Ask app filters and "on screen in …" wording
  - [ ] Media URL lookup (after A6 #145)
  - [ ] Drop the frame row's copy of the focused window's app, title, URL
- [ ] Remove PrintWindow, its render thread, the 1920 px cap and the window-under-cursor path
  - [ ] Remove `GdiScreen.grab` (#92)
- [ ] Episode, search-hit and Ask images: the cited window's visible part, the rest masked
  - [ ] No image when the window was not visible on the frame
  - [ ] Thumbnail cache key includes the crop (after B4 #62)
  - [ ] Latest frame counts while its monitor stayed "shown" since and the window is on it
- [ ] Timeline (after B8 #63)
  - [ ] Stage at relative positions; focused monitor marked; hover lists windows
  - [ ] States drawn as such, with the age of the last check
  - [ ] Pre-sized stage images
  - [ ] Zoomable day strip with app bands; scroll, drag and keys
  - [ ] Day navigation; the app filter highlights bands, never hides monitors
  - [ ] Opening a moment: stage at that instant, cited monitor enlarged, window outlined
  - [ ] Each moment has an address; Ask and episode links and Back land on it
- [ ] Guides when A9 lands
  - [ ] CAPTURE, STORAGE, RETRIEVAL, UI, ARCHITECTURE take the design's lasting rules
  - [ ] SECURITY.md exceptions: titles, password fields, copies, notifications, background media
  - [ ] SECURITY.md: unreadable address always kept; drop "focus state" and media-title rules
  - [ ] DECISIONS.md: 2026-11-14 and 2026-11-15 rows to done; D2 and the background-media row
    updated
  - [ ] `tests/manual/CHECKLIST.md`: screen items 1-3, 5, 7 and gaming 3 for monitor capture
  - [ ] OPEN.md: drop #158, #144, the #147 cost line (the cost spike) and #162's hung-window check
  - [ ] Remove the "being replaced" notes at the top of CAPTURE.md and SECURITY.md
  - [ ] Delete the design file
- [ ] Hardware tests with `synthetic_window.py`: composition on mixed DPI and negative coordinates
- [ ] Manual checklist on three monitors
  - [ ] Private window on a side monitor; translucent window over a private one
  - [ ] Alt-Tab, Snap Assist, a taskbar preview and Win+Shift+S with a private window open
  - [ ] A game; a password-manager extension popup; minimizing a private window
  - [ ] Dropdown, suggestions and tooltip on a private page; a page navigating to a bank address
  - [ ] A hung private window; a private window on a side monitor cuts that browser's media
  - [ ] Bank page with a readable address redacted, kept while typing in its bar
  - [ ] A login page with a focused password field is kept

### A8. Dataset export (gate to Part C)

- [ ] Encrypted export for agent testing (DECISIONS, 2026-11-12)
  - [ ] An explicit user action copies the database and media, optionally by date range or category
  - [ ] Re-encrypted under a new passphrase; opens with `AKASHIC_DATA_DIR`; no plaintext temp files
  - [ ] Live store unchanged; writers stopped or a consistent snapshot; no partial export left

Then the user records daily use with Part A in place and exports it; Part C works on that export.

## Part B: any time, no real data needed

### B1. Trustworthy measurement

- [ ] Ask eval runs production's path
  - [ ] #113 Index through `EmbedHandler` (segment and frame passages) (after A9)
  - [ ] #113 Cases: both target questions in one thread, runs at 00:40 and 09:00, ≥ 8 videos
  - [ ] #113 `eval_hard_memory` embeds `semantic_query`, not the whole question
  - [ ] #124 One test from capture rows through `build_episodes` to the video answer
- [ ] Scorers accept only right answers
  - [ ] #114 Facts on word boundaries after removing `[n]`; reject alternatives seen in distractors
  - [ ] #115 Abstention: no cited in-window episode, anchored refusal, `forbid` on every question
  - [ ] #116 Score cited-episode precision; forbid other-day and other-kind titles
- [ ] Honest reporting
  - [ ] #118 Freeze a held-out set; report development and held-out apart; say so in README.md
  - [ ] #126 `--repeats N` and `--output` JSON; compare per question with one stored baseline
  - [ ] #127 `lang` per question, per-language results, mixed-language lines and questions
- [ ] Fixtures cover real failure shapes
  - [ ] #125 Noisy and low-confidence anchor lines, with questions that require a hedge
  - [ ] #123 Mixed, unattributed and mis-attributed speaker decoys in the Ask fixtures
  - [ ] #124 Content-free Debug counter: unbuilt sessions, oldest age, horizon age
- [ ] Promote the method studies' labelled sets and fixture builders from `eval/` into tests (§7.6)
  - [ ] 445 follow-up pairs and 577 time questions
  - [ ] 1,008-segment speaker fixture and the hallucination fixture
- [ ] STT comparison panel measures fairly (built now, used in C1)
  - [ ] #117 Empty reference, draft read-only beside it; record the draft model; WER with/without
  - [ ] #119 Count failures as deletions or compare common clips; required `condition` tag
  - [ ] #120 Accept `empty` segments and empty references; score inserted words as hallucinations
  - [ ] #121 Run candidates through production post-processing with a fixed previous text
  - [ ] #122 Normalize FR/EN numbers and hyphens on both sides; report CER and term hits
- [ ] Local speaker comparison (D6): label chosen clips, compare correct/wrong/unknown authors

### B2. Regression tests

- [ ] #75 Hardware test checks `SPEECH_CANDIDATES[DEFAULT_WHISPER_MODEL]`
- [ ] #76 Fake `torch`/`transformers`; test `make_transcriber`, `TransformerTranscriber`, log mute
- [ ] #77 "Survives errors" test: wait on predicates, assert alive before stop, state after resume
- [ ] #80 Replace fixed sleeps in the runner and scheduler tests with `wait_for(predicate)`
- [ ] #81 Memory API test uses the route's clock, not a pre-computed "today"
- [ ] #83 Test `_overall_state`, `_ask_blocker`, `autostart_command`, `GET /api/stt/recordings`
- [ ] #84 Delete duplicate `conn` fixtures; fixtures yield and close
- [ ] #85 Network-guard test asserts the UI folder exists instead of skipping
- [ ] #86 Game-session test asserts the row exists before checking its kind
- [ ] #87 Whisper/worker test also asserts the job is back to `pending`
- [ ] #88 Tighter scheduler smoke check; French OCR/SAPI case; fix references to deleted docs
- [ ] #89 Delete the two trivial tests and the unused import; fix the docstring

### B3. Processing never stops silently

- [ ] Missing models block nothing
  - [ ] #30 Run mic speaker jobs without models; queue none while the speaker models are absent
  - [ ] #31 Transcript horizon gates only media and conversations; games and app runs use the gap
  - [ ] #41 Remember model load failures and report them from `blocked()`; jobs stay pending
- [ ] Every queue state has an exit
  - [ ] #154 `release_running` fails jobs at `max_attempts` (`give_up`) and backs off the rest
  - [ ] #155 "Run waiting jobs now" also resets failed segments, so transcription reruns
  - [ ] #156 Retry `complete()`/`fail()` on `OperationalError`; release orphaned `running` jobs
  - [ ] #33 Embed give-up marks ids skipped (or retries one by one); purge old failed jobs
  - [ ] #40 Ask jobs get 1–2 attempts; no server restart within a run after a failed start
  - [ ] #42 STT comparison re-checks game and heavy gates between clips and requeues the rest
- [ ] Leaks and loops
  - [ ] #39 Catch `HTTPException`; terminate llama-server on any `_start` exception
  - [ ] #35 `unload_if_idle` tests `self._model is not None`
  - [ ] #56 Retention also catches `MediaPathError`, logs the type and skips the path
- [ ] #159 Housekeeping steps fail independently and publish per-step last-success times
- [ ] #143 Queue OCR for windows stored without text (after A9)

### B4. Storage integrity and deletion

- [x] #10 Test that every `sqlite_master` table is deleted by "Delete all" or on a keep-list
- [ ] #164 Rebuild `media_sessions`, `game_sessions`, `audio_segments` with AUTOINCREMENT
- [ ] #164 An ignored episode ref is skipped and logged, never a stalled watermark
- [ ] #49 `transaction()` rolls back only when in a transaction; rolls back and re-raises on commit
- [ ] #32 Episode builders honour the episode retention cutoff; expired episodes are not rebuilt
- [x] #62 Thumbnail cache keyed by `(frame_id, image_path, w)`; cleared on category deletes
- [ ] #55 Reject keyfile Argon2 parameters above sane ceilings before using them

### B5. Episodes are built and correct

- [ ] Identity-proven refactors first; port the `eval/` fuzz suites into `tests/` (after B3, B4)
  - [ ] #103 App-run single pass C1, byte-identical to today
  - [ ] #102 Conversation sweep C4 against its written contract; fixes #36 mic chaining
- [ ] Video episodes
  - [ ] #165 Store `process_for_app(app)` in `episodes.app`, display name in body; test Chrome/Edge
  - [ ] #166 Match the Edge soundtrack by exact executable through `process_for_app`
  - [ ] #167 Build video episodes from playing spans in `media_events` (after A6 #167)
  - [ ] #173 Build media episodes at close with a transcribe-only horizon; refresh the body later
  - [ ] #174 A body rebuild keeps the episode's own head line (channel)
- [ ] People and late evidence
  - [ ] #46 Merge re-points `episode_people` to the target; `reassign_turn` recomputes people
  - [ ] #169 Late evidence: recompute people, lower the watermark, rebuild bodies; re-run C4 replays
  - [ ] #178 Prefix digest utterances with "You:", the speaker's name or "someone:"
  - [ ] #3 One digest builder, in Python; drop the SQL view the deletion triggers use (2026-11-12)

### B6. Ask answers the target questions

- [ ] Follow-ups (#175), rules by default (D8)
  - [ ] Plan each question standalone, then carry fields with the follow-up C1 cue rules (D4)
  - [ ] Embed the planner's `semantic_query`, not "previous\nnew"
- [ ] Time windows (#179–#181), rules by default (D8)
  - [ ] Time C7 slot grammar on the 05:00 logical day (D1); review the 36 changed test expectations
  - [ ] RAM-only list of captured media titles vetoes time words inside titles (`PRAGMA data_version`)
  - [ ] Time C5 retrieval: keep lines by overlap; extend to a voice call crossing the window
  - [ ] #45 Read "il y a 2h" and "2h ago" as durations, never as a clock time
- [ ] Lists and evidence
  - [ ] #177 Topic-free questions: chronological order, "N of M", up to `MAX_OVERVIEW_EPISODES`
  - [ ] #176 With people, time or kinds set, append the remaining eligible lines, newest first
  - [ ] Flagged or unclear text never alone supports a factual answer (D9)
  - [ ] #143 List overlapping capture gaps in Ask's coverage line (after A7)
- [ ] Planner rules
  - [ ] #182 Topic keywords in French and English; prefix terms for words of 4+ characters
  - [ ] #183 Extend the stopwords; filter model keywords for cues, names and time words
  - [ ] #184 Tighten cues, add "maté"; the model may veto lower-case rule-matched names
  - [ ] #50 Strip the quoted name key once and use it for both the test and the lookup
- [ ] Answers and citations
  - [ ] #185 Sources give start and end; links open at the excerpt's segment and time
  - [ ] #186 Raise `max_tokens` and mark truncation; flag uncited answers as unsupported
  - [ ] #53 `episode_lines` drops empty and expired lines before applying the limit
  - [ ] #54 `replace(lower(x), '.exe', '')` everywhere

### B7. Vectors

- [ ] #47 Retire legacy per-segment vectors and their search path (2026-11-10)
- [ ] #100 Vector scan C3 columnar chunk with C1's DAZ/FTZ self-test; identity harness
- [ ] #101 Frame text reuse C3, re-evaluated for per-window text (after #100, B4 #10, A9)
- [ ] #104 Windowed episode vector scan C2 split overlap; identity harness
- [ ] #48 Check the windowed passage query with `EXPLAIN QUERY PLAN`; force the time side outer

### B8. Bounded background and API work

- [ ] #34 Watermark per (kind, model) for the missing-embedding queries
- [ ] #61 `/api/status` stays index-only; the app list comes from its own endpoint or a cache
- [ ] #13 A separate read-only connection for read endpoints
- [x] #63 Only `frames.get` under the lock; decrypt and thumbnail outside it (after B4 #62)
  - [x] Generation counter: `clear_thumbs` bumps it, `put_thumb` drops renders started before
- [ ] #64 Debug counts with `SELECT count(*) FROM frames WHERE ts_ms >= ?`
- [ ] #65 Log view cached by `(size, mtime_ns)`; parse appended bytes only
- [ ] #58 Cache the storage report; the worker refreshes it after retention
- [ ] #51 Text-change trigger fires only on text, title, URL updates (after A9: per-window rows)
- [ ] #37 Queue embed batches only when no transcription is runnable
- [ ] #38 Reset the CPU baseline after each job, or subtract the worker's own time
- [ ] Lead: NVML free-VRAM and free-RAM admission check in the scheduler (§9)

### B9. App shell and health

- [ ] #157 `/api/status`: worker state, blocked kinds, failed count, oldest pending, horizon age
- [ ] #157 Tray and sidebar show "needs attention" and "processing stopped"
- [ ] #67 Type-only `threading.excepthook`; guard the monitor loop body
- [ ] #68 Children catch `ConfigError`; exit with a distinct code the supervisor reports
- [ ] #69 Tray Pause/Resume runs on a short-lived thread
- [ ] #66 Model download thread catches everything and marks the remaining keys failed
- [ ] #70 Settings view uses a sequence token and skips stale builds
- [ ] #71 Ask polling retries with backoff and stops only on 404
- [ ] #72 `Services.close()` under `_lock`
- [ ] #73 Category deletion maps `OSError`/`CryptoError` like "Delete all"
- [ ] #74 `python -I` for autostart, `Akashic.cmd` and child spawns
- [ ] Remove the low-disk warning `disk_warning_free_gb` (2026-11-12); old configs still load
- [ ] Lead: isolated Edge `--app` window for the UI (§9) (after A9: no bar, so it is recorded)

### B10. Structure and clean-up

- [ ] Single sources of truth
  - [ ] #1 Move `pipeline/jobs.py` to `storage/jobs.py` (2026-11-10); layout test checks direction
  - [ ] #2 `query_vector` to `pipeline/query.py` or `api/`; `UNCLEAR_BELOW` to a neutral module
  - [ ] #4 `JobQueue` gains `retry_failed`, `replace`, `has_pending`, `oldest_pending`, `key_for`
  - [ ] #5 Handlers declare the resources they use; the runner idles conflicting ones
  - [ ] #6 Define job kinds once, next to the queue
  - [ ] #7 Add `storage/questions.py` and `storage/stt_samples.py`; HTTP handlers hold no SQL
  - [ ] #8 One bounds table for the Settings form and `parse_config`; `choices=LOG_LEVELS`
  - [ ] #9 Derive `RETENTION_TYPES` and the Settings rows from `PURGERS`
  - [ ] #11 One neutral `akashic/apps.py` for app identity
  - [ ] #12 Use `models.is_ready`/`is_complete` instead of `glob("*.gguf")`
  - [ ] #14 Regenerate the ARCHITECTURE.md layout from the tree; state the real dependency rules
- [ ] Dead code and stale docs
  - [ ] #91 Delete `change_passphrase` and its tests; reword SECURITY.md and STORAGE.md (2026-11-10)
  - [ ] #90 Drop `media_sessions_fts` and its triggers if still unread; keep `media_events` (user)
  - [ ] #93 Delete `_WORD_TOKEN`, `WHISPER_MODELS` and dead CSS; rewrite that test on `REGISTRY`
  - [ ] #94 Build `REGISTRY` from `SPEECH_CANDIDATES`; keep the GPT-OSS 20B and Qwen3 14B choices
  - [ ] #95 Debug uses settings, the storage report and `Paths.db_file`; no 50 GB warning
  - [ ] #96 Fix citations to absent docs and deleted manual tests; correct the CUDA/cuDNN claim
  - [ ] #97 Annotate `state: State`; build one `MediaStore`
  - [ ] #98 One `now_ms()`; import the shared clamp
  - [ ] #99 One disposable-store and embedding-server helper for the eval scripts
  - [x] `scripts/check.ps1` passes: ruff format, ruff check and strict pyright clean in tests
  - [ ] Remove the no-op `# pyright: ignore[reportPrivateUsage]` comments in tests (rule off there)

### B11. Dependencies and packaging (conditional)

- [ ] #107 Plain `onnxruntime`, drop the CUDA/cuDNN 13 wheels and override (ask; Whisper smoke test)
- [ ] #108 `cryptography` Argon2id instead of `argon2-cffi`; existing keyfiles must unlock first
- [ ] #109 Below-normal worker and llama-server priority, never capture (after C1 peaks)
- [ ] Lead: per-process GPU priority (`D3DKMTSetProcessSchedulingPriorityClass`) (§9)

### B12. Workspace (audit 2026-11-10 § Workspace, `/frogg:create-workspace`)

- [ ] `.gitignore`: OS files, secrets, `.claude/settings.local.json`, `.claude/worktrees/`, logs
- [ ] `.gitattributes` for `.ps1`/`.cmd` line endings and binaries
- [ ] AGENTS.md: measure the `@docs` import size; add commands and dev loop; drop dated history
- [ ] README.md: license line, no milestone history, dev command agrees with TOOLING.md
- [ ] Add LICENSE and `docs/changelogs/`; decide whether DESIGN.md adds anything to DECISIONS.md
- [ ] Shared `.claude/settings.json` permissions for the check and test commands
- [ ] Headless stop/status script, if needed
- [ ] Dependency audit with `uvx pip-audit` (sends package names to PyPI/OSV: ask first)
- [ ] Run `scripts/check.ps1` before every commit (habit or a git pre-commit hook)

## Part C: needs the real dataset (after A8)

### C1. Real-data references (after B1)

- [ ] #117 Type STT references from scratch on exported clips: FR, EN, mixed, conditions
- [ ] #123 Use the user's speaker turn corrections as attribution gold labels
- [ ] #124 Measure the delay from capture to searchable on the export
- [ ] Measure real RAM and VRAM peaks (§7.4; gates #105 and #109)

### C2. Hallucination gate (after C1)

- [ ] #129 Store `no_speech_prob` per segment; keep `LOG_PROB_THRESHOLD = -2.0`
- [ ] Hallucination C7 logistic gate: reject, unclear or keep; count unclear apart (D5)
- [ ] Check the gate on the real references; refit if the score distributions moved (§7.3)
- [ ] #128 Prompt-echo rule: a line mostly made of `initial_prompt` words is dropped or unclear
- [ ] #138 Strip known credit sentences at transcript edges; spoken outros stay flagged (D5)
- [ ] #141 Qwen and Canary return `exp(score)`; `None` confidence counts as unclear

### C3. Transcript continuity and vocabulary (after C2)

- [ ] #130 A continuation uses its `continues_id` segment's transcript as previous text
- [ ] #137 Overlap trim tolerates the half-cut word; record success in a `trimmed` column
- [ ] #140 Add named non-self speakers to the prompt vocabulary within `PROMPT_MAX_CHARS`
- [ ] #139 Short or unsure clips: decode both languages, or reuse the stream's last language
- [ ] #43 Constrain Qwen3-ASR to French and English
- [ ] Re-run the hallucination harness after each change above; refit the gate if needed (§7.3)

### C4. Speaker attribution, Route A (after C3 #137, B5 #169)

- [ ] #132 #133 Speakers C2 calibrated fusion plus the C9 seam check, using `trimmed`
- [ ] Spread windows evenly to bound C2's worst case on 10.3–10.5 s segments (from C7)
- [ ] Deferred authors (NULL until the continuation runs) reach episodes through #169
- [ ] #132 Fix the outdated speakers module docstring
- [ ] #134 Store the match score; update a profile only above threshold plus a runner-up margin
- [ ] #135 Create profiles only when a voice-chat or game process is audible
- [ ] #136 Check the fbank window against WeSpeaker's reference; add a feature-parity test
- [ ] Compare Route A with today on the user's labelled clips (D6 feature from B1)

### C5. Episode rules tuned on real use (after B5)

- [ ] #168 A video doesn't claim source-less audio when a voice app or game was audible
- [ ] #170 In-game voice chat takes the overlapping game's process as its app
- [ ] #171 Short videos: lower the 60 s minimum to 10–15 s, or keep clips played to the end
- [ ] #172 App runs chain on gap only, kind by dominant time; ties go to the first seen
- [ ] #149 Derive visible-window sessions from unfocused frames too (after A9)

### C6. Conditional extensions (spike or evidence first)

- [ ] Speech and speakers
  - [ ] #106 Word timestamps; each word goes to the most-overlapping turn; refit the gate after
  - [ ] Speakers Route B: C7 span statistics plus C9; derived ONNX output pinned by checksum
  - [ ] Route B: `OPENBLAS_NUM_THREADS=1` in the worker; re-run the vector scan harnesses (§7.5)
  - [ ] Stronger GPU segmentation (Sortformer, DiariZen) only if CPU-only speakers is reopened
  - [ ] #111 Compare Canary-1B-v2 and Cohere Transcribe on French-dominant real clips
  - [ ] Hallucination C3 CTC alignment, only if the last 3% outweighs VRAM and unclear flags
  - [ ] Lead: full-size French Whisper fine-tune (`bofenghuang/whisper-large-v3-french`) (§9)
- [ ] Ask, rules stay the default (D8)
  - [ ] Follow-up C6 relation tag, optional when the model is loaded
  - [ ] Time C9 grounded slots in the plan call, optional
  - [ ] #105 Qwen3-Reranker through llama-server, once peaks show room and retrieval is the gap
  - [ ] #112 Spike quote-then-answer: verbatim quotes checked in code; decide streaming first
  - [ ] Leads: Qwen3-Embedding-4B, tuned convex fusion, Qwen3.6-35B-A3B chat, French FTS (§9)
