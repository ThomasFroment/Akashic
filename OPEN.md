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

- #21 Electron's `IsOffscreen` on a virtualized list (long Discord channel) not checked
- #150 Not spiked on the real PC: `SHQueryUserNotificationState` while a borderless game, an exclusive-fullscreen game, fullscreen mpv/MPC-BE and RDP run (browser F11 spiked 2026-11-18: QUNS stays state 5); and whether QUNS reports D3D fullscreen while another window is in front (alt-tab, side monitor). Microsoft documents only that an exclusive D3D app is running, so tying it to the focused window is an assumption, bounded by the covers-monitor check. `kmplayer64.exe` comes from a third-party listing, not the vendor.
- #147 Not checked in the running app: Discord and a video on side monitors during a fullscreen game

## A3. Capture keeps running and keeps what it recorded

- #22 F8 Not checked in the running app: the per-HWND backoff against a hung browser (the 2026-11-16 browser-reads spike measured the readers alone: about 1.0 s, `None`, over `SLOW_CALL_MS`). Not timed on the user's real profiles, tab counts or pages (throwaway profiles, one tab, tiny local pages; Zen's sidebar layout not varied), so the 900 ms box is unchecked against slow, empty (None) reads on real, larger pages
- #23 #160 Not checked on a real device unplug or default-device switch in the running app
- #162 Thresholds (3 failed grabs, 60 s without a device read) unmeasured; not checked with a real unplugged mic in the running app
- #161 Not checked against a real long retention or Delete-all lock in the running app
- #161 A database busy past the 8 s stop deadline drops the queued pictures, audio segments and gap events (`stop_dropped`); an abandoned audio write may orphan an `.opus.enc`; open media and game rows wait for `close_stale`
- F3 (Recording readiness) Not checked against a real long lock in the running app
- F4 (Recording readiness) The 60 s drop window and the one-failure rule are chosen, not measured; not seen in the running app

## A5. Audio sources

- #151 `MIXER_PROCESSES` (`elgato.wavelink.exe`) unconfirmed: the 2026-11-17 read-only run saw five playback endpoints (Elgato Wave:3 virtual, Voice Chat virtual, DT-770 PRO, System virtual, Headphones Wave:3) with nothing audible
- #151 Discord's real routing not checked
- #131 Checked on public read speech (`2026-11-17-continuation-speech.md`); headset/Discord and the running app open
- F15 (Part A unattended) The quiet gate judges the whole voiced RMS including the 2 s overlap; proposed: judge fresh windows only (`2026-11-17-continuation-speech.md`)

## A6. Media and game sessions

- #146 #167 Lock/UAC during a video not checked in the running app
- F13 (Part A unattended) Zen published no media session in 804 polls; Edge, Brave and Firefox unmeasured (`2026-11-17-media-sessions.md`)

## A7. Capture gaps

- #143 Not checked in the running app: pause, lock, unplugging the mic, default-device switch, Quit and a kill, each against the rows written
- #143 A write that fails for a reason other than a busy database drops that one gap row (counted as `write_rejected` in the Debug data)

## A8. Export

- Not run on the real store or in a browser
- VACUUM with `temp_store=MEMORY` needs RAM of about the db size; past a few GB consider `VACUUM INTO`
- Rebuilt bodies use `retained_episode_bodies` (rtrim vs `rstrip()`, B5 #3); a mismatch counts as changed and drops the vector
- The export's table scope map could replace Delete all's completeness list (B4 #10)

## A2. Capture stops when asked

- #60 #163 Not checked in the running app: Lock, Quit, pause then lock/unlock, pause then restart

## A9. Monitor capture and timeline

- F21 (Part A unattended) Not run in `2026-11-18-focus-surfaces.md`: Chrome password-manager extension popup (none installed, branded Chrome ignores `--load-extension`); emoji panel in Chrome and Zen (Win+. did not open it); Zen login autocomplete popup; toast on the primary (none appeared); IME candidate window (no IME layout); UAC prompt or secure desktop (none appeared)
- F21 (Part A unattended) Taskbar thumbnail of an excluded minimized window not measured: the only new window was 0x0 and WGC could not capture it; 2026-11-15 measured 25,070 marker pixels with a desktop grab, so the case is open
- F21 (Part A unattended) The 1,292 marker pixels with PowerToys Command Palette (2026-11-15) stay unexplained: the marker test (R > 180, G < 80, B > 180) misses tinted or blurred (acrylic) markers, so the 0 of 2026-11-18 does not explain them
- F21 (Part A unattended) Snipping overlay probe frames were blank although the overlay held a frozen copy of the marker (likely excluded from capture), so the overlay case rests on the 2026-11-15 measurement
- F21 (Part A unattended) Chrome and Zen HTTP Basic auth and Chrome WebAuthn prompts: only the page request was checked, not that a prompt showed on screen
- F21 (Part A unattended) Proton Pass popup content not read; "signed in" not checked (new profile, expected signed out)
- F8 Brave and Firefox not measured (not installed); Chrome 157, Edge 157 and Zen 1.24.1b only
- F8 Chrome and Edge keep the address bar readable in F11 and video fullscreen in these versions; not a guarantee
- F8 Edge's first spike launches (before `--disable-sync`) signed the throwaway profile in with the PC's Microsoft account; local test pages may be in that account's sync history
- F6 `WgcCaptures` abandons a close after 0.3 s, but the earlier hang of `session.close()` after the target process died did not reproduce: 0 of 10 hangs in each of kill, WM_CLOSE exit and window destroyed with the process alive, in both close orders, static and animated windows; the pool now closes first (0 of 10 blocked per case), so an abandoned close would leak only the session and its thread, none seen
- A9 Hardware run 2026-11-16 (all hardware tests, three monitors, 96 and 120 DPI, one at negative x): all passed, except the Edge case of `test_real_browser_layouts_on_synthetic_pages`, not run because a fresh Edge profile may sign in to the PC's Microsoft account even with `--disable-sync`; the French OCR and French SAPI checks skipped (no French OCR language pack, no French SAPI voice installed)
- A9 Capture border: none on build 26200 for this unpackaged process, but Microsoft documents turning it off as needing a capability; re-check after each Windows update.
- A9 Ghost mapping uses the undocumented `HungWindowFromGhostWindow`; if it stops resolving, every ghost window is blank; re-check after each Windows update.
- F11 (Part A unattended) A covered Akashic page keeps screen, audio and media halted behind other windows (`2026-11-17-presence-timing.md`)
- F11 (Part A unattended) A half-covered Chrome page reported hidden once while on screen (`2026-11-17-presence-timing.md`)
- F11 (Part A unattended) Edge not run (`2026-11-17-presence-timing.md`)
- F8 (Part A unattended) `Verdicts._other` and `_recognize` read every ownerless window of the exe at each check; 100 ms per check was crossed at 5 Chrome windows (about 26 ms per visible main) and at 5 Zen windows with minimized mains (`2026-11-17-uia-under-load.md`)
- F10 (Part A unattended) Zen leaked once at d=600 ms, with the address 596 ms after the title (`2026-11-17-title-hold-pages.md`)
- F10 (Part A unattended) Host-less substring user globs and `proton.me/pass` can be exposed by Chrome's 200 ms same-document title delay (`2026-11-17-title-hold-pages.md`)
- F12 (Part A unattended) Only en-US OCR is installed, `WindowsOcr("fr")` resolves to en-US, accented words read exactly in 40-46% (91-96% accent-folded) (`2026-11-17-ocr-decode-quns.md`)
- F12 (Part A unattended) Whole-window OCR of a window with a noisy picture can return almost nothing (game windows, deferred `ocr_window`) (`2026-11-17-ocr-decode-quns.md`)
- F19 (Part A unattended) A late address is handed over once and never keeps a window, so a browser whose reads keep missing the 50 ms wait stays held; real-desk rate unmeasured
- F19 (Part A unattended) A stalled focused browser costs one text wait (1.2 s) per budget interval (~20 s) while it stays stalled, and again when another of its windows is focused before that walk's result is read; unmeasured on a real desk
- F19 (Part A unattended) The 16-worker cap evicts the least recently read idle worker and drops its late results; churn cost unmeasured
- F19 (Part A unattended) Real-PC end-to-end run (2026-11-18) used Chrome only, synthetic monitor halves, focus stubbed and the own-address watch stepped by hand; Zen, a real focused text walk and `run()`'s own watch timing not run
- Spike conduct (Part A unattended) Browsers take the foreground at launch despite `SW_SHOWNOACTIVATE` and `SW_SHOWMINNOACTIVE`
- Spike conduct (Part A unattended) Zen's safe-mode prompt appears on the primary monitor after killed runs

## Epic: Recording readiness

- Exit-139 crashes of check.ps1 with coverage (seen while another agent edited, or after a failing test) did not recur in two quiet full runs; cause unknown
- tests/test_llm.py stream transport test failed once (RST on Windows) before the fake read the request body; not seen since
