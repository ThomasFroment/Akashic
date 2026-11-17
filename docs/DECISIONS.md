# Decisions

The user's binding product and process decisions, one line each, newest review on top. A guide
states the rule for its area. This file records who decided it, when, and whether the code
already follows it. Change a decision only when the user says so, and update its line here
(date the change) instead of adding a contradicting one.

**Status:** *done* = the code follows it · *pending* = decided, the code does not follow it yet
(its [TODO.md](../TODO.md) milestone is named) ·
*rule* = an ongoing constraint, nothing to build.

## Product

| Decision | Date | Status |
| --- | --- | --- |
| Capture stop and the window event hook: a busy database at stop is retried by the gap thread until the 8 s stop deadline, while the screen and audio writers drop what is queued at their first busy write. While the hook is down, held browser windows off the busy monitor may stay blank, reported as `window_events_down` after 30 s. Rules: [CAPTURE.md](CAPTURE.md#writes). | 2026-11-17 | done |
| Game detection: D3D exclusive fullscreen (on a window covering a monitor) alone marks a game, even with a caption or on the deny-list; otherwise the borderless + covers-monitor + deny-list heuristic applies; the user's `games`/`not_games` win. (#150) | 2026-11-13 | done |
| Monitor screenshots: each monitor's frame is composed from per-window captures (Windows.Graphics.Capture) of its kept windows, in stacking order; excluded windows are never captured. Stored at native resolution for now, until disk per normal day is measured, per monitor at its own rate; the timeline shows each monitor's latest frame. No real data exists: the schema is edited in place and the store recreated. | 2026-11-14 (reverses 2026-11-09 "render the selected app window, never the monitor"); 2026-11-15: composed from per-window captures | done |
| Exclusions for now: app (built-in password managers and sign-in surfaces, the user's process list), browser private mode, and a readable browser address (built-in bank and password-manager sites, the user's address list). Titles are never checked, for windows or media, except the browser's own private-mode suffix of its title or caption ("(Incognito)", "[InPrivate]"): title exclusions are removed, to be added back later. A focused password field no longer excludes its window. Accepted security risk. Rules: [CAPTURE.md](CAPTURE.md#which-windows-are-captured); accepted exceptions: [SECURITY.md](SECURITY.md#content-handling). | 2026-11-14 (supersedes D2 of 2026-11-11); 2026-11-15: D2 merged here, Akashic's address moved to the UI-open pause row; 2026-11-16: never-read Chrome/Brave windows need a clean caption, path rules cover their whole site | done |
| Excluded windows are never captured and produce no screen text; their area of the composed frame is left blank. An unknown app or unreadable private state counts as excluded. Popups take their owner's verdict; an ownerless popup is excluded when its app has an excluded or unknown window; every browser popup, owned or not, is blank for now (2026-11-16, until the popups spike). Media logging keeps today's per-browser rule, now fed by every visible browser window (stricter). Rules: [CAPTURE.md](CAPTURE.md#which-windows-are-captured). | 2026-11-14; 2026-11-15: never captured, area left blank (not painted over), the title-change hold ends with the read; 2026-11-16: every browser popup blank until the popups spike | pending, TODO A9 (popups spike) |
| The shell's copy surfaces (Alt-Tab, Snap Assist, Task View, taskbar previews, the Snipping Tool overlay) are always excluded. Accepted exceptions for monitor frames: other apps showing a copy of other windows' pixels or text (Magnifier and zoom tools, clipboard history, jump lists, Live Captions, OBS, screen-share and remote-desktop previews, an app displaying a screenshot) are captured as they appear; notifications are captured; the taskbar is painted in monitor frames but never OCR'd or listed; background browser media stays logged for browsers that are minimized, covered or on another desktop, and for background tabs of a kept window. Rules: [CAPTURE.md](CAPTURE.md#which-windows-are-captured); exceptions: [SECURITY.md](SECURITY.md#content-handling). | 2026-11-14; 2026-11-15: shell copy surfaces excluded instead of accepted; 2026-11-16: taskbar painted, never OCR'd; background media: 2026-11-12 | done |
| While Akashic's own UI is open, all recording pauses (screen, audio, media sessions) and the pause is recorded as a gap of its own, not shown as the user's Pause. The UI opens only from the tray icon or on the first run, never at autostart. Rules: [CAPTURE.md](CAPTURE.md#while-akashic-is-open), [UI.md](UI.md#while-akashic-is-open). | 2026-11-15 (replaces excluding Akashic's own pages); 2026-11-16: UI not opened at autostart, meter unavailable while open | done |
| Timeline: a stage showing all monitors at their relative positions over a zoomable day strip with app bands only (no speech, media, game or gap lanes for now). Episode, search-hit and Ask images show only the cited window's visible part, other windows hidden in that image; the stored frame is unchanged. Rules: [UI.md](UI.md#timeline), [RETRIEVAL.md](RETRIEVAL.md#screen-evidence). | 2026-11-14; 2026-11-15: done at display, not on the stored frame | done |
| Screen text: UIA for the focused window only; OCR for each other visible kept window separately, on the whole window as captured (covered parts included), for now. A search hit can quote text the stored picture does not show. Deferred OCR (game mode) reads whole windows too. Rules: [CAPTURE.md](CAPTURE.md#screen-text). | 2026-11-14; 2026-11-16: whole window instead of its visible area (as screenpipe); deferred OCR from stored window captures | done |
| Transcription quality before speed: slower deferred STT is acceptable; a smaller/faster model needs FR/EN/mixed quality evidence. | by 2026-11-10 | rule |
| Retention defaults to forever. Never delete recorded history automatically to free space, and show no storage-pressure warning: remove the existing low-disk warning (`disk_warning_free_gb`). | 2026-11-12 | done |
| Logical day starts at 05:00: before 05:00, "yesterday" means the day before the previous calendar day, and part-of-day ranges follow that day (D1). | 2026-11-11 | pending, TODO B6 |
| Music-app audio: drop songs, but keep music-app segments when there is evidence of real speech, such as a podcast (D7). Today all system sound whose only sources are music apps is dropped. | 2026-11-11, clarified 2026-11-12 | pending, TODO A5 |
| Removing or expiring screen text also clears its copied excerpts and the saved answers that used it (D3). Transcripts behave the same way. | 2026-11-11 | done |
| Follow-ups: after "What did I do on Tuesday?", a weekday follow-up keeps the previous topic and omitted filters; a standalone question never inherits from earlier turns (D4). | 2026-11-11 | pending, TODO B6 |
| Hallucination filter: "unclear" is kept and counted apart from "rejected"; plausibly spoken outros stay flagged, not deleted (D5). | 2026-11-11 | pending, TODO C2 |
| Speaker identity may be used day to day before a local accuracy comparison exists; that comparison is an ongoing feature, not a release gate (D6). | 2026-11-11 | rule (feature: TODO B1) |
| Planning uses rules by default; model-assisted time/follow-up planning stays optional, needing evidence and the user's explicit choice (D8). | 2026-11-11 | rule |
| Flagged/unclear text stays searchable but never alone supports a factual answer, even with a hedge; copies of one transcript do not corroborate each other (D9). | 2026-11-11 | pending, TODO B6 |
| Learned title state (aliases) stays in RAM only, never persisted. | 2026-11-11 | rule |

## Code structure

| Decision | Date | Status |
| --- | --- | --- |
| One episode digest builder, in Python. The SQL view that deletion triggers use to rebuild digests goes away. | 2026-11-12 (reverses 2026-11-10 "keep both + equality test") | pending, TODO B5 |
| Retire legacy per-segment vectors and their second search path; old segments lose meaning-search until re-embedded as passages. | 2026-11-10 | pending, TODO B7 |
| Move the job queue to `storage/jobs.py`; runner and scheduler stay in `pipeline/`. | 2026-11-10 | done |
| Delete `change_passphrase` instead of wiring it to Settings. | 2026-11-10 | done |
| Keep the `media_events` table (used to find playing spans). | 2026-11-10 | done |

## Development

| Decision | Date | Status |
| --- | --- | --- |
| No development runner limits (memory, process, thread, priority) and no mandatory model preflight; do not restore them without asking. | 2026-11-10 | rule |
| Agents test on real data only through an **export** the app writes, re-encrypted with a separate passphrase that the user gives the agent. Never open the live data folder. Use disposable encrypted stores for deletion, upgrade and failure tests. | 2026-11-12 | rule |
| Checks run through `scripts\check.ps1` (`uv run`, which may sync the environment). | 2026-11-12 | rule |
| Work follows the [TODO.md](../TODO.md) milestones: Part A (collection quality) before the real dataset, Part B any time, Part C on the dataset export; each milestone goes through `/frogg:implement` or `/frogg:epic`. The audit handoff is retired. | 2026-11-12 (replaces the handoff milestone process) | rule |
| Audits, baselines, method reports and `eval/` stay in place as evidence. | 2026-11-12; 2026-11-15: no later rework scheduled | rule |
