# OPEN

What a finished TODO.md item left open: checks not run, tests not written, deferred review
findings, rejected fixes. Grouped by the milestone the item belongs to. One line per point,
naming the item it came from.

When a point is done, delete its line in the commit that does it. When it turns into planned
work, move it to TODO.md as a task and delete it here. Drop a milestone heading once it is empty.

## B1. Evaluation

- #118 No Ask eval run since split-only reporting

## B3. Job queue

- #156 A dead worker's `running` jobs are released only at the next start
- #30 Conversations built without voice models keep their `episode_people` after the backfill
- #41 A CUDA error at a fresh model's first decode counts as a failed attempt; "Run waiting jobs now" doesn't retry a failed load
- F9 (Recording readiness): api/debug.py keeps inline job SQL (counts, list, Run waiting jobs now) instead of `JobQueue` methods (B10 #4)

## B4. Storage integrity and deletion

- #10 The completeness test counts a conditional `DELETE ... WHERE` as full coverage; only `state` (keeps `capture_paused`) relies on it today. An unconditional-only rule plus an explicit row-level keep for `state` would stop a future conditional delete from passing.
- #10 An external-content FTS table counts as covered without checking it has a delete trigger; the end-to-end MATCH check covers only `frames_fts` and `audio_segments_fts`, and the shadow skip matches by suffix, not by owning FTS table.

## B6. Search and answers

- #143 The Not recorded line isn't shown in the Ask page; no Ask baseline rerun
- F13 (Recording readiness) Moment and Window texts links not checked in a browser; `frameView`, `episodeView` and `chatView` have no stale-route guard

## B9. Disk and processing status

- #157 No hysteresis for a crash-looping worker; a stale worker row (`updated_ms`) is not treated as unknown
- #157 Processing line and tray states not seen in the running app; Debug has no processing view

## A4. Screen coverage

- #21 Electron's `IsOffscreen` on a virtualized list (long Discord channel) not checked; Gecko (Zen) `IsOffscreen` on scrolled-away nodes not seen, the throwaway Zen page exposed no web content in the 2026-11-13 re-run
- #150 Not spiked on the real PC: `SHQueryUserNotificationState` while a borderless game, an exclusive-fullscreen game, fullscreen mpv/MPC-BE, browser F11 and RDP run; the cost of the call (cached 1 s); and whether QUNS reports D3D fullscreen while another window is in front (alt-tab, side monitor). Microsoft documents only that an exclusive D3D app is running, so tying it to the focused window is an assumption, bounded by the covers-monitor check. `kmplayer64.exe` comes from a third-party listing, not the vendor.
- #147 Not checked in the running app: Discord and a video on side monitors during a fullscreen game
- #147 No test asserts the `BrowserState` verdict a side browser publishes during a game

## A3. Capture keeps running and keeps what it recorded

- #22 F8 Not checked in the running app: the per-HWND backoff against a hung browser (the 2026-11-16 browser-reads spike measured the readers alone: about 1.0 s, `None`, over `SLOW_CALL_MS`). Not timed on the user's real profiles, tab counts or pages (throwaway profiles, one tab, tiny local pages; Zen's sidebar layout not varied), so the 900 ms box is unchecked against slow, empty (None) reads on real, larger pages
- #23 #160 Not checked on a real device unplug or default-device switch in the running app
- #162 Thresholds (3 failed grabs, 60 s without a device read) unmeasured; not checked with a real unplugged mic in the running app
- #161 Not checked against a real long retention or Delete-all lock in the running app
- #161 Play/pause events of a live session are lost on a busy database (matters for #167)
- #161 A database busy past the 8 s stop deadline drops the queued pictures, audio segments and gap events (`stop_dropped`); an abandoned audio write may orphan an `.opus.enc`; open media and game rows wait for `close_stale`
- F3 (Recording readiness) The stop deadline starts when the screen loop returns, not at the stop request. Not checked against a real long lock in the running app
- F4 (Recording readiness) The 60 s drop window and the one-failure rule are chosen, not measured; not seen in the running app
- F1 (Recording readiness) WGC fault counts are not in the Debug data. Sampler and segment-writer warnings and wgc's "no D3D device" are not on a `FaultLog`. Settings > Debug does not show the audio `faults` or `gaps` entries

## A5. Audio sources

- #151 `MIXER_PROCESSES` (`elgato.wavelink.exe`) unconfirmed: the 2026-11-17 read-only run saw five playback endpoints (Elgato Wave:3 virtual, Voice Chat virtual, DT-770 PRO, System virtual, Headphones Wave:3) with nothing audible
- #151 Discord's real routing not checked
- #151 `voice_elsewhere` is no health problem; only Ask's line shows it
- #131 Not checked on real speech

## A6. Media and game sessions

- #146 A UAC prompt shorter than one 2 s media poll is not seen as locked, so a `url_unknown` row may close (BrowserState generation counter)
- #142 A game row is not split on a wake with no lock screen and the game still in front; a media-thread stall over 10 s splits a row
- #146 #167 Lock/UAC during a video not checked in the running app

## A7. Capture gaps

- #143 A stop whose database stays busy past the 8 s deadline is still recorded as `crashed` from the last heartbeat (up to ~30 s off); a clean-stop marker written at the deadline would date it exactly
- #143 Not checked in the running app: pause, lock, unplugging the mic, default-device switch, Quit and a kill, each against the rows written
- #143 A write that fails for a reason other than a busy database drops that one gap row (counted as `write_rejected` in the Debug data)

## A8. Export

- Not run on the real store or in a browser; cancel during VACUUM not run
- VACUUM with `temp_store=MEMORY` needs RAM of about the db size; past a few GB consider `VACUUM INTO`
- Rebuilt bodies use `retained_episode_bodies` (rtrim vs `rstrip()`, B5 #3); a mismatch counts as changed and drops the vector
- An export run as a data folder lets the worker rebuild excluded derived categories
- The export's table scope map could replace Delete all's completeness list (B4 #10)

## A2. Capture stops when asked

- #60 #163 Not checked in the running app: Lock, Quit, pause then lock/unlock, pause then restart
- #163 No test sends a real `POST /api/pause` over HTTP; tests call `controls.pause()` directly
- #163 No test uses a really closed connection; the `ProgrammingError` is injected
- #163 The pause write holds `_restart_lock`, so a busy database delays `poll()` (up to 5 s; see #69)

## A9. Monitor capture and timeline

- F8 Brave and Firefox not measured (not installed); Chrome 157, Edge 157 and Zen 1.24.1b only
- F8 The hung browser was a suspended process; a browser busy in a long task or deadlocked in one thread is untested; no case was skipped as "not run"
- F8 Parallel reads from per-thread `WindowsUia` still serialize (four threads: 0.76-0.81 of the sequential wall time); not separated whether the Python lock, UI Automation or the browsers cause it, which decides whether F25's per-HWND thread helps
- F8 Chrome and Edge keep the address bar readable in F11 and video fullscreen in these versions; not a guarantee
- F9 Chrome's same-document title event comes 200 ms after the change (Zen 3 ms); a page that retitles and leaks in one step is unprotected for those 200 ms, not measured with such a page
- F9 No page produced both a title event and an address lagging the pixels, so the hold's leak prevention is simulated, not seen: 0 leaks with or without it in 330 runs; chosen bound T = 500 ms, N = 3 (25% of the time pending for a page that retitles every 2 s)
- F10 Not measured: how often the `OBJID_CLIENT` (-4) `NAMECHANGE` that F5 now reports as `title` leaves a window held (Chrome sends it on every navigation, also with an unchanged title), and what reading the address of every ownerless browser window costs on real profiles with many windows
- F8 Edge's first spike launches (before `--disable-sync`) signed the throwaway profile in with the PC's Microsoft account; local test pages may be in that account's sync history
- F6 `WgcCaptures` abandons a close after 0.3 s, but the earlier hang of `session.close()` after the target process died did not reproduce: 0 of 10 hangs in each of kill, WM_CLOSE exit and window destroyed with the process alive, in both close orders, static and animated windows; the pool now closes first (0 of 10 blocked per case), so an abandoned close would leak only the session and its thread, none seen
- F19 Not run: real-browser timing of the presence loop (a page shown by Zen, Chrome and Edge: time from the tab becoming visible to recording halted and to content shown; the 6 s lease and the 250 ms/2 s schedule are chosen, not measured; a background or occluded tab may be throttled by the browser)
- F19 Not run: the own-address failsafe on Zen, Chrome and Edge (address read of a visible window on Akashic's port, with and without a scheme; a page not opened through the tray, such as a bookmarked address)
- A9 Hardware run 2026-11-16 (all hardware tests, three monitors, 96 and 120 DPI, one at negative x): all passed, except the Edge case of `test_real_browser_layouts_on_synthetic_pages`, not run because a fresh Edge profile may sign in to the PC's Microsoft account even with `--disable-sync`; the French OCR and French SAPI checks skipped (no French OCR language pack, no French SAPI voice installed)
- A9 Capture border: none on build 26200 for this unpackaged process, but Microsoft documents turning it off as needing a capability; re-check after each Windows update.
- A9 Ghost mapping uses the undocumented `HungWindowFromGhostWindow`; if it stops resolving, every ghost window is blank; re-check after each Windows update.

## Epic: Recording readiness

- Exit-139 crashes of check.ps1 with coverage (seen while another agent edited, or after a failing test) did not recur in two quiet full runs; cause unknown
- tests/test_llm.py stream transport test failed once (RST on Windows) before the fake read the request body; not seen since
