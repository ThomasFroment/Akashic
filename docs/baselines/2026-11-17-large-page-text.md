# Large-page text reads spike 2026-11-17

Measurements behind TODO A3 and F16 of the monitor-capture rules ([CAPTURE.md](../CAPTURE.md),
"UI Automation timeouts"): can a text reader other than today's `WindowsUia.read_text` read the
visible text of a browser window showing a page of about 30k DOM elements in 666 ms or less,
without losing visible text and without reading a password field. Four candidates were timed
against each other in the same window of Chrome and of Zen: today's reader (A), A without live
element references (B), the TextPattern of the page Document (C) and a pruned tree walk (D).
Test windows were disposable browser processes started by the spike with throwaway profiles and
a local page server on 127.0.0.1; the user's browsers and profiles were not used; see Limits for the foreground deviations.
Reads were scored in memory against a ground truth computed by the page itself; this report and
the raw result files hold numbers, counts, booleans, flag names and candidate labels only (no
page text, no title, no address). The script was throwaway and is not kept; no file under
`src/` or `tests/` changed.

## Machine

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01), Windows 11 build 26200, Python
3.14.10. Monitors, left to right: 2560x1440 at 100%, 3840x2160 at 125% (primary), 2560x1440 at
100% (the right monitor, `left` = 3840, 100% scale). Every test window sat on the right monitor.

| Browser | Version | Window (physical px) | Page viewport (CSS px) |
| --- | --- | --- | --- |
| Chrome | 157.0.8103.79 | 2400x1280 | 2384x1185 |
| Zen | 1.24.1b (`application.ini` `Version`; build 20261106124806) | 1920x1024 | 1710x999 |

Zen did not keep the requested 2400x1280 and settled at 1920x1024 (likely a side effect of the
spike's placement, see Limits), so the two browsers show different numbers of visible markers. Edge, Brave and Firefox were not measured.

## Test page

A page served by the spike, built to the description of the 2026-11-13 re-run: 2300 sections
in 46 wrapper containers (50 sections each); each section holds one paragraph, one link, three
list items, one plain container and two table cells. Every text-bearing element (8 per section)
carries a unique synthetic marker token, 18,400 in all. An autofocused password field with a
synthetic value is the last element, inside the end viewport. The page scrolls itself to the
end with JavaScript (no input is sent); after the scroll position had been stable for 1.5 s and
a further 0.5 s it POSTed to its own origin the markers whose element rectangle overlaps the
viewport. That set is the ground truth.

| | Chrome | Zen |
| --- | --- | --- |
| DOM elements | 29,955 | 29,955 |
| UIA descendants under the window root (raw view, counted after the timed reads) | 25,343 | 32,310 |
| Markers in the page | 18,400 | 18,400 |
| Markers visible per ground truth | 48 | 40 |
| Page scroll position at the end (px) | 449,640 | 458,107 |
| Password field in the viewport / `document.activeElement` is the password field | yes / yes | yes / yes |
| `document.hasFocus()` (false: the window is not the foreground window) | false | false |

## Method

- **Readers.** All four ran on one `WindowsUia` instance (one `IUIAutomation2`, 1000 ms
  connection and 2000 ms transaction timeouts), on the same window, with `max_nodes` =
  `MAX_UIA_NODES` (3000). A is the unchanged `WindowsUia.read_text`. B is the same code with
  `cache.AutomationElementMode = AutomationElementMode_None` (same condition, same cached
  properties, same loop). C finds the first Document control with `FindFirstBuildCache`,
  takes its TextPattern, calls `GetVisibleRanges` and `GetText(MAX_TEXT_CHARS)` on each range
  and joins the texts. D walks from the window root with a recursive
  `FindAllBuildCache(TreeScope_Children)` caching the properties of A plus `IsOffscreen`,
  descending only into children that are not `IsOffscreen` and whose rectangle meets the
  window, plus children with an empty rectangle, keeping text-type, non-password, visible
  nodes under A's per-node rules, and stopping after 3000 kept nodes. A, B and D return
  `TextBlock`s that went through `assemble_text`; C returns one text.
- **Timing.** The wall time of the reader call only (`assemble_text` and scoring excluded).
  Per cell: n, median, p95 (nearest rank) and max over 20 measured reads, plus the first read
  after a 2 s settle on its own (A, B, C, D in that order, each after its own 2 s). Reads were
  interleaved round-robin (A, B, C, D, A, ...). A "failed" read is one that returned `None`
  or raised a `COMError`. "Reached 2000 ms" counts reads of 2000 ms or more.
- **C recovery.** After a C read Chrome stayed busy for tens of seconds and every call to it
  timed out. After each C read the spike therefore waited, untimed, until a cheap call
  (`ElementFromHandle` + control type) took under 200 ms (cap 120 s), so the next reader
  met a responsive browser. These waits are reported below; they are not in any timing.
- **Scoring.** Recall = visible ground-truth markers found in the stored text over visible
  markers in the ground truth (a failed read counts 0). "Away" = markers in the stored text
  that the ground truth says are scrolled away. Leak = the password field's synthetic value
  appears in the stored text.
- **Launch.** Flagless first: each browser started with only its profile and first-run/sync
  flags (Chrome: `--user-data-dir`, `--no-first-run --no-default-browser-check
  --disable-search-engine-choice-screen --disable-sync`, plus `--window-position`,
  `--window-size` and `--start-minimized`; Zen: `-no-remote -profile` and a `user.js` with
  only first-run/update/data-reporting prefs, no accessibility pref). Today's read ran every
  0.5 s for up to 10 s from the moment the window was placed. Both browsers exposed page
  markers, so the timed runs used the flagless launch (no `--force-renderer-accessibility`,
  no Zen pref).
- **Placement.** Each browser was launched in a Job object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` with `STARTUPINFO` `SW_SHOWMINNOACTIVE`, then
  moved onto `right_monitor()` with `SetWindowPlacement` and shown with
  `SW_SHOWNOACTIVATE`. Both browsers took the foreground anyway. The spike then restored the
  foreground to the window that had it with `SetForegroundWindow`, `SwitchToThisWindow` and a
  thread-input attach (see Limits). The process was per-monitor DPI aware. No SendInput, no
  click.
- **Foreground guard.** Before and after each read the foreground window was compared with the
  previous step's; a read counted as disturbed when it changed or was a window of a process
  in the job (or of the spike). Disturbed reads would have been dropped and redone until 20
  clean reads existed.
- **Flagless markers appear in the window.** Time from placement to the first read that held a
  page marker.
- **`IsOffscreen` diagnostic (not timed).** One `FindAllBuildCache` of all text-type nodes
  without the `IsOffscreen` condition (60 s transaction timeout, a separate `WindowsUia`),
  caching Name, `IsOffscreen` and the rectangle, counted by marker nodes.

## Read times

Cells give n / median / p95 / max in ms. "First read" is the first read after a 2 s settle on
that candidate's own turn. A, B and D never leaked the password value (0 of 20 in every cell);
C never leaked either (0 of 20 on Zen, no text on Chrome).

**Chrome** (flagless; markers first seen 2.66 s after placement, 4 reads)

| Candidate | n / median / p95 / max (ms) | First read (ms) | Failed | Reached 2000 ms | Recall mean (min) | Away, median / max | Leaks |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A today | 20 / 814.6 / 971.9 / 996.1 | 1087.9 | 0 | 0 | 1.000 (1.000) | 0 / 0 | 0 |
| B no element refs | 20 / 820.2 / 937.4 / 943.1 | 972.4 | 0 | 0 | 1.000 (1.000) | 0 / 0 | 0 |
| C TextPattern | 20 / 2179.0 / 2187.0 / 2196.8 | 2191.9 | 20 | 20 | 0.000 (0.000) | 0 / 0 | 0 |
| D pruned walk | 20 / 2251.4 / 2432.3 / 2566.5 | 2217.5 | 0 | 20 | 1.000 (1.000) | 0 / 0 | 0 |

- C: every read hit the transaction timeout and failed (the Document and its TextPattern
  were found in 7-10 ms; `GetVisibleRanges` did not return). The browser then stayed busy:
  recovery waits (n 21) median 22.3 s, p95 24.2 s, max 25.3 s. In a separate probe with 120 s
  timeouts `GetVisibleRanges` returned after 31.96 s and 32.01 s (two runs) with 0 ranges.
- D: median 105 `FindAllBuildCache` calls per read (max 110). The 2000 ms count is the
  whole walk, not one call; each call itself stayed under the transaction timeout.
- Every reader found 48 of 48 visible markers where it returned text (A, B, D).

**Zen** (flagless; markers first seen 0.99 s after placement, 2 reads)

| Candidate | n / median / p95 / max (ms) | First read (ms) | Failed | Reached 2000 ms | Recall mean (min) | Away, median / max | Leaks |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A today | 20 / 326.7 / 416.6 / 433.5 | 344.0 | 0 | 0 | 1.000 (1.000) | 0 / 0 | 0 |
| B no element refs | 20 / 336.5 / 405.6 / 408.2 | 328.7 | 0 | 0 | 1.000 (1.000) | 0 / 0 | 0 |
| C TextPattern | 20 / 47.6 / 58.5 / 59.6 | 18.9 (returned text, recall 0.000) | 0 | 0 | 1.000 (1.000) | 0 / 0 | 0 |
| D pruned walk | 20 / 1995.6 / 2427.1 / 2588.4 | 2049.4 | 0 | 10 | 1.000 (1.000) | 0 / 0 | 0 |

- C: the first read after the settle returned text with none of the markers; all 20
  measured reads held all 40. Recovery waits after C (n 21): median 1.1 s, p95 1.7 s, max 1.7 s.
- D: median 156 `FindAllBuildCache` calls per read (max 169).
- A, B, C and D returned 0 scrolled-away markers on both browsers.

**Disturbed reads: 0 on Chrome, 0 on Zen.**

## Zen `IsOffscreen` (OPEN.md A4 #21)

Marker-bearing text-type nodes from one unfiltered `FindAllBuildCache`, not timed. Chrome as
reference. Text-type nodes in all: Chrome 20,718, Zen 27,627.

| | Chrome | Zen |
| --- | --- | --- |
| Visible marker nodes, `IsOffscreen` true | 0 | 0 |
| Visible marker nodes, `IsOffscreen` false | 48 | 40 |
| Scrolled-away marker nodes, `IsOffscreen` true | 18,352 | 18,360 |
| Scrolled-away marker nodes, `IsOffscreen` false | 0 | 0 |
| Scrolled-away nodes whose rectangle lies outside the window | 18,348 | 18,360 |
| Scrolled-away nodes whose rectangle meets the window | 4 | 0 |

On this page Zen 1.24.1b marks every scrolled-away node `IsOffscreen` and no visible node,
as Chrome does.

## Machine load

Other agents of the epic ran CPU work during the spike (tests, an offline speech spike).
The machine's total CPU load, sampled once a second by the spike over the whole run:
Chrome run, 615 samples, mean 15.5%, median 13.8%, p95 26.6%, max 50.5%; Zen run, 80
samples, mean 20.8%, median 18.0%, p95 40.2%, max 57.3%.

## Verdict for F16

Eligible = a candidate that, on both browsers, has a lower median and a lower max than A,
recall at least A's and 0 password-value leaks.

| Candidate | Chrome median / max vs A (814.6 / 996.1) | Zen median / max vs A (326.7 / 433.5) | Recall | Leaks | Eligible |
| --- | --- | --- | --- | --- | --- |
| B | 820.2 higher / 943.1 lower | 336.5 higher / 408.2 lower | equal | 0 | no (median higher on both) |
| C | 2179.0 higher / 2196.8 higher | 47.6 lower / 59.6 lower | Chrome 0.000, lower | 0 | no |
| D | 2251.4 higher / 2566.5 higher | 1995.6 higher / 2588.4 higher | equal | 0 | no |

Winner: **none; F16 does not land.** Today's reader (A) stays. For reference, A's own max is
433.5 ms on Zen (under the 666 ms target) and 996.1 ms on Chrome (over it), on this page.
C fails every read on Chrome and blocks the browser for about 22 s each time; on Zen its first
read returned no visible markers (recall 0.000).

## Limits

- One synthetic page of one shape, 30k elements, scrolled to the end; one launch per browser,
  windows visible and unfocused; n = 20 per cell, so p95 is the second-largest read. Chrome's
  A here (815 ms median) is far below the 1.7-1.9 s of the 2026-11-13 re-run on a differently
  built page and an older Chrome build; the two are not comparable.
- Edge was not measured (sign-in risk with the PC's account). Brave and Firefox are not
  installed. Throwaway profiles with no extensions and no other tabs; the user's real
  profiles and pages were not measured.
- The first reads came in a fixed order (A, B, C, D), so A's first read is the coldest.
- The untimed recovery wait hides a cost a real capture loop would pay: on Chrome the
  browser stayed blocked for about 22 s after each C read.
- D always ran straight after C's recovery wait. On Chrome the browser had just been busy for
  about 22 s, and the under-200 ms check does not prove it had settled, so D's Chrome times may
  be inflated. D's verdict holds anyway: D is also slower than A on Zen, where recovery took
  about 1 s.
- Window size differs (Chrome 2400x1280, Zen 1920x1024), so visible-marker counts and
  node counts are not comparable between browsers. Zen's 1920x1024 is exactly 2400x1280 / 1.25,
  the primary monitor's scale: the likely cause is a side effect of the spike's placement (the
  window was sized during its second on the primary monitor), not something Zen chose. Every
  candidate is scored against A in the same window with that window's own ground truth, so
  eligibility and recall are unaffected; only A's Zen max of 433.5 ms "under the target" was
  measured at the smaller window.
- The flagless Chrome window showed page markers after 2.66 s, while the 2026-11-13 spike in
  CAPTURE.md found a flagless Chrome window never exposed page text (PC locked). This run
  differs: unoccluded window, unlocked PC, window just in the foreground. The activation at
  launch may have played a part, so read this as "worked here", not "flagless always works".
- Spike-rule deviations. Both browsers took the foreground at launch despite
  `SW_SHOWMINNOACTIVE` (Chrome also ignored `--start-minimized`). To give the foreground back,
  the spike activated the user's previous foreground window, in a process the spike did not
  start, with `SetForegroundWindow` and, as fallbacks, `SwitchToThisWindow` and a thread-input
  attach to that window's input thread (no synthetic input). This breaks the spike rule, as do
  the activation at launch and Zen's window appearing for about a second on the primary
  monitor before it was moved. During the timed reads neither browser was the foreground
  window.
- The UIA descendant counts were taken after the timed reads, by a separate `WindowsUia`
  with longer timeouts; the `IsOffscreen` diagnostic likewise.
- Recall of a failed read counts as 0; the 20,000-character cap of `assemble_text` was not
  reached by any read. Text was kept in memory only; the profiles and the job's processes
  were deleted and killed afterwards.
- Other CPU work ran on the machine at the same time (see Machine load); timings are real
  wall times under that load, not isolated.
