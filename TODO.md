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
User decisions (D1, D3–D9 and dated ones) are in [DECISIONS.md](docs/DECISIONS.md).

Every item keeps synthesis §7.7: exclusions before disk, everything encrypted, new tables and
columns covered by retention and "Delete all", no captured text in logs, no dependency without asking.

Three parts:
- **Part A, before the real dataset:** changes what is recorded (screen, audio, media, exclusions)
  or whether recording keeps going. Finish A, record daily use, then export it (A8).
- **Part B, any time:** needs no real data and does not change what is recorded. Deterministic
  defects, measurement tools, structure and speed, verified on synthetic fixtures.
- **Part C, needs the real dataset:** transcription, speaker identity and episode rules whose right
  behaviour depends on the user's own audio, screens and habits. Starts on the A8 export.

## Audit 2026-11-18

Findings #187-#240 (#201 dropped, user 2026-11-21) of
[audit.md](docs/audits/2026-11-18-quality/audit.md) and fixes R1-R11 of
[quality.md](docs/audits/2026-11-18-quality/quality.md) (evidence and costs:
[synthesis.md](docs/audits/2026-11-18-quality/synthesis.md), `docs/solutions/2026-11-19*`,
`2026-11-20*`). Read the row before starting. List order = work order: dependencies, then impact on
the target questions; a step waiting on the user does not hold back the next independent item.
Inside this milestone, `(after X)` marks the items that must wait for X; the rest can go ahead. A
fix that changes what is recorded or derived ends with a harness rerun (`eval/quality/run.py`)
compared with the clean pair's new dated baseline
([2026-11-18](docs/baselines/2026-11-18-quality.md) stays the before-fix reference); tests,
structure and clean-up items run the tests and `scripts/check.ps1` only.

- [ ] Harness and baseline before the fixes
  - [ ] Driver focus steps that take the foreground (25 browser steps failed on the day run)
  - [ ] One-word chat reply and notification badge truth cases (R7's acceptance)
  - [ ] Driver input between focus steps, or presence-aware truth (R5's away span)
  - [ ] OCR time per call (R8's +50 ms p95 budget)
  - [ ] Clean same-seed standard pair on that harness (5 % rule; the user away)
- [ ] R1 Keep transcription alive through a long day
  - [ ] Embedding server gets a small `ctx-size` (`EMBEDDING_FLAGS`, `pipeline/llm.py`); measure VRAM
  - [ ] No model swap per job kind: embedder resident beside Whisper (evict for chat), or drain
    one kind with query embeddings first
  - [ ] #203 #37 Drain embed batches back to back while no transcription is runnable
  - [ ] #34 Watermark per (kind, model) for the missing-embedding queries
  - [ ] Restart the worker after repeated model-load failures
- [ ] R2 Bound capture VRAM growth
  - [ ] Keep per-window captures alive across game-mode switches
  - [ ] Capture child exits past a VRAM or capture-restart threshold; the supervisor restarts it
  - [ ] Day run after R1 and R2: no manual restart, transcribe p50 near 16-17 s, capture VRAM flat
- [ ] R9 People and authorship study (replays of the existing runs; no code dependency)
  - [ ] /frogg:invent speaker-profile fragmentation and episode people on the quality runs
    - [ ] With R6's simulated per-app streams (`eval/overlapping-speech/`): wrong authors rise
    - [ ] #204 Evenly spread speaker windows measured alone inside it (recurring C4)
- [ ] R11 /frogg:invent names and terms in transcripts (term hits 0.50-0.75; `docs/SPEECH.md`)
- [ ] R4 Akashic's own audio session out of every source list
  - [ ] #216 Test `_audible` (state, peak, pid, own pid); hardware test plays a clip
  - [ ] #187 `_audible` skips Akashic's own pid
  - [ ] #202 #170 A conversation with no voice app (in-game chat) takes the overlapping game's process
  - [ ] Standard run: M7.1 segments listing Akashic itself 0; conversation apps
- [ ] Fresh app, no compatibility code (2026-11-19; before R3, R5, R6 change the schema)
  - [ ] #221 Delete `StaleSchemaError`, its catch, STORAGE.md sentences and tests
  - [ ] #222 #223 Plain schema create (`db.migrate` kept), `storage/schema.sql`, docs aligned
    - [ ] `eval/app-time-between-frames` harness off `db.load_migrations`
  - [ ] #220 Old config keys become unknown-key errors; keep the not-offered and default checks
  - [ ] #224 Rename `audio.whisper_model` to `speech_model` everywhere, no alias
- [ ] R3 Games and videos (C1 of "Game Sessions And Episodes")
  - [ ] #215 Test `platform/win/media.py` `_read`
  - [ ] #194 Reset the GSMTC manager after a `get_sessions()` failure
  - [ ] Video episodes from chains of same-title media rows (gaps ≤ 180 s, 60 s on summed play)
  - [ ] #197 One game row per process instance (held process handle); time in front as a ledger
  - [ ] #188 Provenance verdict, exclusions first: game mode kept, row content-free, no name logged
  - [ ] #192 Big Picture, F11 Terminal/VS Code, Photos, frameless Electron are not games
  - [ ] Handle refused (protected, anti-cheat): C2 focus rows with identity reopen, `IsWindow` end
  - [ ] A Pause splits game rows, game episodes and viewing chains
  - [ ] `gaming_now` and game mode follow the in-front ledger (`last_front_ms`)
  - [ ] App runs drop only the game's own process visits
  - [ ] Game name: Steam appmanifest, then `ProductName` if `OriginalFilename` matches, then title
  - [ ] #200 Drop game-only conversation clusters with no mic segment
  - [ ] #189 Media row held through `url_unknown` while live, bounded by `FRESH_S`
  - [ ] Standard and day runs: game rows per session ≤ 1.5, end error ≤ 15 s, 0 false games
    and excluded names, episode F1 video and game and titles ≥ 0.8
- [ ] R5 App time between frames (C3 + C4 of "App Time Between Frames"; after R3's game rows)
  - [ ] #196 Foreground switches stored as rows end app bands; cut at screen/all gaps, hidden focus
  - [ ] Keyboard and mouse idle stored as a back-dated record
  - [ ] Away span: every presence channel quiet 180 s, back-dated, cuts bands
  - [ ] A game counts as presence only while in front (R3's ledger), not until its process exits
  - [ ] #196 Test a session across a lock gap
  - [ ] #199 App-session watermark from the newest row
  - [ ] Standard and day runs: holes ≤ 5 % of app time, duration error below today's on each run
- [ ] R6 Overlapping speech (C1 + language split of "Overlapping Speech"; after R4)
  - [ ] #193 `DEVICE_CHECK_S` 1 s
  - [ ] #218 Silero VAD scores public speech above 0.5
  - [ ] System rows ≥ 5 s with an unclear FR/EN choice: cut at an energy dip, decode per side
  - [ ] Standard and day runs: mixed WER
  - [ ] #110 Real Discord call with a video playing: the call stays in the endpoint residual (user)
    - [x] Synthetic tone tree: activation works, all-zero capture, undecided
      (`docs/baselines/2026-11-17-process-loopback.md`)
  - [ ] Per-app process-loopback streams for up to 3 audible non-voice apps, the call as the rest
    (after the #110 call)
    - [ ] Schema: audio streams beyond `mic` and `system`
    - [ ] An excluded app's stream is never kept; any system row overlapping its sound within 1 s
      is dropped (strict rule; keeping other apps' speech stored 33.2 s of excluded audio)
    - [ ] Browser as its own stream, dropped while a private or excluded page is open (after A9;
      asked under [A1](#a1-exclusions-before-disk))
    - [ ] Per-stream source for attribution (voice-chat stream = other people, browser = video)
  - [ ] Harness scorer groups overlap by (stream, source), not `stream = 'system'` only
  - [ ] Standard and day runs: overlap WER, DER; watch wrong authors (6 → 19, 46 → 71)
- [ ] R7 Small screen changes (C5fix of "Small Screen Change Detection")
  - [ ] #208 `imaging.differs` at 1440p and 4K: short text line differs, caret blink does not
  - [ ] Exact clusters of moved pixels, minus regions that change at every check; OCR gate too
  - [ ] Standard run: one-word reply and badge recall (side monitors checked every 15 s)
  - [ ] /frogg:invent one image reduction per frame for black check and signature (#195)
  - [ ] #195 fix: the study's chosen candidate; same verdicts, tests and `scripts/check.ps1`
- [ ] R8 Small text OCR (C2 + C1's scale of "Small Text Windows OCR")
  - [ ] #227 `OcrEngine.recognize` returns blocks; grouping and rendering move into `capture/`
  - [ ] #214 Test OCR block grouping and column order
  - [ ] #190 One OCR call on a mosaic of line strips; strips under 16 device px enlarged
  - [ ] ClearType strips rebuilt from their subpixels; other and deferred strips use C1's scale
  - [ ] Live standard run: CER ≤ 11 px and 1440p, OCR time (stored WebP cannot show it)
  - [ ] #191 Focused window: run the OCR gate too and keep the longer of UIA and OCR text
  - [ ] Standard run: focused-window text CER (longer OCR text must not replace good UIA text)
- [ ] R9 fix: the study's chosen candidate (after the R9 study, R6)
  - [ ] Standard and day runs: distinct people, wrong and unknown authors, useless episodes
- [ ] R10 Repeated screen text in search
  - [ ] Merge keyword hits per window run (hwnd, title, URL) at query time (`search/keyword.py`)
  - [ ] Measure top-5 slots repeating one window
  - [ ] Line store (C1 of "Screen Text Duplication And Noise") only if index size or #203 needs it
- [ ] R11 fix: the study's chosen candidate (after the R11 study); runs: term hits
- [ ] Browser visits under 5 s store no focused frame with their URL (M4.1): find why, fix
  - [ ] Standard run: M4.1 URL on visits under 5 s
- [ ] Storage
  - [ ] #198 `audio_segments (end_ms)` index for conversation and media segment reads
  - [ ] #206 `tab_frames` with the time side outer; check `EXPLAIN QUERY PLAN`
  - [ ] #225 One retention helper for spent frames and monitor history, used by the API too
  - [ ] #205 Retention text and transcript expiry paged by id, short write transactions
  - [ ] #239 Retention docstring: per-type bullets out of "Safety rules"
- [ ] Store growth: key-relative tile deltas (C1 of "Screenshot Store Growth", after R7, #225)
  - [ ] Delta files: changed 64-px tiles vs the monitor's WebP key, checked bit-exact at full res
  - [ ] Full-size view composes a delta (JPEG q90, or composed in the browser)
  - [ ] Retention expiry and export keep or rewrite the boundary key group per monitor
  - [ ] Standard and day runs: store growth (G4), M3.1 CER, every moment decodes
- [ ] Tests
  - [ ] #211 #212 #213 Replace sleeps and tight clocks with `wait_for` and real assertions
  - [ ] #207 Delete `test_capture_ports.py` but its VOICE_APPS invariant; conformance to fakes
  - [ ] #209 Delete shape tests; writer-imports-desk rule in `test_layout.py` or dropped
  - [ ] #210 #235 Delete guards of finished moves and removed routes, columns and paths;
    strip only the guard lines inside behaviour tests
  - [ ] #217 Table-test `UiaBudget.allow` tiers, expiry and recovery
  - [ ] #219 Move shared rigs out of test modules into helper modules
- [ ] Architecture (2026-11-19)
  - [ ] #226 Typed `ProcessState`; processing status and run sets move out of `storage/`
  - [ ] #228 Required `Services` collaborators, `Paths` passed once, routers always mounted
  - [ ] #229 One side-effect-free config reader for the shell and API
  - [ ] #230 Named, documented key for capture's device list
  - [ ] #231 ARCHITECTURE.md lists the real package edges and top-level helpers (after #226-#229)
- [ ] Clean-ups (2026-11-19)
  - [x] #232 Tick #49 #51 #56 #73 #84 #85 #87; tick #89 as obsolete
  - [x] #233 Drop the three ticked epics, A2 and A6; keep A8 as the Part C gate
  - [ ] #234 DECISIONS #47 line: drop "old segments lose meaning-search" (user, 2026-11-19)
  - [ ] #236 #237 Drop section-number citations and change-narrating comments
  - [ ] #238 One `MediaStore` in the worker (with #97)
  - [ ] #240 Drop the unused TD002, TD003, FIX002 ignores

## Part A: collection quality, before the real dataset

### A1. Exclusions before disk

- [x] Password checks fail closed on every monitor
  - [x] #16 Treat the focused HWND as focused on every monitor; capture each HWND once per tick
  - [x] #17 Skip the frame when UIA errors in `focused_is_password`; count it (superseded by A9)
- [x] URL and site exclusions reach every path
  - [x] #18 Reject a browser's media session while its focused tab is URL-excluded
  - [x] #19 Unreadable address bar: match finance and user URL rules on the title, fail closed (D2) (superseded by A9)
  - [x] #20 Read the URL from the omnibox only (`OmniboxViewViews`), never an in-page input
  - [x] #26 Match built-in finance rules as host suffixes; keep globs for user rules
  - [x] #27 Exclude Akashic's own API origin in `check_url` (superseded by A9)
- [x] Process and title exclusions
  - [x] #24 Audio process lookup returns unknown on COM error; drop or flag uncovered segments
  - [x] #25 Resolve UWP CoreWindow PIDs; title patterns for every listed password manager (superseded by A9)
  - [x] #28 Reload AUMID names with backoff instead of caching a failed load for the process life
  - [x] #148 Private markers only as browser title suffix; manager globs only off-browser (D2) (superseded by A9)
- [x] Live check on Zen, Chrome, Edge (playing tabs, private windows, user URL rule) and Proton Pass
- [x] Unexamined background browser media is logged: accepted and documented
  (DECISIONS, 2026-11-14 shell copy-surfaces row)
- [ ] Follow-ups found during A1
  - [ ] Ask the user: drop browser audio while a private or excluded page is open? (after A9)

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
  - [x] Guard `capture.close()` in `run_capture`'s finally so a DB error can't skip the joins
  - [x] Clear `_game_id` when the game end-row write raises; leave the row to `close_stale`
  - [x] Per-HWND UIA backoff after a timeout; time-box the readers without a `UiaBudget`
  - [x] Shorter `busy_timeout` once stop is set, so a busy database at stop fits the 10 s kill
  - [x] One rate-limited, type-only fault log shared by the screen, audio, media and gap loops
    (replaces `_log_fault` in capture/audio.py, `_log_loop_fault` in capture/screen.py and
    `warn` in platform/win/wgc.py)
  - [x] Report dropped audio segments and failed frame writes as capture problems (with #143)
  - [ ] Faster UIA text reader on very large pages, to shorten the screen-loop hold
    - [x] Keep the text: walks wait up to `TEXT_WAIT_S` (1.2 s), accepted 2026-11-18
    - [x] Spike other readers: none eligible; TextPattern blocks Chrome for about 22 s
      (`docs/baselines/2026-11-17-large-page-text.md`)
    - [ ] Reach 666 ms or less on the 30k page: Chrome median 815 ms, max 996 ms, Zen max 434 ms

### A4. Screen coverage

- [x] #144 Scroll capture uses the window under the cursor, skipping the focused HWND (superseded by A9)
- [x] #147 Game mode: idle captures of other monitors without UIA, OCR deferred; measure the cost
- [x] #150 Game detection: add common players, new Teams and RDP/Hyper-V clients; D3D exclusive fullscreen on a covered monitor marks a game
- [x] #21 UIA FindAll condition filters `IsOffscreen == False` before the 3000-node cut
- [x] #82 `FakeUia` returns `TextBlock`s; test the controls-only OCR fallback and `MAX_TEXT_CHARS`
- [ ] Lead: spike Chrome 138+ native UIA as the cause of the blank Edge probe (§9)
  - [x] Locked PC (2026-11-13): occlusion, not lazy UIA, blanks page text; URL and caption still read
  - [x] Rerun unlocked, window foreground: markers after 0.55 s in 5 of 5 launches, and while covered
    (`2026-11-18-focus-surfaces.md`)
  - [ ] Unlocked occlusion blanking not tested: Chrome did not treat the covered window as hidden
    (2026-11-18 `focus-surfaces`)

### A5. Audio recording

- [x] #131 Keep a seeded continuation's short fresh speech after a 30 s cut; keep the quiet check
- [x] #151 Attribute only the recorded endpoint's processes; log a gap when voice plays elsewhere
- [ ] Record a second loopback on the device voice chat plays on when it is not the recorded one
  (today only a `voice_elsewhere` gap; check the Wave Link routing first, CHECKLIST audio 7)
- [x] #152 Music-app audio: speech tag only; music-app songs still dropped (D7, 2026-11-17)

### A7. Capture gaps

- [x] #143 Record outages in a content-free `capture_gaps(kind, reason, start_ms, end_ms)`
  - [x] Pause, lock, missing or failed audio device, gated drops (screen drops: A9 states)
  - [x] OCR unavailable, capture process down
  - [x] Covered by retention and "Delete all" (after B4 #10)
- [x] Shared overlapping-gaps reader (open rows end at heartbeat or now), with its first consumer
- [ ] Own retention key for gaps if "longest limit, only when every type has one" proves wrong

### A9. Monitor capture and timeline (before A8)

Decisions of 2026-11-14 to 2026-11-16 in DECISIONS.md; rules in CAPTURE.md. Supersedes #78
(selected-window capture test), #144 (window under the cursor), #153 (full-res copy for game OCR),
#92 (`GdiScreen.grab`) and the A4 rate-state follow-up; the A4 owned-popups follow-up is covered by the popup rules.
Spike results go to a dated report in `docs/baselines/`; guides keep only the rules. Spikes keep
captured pixels and titles in memory; reports hold numbers only.

- [x] Spike per-window composition and storage (baseline 2026-11-15 `window-composition.md`)
- [x] Ask the user: the WGC packages (`winrt` Graphics.Capture and DirectX projections: what they
  do, network calls, alternative) before building on them (approved 2026-11-15, TOOLING.md)
- [ ] Remaining spikes on the real PC
  - [x] Popups not yet seen (`2026-11-18-focus-surfaces.md`; open points: OPEN.md A9 F21)
    - [x] Password-manager extension popup, autofill and save-password bubbles
    - [x] IME and emoji panels, credential prompts, WebView2 popups
    - [x] Every browser popup stays blank (F22, 2026-11-18)
  - [x] Shell surfaces (`2026-11-18-focus-surfaces.md`; open points: OPEN.md A9 F21)
    - [x] Taskbar previews kept out of the composite
    - [x] The snipping overlay outliving its window, notifications, elevated windows
    - [x] The 1,292 marker pixels seen with PowerToys Command Palette
  - [x] Title-change hold (20+ runs, title-change event hook)
    - [x] Same-title and in-page navigation, switching to a loaded tab, late titles, Zen
    - [x] Back/forward cache
    - [x] Baseline 2026-11-16 `title-change-hold.md`; leaking pages 2026-11-17 `title-hold-pages.md`
  - [ ] Game frame times below the frame cap, exclusive fullscreen, DRM video, HDR output
  - [ ] A normal day
    - [ ] Number of windows, capture VRAM, frame-copy CPU, composition time
    - [ ] Side-monitor check time and the post-game `ocr_window` backlog
  - [ ] Disk per normal day with tile deltas, OCR of stored frames (after Audit 2026-11-18 C1)
  - [x] OCR and decode times (baseline 2026-11-17 `ocr-decode-quns.md`)
    - [x] OCR time of whole windows on 1440p and 4K
    - [x] Decode time of several frames per moment
  - [x] UIA reads under load (baseline 2026-11-17 `uia-under-load.md`; decides F25)
    - [x] Caption and address reads for every visible browser
    - [x] A hung side browser; timed-out UIA reads and the checks they delay
  - [ ] Monitor identity across reboot and replug, incl. two identical monitors
- [ ] Ask the user: resolution, format and store drive, with disk per day
  - [ ] If the store leaves the system drive: Settings chooses the data folder; documented move
  - [ ] If resolution is capped: restore #153 (full-resolution copy for deferred game OCR)
- [ ] Ask the user: acceptable game frame-rate loss, with the spike's numbers
- [x] Platform adapters
  - [x] Monitors with stable identity and layout
  - [x] Per-window WGC captures: started after a kept verdict, stopped on exclusion or close,
    border off, minimum update interval
  - [x] Visible windows with stacking order, cloaking, owner, constant alpha, UWP
    hosted app, child windows of other processes
  - [x] Window events (show, hide, move, reorder, title change) instead of polling
- [x] Pause all recording (screen, audio, media sessions) while Akashic's UI is open
  - [x] Triggers: a heartbeat from each visible UI page; a visible window with Akashic's readable
    localhost address as a failsafe
  - [x] A UI page shows no recorded content until the server confirms the pause
  - [x] Its own gap reason (`ui_open`, kind `all`); never persisted or shown as the user's Pause
  - [x] The utterance in progress is kept; the worker keeps processing recorded work
  - [x] Drop the `own_ui` verdict and the own-UI title rule (#27 origin check): Akashic's address
    now triggers the pause
- [ ] Keep the UI-open halt for a short grace after a page's hidden report
  - [ ] A minimize or tab switch must not capture Akashic's own UI: 2 of 92 hides, presence timing
    (`docs/baselines/2026-11-17-presence-timing.md`)
- [x] Verdicts: process, private mode and readable address (plus the popup, ghost, child-window
  and copy-surface rules below)
  - [x] Remove all title exclusions (built-in, user list, media) and the password check
  - [x] Reword the code comments that cite D2 (`capture/exclusions.py`, `capture/media.py`)
  - [x] Zen decoded addresses with spaces parse as addresses, so address rules apply
  - [x] Unreadable address kept; Settings and config lose the title list
  - [x] Unknown app, unresolved UWP host or unreadable private state counts as excluded;
    re-judged at every check
  - [x] PiP and DevTools take their browser window's verdict
  - [x] Popups follow their owner; ownerless: excluded if its app has an excluded or unknown
    window, minimized or on another desktop included
  - [x] Ownerless browser popups always excluded
  - [x] Browser extension, autofill and save-password popups always excluded
  - [x] Ghost windows take the hung window's verdict (`HungWindowFromGhostWindow`), else excluded
  - [x] A child window of an excluded or unknown app excludes the window that contains it
  - [x] Shell copy surfaces (Alt-Tab, Snap Assist, Task View, taskbar previews, snipping overlay)
    always excluded
  - [x] Media: per-browser rule unchanged (excluding, unknown or stale), fed by every window
- [x] Composition
  - [x] Kept windows' latest frames in stacking order; excluded and pending areas left blank
  - [x] Turning excluded: stop the capture, discard the last frame; back to kept: nothing older
    than the change is painted
  - [x] Skip alpha-0 overlays
  - [x] Browser title change: pending until address and private state are read again; an
    unreadable address is then kept
  - [x] Verdicts and UIA reads outside the composition lock
- [x] What a frame keeps
  - [x] Excluded windows: no text, absent from the window list; only "an area was hidden"
  - [x] All-excluded monitor stores no frame, state hidden
  - [x] Writer-spy tests: an excluded window's title, URL, text never reach SQL, files, logs
- [x] Storage, schema edited in place, store recreated
  - [x] Monitors and their layout over time
  - [x] Windows per frame
  - [x] Per-window text rows
  - [x] Per-window text keeps D3: expiry clears copied excerpts and answers that used it (#57)
  - [x] States shown/hidden/dropped/failed, no reason; "not recording" from gaps (after A7 #143)
  - [x] Retention and "Delete all" cover them (after B4 #10)
- [x] Capture loop per monitor (after A3 per-HWND UIA backoff)
  - [x] Rate and change detection kept per monitor identity
  - [x] Compose only when a window on the monitor sent a frame or the window list changed
  - [x] Change measured against the last stored picture; lost frames mark dropped or failed
  - [x] Side monitors checked <= 30 s; every check refreshes media verdicts, even if dropped
  - [x] Input also captures the monitor under the cursor
  - [x] Game mode: game monitor at the game rate, others at the side rate, no UIA, OCR deferred
  - [x] If a timed-out UIA call per HWND per 30 s still delays other monitors: per-process UIA thread
    - [x] Built (F19); siblings of a stalled window stall with their process
    - [x] Spike first: comtypes UIA calls from several MTA threads
  - [x] Caption and address reads never delay another monitor beyond the 50 ms wait
    - [x] A focused-window text walk holds the screen loop up to `TEXT_WAIT_S`
    - [x] At most once per app budget interval, healthy walks included (accepted 2026-11-18)
  - [x] Re-check a monitor sooner (`min_gap_s`) while its frame shows a held or pending window
    - [x] So a held browser is not blank until the side rate; also while the event hook is down
  - [x] Black monitor stores no image, only its window list and any focused-window UIA text
  - [x] Encoding off the loop, bounded queue; full: drop the oldest side-monitor frame
  - [x] Health per monitor: failure and drop runs; full disk reported as a problem
- [x] Text: UIA for the focused window, OCR per kept window (whole capture), unchanged windows
  reuse text
  - [x] Never OCR excluded or pending windows, the desktop or the taskbar
  - [x] Deferred OCR (game mode) follows the same per-window rules
- [x] Downstream reads the window list and per-window text
  - [x] App sessions follow the focused window
  - [x] Search, its app filter and passages per window
  - [x] Ask app filters and "on screen in …" wording
  - [x] Media URL lookup (after #145)
  - [x] Drop the frame row's copy of the focused window's app, title, URL
- [x] Remove PrintWindow, its render thread, the 1920 px cap and the window-under-cursor path
  - [x] Remove `GdiScreen.grab` (#92)
- [x] Episode, search-hit and Ask images: the cited window's visible part, the rest masked
  - [x] No image when the window was not visible on the frame
  - [x] Thumbnail cache key includes the crop (after B4 #62)
  - [x] Latest frame counts while its monitor stayed "shown" since and the window is on it
- [x] Timeline (after B8 #63)
  - [x] Stage at relative positions; focused monitor marked; hover lists windows
  - [x] States drawn as such, with the age of the last check
  - [x] Pre-sized stage images
  - [x] Zoomable day strip with app bands; scroll, drag and keys
  - [x] Day navigation; the app filter highlights bands, never hides monitors
  - [x] Opening a moment: stage at that instant, cited monitor enlarged, window outlined
  - [x] Each moment has an address; Ask and episode links and Back land on it
- [x] Guides when A9 lands
  - [x] CAPTURE, STORAGE, RETRIEVAL, UI, ARCHITECTURE take the design's lasting rules
  - [x] SECURITY.md exceptions: titles, password fields, copies, notifications, background media
  - [x] SECURITY.md: unreadable address always kept; drop "focus state" and media-title rules
  - [x] DECISIONS.md: 2026-11-14 and 2026-11-15 rows to done
  - [x] `tests/manual/CHECKLIST.md`: screen items 1-3, 5, 7 and gaming 3 for monitor capture
  - [x] OPEN.md: drop #158, #144, the #147 cost line (the cost spike) and #162's hung-window check
  - [x] Remove the "being replaced" notes at the top of CAPTURE.md and SECURITY.md
  - [x] Delete the design file
- [x] Run the hardware tests: tests/hardware/test_monitor_capture.py and the A9 checks of
  tests/hardware/test_native_exclusions.py (mixed DPI, negative coordinates); passed 2026-11-16
- [ ] Run tests/manual/CHECKLIST.md screen 3, 5, 7, media 1, gaming 3 and the timeline item on
  three monitors
  - [ ] Evidence images on the real desktop: a window partly covered by another, one sticking
    out past a monitor edge, negative coordinates
  - [ ] Timeline on three monitors with mixed DPI: real positions, pre-sized images, hover
    lists, the cited window outlined; a real Ask video link opening its moment

### A8. Dataset export (gate to Part C)

- [x] Encrypted export for agent testing (DECISIONS, 2026-11-12)
  - [x] An explicit user action copies the database and media, optionally by date range or category
  - [x] Re-encrypted under a new passphrase; opens with `AKASHIC_DATA_DIR`; no plaintext temp files
  - [x] Live store unchanged; writers stopped or a consistent snapshot; no partial export left

Then the user records daily use with Part A in place and exports it; Part C works on that export.

## Part B: any time, no real data needed

### B1. Trustworthy measurement

- [ ] Ask eval runs production's path
  - [ ] #113 Index through `EmbedHandler` (segment and frame passages) (after A9)
  - [ ] #113 Cases: both target questions in one thread, runs at 00:40 and 09:00, ≥ 8 videos
  - [ ] #113 `eval_hard_memory` embeds `semantic_query`, not the whole question
  - [ ] #124 One test from capture rows through `build_episodes` to the video answer
- [x] Scorers accept only right answers
  - [x] #114 Facts on word boundaries after removing `[n]`; reject alternatives seen in distractors
  - [x] #115 Abstention: no cited in-window episode, anchored refusal, `forbid` on every question
  - [x] #116 Score cited-episode precision; forbid other-day and other-kind titles
- [x] Honest reporting
  - [x] #118 Freeze a held-out set; report development and held-out apart; say so in README.md
  - [x] #126 `--repeats N` and `--output` JSON; compare per question with one stored baseline
  - [x] #127 `lang` per question, per-language results, mixed-language lines and questions
- [ ] Fixtures cover real failure shapes
  - [ ] Question shapes beyond the AGENTS.md examples, FR and EN: game kind ("what solo game"),
    names read off the screen ("the Minecraft Rust project"), counts, places, people
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
- [ ] Quality harness `eval/quality/` follow-ups (audit 2026-11-18-quality)
  - [ ] Brave and Firefox cases in the app list
  - [ ] Scan export for M10.1 once a harness drives the app shell (UI presence, failsafe)
  - [ ] Ask the user: a French SAPI voice, to measure French speech (fr-FR OCR installed)
  - [ ] `eval/quality` under ruff and pyright; rename `scripts/eval_quality.py`
  - [ ] Attended run: lock, sleep, default-output change, UAC (M5.4, M13.1)
  - [ ] Exclusive-fullscreen re-entry and real-app launch paths beyond smoke's

### B2. Regression tests

- [ ] #75 Hardware test checks `SPEECH_CANDIDATES[DEFAULT_WHISPER_MODEL]`
- [ ] #76 Fake `torch`/`transformers`; test `make_transcriber`, `TransformerTranscriber`, log mute
- [ ] #77 "Survives errors" test: wait on predicates, assert alive before stop, state after resume
- [ ] #80 Replace fixed sleeps in the runner and scheduler tests with `wait_for(predicate)`
- [ ] #81 Memory API test uses the route's clock, not a pre-computed "today"
- [ ] #83 Test `_overall_state`, `_ask_blocker`, `autostart_command`, `GET /api/stt/recordings`
- [x] #84 Delete duplicate `conn` fixtures; fixtures yield and close
- [x] #85 Network-guard test asserts the UI folder exists instead of skipping
- [ ] #86 Game-session test asserts the row exists before checking its kind
- [x] #87 Whisper/worker test also asserts the job is back to `pending`
- [ ] #88 Tighter scheduler smoke check; French OCR/SAPI case; fix references to deleted docs
- [x] #89 Obsolete: its targets no longer exist (#232)

### B3. Processing never stops silently

- [x] Missing models block nothing
  - [x] #30 Run mic speaker jobs without models; queue none while the speaker models are absent
  - [x] #31 Transcript horizon gates only media and conversations; games and app runs use the gap
  - [x] #41 Remember model load failures and report them from `blocked()`; jobs stay pending
- [ ] Every queue state has an exit
  - [x] #154 `release_running` fails jobs at `max_attempts` (`give_up`) and backs off the rest
  - [x] #155 "Run waiting jobs now" also resets failed segments, so transcription reruns
  - [x] #156 Retry `complete()`/`fail()` on `OperationalError`; release orphaned `running` jobs
  - [ ] #33 Embed give-up marks ids skipped (or retries one by one); purge old failed jobs
  - [ ] #40 Ask jobs get 1–2 attempts; no server restart within a run after a failed start
  - [ ] #42 STT comparison re-checks game and heavy gates between clips and requeues the rest
- [ ] Leaks and loops
  - [ ] #39 Catch `HTTPException`; terminate llama-server on any `_start` exception
  - [ ] #35 `unload_if_idle` tests `self._model is not None`
  - [x] #56 Retention also catches `MediaPathError`, logs the type and skips the path
- [ ] #159 Housekeeping steps fail independently and publish per-step last-success times
- [ ] Retry screen frame and game-row writes on a busy database, like audio's
  `SegmentWriter._add_with_retry`, if failure runs show up in daily use (found in A9)
- [ ] #143 Queue OCR for windows stored without text (after A9)

### B4. Storage integrity and deletion

- [x] #10 Test that every `sqlite_master` table is deleted by "Delete all" or on a keep-list
- [ ] #164 Rebuild `media_sessions`, `game_sessions`, `audio_segments` with AUTOINCREMENT
- [ ] #164 An ignored episode ref is skipped and logged, never a stalled watermark
- [x] #49 `transaction()` rolls back only when in a transaction; rolls back and re-raises on commit
- [ ] #32 Episode builders honour the episode retention cutoff; expired episodes are not rebuilt
- [x] #62 Thumbnail cache keyed by `(frame_id, image_path, w)`; cleared on category deletes
- [ ] #55 Reject keyfile Argon2 parameters above sane ceilings before using them

### B5. Episodes are built and correct

- [ ] Identity-proven refactors first; port the `eval/` fuzz suites into `tests/` (after B3, B4)
  - [ ] #103 App-run single pass C1, identical to the current code (app sessions from windows, A9)
  - [ ] #102 Conversation sweep C4 against its written contract; fixes #36 mic chaining (after R3, R4)
- [ ] Video episodes (after R3)
  - [ ] #165 Store `process_for_app(app)` in `episodes.app`, display name in body; test Chrome/Edge
  - [ ] #166 Match the Edge soundtrack by exact executable through `process_for_app`
  - [ ] #167 Build video episodes from playing spans in `media_events`
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
  - [ ] Explicit ranges ("between 14:00 and 15:00", "entre 14h et 15h") give that window
    (today the first clock time becomes a one-hour window, 13:30-14:30)
- [ ] Lists and evidence
  - [ ] #177 Topic-free questions: chronological order, "N of M", up to `MAX_OVERVIEW_EPISODES`
  - [ ] #176 With people, time or kinds set, append the remaining eligible lines, newest first
  - [ ] Flagged or unclear text never alone supports a factual answer (D9)
  - [x] #143 List overlapping capture gaps in Ask's coverage line (after A7)
- [ ] Planner rules
  - [ ] #182 Topic keywords in French and English; prefix terms for words of 4+ characters
  - [ ] #183 Extend the stopwords; filter model keywords for cues, names and time words
  - [ ] #184 Tighten cues, add "maté"; the model may veto lower-case rule-matched names
  - [ ] #50 Strip the quoted name key once and use it for both the test and the lookup
- [ ] Answers and citations
  - [ ] #185 Sources give start and end; links open at the excerpt's segment and time
  - [x] Search hits and Ask "Related moments" open the timeline moment
    (`#/moment/<ts>?monitor=&window=`) instead of `#/frame/<id>` (found in A9)
  - [ ] #186 Raise `max_tokens` and mark truncation; flag uncited answers as unsupported
  - [ ] #53 `episode_lines` drops empty and expired lines before applying the limit
  - [ ] #54 `replace(lower(x), '.exe', '')` everywhere

### B7. Vectors

- [ ] #47 Retire legacy per-segment vectors and their search path (2026-11-10)
- [ ] #100 Vector scan C3 columnar chunk with C1's DAZ/FTZ self-test; identity harness
- [ ] #101 Frame text reuse C3, re-evaluated for per-window text (after #100, B4 #10, A9, R10)
- [ ] #104 Windowed episode vector scan C2 split overlap; identity harness
- [ ] #48 Check the windowed passage query with `EXPLAIN QUERY PLAN`; force the time side outer

### B8. Bounded background and API work

- [x] #61 `/api/status` stays index-only; the app list comes from its own endpoint or a cache
- [ ] #13 A separate read-only connection for read endpoints
- [x] #63 Only `frames.get` under the lock; decrypt and thumbnail outside it (after B4 #62)
  - [x] Generation counter: `clear_thumbs` bumps it, `put_thumb` drops renders started before
- [ ] #64 Debug counts with `SELECT count(*) FROM frames WHERE ts_ms >= ?`
- [ ] #65 Log view cached by `(size, mtime_ns)`; parse appended bytes only
- [ ] #58 Cache the storage report; the worker refreshes it after retention
- [x] #51 Text-change trigger fires only on text, title, URL updates (after A9: per-window rows)
- [ ] #38 Reset the CPU baseline after each job, or subtract the worker's own time
- [ ] Lead: NVML free-VRAM and free-RAM admission check in the scheduler (§9)

### B9. App shell and health

- [x] #157 `/api/status`: worker state, blocked kinds, failed count, oldest pending, horizon age
- [x] #157 Tray and sidebar show "needs attention" and "processing stopped"
- [ ] Debug page: screen writer counters (stored, dropped, failed, OCR ran or reused,
  deferred, encode time) (found in A9)
- [ ] #67 Type-only `threading.excepthook`; guard the monitor loop body
- [ ] #68 Children catch `ConfigError`; exit with a distinct code the supervisor reports
- [ ] #69 Tray Pause/Resume runs on a short-lived thread
- [ ] #66 Model download thread catches everything and marks the remaining keys failed
- [ ] #70 Settings view uses a sequence token and skips stale builds
- [ ] #71 Ask polling retries with backoff and stops only on 404
- [ ] #72 `Services.close()` under `_lock`
- [x] #73 Category deletion maps `OSError`/`CryptoError` like "Delete all"
- [ ] #74 `python -I` for autostart, `Akashic.cmd` and child spawns
- [x] Remove the low-disk warning `disk_warning_free_gb` (2026-11-12); old configs still load
  - [x] Also drop the "Verify the disk-space warning" line of tests/manual/CHECKLIST.md
    (Settings, retention and diagnostics, item 3)
- [ ] Lead: isolated Edge `--app` window for the UI (§9) (after A9: any visible UI page
  halts recording)

### B10. Structure and clean-up

- [ ] Single sources of truth
  - [x] #1 Move `pipeline/jobs.py` to `storage/jobs.py` (2026-11-10); layout test checks direction
  - [ ] #2 `query_vector` to `pipeline/query.py` or `api/`; `UNCLEAR_BELOW` to a neutral module
  - [ ] #4 `JobQueue` gains `retry_failed`, `replace`, `has_pending`, `key_for`
  - [ ] #5 Handlers declare the resources they use; the runner idles conflicting ones
  - [ ] #6 Define job kinds once, next to the queue
  - [ ] #7 Add `storage/questions.py` and `storage/stt_samples.py`; HTTP handlers hold no SQL
  - [ ] Move the read-only `app_bands` reader from pipeline/sessions.py to storage/sessions.py,
    so the API depends only on storage for timeline reads (found in A9)
  - [ ] #8 One bounds table for the Settings form and `parse_config`; `choices=LOG_LEVELS`
  - [ ] #9 Derive `RETENTION_TYPES` and the Settings rows from `PURGERS`
  - [ ] #11 One neutral `akashic/apps.py` for app identity (holds `process_for_app`)
    - [ ] Then remove the two tests/test_layout.py exceptions (api/memory.py, api/ask.py to
      capture.media) and `storage/monitors.episode_evidence`'s `process_of` parameter
  - [ ] #12 Use `models.is_ready`/`is_complete` instead of `glob("*.gguf")`
  - [x] #14 Regenerate the ARCHITECTURE.md layout from the tree; state the real dependency rules
- [ ] Dead code and stale docs
  - [x] #91 Delete `change_passphrase` and its tests; reword SECURITY.md and STORAGE.md (2026-11-10)
  - [ ] #90 Drop `media_sessions_fts` and its triggers if still unread; keep `media_events` (user)
  - [ ] #93 Delete `_WORD_TOKEN`, `WHISPER_MODELS` and dead CSS; rewrite that test on `REGISTRY`
  - [ ] #94 Build `REGISTRY` from `SPEECH_CANDIDATES`; keep the GPT-OSS 20B and Qwen3 14B choices
  - [ ] #95 Debug uses settings, the storage report and `Paths.db_file` (50 GB note already gone)
  - [x] Drop the capture CPU-only rule from code (DECISIONS row removed 2026-11-15): the
    `capture/main.py` and `capture/vad.py` docstrings and the two no-CUDA capture tests
    (`test_processes.py`, `test_audio_wiring.py`); the voice detector may stay on CPU
  - [ ] #96 Fix citations to absent docs and deleted manual tests; correct the CUDA/cuDNN claim
  - [ ] #97 Annotate `state: State`; build one `MediaStore`
  - [ ] #98 One `now_ms()` (api/timeline.py, api/memory.py, api/ask.py); import the shared clamp
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
- [ ] README.md: license line
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
- [ ] Measure how much of the user's screen text is 9-10 px, where R8's gain lies

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
- [ ] #140 Add named non-self speakers to the prompt vocabulary within `PROMPT_MAX_CHARS` (after R11)
- [ ] #139 Short or unsure clips: decode both languages, or reuse the stream's last language
- [ ] #43 Constrain Qwen3-ASR to French and English
- [ ] Re-run the hallucination harness after each change above; refit the gate if needed (§7.3)

### C4. Speaker attribution, Route A (after C3 #137, B5 #169)

- [ ] #132 #133 Speakers C2 calibrated fusion plus the C9 seam check, using `trimmed`
- [ ] Spread windows evenly to bound C2's worst case on 10.3–10.5 s segments (from C7, after R9)
- [ ] Deferred authors (NULL until the continuation runs) reach episodes through #169
- [ ] #132 Fix the outdated speakers module docstring
- [ ] #134 Store the match score; update a profile only above threshold plus a runner-up margin
- [ ] #135 Create profiles only when a voice-chat or game process is audible
- [ ] #136 Check the fbank window against WeSpeaker's reference; add a feature-parity test
- [ ] Compare Route A with today on the user's labelled clips (D6 feature from B1)

### C5. Episode rules tuned on real use (after B5)

- [ ] #168 A video doesn't claim source-less audio when a voice app or game was audible
- [ ] #171 Short videos: lower the 60 s minimum to 10–15 s, or keep clips played to the end
- [ ] #172 App runs chain on gap only, kind by dominant time; ties go to the first seen
- [ ] #149 Derive visible-window sessions from unfocused frames too (after A9)

### C6. Conditional extensions (spike or evidence first)

- [ ] Speech and speakers
  - [ ] Keep music-app segments with evidence of real speech, such as a podcast (D7)
  - [ ] Overlap-gated separation (C6 of "Overlapping Speech"), only on a real same-app need
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
- [ ] Storage
  - [ ] Retroactive AV1 re-encoding of older screenshots (C5 of "Screenshot Store Growth"), if asked
