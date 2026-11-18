# Architecture

## Boundaries and rationale

The application shell owns unlock, user interaction and child-process supervision. Capture
records lightweight evidence; a separate worker performs deferred processing and model inference.
This isolation keeps a model crash or expensive job from taking capture down with it.
Capture uses a Direct3D device to copy window frames (Windows.Graphics.Capture) and compose
them into monitor frames. The worker owns heavy inference resources.

Windows integration belongs behind small interfaces. Core code decides exclusions, persistence
and encryption; platform adapters report facts. Keep business rules out of Windows glue and
avoid adding a service or framework where local code and the existing queue suffice.

The persistent queue separates recording from processing so work survives interruption and can
wait while the user plays a game. Recovery must avoid both lost evidence and jobs that starve
all later work. A refactor must preserve evidence links and make stalled processing observable.

Every queue state has an exit. At worker start an interrupted job goes back to pending with
backoff, or fails (`give_up`) at its last attempt. A refused `complete`/`fail` is retried at the
next step before anything new is claimed. A job whose resource is missing is deferred without
counting an attempt (`DeferJob`). The worker publishes queue health in its `process_info` row
(failed per kind, oldest pending age, episode horizon, transcription stall), which `/api/status`
returns as `processing` and, as the one verdict `storage.debuginfo.processing_attention`, as
`attention`: the tray and the sidebar read that verdict with their own capture-running sets. The
transcript horizon holds back only media and conversation episodes.

## Source map

All code is under `src/akashic/`.

| Process | Entry point | Packages it uses |
| --- | --- | --- |
| App shell: unlock, tray, local UI server, supervision | `app.py` (`supervisor.py`, `tray.py`, `unlock*.py`) | `api/` (HTTP handlers) serving `ui/` (static HTML/JS/CSS) |
| Capture | `capture/main.py` | `capture/` (screen, media, audio, exclusions, gaps, health) |
| Worker: deferred jobs and models | `pipeline/main.py` | `pipeline/` (queue runner, transcription, speakers, episodes, embeddings, Ask, window OCR) |

Shared by all: `storage/` (encrypted database, media files, migrations, retention, job
queue), `search/` (time parsing, planner, keyword/semantic/hybrid ranking, answer building),
`platform/win/` (Windows API adapters), and the top-level `config.py`, `ipc.py`, `log.py`,
`offline.py`. `log.FaultLog` is the capture loops' one rate-limited, type-only fault log: counts
per what and exception type, one line per 5 min, never the message; its counts reach the Debug
data. `storage.db.DB_ERRORS` (the sqlite3 and sqlcipher3 errors) is what the app shell, the API and Ask
catch for a database failure.

Screen capture modules, by role:

- `platform/win/`: `displays` (monitors), `events` (window events), `wgc` (per-window captures),
  `windows` (window list and facts), `uia` (UI Automation).
- `capture/`: `verdicts` (keep, hold or exclude), `desk` (captures and per-monitor snapshots),
  `compose` (painting), `occlusion` (what lies in front), `screen` (the loop), `screen_writer`
  (the writer thread and per-monitor health), `uia_budget` (rationing and backoff), `uia_threads`
  (per-process UIA workers).
- `storage/`: `monitors` (layouts, states and the evidence readers `monitor_rect`,
  `latest_valid_frame`, `episode_evidence`, `frame_moment`), `frames`, `gaps`, `jobs`.
- Export: `storage/export` (snapshot, scope, re-encryption) run by `api/export.Exports` on its own
  thread and connection, never the API's shared lock. See [STORAGE.md](STORAGE.md).
- `storage/debuginfo`: `Processing` and `processing_status`, the worker's published queue health.
- `pipeline/ocr_windows`: the worker's `ocr_window` job.
- `api/`: `frames` (frame routes and masked window crops), `timeline`; `ui/timeline.js`. The API
  server serves `/api/status` without the app list and `/api/apps` on its own; memory and Ask
  read evidence through `storage/monitors`.

Other modules worth knowing:

- Child startup: `child.py` `ChildContext` holds the stop event and when the stop was requested
  (`stop_requested_at`), which the bounded stop measures from.
- Capture ports: `capture/ports.py` also holds `slept()`, the rule that tells a sleep from awake
  time for the media, gaming and gap clocks.
- Browser state: `capture/browser_state.py` bumps a clear generation, which the media tracker reads
  to tell that the verdicts were cleared.
- Derived rows: `pipeline/episodes.py` depends on `storage.sessions` for game sessions.
- Platform: `platform/win/windows.py` provides window facts and awake time (`awake_ms`).

`capture/health.py` holds the problem codes and the per-monitor runs (`<code>@Monitor N`); the
screen writer owns them and the monitor labels.

## Layout rules

Checked mechanically by `tests/test_layout.py` (local UI assets: `tests/test_network_guard.py`):

- `storage/` imports no higher package; `episode_evidence` takes the app-to-process mapping as a
  parameter.
- Core packages (`capture`, `pipeline`, `storage`, `search`, `api`) import `platform` only from
  `main.py`.
- Outside `capture/` and `platform/` only `capture.ports` and `capture.occlusion` are imported,
  with two exceptions until B10 #11: `api/memory.py` and `api/ask.py` import `capture.media`.
- Relaxed typing (`# pyright: basic`) exists only in `platform/win/`.
- Servers bind `127.0.0.1`, and the UI loads only local assets.

Other boundaries: only `capture/desk.py` starts and stops window captures; only
`capture/verdicts.py` decides keep, hold or exclude and publishes to `BrowserState`; in the
capture process only the screen-writer thread writes frames, `frame_windows` and the monitor
tables. Excluded and held windows reach the writer as content-free rectangles. Each capture
owner reads its connection from its own store and shortens its busy wait at stop
(`capture/gaps.py`: `STOP_BUSY_TIMEOUT_MS`, `limit_wait_at_stop`; storage keeps only
`db.limit_wait`). The rule for which gaps Ask reports (kinds, not-lost filter, clip, merge) lives
in `search/answer.py` (`gap_kinds`, `lost_spans`) and reads `storage.gaps.overlapping`.
All UIA calls run on per-process worker threads (`capture/uia_threads.py`, each with its own
`WindowsUia`), reached via `BudgetedUia`, and only the screen-loop thread requests reads; waits,
backoff and fail-closed rules: [CAPTURE.md](CAPTURE.md#ui-automation-timeouts). The rule for what a
speech segment was heard as (`heard_tag`/`speech_tag`) lives in `search/transcripts.py`, and
`pipeline/episodes.py` uses `heard_tag` for calls.

## Capture threads and data flow

The capture process runs a screen loop, the `ScreenWriter` thread, the media poller, the audio
recorders and the gap thread. A screen tick runs `Desk.check` (`Verdicts.judge`, the WGC copies,
composition), compares the result with the last stored picture and hands a changed one to the
`ScreenWriter`, which writes through `FrameStore` and `MonitorLog`. The worker's `ocr_window` job
reads stored window captures and finishes them (`finish_capture`).

The audio sources thread polls audible processes per playback endpoint. The system recorder tells
it which endpoint it records, and the audio writer stores that endpoint's processes. The audio
sources thread reports voice chat heard elsewhere: it writes the gap (`SourceSampler`) and
`AudioCapture.problems` reads it as the capture problem (`voice_elsewhere`).

## Messages between processes

`ipc.py` is the authority. The parent sends the data key on stdin, then the commands `stop`,
`pause`, `resume`, `ui_open` and `ui_closed`; a child reports its state on stdout (including
`ui_paused`). A UI page posts `/api/presence` while visible; `Presence` turns that into
`ui_open` or `ui_closed`, which the supervisor sends to capture only. The worker never halts for
the UI.

## Resource ownership

The worker owns model lifecycles. Serial jobs alone do not bound memory: resident models,
allocators, child processes and other applications all contribute. Coordinate model release and
measure actual RAM/VRAM and responsiveness before claiming a memory improvement.

Background processing should yield to games and interactive use. Explicit requests may process
stored memories while capture is paused, but must not resume recording. Avoid promises of
immediate unload or preemption without evidence from the actual implementation.

## Lifecycle guarantees

Capture starts only after unlock. Lock and quit must stop all writers and prevent a concurrent
restart from reviving them. Pausing, restarting and deleting data must preserve the user's
recording choice, including while children are starting. Lock and Quit cancel a running export
first (`Exports.cancel_and_wait`), before the API, the children and the key go.

Capture stop is bounded: it ends before the supervisor's 10 s kill and gives up what a busy
database still holds. Rules: [CAPTURE.md](CAPTURE.md#writes).

Full-data deletion requires an exclusive boundary with recording, processing and API writers;
it is not ordinary age-based retention. See [STORAGE.md](STORAGE.md) for the contract.
Planned structural changes are in [TODO.md](../TODO.md); leftovers of finished work are in
[OPEN.md](../OPEN.md).

## Extending

- A gap reason: the `0001` CHECK, `GapReason`, and its words in `search/answer.py`.
- A retention category: `PURGERS` plus its export scope handling.
- A capture problem: its code in `capture/health.py`, the owner's `problems()`, and its words in
  `ui/app.js` `PROBLEM_WORDS`.
- A Debug counter: the owner's `stats_snapshot()` or `FaultLog`, collected in `capture/main.py`
  `_collect` and shown in `ui/debug.js`.
- A Settings tab: `SETTINGS_TABS` in `ui/settings.js`.
