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
| Personal, non-commercial project (needed for ported screenpipe logic). The repository becomes **private**; the user changes the GitHub setting. | 2026-11-12 (reverses 2026-11-10 "stay public") | pending, user |
| Background browser media: a minimized or covered browser window that the screen loop never examined has no exclusion verdict, and its media title is logged even on a private, bank or user-excluded page. Accepted and documented in SECURITY.md rather than failing closed, to keep background videos answerable. | 2026-11-12 | done |
| Window screenshots: render the selected app window (HWND), never fall back to the monitor. | 2026-11-09 | done |
| Transcription quality before speed: slower deferred STT is acceptable; a smaller/faster model needs FR/EN/mixed quality evidence. | by 2026-11-10 | rule |
| Retention defaults to forever. Never delete recorded history automatically to free space, and show no storage-pressure warning: remove the existing low-disk warning (`disk_warning_free_gb`). | 2026-11-12 | pending, TODO B9 |
| Logical day starts at 05:00: before 05:00, "yesterday" means the day before the previous calendar day, and part-of-day ranges follow that day (D1). | 2026-11-11 | pending, TODO B6 |
| Music-app audio: drop songs, but keep music-app segments when there is evidence of real speech, such as a podcast (D7). Today all system sound whose only sources are music apps is dropped. | 2026-11-11, clarified 2026-11-12 | pending, TODO A5 |
| Browser exclusions: private-mode markers count only as the browser's own title suffix; password-manager title patterns apply only to non-browser apps; fail closed when unreadable (D2). | 2026-11-11 | done |
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
| Move the job queue to `storage/jobs.py`; runner and scheduler stay in `pipeline/`. | 2026-11-10 | pending, TODO B10 |
| Delete `change_passphrase` instead of wiring it to Settings. | 2026-11-10 | pending, TODO B10 |
| Keep the `media_events` table (used to find playing spans). | 2026-11-10 | done |

## Development

| Decision | Date | Status |
| --- | --- | --- |
| No development runner limits (memory, process, thread, priority) and no mandatory model preflight; do not restore them without asking. | 2026-11-10 | rule |
| Agents test on real data only through an **export** the app writes, re-encrypted with a separate passphrase that the user gives the agent. Never open the live data folder. Until the export exists, use disposable encrypted stores. | 2026-11-12 | pending, TODO A8 |
| Checks run through `scripts\check.ps1` (`uv run`, which may sync the environment). | 2026-11-12 | rule |
| Work follows the [TODO.md](../TODO.md) milestones: Part A (collection quality) before the real dataset, Part B any time, Part C on the dataset export; each milestone goes through `/frogg:implement` or `/frogg:epic`. The audit handoff is retired. | 2026-11-12 (replaces the handoff milestone process) | rule |
| Audits, baselines, method reports and `eval/` stay in place as evidence and will be worked on after this cleanup. | 2026-11-12 | rule |
