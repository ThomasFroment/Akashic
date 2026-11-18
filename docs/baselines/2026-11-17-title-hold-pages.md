# Title-hold pages spike 2026-11-17

Measurements of TODO F10, deciding F17 (the title-change hold of `Verdicts`,
[CAPTURE.md](../CAPTURE.md) "Which windows are captured"). [2026-11-16-title-change-hold.md](2026-11-16-title-change-hold.md)
simulated the hold over recorded timelines of pages that never leaked; here pages that change
their title, repaint and then move their address run through the production capture chain, and
what the writer receives is counted. Test windows were disposable Chrome and Zen processes
started by the spike inside a Job object (`KILL_ON_JOB_CLOSE`) with throwaway profiles, and a
page server on 127.0.0.1 inside the spike; no user window was read or captured by the
production chain (four debugging runs printed window data; one of the printed windows was not a
spike window, see Limits). Pixels, titles and addresses were
compared in memory and dropped; the result files hold timestamps, classes and counts only. The
scripts were throwaway, live only in the scratchpad and are not kept; no file under `src/` or
`tests/` changed.

**Scope: which rules a same-document change can cross.** A user rule that names a path after a
"/" covers its whole site (`_path_rule_sites`, `Exclusions._on_user_site`,
[DECISIONS.md](../DECISIONS.md) 2026-11-16) and `pushState` cannot change the origin, so such a
rule cannot be crossed. Two kinds of rule can: a built-in path rule, `PASSWORD_MANAGER_PATHS`,
today only `proton.me` `/pass`, and a user rule with no path after a "/" (a host-less substring
glob such as `*secret*`, matched against the whole address). Cross-document changes get Chrome's
title event in 6-11 ms (2026-11-16). The 200 ms delay of Chrome's same-document title event can
therefore expose `proton.me/pass`, entered from another `proton.me` path without a navigation,
and host-less substring globs. No bank host or user path rule can be reached that way. The spike
stands in for the first with an extra entry `("127.0.0.1", "/secret")` in `PASSWORD_MANAGER_PATHS`,
added inside the spike process only.

## Machine

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01), Windows 11 build 26200, Python
3.14.10. The test window was on the right monitor only: LG UltraGear, 2560x1440, scale 100%, 120
Hz (`EnumDisplaySettings`), x 3840..6400. The page reported a 1200x800 viewport in every window;
the outer window was 1216x895 for Chrome and 1410x825 for Zen (its sidebar), at (3940, 100).

| Browser | Version |
| --- | --- |
| Chrome | 157.0.8103.79 (`chrome.exe` ProductVersion of the install; 2026-11-16 had .51) |
| Zen | 1.24.1b (`application.ini` `Version`, BuildID 20261106124806) |

Edge was left out: it is installed (157.0.4341.60), but a fresh Edge profile may sign in to the
PC's Microsoft account, which a throwaway profile must not do. Brave and Firefox were not tried.

## Method

- **Pages.** One neutral grey page (rgb 140,140,140) with a hidden muted-colour block (rgb
  190,110,80, the "B marker") over the centre 50% of the viewport (25% of its area); no flashing,
  at least 1.5 s between changes (a run lasts 3 s and a reset follows). Title "AKF17 page A" at
  `/a` is kept; "AKF17 page B" at `/secret` is excluded (`password_manager`) by the spike-only
  path entry. Before each browser start `Exclusions.check_url` was asserted for both addresses
  and for `normalize_url` of the Zen form. Akashic's own origin was given as port 5123, so
  loopback is not `own_ui`. A `/go` long-poll released each page action; t0 is the monotonic time
  just before the response (headers and body in one write).
- **Leak cases, no keystrokes.** L1: in one task the page sets a new `document.title` and shows
  the B marker, then moves its address to `/secret` with `pushState` after d = 0 (same task), 2
  animation frames, 100, 300 and 600 ms. L2 (control): show B, move the address two animation
  frames later, and set the title in that same task, last.
- **Production chain.** `ScreenCapture.run` (src/akashic/capture/screen.py) with `Settings()`,
  which builds `Desk` (`FRAME_INTERVAL_MS` 1000), `Verdicts` and `BudgetedUia` itself, over the
  real `Win32WindowEvents` (a subclass in the spike filters the hook to the test window and
  carries the variant switches), `WindowsUia` (a thin wrapper stamps each address read),
  `WgcCaptures` and `Exclusions`. Ports: displays are the right monitor only; the window list is
  the one test window (`Win32Windows._top_window` of that handle, nothing else is enumerated);
  focus is none (so the monitor is a side monitor, checked every 15 s when nothing triggers it),
  cursor and input are constants, `is_locked` and `awake_ms` are real. A writer spy classifies
  each submitted composite in memory: a leak is a submitted composite whose test-window rectangle
  holds at least 2% B-marker pixels (colour within +-12 of the marker). A run is "leaked" with
  such a composite, "excluded in time" when the independent recorder saw B but no composite
  carried it, "failed" when B was never shown.
- **Check timing.** Before each run the spike settled the window (kept, painted with A, no
  hold, last independent frame without B), set the monitor's `last_check` to minus infinity in
  the spike process (as game mode does) to force one check, and released t0 at a uniform random
  offset in [0, 1.2 s) after the end of that check. A run lasts 3 s from t0; a reset follows.
- **Variants** (switched inside the spike process): production (T = 500 ms, N = 3); hold off
  (`Verdicts._hold_start` returns none); `-4 dropped` (the spike's hook subclass drops the
  `OBJID_CLIENT` NAMECHANGE for Chrome; see "The -4 events and F5" for why it equals
  production); T = 1000 ms (`HOLD_MIN_S`), run when the production cell leaked or a hold ended
  at the bound. One variant was added after the first results: `-4 passed`, the hook
  subclass reports the `-4` NAMECHANGE as F5 intends, with `idChild` set to 0, in both
  browsers.
- **Recorders** (one process, stamped with `time.monotonic()`, the clock of the F5 events): a raw
  `SetWinEventHook` NAMECHANGE on the browser process (any `idObject`, any window; classified
  root window, child window, other); the production F5 events, `title` for the test window; a 2
  ms `GetWindowTextW` poll; a second `WgcCaptures` session at interval 0 with a 2 ms poll
  (frames classified for B); and the production address reads. `Verdicts._held`, `_hold_ends`,
  `_hold_start`, `judge` and `Desk.check` are wrapped to stamp holds (start, end, how, restart)
  and verdicts. "Ended by exclusion" is the check in which the address read returned the excluded
  address and the verdict flipped: the hold ends there, not through `_hold_ends`.
- **Validity, per run.** Invalid when, from t0 - 0.3 s to the end, the root window at the
  viewport centre (`WindowFromPoint`) was not the test window, the F5 hook reported unhealthy,
  or a spike window was the foreground window. 22 valid runs per leak cell. A read-only scan for
  other sessions' python/chrome/zen/firefox/msedge processes (command line in `Temp\claude\`
  outside this session, or `synthetic_window.py`) ran before each cell and each scenario; its
  count is in the tables (0 every time, the spike never waited).
- **Needless-hold scenarios.** Kept pages only, no B, one 3-minute run each on the production
  chain (and again with `-4 passed`): idle; a text counter updated each second; retitle every 2
  s; unread-count title every 10 s; `location.reload()` every 10 s; navigation to another kept
  page with the same title every 10 s; `pushState` to another kept path every 5 s; iframe load
  every 10 s; favicon change every 10 s. The blank share is the time the test window was held
  or pending in the entries of submitted composites (a window pending less than 0.5 s is left
  out of the picture by `Desk`, so it is never blank there); "time in hold" is derived
  (holds per minute times median length).
- **Needless hold (one definition, used in every verdict).** A hold that starts on a kept
  window in a scenario where no excluded address is ever shown (the needless-hold scenarios:
  kept pages only, no B), whatever its length and however it ends. A hold is counted when a
  check leaves it open; a hold that starts and ends in the same check (the address read already
  differs) is never open after a check and is not counted. Two costs are given for it: the
  blank share (`Desk` leaves the monitor out of the snapshot for up to 0.5 s of pending,
  `PENDING_WAIT_S`, then composes it with the window blank), and the extra address reads (reads in the run minus the 12 reads of an idle
  3-minute run). Whether a needless hold is worth its cover is read from the leak cells.
- **Focus.** A launched Chrome or Zen takes the foreground. Right after placing the window the
  spike gave the foreground back to the window that had it (`SetForegroundWindow`, then
  `SwitchToThisWindow`; no synthetic input) and counted it.

## Results

Statistics are median / p95 (nearest rank) / max; "-" means no value. Times are ms from t0.

### Leaks per cell

"Valid (attempts)" counts the runs that passed the validity rules.

| Browser | Scenario | Variant | Valid (attempts) | Invalid reasons | Leaked / excluded in time / failed | Runs with a leak | Leaked composites | Exclusivity scan |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chrome | L1 d=0 (same task) | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=0 (same task) | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=0 (same task) | -4 events dropped | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=0 (same task) | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | -4 events dropped | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=100 ms | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=100 ms | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=100 ms | -4 events dropped | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=100 ms | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=300 ms | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=300 ms | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=300 ms | -4 events dropped | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=300 ms | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=600 ms | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=600 ms | hold off | 22 (22) | - | 3 / 19 / 0 | 3 | 3 | 0 |
| chrome | L1 d=600 ms | -4 events dropped | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L1 d=600 ms | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L2 control (paint, address, title) | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L2 control (paint, address, title) | T=1000 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L2 control (paint, address, title) | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L2 control (paint, address, title) | -4 events dropped | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| chrome | L2 control (paint, address, title) | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=0 (same task) | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=0 (same task) | hold off | 22 (23) | obstructed 1 | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=0 (same task) | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=2 frames | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=2 frames | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=2 frames | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=100 ms | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=100 ms | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=100 ms | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=300 ms | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=300 ms | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=300 ms | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=600 ms | production T=500 N=3 | 22 (24) | foreground 2 | 1 / 21 / 0 | 1 | 1 | 0 |
| zen | L1 d=600 ms | T=1000 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L1 d=600 ms | hold off | 22 (22) | - | 6 / 16 / 0 | 6 | 6 | 0 |
| zen | L1 d=600 ms | -4 passed (any idChild) | 22 (22) | - | 1 / 21 / 0 | 1 | 1 | 0 |
| zen | L2 control (paint, address, title) | production T=500 N=3 | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L2 control (paint, address, title) | hold off | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |
| zen | L2 control (paint, address, title) | -4 passed (any idChild) | 22 (22) | - | 0 / 22 / 0 | 0 | 0 | 0 |

### Times from t0, production cells

| Browser | Scenario | Valid | -4 NAMECHANGE (n) | root NAMECHANGE (n) | F5 title event (n) | Title text poll, 2 ms (n) | First B pixels (n) | First read returning B, end (n) | Other raw NAMECHANGE (runs) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chrome | L1 d=0 (same task) | 22 | 0.9 / 1.1 / 1.4 (22) | 1.5 / 1.8 / 2.0 (22) | 1.5 / 1.8 / 2.0 (22) | 2.5 / 3.7 / 4.0 (22) | 7.8 / 19.1 / 24.9 (22) | 569.2 / 1004.0 / 1041.8 (22) | 0 |
| chrome | L1 d=2 frames | 22 | 0.9 / 1.0 / 1.0 (22) | 7.5 / 11.3 / 12.1 (22) | 7.5 / 11.3 / 12.1 (22) | 8.2 / 12.5 / 13.3 (22) | 5.8 / 12.8 / 16.6 (22) | 804.0 / 1072.8 / 1081.5 (22) | 0 |
| chrome | L1 d=100 ms | 22 | 0.8 / 1.0 / 1.1 (22) | 105.6 / 112.7 / 112.9 (22) | 105.7 / 112.7 / 112.9 (22) | 107.3 / 114.4 / 115.0 (22) | 4.4 / 7.0 / 12.9 (22) | 738.8 / 1102.4 / 1157.5 (22) | 0 |
| chrome | L1 d=300 ms | 22 | 0.8 / 1.0 / 1.1 (22) | 209.6 / 216.1 / 216.2 (22) | 209.6 / 216.2 / 216.2 (22) | 210.4 / 218.5 / 218.8 (22) | 4.2 / 11.0 / 19.5 (22) | 599.8 / 1043.6 / 1268.5 (22) | 0 |
| chrome | L1 d=600 ms | 22 | 0.8 / 1.0 / 1.0 (22) | 211.6 / 216.0 / 216.7 (22) | 211.6 / 216.0 / 216.7 (22) | 213.3 / 217.6 / 218.9 (22) | 4.6 / 14.4 / 17.9 (22) | 764.4 / 1125.9 / 1243.6 (22) | 0 |
| chrome | L2 control (paint, address, title) | 22 | 7.7 / 11.9 / 12.4 (22) | 214.4 / 221.9 / 224.6 (22) | 214.4 / 221.9 / 224.6 (22) | 215.6 / 221.8 / 226.5 (22) | 7.0 / 19.1 / 20.4 (22) | 812.9 / 1022.2 / 1093.6 (22) | 0 |
| zen | L1 d=0 (same task) | 22 | 1.9 / 3.9 / 5.9 (22) | 5.0 / 7.6 / 7.8 (22) | 5.0 / 7.6 / 7.8 (22) | 6.2 / 7.7 / 10.2 (22) | 3.5 / 14.2 / 16.4 (22) | 529.4 / 1079.8 / 1084.7 (22) | 0 |
| zen | L1 d=2 frames | 22 | 2.9 / 4.3 / 4.7 (22) | 5.2 / 9.7 / 11.1 (22) | 5.2 / 9.8 / 11.1 (22) | 6.9 / 9.6 / 11.0 (22) | 3.3 / 9.4 / 13.6 (22) | 659.0 / 1086.1 / 1091.0 (22) | 0 |
| zen | L1 d=100 ms | 22 | 2.5 / 2.9 / 2.9 (22) | 3.2 / 3.5 / 3.5 (22) | 3.1 / 3.5 / 3.6 (22) | 4.5 / 5.1 / 5.5 (22) | 3.4 / 10.2 / 13.1 (22) | 549.5 / 1079.6 / 1087.1 (22) | 0 |
| zen | L1 d=300 ms | 22 | 2.7 / 3.1 / 3.5 (22) | 3.4 / 3.7 / 4.2 (22) | 3.4 / 3.7 / 4.3 (22) | 4.5 / 6.1 / 6.3 (22) | 3.5 / 8.4 / 9.4 (22) | 618.6 / 1072.9 / 1086.0 (22) | 0 |
| zen | L1 d=600 ms | 22 | 2.7 / 3.7 / 3.9 (22) | 3.5 / 4.4 / 5.1 (22) | 3.5 / 4.4 / 5.1 (22) | 4.6 / 5.5 / 5.7 (22) | 4.5 / 9.8 / 11.9 (22) | 696.7 / 1046.3 / 1046.3 (18) | 0 |
| zen | L2 control (paint, address, title) | 22 | 7.2 / 16.7 / 24.3 (22) | 5.1 / 6.0 / 7.0 (22) | 5.1 / 6.0 / 7.0 (22) | 6.9 / 7.6 / 7.7 (22) | 20.2 / 45.3 / 46.0 (22) | 386.8 / 998.9 / 1031.2 (22) | 0 |

### Order counts, production cells

Counts are before / after-or-same-instant / none, relative to the first B pixels of the
independent recorder. Orders put `=` between events less than 0.5 ms apart.

| Browser | Scenario | Valid | -4 before pixels | root before pixels | F5 before pixels | read B before pixels | Order seen (runs) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| chrome | L1 d=0 (same task) | 22 | 22 / 0 / 0 | 22 / 0 / 0 | 22 / 0 / 0 | 0 / 22 / 0 | `-4 < F5 = root < pixels < read B` 17; `-4 < root = F5 < pixels < read B` 4; `-4 = root = F5 < pixels < read B` 1 |
| chrome | L1 d=2 frames | 22 | 22 / 0 / 0 | 8 / 14 / 0 | 8 / 14 / 0 | 0 / 22 / 0 | `-4 < pixels < F5 = root < read B` 13; `-4 < F5 = root < pixels < read B` 6; `-4 < root = F5 < pixels < read B` 2; `-4 < pixels = F5 = root < read B` 1 |
| chrome | L1 d=100 ms | 22 | 22 / 0 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | `-4 < pixels < F5 = root < read B` 17; `-4 < pixels < root = F5 < read B` 5 |
| chrome | L1 d=300 ms | 22 | 22 / 0 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | `-4 < pixels < F5 = root < read B` 18; `-4 < pixels < root = F5 < read B` 4 |
| chrome | L1 d=600 ms | 22 | 22 / 0 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | `-4 < pixels < F5 = root < read B` 21; `-4 < pixels < root = F5 < read B` 1 |
| chrome | L2 control (paint, address, title) | 22 | 12 / 10 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | 0 / 22 / 0 | `-4 < pixels < F5 = root < read B` 7; `pixels < -4 < F5 = root < read B` 4; `pixels < -4 < root = F5 < read B` 3; `pixels = -4 < F5 = root < read B` 3; `-4 = pixels < F5 = root < read B` 2; `-4 < pixels < root = F5 < read B` 2; `-4 = pixels < root = F5 < read B` 1 |
| zen | L1 d=0 (same task) | 22 | 22 / 0 / 0 | 8 / 14 / 0 | 8 / 14 / 0 | 0 / 22 / 0 | `-4 < pixels < F5 = root < read B` 13; `-4 < F5 = root < pixels < read B` 7; `-4 = F5 = root < pixels < read B` 1; `-4 < pixels < root = F5 < read B` 1 |
| zen | L1 d=2 frames | 22 | 17 / 5 / 0 | 6 / 16 / 0 | 6 / 16 / 0 | 0 / 22 / 0 | `-4 < pixels < F5 = root < read B` 5; `-4 < F5 = root < pixels < read B` 4; `-4 = pixels < F5 = root < read B` 4; `pixels = -4 < F5 = root < read B` 3; `-4 < root = F5 < pixels < read B` 2; `-4 = pixels < root = F5 < read B` 2; `-4 < pixels = root = F5 < read B` 1; `-4 < pixels = F5 = root < read B` 1 |
| zen | L1 d=100 ms | 22 | 22 / 0 / 0 | 15 / 7 / 0 | 15 / 7 / 0 | 0 / 22 / 0 | `-4 < F5 = root < pixels < read B` 10; `-4 < F5 = root = pixels < read B` 4; `-4 = pixels = F5 = root < read B` 3; `-4 < root = F5 = pixels < read B` 1; `-4 = pixels < F5 = root < read B` 1; `-4 = pixels = root = F5 < read B` 1; `-4 < F5 = pixels = root < read B` 1; `-4 < pixels = F5 = root < read B` 1 |
| zen | L1 d=300 ms | 22 | 22 / 0 / 0 | 11 / 11 / 0 | 10 / 12 / 0 | 0 / 22 / 0 | `-4 < F5 = root < pixels < read B` 8; `-4 < pixels = F5 = root < read B` 6; `-4 = pixels = F5 = root < read B` 3; `-4 < root = F5 = pixels < read B` 2; `-4 < root = F5 < pixels < read B` 1; `-4 < F5 = pixels = root < read B` 1; `-4 < pixels < F5 = root < read B` 1 |
| zen | L1 d=600 ms | 22 | 22 / 0 / 0 | 13 / 9 / 0 | 12 / 10 / 0 | 0 / 18 / 4 | `-4 < F5 = root < pixels < read B` 10; `-4 < pixels = F5 = root < read B` 5; `-4 < pixels = root = F5 < read B` 2; `-4 < pixels = F5 = root` 2; `-4 < F5 = root < pixels` 2; `-4 < root = F5 = pixels < read B` 1 |
| zen | L2 control (paint, address, title) | 22 | 21 / 1 / 0 | 22 / 0 / 0 | 22 / 0 / 0 | 1 / 21 / 0 | `F5 = root < -4 < pixels < read B` 11; `-4 < F5 = root < pixels < read B` 5; `-4 = F5 = root < pixels < read B` 2; `-4 < F5 = root = pixels < read B` 1; `F5 = root < -4 < read B < pixels` 1; `F5 = root < pixels < -4 < read B` 1; `-4 < root = F5 < pixels < read B` 1 |

### Holds in the leak runs

Holds that started before t0 (the late title event of the reset, Chrome) are left out of the
statistics and counted in the last column.

| Browser | Scenario | Variant | Runs with a hold | Hold start, ms from t0 | Hold end, ms from t0 | Ended by address / bound / exclusion | Restarted holds | Runs with a hold verdict | Holds from before t0 (left out) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chrome | L1 d=0 (same task) | production T=500 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=0 (same task) | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=0 (same task) | -4 events dropped | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=0 (same task) | -4 passed (any idChild) | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | production T=500 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | -4 events dropped | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=2 frames | -4 passed (any idChild) | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 1 |
| chrome | L1 d=100 ms | production T=500 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=100 ms | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=100 ms | -4 events dropped | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=100 ms | -4 passed (any idChild) | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=300 ms | production T=500 N=3 | 1 | 207.2 / 207.2 / 207.2 | 409.9 / 409.9 / 409.9 | 0 / 0 / 1 | 0 | 1 | 0 |
| chrome | L1 d=300 ms | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=300 ms | -4 events dropped | 3 | 214.2 / 214.4 / 214.4 | 400.3 / 419.1 / 419.1 | 0 / 0 / 3 | 0 | 3 | 0 |
| chrome | L1 d=300 ms | -4 passed (any idChild) | 3 | 102.1 / 214.6 / 214.6 | 371.8 / 415.5 / 415.5 | 0 / 0 / 3 | 3 | 3 | 0 |
| chrome | L1 d=600 ms | production T=500 N=3 | 7 | 210.8 / 213.9 / 213.9 | 656.5 / 705.4 / 705.4 | 0 / 0 / 7 | 0 | 7 | 0 |
| chrome | L1 d=600 ms | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L1 d=600 ms | -4 events dropped | 9 | 210.1 / 216.3 / 216.3 | 688.3 / 738.2 / 738.2 | 0 / 0 / 9 | 1 | 9 | 0 |
| chrome | L1 d=600 ms | -4 passed (any idChild) | 12 | 208.3 / 216.3 / 216.3 | 700.4 / 1689.3 / 1689.3 | 0 / 0 / 12 | 3 | 12 | 0 |
| chrome | L2 control (paint, address, title) | production T=500 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 1 |
| chrome | L2 control (paint, address, title) | T=1000 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 2 |
| chrome | L2 control (paint, address, title) | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L2 control (paint, address, title) | -4 events dropped | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| chrome | L2 control (paint, address, title) | -4 passed (any idChild) | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=0 (same task) | production T=500 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=0 (same task) | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=0 (same task) | -4 passed (any idChild) | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=2 frames | production T=500 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=2 frames | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=2 frames | -4 passed (any idChild) | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 1 |
| zen | L1 d=100 ms | production T=500 N=3 | 5 | 3.2 / 3.4 / 3.4 | 164.9 / 184.2 / 184.2 | 0 / 0 / 5 | 0 | 5 | 0 |
| zen | L1 d=100 ms | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=100 ms | -4 passed (any idChild) | 1 | 4.9 / 4.9 / 4.9 | 177.9 / 177.9 / 177.9 | 0 / 0 / 1 | 0 | 1 | 0 |
| zen | L1 d=300 ms | production T=500 N=3 | 4 | 3.5 / 3.7 / 3.7 | 345.2 / 373.8 / 373.8 | 0 / 0 / 4 | 0 | 4 | 0 |
| zen | L1 d=300 ms | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=300 ms | -4 passed (any idChild) | 6 | 7.5 / 15.1 / 15.1 | 355.9 / 414.1 / 414.1 | 0 / 0 / 6 | 0 | 6 | 1 |
| zen | L1 d=600 ms | production T=500 N=3 | 11 | 3.3 / 4.1 / 4.1 | 609.1 / 675.4 / 675.4 | 0 / 4 / 7 | 0 | 11 | 3 |
| zen | L1 d=600 ms | T=1000 N=3 | 11 | 3.2 / 3.4 / 3.4 | 650.0 / 707.1 / 707.1 | 0 / 0 / 11 | 0 | 11 | 0 |
| zen | L1 d=600 ms | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L1 d=600 ms | -4 passed (any idChild) | 14 | 8.4 / 22.4 / 22.4 | 619.0 / 707.1 / 707.1 | 0 / 3 / 11 | 0 | 14 | 0 |
| zen | L2 control (paint, address, title) | production T=500 N=3 | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L2 control (paint, address, title) | hold off | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |
| zen | L2 control (paint, address, title) | -4 passed (any idChild) | 0 | - | - | 0 / 0 / 0 | 0 | 0 | 0 |

### Needless-hold scenarios

| Browser | Variant | Scenario | -4 / min | root / min | Holds started / min | Hold length ms (median / p95 / max) | Ended by address / bound / other | Time in hold (derived) | Restarted | Open at end | Blank share of time | Unhealthy drains | Exclusivity scan |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chrome | production | idle page | 0.33 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | text counter every 1 s | 0.67 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | retitle every 2 s | 90.33 | 30.0 | 30.0 | 665.7 / 713.3 / 716.4 | 0 / 90 / 0 | 33% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | unread-count title every 10 s | 18.33 | 6.0 | 6.0 | 681.4 / 714.2 / 714.2 | 0 / 18 / 0 | 7% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | same-address reload every 10 s | 12.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | same-title navigation every 10 s | 54.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | pushState every 5 s | 12.33 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | iframe load every 10 s | 0.67 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | production | favicon change every 10 s | 0.33 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | -4 passed | idle page | 0.33 | 0.0 | 0.67 | 748.9 / 848.5 / 848.5 | 0 / 2 / 0 | 1% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | -4 passed | text counter every 1 s | 0.33 | 0.0 | 0.67 | 758.0 / 820.4 / 820.4 | 0 / 2 / 0 | 1% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | -4 passed | retitle every 2 s | 90.33 | 30.0 | 30.67 | 648.5 / 711.4 / 1191.1 | 0 / 90 / 0 | 33% | 2 | 2 | 0.56% | 0 | 0 |
| chrome | -4 passed | unread-count title every 10 s | 18.67 | 6.0 | 7.0 | 648.5 / 710.8 / 893.8 | 0 / 21 / 0 | 8% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | -4 passed | same-address reload every 10 s | 12.0 | 0.0 | 11.67 | 634.0 / 680.2 / 816.3 | 0 / 36 / 0 | 12% | 0 | -1 | 0.0% | 0 | 0 |
| chrome | -4 passed | same-title navigation every 10 s | 54.0 | 0.0 | 6.0 | 655.1 / 862.7 / 862.7 | 0 / 18 / 0 | 7% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | -4 passed | pushState every 5 s | 12.33 | 0.0 | 0.67 | 700.4 / 924.3 / 924.3 | 1 / 1 / 0 | 1% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | -4 passed | iframe load every 10 s | 0.67 | 0.0 | 1.0 | 693.1 / 935.9 / 935.9 | 0 / 3 / 0 | 1% | 0 | 0 | 0.0% | 0 | 0 |
| chrome | -4 passed | favicon change every 10 s | 0.33 | 0.0 | 0.67 | 771.1 / 849.9 / 849.9 | 0 / 2 / 0 | 1% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | idle page | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | text counter every 1 s | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | retitle every 2 s | 120.0 | 30.0 | 30.33 | 616.1 / 675.8 / 825.2 | 0 / 90 / 0 | 31% | 0 | 1 | 0.0% | 0 | 0 |
| zen | production | unread-count title every 10 s | 24.0 | 6.0 | 6.0 | 623.3 / 684.3 / 684.3 | 0 / 18 / 0 | 6% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | same-address reload every 10 s | 6.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | same-title navigation every 10 s | 27.67 | 10.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | pushState every 5 s | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | iframe load every 10 s | 6.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | production | favicon change every 10 s | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | idle page | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | text counter every 1 s | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | retitle every 2 s | 120.0 | 30.0 | 30.33 | 627.7 / 677.1 / 843.9 | 0 / 90 / 0 | 32% | 0 | 1 | 0.0% | 0 | 0 |
| zen | -4 passed | unread-count title every 10 s | 24.0 | 6.0 | 6.0 | 659.5 / 678.1 / 678.1 | 0 / 18 / 0 | 7% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | same-address reload every 10 s | 6.0 | 0.0 | 6.0 | 630.1 / 678.6 / 678.6 | 0 / 18 / 0 | 6% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | same-title navigation every 10 s | 26.33 | 8.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | pushState every 5 s | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | iframe load every 10 s | 6.0 | 0.0 | 6.0 | 625.0 / 674.2 / 674.2 | 0 / 18 / 0 | 6% | 0 | 0 | 0.0% | 0 | 0 |
| zen | -4 passed | favicon change every 10 s | 0.0 | 0.0 | 0.0 | - | 0 / 0 / 0 | 0% | 0 | 0 | 0.0% | 0 | 0 |

### The -4 events and F5

- Every leak run had a raw `-4` (`OBJID_CLIENT`) NAMECHANGE on the test window: 418 of 418 Chrome
  runs and 286 of 286 Zen runs (production cells and variants of the first series). The first F5
  `title` event was the root event in 417 of 418 and 286 of 286, and the `-4` event came earlier
  than the root event in 418 and 251: F5 never reported a `-4` event in those runs.
- A debugging run (not in the tables) printed `idChild` of Chrome's `-4` NAMECHANGE events:
  -620, -152, -619 and -271, never `CHILDID_SELF`. `Win32WindowEvents._on_event` returns for
  `id_child != _CHILDID_SELF`, so none passes. In Chrome the scenarios with 12-54 `-4` events a
  minute (reload, same-title navigation, `pushState`) started no hold; Zen's `-4` events behaved
  the same (reload 6 a minute, iframe 6 a minute, no hold in production, 6 holds a minute with
  `-4 passed`). Zen's `idChild` values were not printed.
- Raw NAMECHANGE on another object or window of the browser process: none in the leak runs; 3 in
  the Zen favicon scenario.

## What the numbers say

- **Leaks.** Production leaked 1 run of 264 in the first series: Zen L1 d=600 ms, 1 composite.
  Chrome 0 of 132; Zen 1 of 132. The `-4 passed` series gave the same: Chrome 0 of 132, Zen 1
  of 132 (d=600 again). With the hold off, 9 runs of 264 leaked, all at d=600 ms (Chrome 3 of
  22, Zen 6 of 22); at d <= 300 ms nothing leaked even with the hold off. L2 (address first,
  title last) leaked nowhere. Every other cell (all of L1 d <= 300 and all of L2, in every
  variant) was at 0. No run failed (B was shown in all), and no address read returned nothing in a leak run.
- **Why d <= 300 ms did not leak even with the hold off.** Window events are debounced 0.3 s
  (`DEBOUNCE_S["window"]`), a monitor is re-checked at least 1.0 s after its last check
  (`min_gap_s`), and the focus-less monitor is checked every 15 s otherwise. The first read
  returning B ended 387-813 ms after t0 at the median (Chrome 569-813, Zen 387-697; p95 0.99-1.13
  s, max up to 1.27 s) and before the first B pixels in 1 of 264 production runs (Zen L2): the
  pixels (3.3-7.8 ms at the median in L1, 7.0-20.2 ms in L2) came first, the address second, and
  the check last.
- **What the hold covered.** At d=600 ms the check lands between the title event and the
  address move in a part of the runs (offsets 0.5-0.85 s after the last check): with the hold
  off, 3 of 22 (Chrome) and 6 of 22 (Zen) of the composites carried B. With the hold, Chrome
  held 7 runs and 0 leaked: the holds started at the title event (211-214 ms) and ended at
  656-705 ms (median / max) by the exclusion. Zen held 11 runs: 7 ended by exclusion (hold lengths 592-672 ms,
  ends 596-675 ms from t0), 4 reached the bound after 507-586 ms before the address read
  changed, and 1 of those 4 leaked (its composite carried B). The 4 runs of that cell with no
  read returning B are those 4 (see "An address that moves later than the bound"). With T = 1000 ms Zen's 11 holds all ended by exclusion
  (650-707 ms) and 0 of 22 leaked.
- **Title events versus pixels.** Chrome's title event reaches F5 1.5 ms after the title set
  when `pushState` is in the same task, 7.5 ms with 2 frames, 105-107 ms when the address moves
  at 100 ms (the navigation commit flushes it), and 210-225 ms otherwise (d = 300, 600, L2): the
  200 ms same-document delay of 2026-11-16, 0 of 22 before the B pixels at d >= 100. Zen's is
  3-5 ms at the median in every case (max 11 ms), before the pixels in 6-22 of 22 runs (73 of
  132 over the production cells; Chrome 30 of 132). The 2 ms title poll follows F5 by 1-2
  ms at the median. A same-document
  Chrome title event after the pixels has no effect here because the check it triggers comes 0.3 s
  after it; see the decision.
- **Holds.** Production holds ended by exclusion in 8 of 8 Chrome and 16 of 20 Zen leak runs
  with a hold, by the bound in the other 4 (Zen, all d=600 ms), and by a changed address with
  the verdict still kept in none. Restarts by a second event: 0 in the production cells, 1 in
  the Chrome `-4 dropped` d=600 cell (the same events as production), 6 with `-4 passed` in
  Chrome (3 at d=300, 3 at d=600; one hold lasted 1.7 s), 0 in Zen.
- **Needless holds (production).** A kept page that only retitles (Chrome and Zen, every 2 s
  or every 10 s with an unread count) starts one hold per title change that runs to the bound:
  90 and 18 ended holds in each browser, all at the bound, 0.62-0.68 s long at the median (max
  0.83 s), about 33% and 7% of the time in hold (Zen 31% and 6%). The blank share in submitted
  composites was 0.0% in all 18 production cells: the first check comes about 0.3 s after the
  event, so a hold ends before `PENDING_WAIT_S` (a few Zen leak runs had an earlier check, see
  Limits). The other costs are not zero: a held window keeps its monitor out of the snapshot
  for up to 0.5 s of pending, then blank (the 0.0% above counts only the blank part), and the retitle page was read 270 times in 180 s (Chrome; Zen 271)
  against 12 on an idle page, about 3 extra reads a hold and 86 extra reads a minute (unread
  count: 54 reads, 14 extra a minute). The other scenarios (idle, counter, reload, same-title
  navigation, `pushState`, iframe, favicon) started 0 holds in both browsers: Chrome's reload,
  same-title navigation and `pushState` give only `-4` events, which F5 does not deliver, and
  Zen's same-title navigation gives root events whose check sees a changed address, so that
  hold starts and ends in the same check and is not counted (0 started, see Method).
- **Needless holds (`-4 passed`).** Same costs as above, with 0.0% blank share except on the
  Chrome retitle page. Chrome: 11.7 holds a minute on reload (36 of 36 to the bound, 12% of the
  time, 107 reads in 180 s against 12), 6 on same-title navigation (18 of 18, 7%), 0.7 on an idle page and on the
  counter page (stray `-4` events), 1.0 on an iframe, 0.7 on a favicon change, 0.7 on `pushState`;
  2 restarted holds in the retitle scenario, one 1.19 s long, and a blank share of 0.56% there.
  Zen: 6 holds a minute on reload and on iframe load (all to the bound, about 6% of the time,
  54 and 57 reads in 180 s), none elsewhere beyond production; blank share 0.0%. One scenario row has an open count of -1:
  a hold from before the measurement ended inside it.
- **Reset hold.** 4 holds in Chrome and 5 in Zen (all cells) started before t0, from the late
  title event of the reset. Chrome L2's T = 1000 ms cell was run because one of them in the
  production L2 cell ended at the bound (-609 to -43 ms); no hold of that cell's own runs did.

## Decision for F17

Verdicts are per constant; the cells are in "Results". "Needless hold" is the one definition
of Method, applied to every verdict below: a hold in a scenario with no excluded address,
judged by its two costs (blank share, with the monitor left out of the snapshot for up to 0.5 s of pending; extra reads) against
the cover it buys in the leak cells.

1. **`HOLD_MIN_S` (0.5 s): no change.** Chrome 0 of 132 production runs leaked; Zen's single production leak (L1 d=600 ms, 1 of 22) had its address move 596 ms after the
   title event, above the 500 ms bound, and the title event 0.4 ms after the first B pixels
   (not before). T = 1000 ms closed it (1 of 22 to 0 of 22, within noise per Limits) but the page
   chooses its delay: the coverage ends at about event + T (Chrome: title set + 0.21 s + T, Zen:
   T), so a longer bound only moves the edge, at a derived (not measured) cost: bound-ended
   needless holds last about T plus one check (0.62-0.68 s at T = 0.5 s), so about 1.1-1.2 s at
   T = 1.0 s, roughly 55-60% of the time in hold on retitle pages instead of 31-33%, plus blank
   frames once pending passes 0.5 s. The retitle and unread-count holds are needless holds: 90 and 18 holds, every
   one to the bound, 31-33% and 6-7% of the time in hold, about 3 extra reads each (86 extra
   reads a minute on the retitle page) and the monitor left out of the snapshot for up to 0.5 s
   of pending, with 0.0% blank share. A shorter T is not supported by the cells: in Chrome at
   d=600 ms the address moves about 0.39 s after the title event (hold start 211-214 ms, move at
   600 ms from t0), and with the hold off that cell leaked 3 of 22, so a T at or below about
   0.4 s would end these holds before the move; 0.4-0.5 s was not measured. The hold cannot tell at
   its start a retitle from a title change that precedes a move to an excluded address, so these
   needless holds are the price of that cover.
2. **`HOLD_MIN_READS` (3): no change.** The number of reads each bound-ended hold had when T
   first elapsed was not recorded (the result files keep each hold's start and end, not the
   reads between), so how many holds N alone ended cannot be reported. The run totals give
   about 3 extra reads per retitle hold (270 reads for 90 holds, 12 of them idle) in 0.62-0.68 s
   at the median, starting about 0.3 s after the event: a check every `poll_s` (0.1 s) plus the
   check itself, about 0.15-0.17 s. Bound-ended holds last 0.62-0.68 s against T = 0.5 s (Zen
   leak cells 507-586 ms; the longest ran 0.33 s past T, Zen max 825 ms). No cell shows N adding
   cover, and the medians show it adding about one check (about 0.15 s) per needless hold, so
   neither removing nor raising it has a cell behind it.
3. **Hold start.**
   - **Start at the title event: no change.** Hold on: 1 of 264 leaked (Chrome 0, Zen 1);
     hold off: 9 of 264, all at d=600 ms.
   - **Restart on a later event: no change.** 0 restarts in the production cells of both
     browsers and in every production scenario; the one restart in the `-4 dropped` cell ended by
     exclusion. A restart only matters when a second event for the same change arrives, which
     F5 as it is does not deliver for Chrome's `-4`.
   - **`-4` as a start: keep `-4` undelivered.** It adds no cover: `-4 passed` leaked 0 of 132
     in Chrome and 1 of 132 in Zen, the same as production. It adds needless holds, under the
     same definition and costs as the retitle holds above: Chrome 11.7 a minute on reload (12%
     of the time, 107 reads in 180 s) and 6 on same-title navigation (7%), holds on idle and
     counter pages, restarts, a 1.7 s hold and a 0.56% blank share; Zen 6 a minute on reload
     and iframe loads (about 6% of the time each). The retitle holds buy cover (leak cells);
     these buy none. `Win32WindowEvents._on_event` (events.py:263-267) already accepts an
     `OBJID_CLIENT` `NAMECHANGE`, but the `id_child != _CHILDID_SELF` check makes that branch
     dead for Chrome (idChild -620, -152, -619, -271; Zen's not printed, its `-4` events
     behaved the same). F5 as delivered never reports Chrome's `-4`, and the `-4 dropped`
     variant is identical to production. F17 removes the dead `client_name` branch and corrects
     three notes that say it works: the ports.py:228 docstring, the events.py:77 comment and
     line 6 of [2026-11-16-title-change-hold.md](2026-11-16-title-change-hold.md).

Uncovered leaks that remain, and why no bound covers them:

- **An address that moves later than the bound after the title event** (Zen L1 d=600 ms leaked 1
  of 22 at T = 500 ms; hold off 6 of 22; Chrome's covered d goes up to about title delay + T,
  about 0.71 s). The page chooses the delay, so every T has a larger d;
  T = 1000 ms covers d = 600 ms in these cells and costs longer needless holds (see item 1, derived).
  In Zen d=600 ms 4 holds reached the bound before the address read changed and 1 leaked: the
  composite at the bound-end check is the leak. The verdict then stays kept until the next
  interval check, because `pushState` gives no event; those 4 runs had no read returning B
  within 3 s.
- **A check between the B pixels and Chrome's late title event.** Chrome's same-document title
  event and the title text arrive 105-225 ms after the pixels (d >= 100: 0 of 22 before them), so
  no event, no `-4` and no title change exists yet for a check in that gap; no bound or start
  rule helps because the hold has nothing to start from. No event-driven check fell there (the
  debounce is 0.3 s), and a periodic check could: not measured, about 0.2 s over the 15 s
  side-monitor or 5 s focus interval.
- **A same-document change with no title change** (no event of any kind in Chrome after F5's
  filter; 2026-11-16 case 4b leaked 22 of 22): not part of these cells.

## Limits

- Fresh profiles, tiny local pages, one tab, no extensions; Chrome and Zen only; the user's real
  pages and tab counts untested. 22 runs per cell: p95 is the second largest value. The leak
  numbers are counts of 22; 1 of 22 is within noise of 0 of 22.
- The window is a side monitor window with no focus and no input (focus text reads, the 5 s
  focus interval and input triggers are absent), and no keyboard or mouse case. The forced check
  before each run is a spike poke of the monitor's `last_check`; t0 is placed after the end of
  that check, not after its start.
- Only d up to 600 ms: the edge of Chrome's coverage (about 0.71 s) and Zen's (about 0.5 s plus
  up to 0.1 s of check spacing) was not measured beyond it, and T = 1000 ms only in the cells
  that qualified (Zen d=600; Chrome L2, run on carried-over holds, not on its own).
- The page-driven title event of Chrome is flushed by `pushState` after 100 ms or less; a real
  site may behave differently. The scenarios are one 3-minute run each: rates are counts of that
  run (a count of 1 at 0.33 a minute is a single event).
- The independent B detector copies frames at a 2 ms poll and may coalesce frames; the production
  WGC session delivers at most one frame a second, so the pixels that the production chain sees
  are later than the independent recorder's times.
- A few Zen runs had a check at about 70 ms after t0, earlier than the debounce explains
  (cause not established).
- Invalid runs: Zen 3 (1 obstructed, 2 foreground) in the first series; none otherwise. Two
  attempts of the `-4 passed` series were discarded (0 valid runs, no viewport size reported):
  in one a window the spike had not started covered the right monitor, in the other the spike
  picked a Chrome window by class and size instead of by page title; the title is now required
  and only later, valid runs are in the tables. The window picked in the first series was the
  page's window in every cell (B was seen and the viewport was 1200x800 in all of them).
- Zen showed its "Open Zen in Troubleshoot Mode?" dialog on the primary monitor in 3 launches
  (the first two ended the script before a retry existed; it appeared where Zen puts a dialog
  and was closed by killing the spike's Job; the cause was not established). Zen's window
  itself was placed on the right monitor in every counted launch.
- **Focus.** The new window took the foreground in 6 of the 8 launches behind the tables
  (Chrome 4 of 4, Zen 2 of 4) and in about ten other launches (smoke tests, debugging,
  discarded runs). Each time the spike activated the user's previous foreground window, a window of a
  process the spike did not start, with `SetForegroundWindow` and `SwitchToThisWindow` and
  without synthetic input. No window was placed on the primary or left monitor on purpose. Only
  processes in the spike's own Job were ended by the spike; after a discarded attempt, the
  spike's own scripts were stopped by process id.
- Four debugging runs printed window data: two the class, process id and rectangle of the
  window covering the viewport centre (no title; in one of them that window was not a spike
  window, the one that covered the right monitor in a discarded attempt), and two Chrome
  process ids with main-window titles (only the spike's own page title was non-empty). Nothing
  of that is in the result files.
- Edge, Brave, Firefox, private windows, popups, typing in the address bar, hung browsers and real
  websites were not covered; any change of the hold code, bounds or event mapping are for F17.
