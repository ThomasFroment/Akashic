# UI Automation reads under load spike 2026-11-17

Measurements behind TODO A9 F19 (a side-browser reader that cannot stall the capture loop) of the
monitor-capture rules ([CAPTURE.md](../CAPTURE.md)): what a stalled browser costs the real
`Desk.check` for a healthy neighbour, whether a stalled window drags its siblings along, what a
healthy read costs (the proposed worker wait), whether UI Automation reads run in parallel, and
what N ownerless browser windows cost per check (OPEN A9 F10). Follows
[2026-11-16-browser-reads.md](2026-11-16-browser-reads.md). Test windows were disposable Chrome
and Zen processes started by the spike with throwaway profiles and a page server on 127.0.0.1;
the user's browsers, profiles and windows were not touched (see "Foreground" for the one
exception). Reads were compared in memory only; this report and the raw result files hold
numbers, counts, booleans, result kinds and window class names only. The scripts, results and
profiles were throwaway and are deleted; no file under `src/` or `tests/` changed.

## Machine

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01), Windows 11 build 26200, Python
3.14.10. Monitors: 3840x2160 at 120 DPI (primary), 2560x1440 at 96 DPI (100%) at x 3840-6400
(the "right monitor", `right_monitor()`), and a 2560x1440 at 96 DPI left of the primary (not
used). Half A is the left half of the right monitor (x 3840-5120, y 0-1440), half B the right
half (x 5120-6400). Test windows sat inside a half: A at (3857, 10) 1246x693, B at (5137, 10)
1246x693, the sibling at (5137, 730) 1246x553; the F10 windows were 700x500 cascades on half B.

| Browser | Version |
| --- | --- |
| Chrome | 157.0.8103.79 |
| Zen | 1.24.1b (`application.ini` `Version`, build 20261106124806) |

Edge, Brave and Firefox were not measured.

## Method

- **Conduct.** Every browser was a Chrome or Zen process with a throwaway profile. Chrome flags:
  `--no-first-run --no-default-browser-check --disable-search-engine-choice-screen
  --disable-sync --disable-popup-blocking --window-position --window-size`. Zen:
  `-no-remote -profile` with a `user.js` that turns telemetry, updates, accounts, safe browsing
  and geolocation off, sets `toolkit.startup.max_resumed_crashes` to -1 and
  `dom.disable_open_during_load` to false, and a preset `xulstore.json` placing the window on the
  right monitor. Pages came from a spike server on 127.0.0.1 (plain page; opener page; busy page).
  Browsers were started minimized without activation (`STARTUPINFO` `SW_SHOWMINNOACTIVE`), then
  placed with `SetWindowPlacement` (`SW_SHOWNOACTIVATE`) on a half of the right monitor. No
  `SendInput`, keystroke or click.
- **Containment.** The spike put itself in a `KILL_ON_JOB_CLOSE` job object before launching
  anything, so every browser, hand-off process and reader child inherited it. Before every
  `NtSuspendProcess`, `SuspendThread`, UIA read target selection and termination the pid was
  checked against `QueryInformationJobObject` (`JobObjectBasicProcessIdList`) and
  `IsProcessInJob`. Every suspend was undone in a `finally`; the `resume ran` flag was true in
  all 16 suspend and thread-suspend runs (valid and disturbed) that applied a suspend. Cell
  teardown sent `WM_CLOSE` to the job's windows, waited up to 15 s, and terminated only job
  pids left over: 0 terminations in every result file that recorded it (which files did not is
  not known). No process was killed by
  command line.
- **Isolation.** The window list given to `Desk.check` was the real `Win32Windows.windows()`
  filtered to the job's pids (plus any ghost window whose `ghost_of` was a job window: none
  appeared). `Exclusions` got an `api_origin` on another loopback port. Before each cell a
  read-only scan counted python, chrome, zen, firefox and msedge processes whose command line
  pointed at another session's `Temp\claude\` folder: 46 scans, 0 found (so the left-monitor
  spike was not detectable by that rule).
- **Foreground.** Freshly launched Chrome and Zen sometimes took the foreground despite
  `SW_SHOWNOACTIVATE`. After each placement, a spike window that held the foreground was
  handed back to the window that had it before the launch (`SetForegroundWindow`; the
  variant attached to the input threads of both windows; then `SwitchToThisWindow`; no
  synthetic input). **This activates a user (or another session's) window, or the taskbar, in a
  process the spike did not start.** The baseline window was the taskbar (`Shell_TrayWnd`) at
  the start; when a baseline window had gone, the taskbar was used. At least 96 hand-backs were
  counted in the results used for the tables, 1 failed to restore, and 11 foreground changes to a window outside the job were observed
  (the foreground was also moved by the other session's spike and the user). The foreground was
  checked before each check and after each run; a run during which it differed from the
  post-launch baseline, or became a spike window, is "disturbed" and left out of the tables.
  Disturbed counts per cell are in "Stall cases".
- **Set-up.** The real `Desk(WgcCaptures(), Verdicts(Exclusions(...), BudgetedUia(...),
  BrowserState()))` with two synthetic `MonitorInfo` halves (A healthy, B stalled), the
  unchanged `WindowsUia` wrapped only to time each reader call and record its result kind
  (value / `None` / exception type) inside `BudgetedUia`. `Desk.check(monitors, due, windows,
  None, time.monotonic())` every 1 s; "joint" = both halves due; A-only and B-only checks were
  also timed. Frames stayed in memory (real `WgcCaptures`, `FRAME_INTERVAL_MS`). A and B were
  two processes of the same browser (Chrome A and B, Zen A and B), plus a sibling top-level
  window of B's process opened by the B page (`window.open` with `popup,noopener` and a
  position; the sibling was placed under B with `SetWindowPlacement`). Statistics are n,
  median, p95, p99, max (nearest rank) in ms.
- **Control.** One run per browser: 245 checks at 1 s in the pattern joint, A-only, joint,
  B-only, joint (10 discarded as warm-up), all windows healthy.
- **Stall cases** (each for Chrome and Zen, until 3 valid runs of at least 35 s; joint check
  every 1 s; 8 joint checks before the stall): (1) `NtSuspendProcess` of the process that owns
  B's window (Zen: the window's owner process, not the launcher); (2) `SuspendThread` of the
  thread that owns B's window; (3) busy page: a JS loop of 45 s released by a long-poll
  (`fetch('/go')`) answered by the spike at the stall start, plus a 100 ms `fetch('/hb')`
  heartbeat to prove the page was busy. A joint check kept running until 36.5 s after the stall
  start and until one check after the 30 s backoff ended. After the joint phase two B-only and
  two A-only checks were timed. A second thread with its own `WindowsUia` (MTA) read A's window
  (caption then address) every 50 ms for the whole run. Direct sibling reads (own
  `WindowsUia`, not through the budget) were made 6 s and 22 s after the stall start; the Desk's
  own reads of the sibling were recorded too. New top-level windows were counted by class name
  before and after.
- **Parallel reads.** Two set-ups: four windows of one Chrome process (the extra windows via
  `chrome --user-data-dir=<same> --new-window`, a hand-off into the running process), and four
  processes (Chrome A, B, C and Zen), in a 2x2 grid on the right monitor. Three modes
  interleaved per round: sequential (main thread), one persistent thread per reader (own
  MTA `WindowsUia`, released on a barrier), one child process per reader (own `WindowsUia`,
  released by a named event; `perf_counter` stamps are system wide on Windows). 1, 2 and 4
  readers, 2 warm-up rounds and 20 measured rounds each; a round is one caption and one
  address read per reader. Wall per round: sequential from the first call; threads from the
  barrier release; processes from the `SetEvent` to the last call's end.
  `time.thread_time` (per call) has a 15.6 ms tick on this PC, so the summed CPU is a mean over
  rounds, not a per-call figure.
- **F10 cost.** N browser processes (one throwaway profile each, launched minimized without
  activation), each with one ownerless main window, cascaded on half B (Chrome N = 1, 5, 10,
  20; Zen N = 1, 5, 10). (a) all N windows visible. (b) the N main windows minimized plus one
  visible ownerless non-main window: the first profile started with
  `--auto-open-devtools-for-tabs` (Chrome, dock state `undocked` preset in `Preferences`) or
  `-devtools` with `devtools.toolbox.host` = `window` (Zen), no input. One cell at N = 10 with
  10 tabs per window (a). 25 measured checks (3 discarded), `due` = half B, each cell
  repeated (up to 3 tries) when disturbed.

## Healthy control

Reads came through `BudgetedUia` in the joint, A-only and B-only checks of the control run
(3 windows in a joint check; n counts reads of all three).

| Browser | Measure | n | Median | p95 | p99 | Max |
| --- | --- | --- | --- | --- | --- | --- |
| Chrome | caption read | 564 | 6.0 | 7.3 | 9.0 | 15.7 |
| Chrome | address read | 564 | 14.3 | 17.4 | 19.5 | 34.8 |
| Chrome | `Desk.check` joint | 141 | 60.6 | 71.9 | 85.1 | 93.0 |
| Chrome | `Desk.check` A-only | 47 | 21.4 | 25.6 | 32.9 | 32.9 |
| Chrome | `Desk.check` B-only | 47 | 40.7 | 45.1 | 46.9 | 46.9 |
| Chrome | `Win32Windows.windows()` | 235 | 2.1 | 3.1 | 3.6 | 4.7 |
| Zen | caption read (`None` at once by design) | 564 | 0.0 | 0.0 | 0.0 | 0.2 |
| Zen | address read | 564 | 8.6 | 11.5 | 16.6 | 24.7 |
| Zen | `Desk.check` joint | 141 | 26.0 | 37.4 | 44.4 | 44.5 |
| Zen | `Desk.check` A-only | 47 | 9.5 | 15.2 | 18.3 | 18.3 |
| Zen | `Desk.check` B-only | 47 | 17.2 | 20.6 | 37.0 | 37.0 |
| Zen | `Win32Windows.windows()` | 235 | 2.2 | 3.4 | 4.5 | 8.6 |

Every Chrome caption and address read returned a value (564 of 564 each); every Zen address
read returned a value and every Zen caption read `None`. The first joint check after the
launch took 280 ms (Chrome) and 301 ms (Zen), the next nine 20-69 ms (Chrome) and 14-43 ms (Zen).

Cold first reads (first reader call 2 s after the window appeared, 3 launches per browser,
launches 1 and 3 caption first, launch 2 address first), ms:

| Browser | Launch 1: caption, then address | Launch 2: address, then caption | Launch 3: caption, then address |
| --- | --- | --- | --- |
| Chrome | 28.2 -> 20.8 | 23.8 -> 6.0 | 16.6 -> 15.1 |
| Zen | 0.0 -> 50.8 | 32.0 -> 0.0 | 0.0 -> 51.0 |

### Worker wait for F19

The largest healthy warm p99 over readers and browsers is 19.5 ms (Chrome address read), so
the **proposed worker wait is 50 ms** (the floor). Share of healthy reads above it:

| Set-up | Chrome | Zen |
| --- | --- | --- |
| Warm reads (control) | 0 of 1128 | 0 of 1128 |
| Cold first call after launch | 0 of 3 | 0 of 3 |
| Cold address read after launch | 0 of 3 | 2 of 3 (50.8, 51.0) |
| During a suspended process in another window (second thread only) | 0 of 5068 (max 39.5) | at least 2 of 5054 (max 102.8) |
| During one suspended thread in another window (second thread only) | 0 of 5070 (max 45.0) | at least 2 of 5052 (max 147.1) |
| During a busy page in another window (Desk reads of A plus the second thread) | 0 of 5284 | 21 of 5262 (max 189.6) |

Parallel set-ups (calls above 50 ms at 1 / 2 / 4 readers; in the four-process set-up the
readers are Chrome A, B, C and Zen, so one cell covers both browsers):

| Set-up | Mode | Calls above 50 ms |
| --- | --- | --- |
| One Chrome process, four windows | threads | 0/40, 0/80, 16/160 |
| One Chrome process, four windows | processes | 0/40, 4/80, 40/160 |
| Four processes, both browsers | threads | 0/40, 1/80, 15/160 |
| Four processes, both browsers | processes | 0/40, 3/80, 71/160 |
| One-process and four-process set-ups, sequential reads | main thread | 0 of 280 each |

## Stall cases

3 valid runs per cell (stall lasted 37.0-37.3 s in every valid run). "Delay on A" is the joint
`Desk.check` wall time minus the control median (Chrome 60.6 ms, Zen 26.0 ms); the F19 test
subtracts the control max (Chrome 93.0, Zen 44.5) instead. Phases: first = the first check
after the stall began; inside = checks while the backoff was active; after = the first check
at or after the backoff's end (30 s after the check that started it). Ranges are over the 3
runs.

| Cell | Attempts / disturbed / valid | Desk reader results against B | Reached `SLOW_CALL_MS` and backoff (`uia_backoff`) | Read over 2000 ms | Slow read that returned a value |
| --- | --- | --- | --- | --- | --- |
| Chrome, suspended process | 3 / 0 / 3 | address 1009.1-1014.7 ms, `None` in 3 of 3 runs; caption not read (the address read started the backoff in the same check); direct sibling reads below | 3 of 3 runs; `uia_backoff` 4 per run (two windows, twice) | 0 | 0 |
| Zen, suspended process | 7 / 4 / 3 | address 1007.7-1012.3 ms, `None`; caption `None` at 0.0 ms by design | 3 of 3; 4 per run | 0 | 0 |
| Chrome, one thread suspended | 3 / 0 / 3 | address 1006.7-1010.7 ms, `None`; caption not read | 3 of 3; 4 per run | 0 | 0 |
| Zen, one thread suspended | 3 / 0 / 3 | address 1004.4-1010.7 ms, `None`; caption `None` at 0.0 ms | 3 of 3; 4 per run | 0 | 0 |
| Chrome, busy page | 7 / 4 / 3 | caption 10.5-15.7 ms and address 21.8-23.2 ms, both a value | 0 of 3 (no backoff, `uia_backoff` absent) | 0 | 0 |
| Zen, busy page | 3 / 0 / 3 | address 11.5-31.3 ms, a value; caption `None` at 0.0 ms | 0 of 3 | 0 | 0 |

The busy page was busy for the whole stall: the page sent 127-129 heartbeats before the stall
and 0 after it in all 6 busy runs (the longest gap 37.0-37.1 s, the run length).

Delay seen by healthy half A (ms; B-only and A-only are check wall times):

| Cell | First check | Inside the backoff: checks, median / max delay | First check after the backoff ended | B-only checks | A-only checks (median / max) |
| --- | --- | --- | --- | --- | --- |
| Chrome, suspended process | +1980 to +2013 | 26-28, -30 to -29 / -19 to -5 | +1978 to +1994 | 0 ms (both readers backed off) | 36 / 55 |
| Zen, suspended process | +2005 to +2023 | 28, -16 to -14 / +2 to +20 | +1991 to +2012 | 0 ms | 9 / 9 |
| Chrome, one thread suspended | +1973 to +1984 | 26, -31 to -30 / -23 to -21 | +1978 to +1984 | 0 ms | 22 / 27 |
| Zen, one thread suspended | +2007 to +2026 | 28, -15 / -4 to +14 | +1991 to +2006 | 0 ms | 9 / 16 |
| Chrome, busy page | +32 to +40 | 37, +28 to +34 / +71 to +109 | no backoff | 63 median, 71 max | 30 / 42 |
| Zen, busy page | +18 to +106 | 37, +10 to +26 / +87 to +206 | no backoff | 30 median, 43 max | 22 / 27 |

- The first check cost 2.0 s because two windows of the stalled process (B and its sibling)
  each took one 1000 ms connection timeout before their own backoff started (HWND key); the
  check after the backoff ended repeated it. During the backoff a check read nothing of B
  and cost less than the control.
- In the busy cells no check of A exceeded the control max by 100 ms in Chrome (largest +76
  ms) and 2 of 114 Zen checks did (+188 and +116 ms: an address read of the healthy window A took 148 ms in one
  check and a read of the sibling 74 ms in the other).
- Pre-stall joint checks (warm, 7 per run, 9 valid runs per browser: 63 per browser) took 56-124 ms in Chrome and 35-95 ms in Zen.
- `Win32Windows.windows()` took at most 33.2 ms in any stall run (Zen busy), and the stalled
  window stayed listed (no ghost window of `dwm.exe` appeared; 0 ghost windows in every check).

Second thread, healthy browser A read every 50 ms during the whole run (all calls, caption and
address together; 3 valid runs):

| Cell | n calls | Median ms (address only) | Max ms | Calls over 100 ms |
| --- | --- | --- | --- | --- |
| Chrome, suspended process | 5068 | 14.6 | 39.5 | 0 |
| Zen, suspended process | 5054 | 9.6 | 102.8 | 2 |
| Chrome, one thread suspended | 5070 | 14.1 | 45.0 | 0 |
| Zen, one thread suspended | 5052 | 9.6 | 147.1 | 2 |
| Chrome, busy page | 5056 | 16.7 | 44.0 | 0 |
| Zen, busy page | 5034 | 12.4 | 189.6 | 2 |

The two Zen calls over 100 ms appear in the busy cell too, where nothing in the read path
stalls, so they cannot be told apart from Zen's own read noise (the control had one reader
max of 24.7 ms in 564 address reads, though without the second thread).

Sibling window of the stalled process (read during the stall):

| Cell | Sibling found | Desk reads of the sibling | Direct reads 6 s and 22 s into the stall | Stalls past the 50 ms wait |
| --- | --- | --- | --- | --- |
| Chrome, suspended process | Direct probe: 2 of 3 runs (third not identified by title; see Limits) | 4 reads, 1002-1015 ms, `None` | caption 1010-1015 ms and address 1004-1016 ms, all `None` (8 reads) | yes |
| Zen, suspended process | 3 of 3 | 6 reads, 1002-1014 ms, `None` | address 1003-1015 ms `None`; caption `None` at 0.0 ms (12 reads) | yes |
| Chrome, one thread suspended | 3 of 3 | 6 reads, 1001-1014 ms, `None` | caption 1001-1015 ms, address 1002-1014 ms, `None` (12 reads) | yes |
| Zen, one thread suspended | 3 of 3 | 6 reads, 1003-1015 ms, `None` | address 1001-1013 ms `None`; caption 0.0 ms (12 reads) | yes |
| Chrome, busy page | 3 of 3 | 228 reads, max 43 ms, a value | caption 11-15 ms, address 17-22 ms, a value | 0 of 228 Desk reads and of the direct reads |
| Zen, busy page | 3 of 3 | 228 reads, max 74 ms (that read returned a value) | address 9-35 ms, a value; caption 0.0 ms | at least 1 (74 ms, returned a value) |

The sibling shared the stalled window's process in every run where the direct probe found it (a
`sibling_pid_same_as_B` check). Its class was `Chrome_WidgetWin_1` (Chrome) and
`MozillaWindowClass` (Zen). New top-level windows during any stall: none (the class lists
before and after were identical: three `Chrome_WidgetWin_1` or three `MozillaWindowClass`). The
sibling landed on the right monitor in every run, and its creation was counted among the
hand-backs below rather than marking the cell not run.

## Parallel reads

Per-thread `WindowsUia` with MTA COM; processes with their own `WindowsUia`. n = 20 measured
rounds per row. "Parallel wall / sequential wall" is the median wall of the mode over the
median of the sequential rounds interleaved with it (four equal calls on four workers would
give 0.25). Summed CPU is the mean per round of the per-call `time.thread_time` sum (15.6 ms
tick).

**Four windows of one Chrome process**

| Readers | Mode | Call ms (median / p95) | Wall per round (median ms) | Wall / sequential wall | Summed CPU per round (mean ms) |
| --- | --- | --- | --- | --- | --- |
| 1 | sequential | 10.6 / 17.8 | 23.2 | 1.00 | 3.9 |
| 1 | threads | 8.6 / 19.2 | 22.8 | 0.98 | 4.7 |
| 1 | processes | 13.1 / 22.2 | 29.6 | 1.27 | 3.1 |
| 2 | sequential | 11.3 / 17.6 | 40.8 | 1.00 | 7.0 |
| 2 | threads | 20.2 / 27.7 | 34.8 | 0.85 | 14.8 |
| 2 | processes | 23.4 / 45.7 | 53.0 | 1.30 | 17.2 |
| 4 | sequential | 12.6 / 19.2 | 81.8 | 1.00 | 20.3 |
| 4 | threads | 27.8 / 57.8 | 62.9 | 0.77 | 21.1 |
| 4 | processes | 34.6 / 1045.1 | 1067.3 | 13.05 | 39.8 |

**Four processes (Chrome A, B, C and Zen, one window each)**

| Readers | Mode | Call ms (median / p95) | Wall per round (median ms) | Wall / sequential wall | Summed CPU per round (mean ms) |
| --- | --- | --- | --- | --- | --- |
| 1 | sequential | 12.5 / 18.6 | 22.9 | 1.00 | 8.6 |
| 1 | threads | 13.9 / 20.4 | 24.3 | 1.06 | 8.6 |
| 1 | processes | 16.4 / 24.5 | 33.4 | 1.46 | 9.4 |
| 2 | sequential | 13.6 / 22.4 | 48.8 | 1.00 | 9.4 |
| 2 | threads | 19.9 / 31.1 | 42.5 | 0.87 | 13.3 |
| 2 | processes | 29.0 / 48.2 | 62.0 | 1.27 | 23.4 |
| 4 | sequential | 8.4 / 17.8 | 75.7 | 1.00 | 13.3 |
| 4 | threads | 32.8 / 54.6 | 64.4 | 0.85 | 38.3 |
| 4 | processes | 47.6 / 68.9 | 93.1 | 1.23 | 29.7 |

- No call raised and every address read returned a value, in every mode.
- With four reader processes on four windows of one Chrome process, 37 of 160 calls took
  1.0-1.05 s and **returned a value** (2 of 80 with two readers; 0 in any other row), so
  they would start no backoff; the median round took 1.07 s. A call of 1.0-1.05 s that returned a
  value matches the 1000 ms `ConnectionTimeout` followed by a successful retry (browser or
  UI Automation core side), which fits the cross-process verdict below.
- Verdict rule (the plan is not preserved; reconstructed from the verdict text): reads are parallel
  if wall / sequential wall at 4 readers is near the ideal 0.25 (0.4 or below); if threads stay
  near 1 but processes reach 0.4, the serialization is in-process; if neither does, it is
  cross-process (UI Automation core or the browsers).
- Verdict by the rule: threads at 4 readers stay near 0.8 (0.77 and 0.85) and **processes do
  not reach 0.4** (13.05 and 1.23), so the serialization is not in-process; processes do not
  beat threads either (their wall includes the event wake-up, which cost 6-11 ms at one
  reader), so it is **cross-process: UI Automation core or the browsers**. It is not the GIL:
  the summed CPU per round (21 and 38 ms at 4 readers) is well under the parallel wall (63 and
  64 ms).

## F10 cost: N ownerless browser windows

Per N and variant, over 25 checks of half B. Reads per check come from the reader call log
(`BudgetedUia.stats` agrees: 56 reads for 28 checks at N = 1 (a), 1120 for N = 20 (a)).
"Over 100 ms" counts checks.

| Browser | N | Variant | Address / caption reads per check | `Desk.check` median / p95 / max ms | Over 100 ms |
| --- | --- | --- | --- | --- | --- |
| Chrome | 1 | a | 1 / 1 | 25.7 / 58.8 / 92.8 | 0 of 25 |
| Chrome | 1 | b | 2 / 2 | 57.7 / 94.2 / 100.6 | 1 of 25 |
| Chrome | 5 | a | 5 / 5 | 129.1 / 187.7 / 309.3 | 25 of 25 |
| Chrome | 5 | b | 6 / 6 | 153.2 / 207.3 / 229.5 | 25 of 25 |
| Chrome | 10 | a | 10 / 10 | 280.6 / 358.8 / 415.4 | 25 of 25 |
| Chrome | 10 | b | 11 / 11 | 271.0 / 376.0 / 383.2 | 25 of 25 |
| Chrome | 10, 10 tabs | a | 10 / 10 | 295.2 / 436.7 / 507.3 | 25 of 25 |
| Chrome | 20 | a | 20 / 20 | 635.2 / 737.2 / 844.0 | 25 of 25 |
| Chrome | 20 | b (disturbed in 3 of 3 tries) | 21 / 21 | 474.2 / 615.7 / 625.6 | 25 of 25 |
| Zen | 1 | a | 1 / 1 | 11.5 / 20.0 / 21.4 | 0 of 25 |
| Zen | 1 | b | 2 / 1 | 23.1 / 34.9 / 93.0 | 0 of 25 |
| Zen | 5 | a | 5 / 5 | 54.0 / 75.3 / 98.8 | 0 of 25 |
| Zen | 5 | b | 6 / 5 | 133.8 / 273.5 / 306.4 | 17 of 25 |
| Zen | 10 | a | 10 / 10 | 192.8 / 344.6 / 361.6 | 25 of 25 |
| Zen | 10 | b | 11 / 9 | 201.1 / 342.7 / 348.3 | 25 of 25 |
| Zen | 10, 10 tabs | a | 10 / 10 | 163.5 / 297.5 / 309.4 | 25 of 25 |

- The median check exceeds 100 ms in every Chrome cell with 5 or more windows and in Zen
  (a) with 10 windows and (b) with 5 or more; Chrome N = 1 and Zen N = 1 and 5 (a) stay under
  (Chrome N = 1 (b) has one check at 100.6 ms). Two causes, shown apart:
  (a) every visible main window costs a read on every check (Chrome (a) medians 129.1, 280.6
  and 635.2 ms at 5, 10 and 20 windows, about 26-32 ms per window; Zen (a) 54.0 and 192.8 ms at 5
  and 10, about 11-19 ms per window), all 5 windows being visible mains; (b) minimized main
  windows are read too (N + 1 address reads per check) because `Verdicts._other` and
  `_recognize` read all ownerless windows of the exe, which is what pushes Zen (b) over the
  line at 5 windows.
- 10 tabs per window did not raise the cost (Chrome +5%, Zen -15%, inside run-to-run noise).
- In Zen variant (b) the address read of a minimized main window or of the toolbox window
  returned `None` (N = 1: 25 of 50 address reads; N = 5: 32 of 150; N = 10: 79 of 275).
- `windows()` median was 2.7-6.6 ms in every F10 cell.
- A first, partly invalid run of these cells (variant (b) placed a main window instead of the
  DevTools window; Zen windows not found) was discarded and rerun; the Chrome (a) medians of
  that first run were 11-25% lower (5: 115 ms, 10: 224 ms, 20: 475 ms), so the same cell varies
  that much between sessions on this PC.

## Decision

Rules, each applied to the numbers above (the plan's wording is not preserved; the F19 rule is
quoted from the check below, the key and wait rules reconstructed from how they were applied).

- **F19 rule:** needed if a stalled browser delays a healthy half's check by more than 100 ms over
  the control max. **F19 is needed.** A suspended process and a one-thread stall (Chrome and Zen, 12 of 12
  valid runs) delayed the first joint check of the healthy half A by 1973-2026 ms over the
  control median (1941-2008 ms over the control max), and the first check after each backoff
  by 1978-2012 ms. The Chrome busy page stayed under the line (up to +76 ms); the Zen busy page
  crossed it in 2 of 114 checks (+116 and +188 ms) through single slow reads, not a stall. F19
  is needed either way.
- **Key rule:** key by HWND only if a sibling of the stalled window reads within the healthy p99
  in every case, otherwise by pid. **Key the backoff by pid.** A sibling window of the stalled process also stalled (1001-1016
  ms, `None`, against a worker wait of 50 ms) in the suspended-process and one-thread cases of
  both browsers. In the busy-page cases it returned a value (Chrome Desk reads up to 43 ms, Zen
  up to 74 ms, direct reads 9-35 ms). The rule needs every case, so the stall cases alone
  (1001-1016 ms, `None`) decide the pid key. Keyed by HWND, a
  stalled process costs one 1 s timeout per window per backoff (2.0 s with two windows).
- **Wait rule:** the largest healthy warm p99, no lower than 50 ms. **Worker wait: 50 ms**
  (19.5 ms, floor 50 ms). Shares above it are in the table above. In the warm and cold sets only
  Zen's first address read after launch exceeded it (50.8 and 51.0 ms in 2 of 3), but Zen
  healthy reads also exceeded it in the busy-page set (21 of 5262, max 189.6) and the stall sets
  (at least 2 of 5054 and 2 of 5052). The 50 ms holds for warm reads one at a time (control 0 of 1128 per browser): under concurrent
  reads, thread readers went over it in 16 of 160 and 15 of 160 calls at 4 readers, process
  readers in 40 of 160 and 71 of 160. A worker design that reads several browsers in parallel
  needs a longer wait or must not treat a late read as fatal.
- **No concurrent reads of one browser process from several reader processes.** Several reader processes on windows of one Chrome
  process produced slow reads that returned a value (37 of 160 calls at 1.0-1.05 s); threads
  of one process did not (p95 57.8 ms). A worker that reads one browser process's
  windows concurrently from several processes would see those and start no backoff.
- Edge, Brave and Firefox remain open: none was measured. Whether a stalled Edge or Brave
  behaves like Chrome is assumed, not shown.
- OPEN A9 F10: the 100 ms-per-check line is crossed at 5 Chrome windows by the per-visible-window
  read cost (about 26 ms per window, all 5 windows visible mains), and at 5 Zen windows with
  minimized mains by the extra reads of minimized mains through `_other` and `_recognize`.

## Limits

- Fresh disposable profiles, one tab (10 in one cell), tiny local pages, no extensions: not the
  user's real profiles, tab counts or pages. The busy page is a JS loop in a page whose
  renderer is a separate process from the sibling's (`noopener`); its reads were not slowed,
  which may differ for a page that is busy because of accessibility work.
- Three valid runs per stall cell (the number asked for); each cell's other attempts were
  disturbed by foreground changes: Zen suspended 4 of 7 attempts, Chrome busy 4 of 7. In
  one Chrome-suspended run the direct sibling probe did not identify the sibling by title (no
  window with the expected title in 30 s), so its direct reads cover 2 of 3 runs. The raw
  results are deleted; the report's own numbers suggest the sibling existed in that run:
  `uia_backoff` was 4 per run (two windows, twice) in 3 of 3 runs, the first check cost +1980 to
  +2013 ms in all 3 (two windows each timing out), and three `Chrome_WidgetWin_1` windows were
  listed in every stall. The Desk-derived numbers are kept; the Desk reads of the sibling list
  4 reads, so they are attributed to 2 runs only.
- B-only check time was timed after the joint phase, when both readers were backed off, so
  it shows the cost of a backed-off check (0 ms), not the cost of an un-backed-off B-only
  check; the first joint check (about 1 s per stalled window) is the un-backed-off cost.
- The stalled browser is a separate process of the same browser from A, so A's reads never
  shared a process with the stall; A's reads were not made against a stalled process that also
  owned a healthy window.
- Zen's sibling was opened by `window.open` from a page (with
  `dom.disable_open_during_load` off), not by `-new-window`: a second `zen` command line on the
  same profile could hand off to a running Zen of the user's. A sibling that took the
  foreground was handed back and still read, instead of being marked "not run".
- Zen's safe-mode prompt (a fixed browser dialog, title length 30, a window of a spike Zen) appeared
  during Zen launches of the F10 cells: at least 11 launch attempts hit it (counted from failed
  window waits; the stall and control rigs always had exactly 3 windows). It was closed by the
  cleanup, never left behind, and its cause was not found; the Zen F10 cells were rerun until
  a launch worked (up to 3 tries).
- `time.thread_time` has a 15.6 ms tick; the CPU column is a mean over rounds, not a
  measurement of one call. The process mode's round wall includes the event wake-up.
- The foreground hand-back moved the foreground to windows the spike did not start (the
  taskbar, and windows of other sessions' processes), at least 96 times in the results
  used for the tables; 1 hand-back failed. The other session's spike was not visible to the process scan
  (0 of 46), so isolation from it rests on the job filter, the right-monitor placement and
  the foreground guard.
- The results used for the tables are the last attempt of each cell; counts of earlier
  discarded attempts (steals, scans) are lower bounds. The 16 suspend runs equal the attempts of
  the four suspend cells (3 + 7 + 3 + 3), so they cover every suspend in the Attempts column;
  a suspend in an earlier discarded attempt is not covered. Which cells' files did not record
  terminations is not known. The first busy runs (without the heartbeat) gave
  the same read results and were discarded.
- Not covered: Edge, Brave, Firefox; `read_text` against a stalled window; `shows_own_address`
  and presence timing; title-hold frequency; other virtual desktops.
