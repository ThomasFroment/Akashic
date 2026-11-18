# Presence timing spike 2026-11-17

Measurements behind the OPEN.md A9 F19 lines (the real-browser timing of the presence loop, and
the own-address failsafe on a bookmarked or typed address) of the UI presence design
([CAPTURE.md](../CAPTURE.md), [UI.md](../UI.md)): how long after a browser shows the UI page
recording is halted and the content is revealed, whether a hidden or covered page stops
reporting, whether the 250 ms and 2 s report schedule and the 6 s lease hold in a real browser,
and whether the own-address failsafe sees every form of the address. The two lines it answers:

- F19 "Not run: real-browser timing of the presence loop (a page shown by Zen, Chrome and Edge:
  time from the tab becoming visible to recording halted and to content shown; the 6 s lease
  and the 250 ms/2 s schedule are chosen, not measured; a background or occluded tab may be
  throttled by the browser)". Answered for Chrome and Zen; Edge not run.
- F19 "Not run: the own-address failsafe on Zen, Chrome and Edge (address read of a visible
  window on Akashic's port, with and without a scheme; a page not opened through the tray, such
  as a bookmarked address)". Answered for Chrome and Zen; Edge not run.

Test windows were disposable Chrome and Zen processes started by the spike with throwaway
profiles; the user's browsers, profiles, tabs and windows were not touched. The server side was
the real `ApiServer`, `Services` and `Presence` (a subclass that only records each `report` and
passes it on unchanged), serving the UI from `src/akashic/ui`, over a disposable store with no
captured data. This report and the raw result file hold numbers, counts, booleans and labels
from a fixed list only: no address, port, title, page text or image. The scripts were throwaway
and none is kept; no file under `src/` or `tests/` changed.

## Machine

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01), Windows 11 build 26200, Python
3.14.10. Monitors, left to right: 2560x1440 at 100% (virtual-screen left -2560), 3840x2160 at
125% (primary), 2560x1440 at 100%. Every window of this spike sat on the left monitor (the
non-primary monitor with `rect.left < 0`, found with `Win32Displays().monitors()`), by the
user's permission, because the right one was in use by another spike: 1200x800,
upper left corner 160 px from the monitor's left edge and 100 px below its top. The refresh rate
of the monitors was not recorded.

| Browser | Version |
| --- | --- |
| Chrome | 157.0.8103.79 (name of the folder in its `Application` directory) |
| Zen | 1.24.1b (`application.ini` `Version`) |

Edge was not run: a fresh profile may sign in to the PC's Microsoft account. It stays open in
OPEN.md. Brave and Firefox are not installed.

## Commit

HEAD 1877212 plus an uncommitted tree. Checked with `git diff` against the modules the spike
used:

| Module | State |
| --- | --- |
| `capture/verdicts.py`, `capture/exclusions.py`, `capture/uia_budget.py`, `platform/win/uia.py`, `platform/win/displays.py` | unchanged when the failsafe phases ran (19:25-19:40); `verdicts.py`, `uia_budget.py` and `uia.py` modified later by other work |
| `api/server.py` | one line added in `_utterance_json` (`tag`); `Presence`, `Controls` and `/api/presence` unchanged |
| `ui/app.js` | speech-search row (`u.tag`) and one `PROBLEM_WORDS` entry; the presence block unchanged |
| `platform/win/wgc.py` | fault log passed to `_new_device`, new `fault_counts`; the capture path unchanged |
| `platform/win/windows.py` | new `awake_ms` and a docstring; `windows()` unchanged |
| `capture/desk.py` | `Snapshot.incomplete` and a pending-frame recheck; `visible_monitors` unchanged |
| `capture/ports.py`, `capture/browser_state.py`, `capture/screen.py` | `WindowInfo.pid` (default 0), `awake_ms` on `WindowSource`, `slept`; a generation counter on `BrowserState`; `Settings` comment only (`poll_s` 0.1, `min_gap_s` 1.0 as before) |

None of the uncommitted changes is on the presence path (`Presence`, `/api/presence`, the page's
report loop). Other work changed the tree while the spike ran: `platform/win/uia.py` and
`capture/uia_budget.py` were modified at 21:52-21:53 and `capture/verdicts.py` at 22:36, and
`capture/uia_threads.py` appeared. The failsafe matrix and timing ran at 19:25-19:40 against the
files as listed above (unchanged); the later phases (schedule, lease, long hides, the second
Zen kill and long-hide batches) do not use these modules.

## Method

- **Harness.** One process: the real `ApiServer` and `Services` on a store made like the
  `client` fixture of `tests/test_app_helpers.py`, a 1.0 s thread calling `presence.expire()`,
  and a second `ThreadingHTTPServer` on 127.0.0.1 (another port) for a blocker page, a `/go`
  long-poll and a plain page. A fake supervisor provided `Controls(states, pause, resume,
  set_ui_open=...)` in two modes. "halts": `capture` becomes `ui_paused` 10 ms after
  `set_ui_open(True)` and `ready` at `set_ui_open(False)`. "never": `capture` stays `ready`.
  Every report (page id, `visible`) and every `set_ui_open` change was recorded with the
  monotonic time and its source (report or expire). The halt of the real capture child was not
  modelled beyond the 10 ms.
- **Browsers.** Chrome flags `--no-first-run --no-default-browser-check --disable-sync
  --disable-background-networking --disable-component-update
  --disable-search-engine-choice-screen`, window position and size. Zen `-no-remote -profile`
  with a `user.js` turning off telemetry, updates, accounts and the welcome page, and
  `dom.allow_scripts_to_close_windows`, plus a preset `xulstore.json` placing the window on the
  left monitor. No flag or pref that changes timer or renderer throttling or occlusion tracking.
  A fresh profile for every launch. Each launch was created suspended, put in a Job object with
  KILL_ON_JOB_CLOSE, then resumed, with `STARTUPINFO` asking for `SW_SHOWNOACTIVATE`. Warm
  launches (a new tab in the running instance): Chrome `--user-data-dir=<profile> <url>`, Zen
  `-no-remote -profile <profile> -new-tab <url>`; both opened a tab in the running window (after
  each hide in case e exactly one browser window of the job was visible). Every window was wholly
  inside the left monitor. No `SendInput` and no `keybd_event`.
- **Focus.** Freshly launched windows can take the foreground despite `SW_SHOWNOACTIVATE`. Right
  after placing a window the spike compared the foreground window with the one from before the
  launch; if a window of the job had taken it, the spike called `SetForegroundWindow` and
  `SwitchToThisWindow` on the earlier window. That activates a user window in a process the spike
  did not start. `bring_to_front` was not used. The foreground was checked after every cold and
  warm launch, not after the minimize, restore and cover actions (they never ask for
  activation). Over the reveal runs: 189 checks for Chrome and 209 for Zen; 6 steals (all Chrome,
  all of case a, the cold launch), of which 1 was handed back and 5 hand-backs failed (the
  foreground stayed with the window of the job until the next change); Zen took the foreground
  in no check; the foreground went to a window that was not the spike's in 8 (Chrome) and 18
  (Zen) checks (the user or another process). The counters of the runs that were repeated
  (earlier batches, see below) and of the kill, schedule, matrix and failsafe-timing phases were
  not kept.
- **Validity.** A run was valid when: the window was on the left monitor; `WindowFromPoint` at
  three viewport points returned the test window (root ancestor) before and after, except while
  the spike's own cover was meant to be up (checked to be up in case d); a read-only scan of
  python, pythonw, chrome, zen and uv processes found none from another session's `Temp\claude\`
  folder and no `synthetic_window.py` window on the left monitor (about 225 scans, one before each
  reveal run, 0 found; the report's counts give 224: 200 valid runs, 12 long hides, 12 invalid; not repeated in the kill, schedule, matrix and failsafe-timing phases);
  the case's own checks passed (below). Invalid runs were counted and replaced.
- **Pixel probe.** `WgcCaptures.start(hwnd, 0)` on the browser window, `latest_seq` polled
  every 2 ms, each new frame grabbed and reduced in memory to a 4x down-sampled grayscale strip
  (the left 35% of the window width, 20%-95% of its height); no frame written to disk. Two
  reference strips per run: the wait note (page in "never" mode) and the shell (after the
  confirmation). A frame is "content" or "wait note" when the mean absolute difference over the
  pixels where the two references differ by more than 40 is under 4 for that reference (and for
  content not larger than for the wait note), else "other". Frame times are the arrival time of
  the production frame. Cases a and b took their references from a separate throwaway launch;
  cases c, d, e from the same launch before the measured hide.
- **Cases (20 valid runs per browser, "halts" mode, t0 = the spike's action).**
  a: cold launch with an `/open?code=` address (t0 just before `CreateProcess`; the probe starts
  when the window is found, so "first frame" excludes the window creation). b: warm launch of an
  `/open?code=` address as a new tab, while the running instance showed a plain spike page (so
  the reveal has a real state flip). c: restore from minimized (`SW_SHOWMINNOACTIVE`, then
  `SW_SHOWNOACTIVATE`). d: a spike-owned opaque `WS_EX_NOACTIVATE|WS_EX_TOPMOST` window
  (class `Static`, black, on its own message-pumping thread) over the whole window is destroyed
  (t0 just before `DestroyWindow`). The planned case b (a warm tab while the UI already held the lease) was not measurable
  because it has no state flip; this variant replaces it. e: a blocker tab, opened by a warm launch, closes itself
  with `window.close()` when the `/go` long-poll is answered (t0 is the time the response is
  written). For c, d, e the page showed content before a hide of 3 s. The long-hide variant (6
  min) ran 3 valid runs per browser for c and e. Per run, from t0: first frame, first visible
  report, the halt (the state change), the confirming reply (the first visible report at or
  after the halt; the report time stands for the reply, which is written at once), the first
  content frame, the content frames before the halt (stale: a leak) and before the confirming
  reply. For the hide: time from the hide action to the `visible: false` report and to
  `set_ui_open(False)`, whether the page reported hidden at all, and the content frames grabbed
  after `set_ui_open(False)` while `visible_monitors` (over the full window list, sampled about
  every 20 ms) still counted the window visible.
- **Schedule and lease.** One browser per run, window visible and unfocused, then half covered
  (the cover over the left half), then fully covered (opaque cover): 60 s each, "never" mode
  then "halts" mode; then 10 min visible and 10 min half covered in "halts" mode. Gaps between
  consecutive visible reports of one page id.
- **Kill.** The Job terminated (`TerminateJobObject`) 1.5-3.5 s (random) after the confirming
  reply, time to `set_ui_open(False)`.
- **Failsafe.** The real `Verdicts(Exclusions(UserExclusions(), api_origin), BudgetedUia(
  WindowsUia()), BrowserState())`; windows from `Win32Windows().windows()`, monitors from
  `Win32Displays().monitors()`, `visible_monitors` over the full list; only windows of the job
  were passed to `shows_own_address` and `judge`. Address forms: the tray-opened `/` after the
  redirect; `http://127.0.0.1:<port>/` typed in a profile with no session; `http://localhost:
  <port>/`; a used `/open?code=`; a second loopback server on another port. Display: browser
  default and scheme shown (Chrome `omnibox.prevent_url_elisions`; Zen `browser.urlbar.trimURLs`
  false). Window states: visible (V), minimized (M), fully covered (C), background tab (T). Five
  reads per cell, 0.4 s apart. Timing: a poll every `Settings().poll_s` (0.1 s) from the launch
  to the first True of `shows_own_address`; then, after 2.5-4.3 s, the hide (minimize or cover,
  alternating) and a poll every `Settings().min_gap_s` (1.0 s) to the first False; the typed
  address counts the `Presence.report` calls. 10 runs per browser and form.
- **Statistics.** n, median, p95 (nearest rank) and max, in ms.
- **History.** The classifier was first looser (a third of the references' difference). It took
  blank first paints of Zen for content (18 of 20 cold launches "stale") and a variant of the
  wait note for content (up to 8 of 20 in case e), so every reveal case was run again with the
  strict limit above; no number of the first batches is reported. Of the 6-min Zen case c runs,
  the first batch (3 runs) logged one visible report each while the window was minimized, a
  second batch (3 runs, with the times logged) none; the first batch's numbers were replaced by
  the second. The Zen kill phase was run twice: the reason for the re-run and the first
  batch's n and result were not kept; the kill table holds only the second batch (n 10).
- **Diagnostic, not in the tables.** Chrome stayed visible too under a covering window of
  another kind (a topmost borderless Windows Forms window started in the job), 10 s.

## Results

### Reveal: time from the action to the halt and to the content

Cells are n / median / p95 / max in ms from t0. "Stale" counts runs with a content frame before
the halt (a leak); "early" runs with a content frame before the confirming reply. Chrome: 0
invalid runs in every case. Zen invalid runs: a 1, b 3, c 1, e 7, none in d. Of these 12, 9 had
no content frame or no confirmation matching the run's references, 2 of case e also with the
blocker page not loaded or the state not back to ready, and 3 had no window within 30 s in the
reference launch.

| Browser | Case | First frame | First visible report | Halt | Confirming reply | First content frame | Stale / early runs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Chrome | a cold launch | 20 / 598.0 / 1760.2 / 1963.2 | 20 / 423.9 / 1187.6 / 1314.5 | 20 / 448.7 / 1210.7 / 1395.9 | 20 / 699.9 / 1459.8 / 1622.4 | 20 / 873.0 / 1760.2 / 1963.2 | 0 / 0 |
| Chrome | b warm new tab | 20 / 111.9 / 175.2 / 212.6 | 20 / 104.3 / 168.4 / 257.4 | 20 / 124.0 / 190.1 / 284.0 | 20 / 374.4 / 443.4 / 538.2 | 20 / 494.6 / 623.0 / 744.2 | 0 / 0 |
| Chrome | c restore | 20 / 6.5 / 13.0 / 44.2 | 20 / 8.1 / 10.9 / 11.0 | 20 / 27.4 / 33.6 / 33.9 | 20 / 268.8 / 278.8 / 279.2 | 20 / 333.8 / 347.1 / 348.7 | 0 / 0 |
| Chrome | e background tab to front | 20 / 25.3 / 35.6 / 37.3 | 20 / 8.1 / 10.7 / 11.8 | 20 / 25.4 / 33.4 / 33.6 | 20 / 269.8 / 276.7 / 278.1 | 20 / 336.2 / 345.5 / 357.7 | 0 / 0 |
| Zen | a cold launch | 20 / 496.5 / 687.6 / 714.8 | 20 / 1587.8 / 1874.0 / 1973.2 | 20 / 1614.0 / 1894.1 / 1993.4 | 20 / 2023.2 / 2379.3 / 3009.0 | 20 / 2427.7 / 3237.9 / 3716.7 | 0 / 0 |
| Zen | b warm new tab | 20 / 35.1 / 62.8 / 65.8 | 20 / 141.2 / 325.4 / 775.3 | 20 / 155.3 / 350.1 / 799.1 | 20 / 397.6 / 592.4 / 1095.8 | 20 / 472.1 / 651.3 / 1233.2 | 0 / 1 |
| Zen | c restore | 20 / 12.4 / 82.3 / 93.1 | 20 / 91.5 / 178.5 / 195.1 | 20 / 110.0 / 193.9 / 209.7 | 20 / 345.8 / 448.9 / 449.3 | 20 / 407.4 / 1057.6 / 1437.8 | 0 / 0 |
| Zen | e background tab to front | 20 / 9.3 / 15.5 / 16.1 | 20 / 19.3 / 21.7 / 26.6 | 20 / 35.5 / 42.8 / 43.1 | 20 / 273.2 / 276.0 / 282.0 | 20 / 304.2 / 313.0 / 322.1 | 0 / 0 |

The one early run (Zen b) had one content frame 7 ms after the halt, before the reply; the
halt was already in force.

Long hide (6 min), 3 valid runs per cell, 0 invalid; the median is the middle run:

| Browser | Case | First visible report | Halt | Confirming reply | First content frame | Stale / early runs |
| --- | --- | --- | --- | --- | --- | --- |
| Chrome | c restore | 3 / 8.8 / 12.0 / 12.0 | 3 / 25.3 / 30.9 / 30.9 | 3 / 266.7 / 274.6 / 274.6 | 3 / 291.1 / 292.5 / 292.5 | 0 / 0 |
| Chrome | e background tab | 3 / 9.5 / 10.3 / 10.3 | 3 / 33.4 / 34.5 / 34.5 | 3 / 268.6 / 268.7 / 268.7 | 3 / 290.5 / 307.6 / 307.6 | 0 / 0 |
| Zen | c restore | 3 / 123.5 / 129.5 / 129.5 | 3 / 133.9 / 152.7 / 152.7 | 3 / 377.6 / 382.8 / 382.8 | 3 / 409.0 / 418.0 / 418.0 | 0 / 0 |
| Zen | e background tab | 3 / 17.8 / 18.4 / 18.4 | 3 / 33.8 / 36.2 / 36.2 | 3 / 271.2 / 272.5 / 272.5 | 3 / 302.4 / 307.4 / 307.4 | 0 / 0 |

### Case d: covering a window did not hide the page

In 20 of 20 valid runs per browser the page never reported hidden while the cover was up (3 s;
the cover was verified at three points and `visible_monitors` did not count the window visible),
so there was no hide to reveal. After the cover was destroyed the next visible report came after
20 / 760.9 / 773.4 / 776.1 ms (Chrome) and 20 / 733.6 / 749.3 / 750.0 ms (Zen), the 2 s schedule
at a fixed phase.

### Hide transitions

Time from the hide action; "reported hidden" is runs with a `visible: false` report; every
`set_ui_open(False)` came from that report (source report), none from the lease expiring.

| Browser | Case | Reported hidden | `visible: false` report ms (= `set_ui_open(False)` ms) | Visible reports while hidden | Content frames after `set_ui_open(False)` while the window counted visible | Window counted visible at the end of the hide |
| --- | --- | --- | --- | --- | --- | --- |
| Chrome | c minimize | 20 / 20 | 20 / 8.6 / 10.3 / 10.6 | 0 | 0 of 20 runs | 0 / 20 |
| Chrome | d cover | 0 / 20 | n/a | 0 | n/a | 0 / 20 |
| Chrome | e background tab | 20 / 20 | 20 / 77.5 / 113.1 / 330.5 | 0 | 1 of 20 runs | 20 / 20 |
| Zen | c minimize | 20 / 20 | 20 / 24.6 / 32.9 / 34.9 | 0 | 0 of 20 runs | 0 / 20 |
| Zen | d cover | 0 / 20 | n/a | 0 | n/a | 0 / 20 |
| Zen | e background tab | 20 / 20 | 20 / 368.4 / 380.7 / 665.2 | 0 | 0 of 20 runs | 20 / 20 |
| Chrome | c, 6 min | 3 / 3 | 3 / 8.3 / 8.9 / 8.9 | 0 | 1 of 3 runs | 0 / 3 |
| Chrome | e, 6 min | 3 / 3 | 3 / 74.3 / 76.8 / 76.8 | 0 | 0 of 3 runs | 3 / 3 |
| Zen | c, 6 min | 3 / 3 | 3 / 26.5 / 84.7 / 84.7 | 0 (the earlier batch of 3 runs: 1 per run) | 0 of 3 runs | 0 / 3 |
| Zen | e, 6 min | 3 / 3 | 3 / 369.8 / 370.4 / 370.4 | 0 | 0 of 3 runs | 3 / 3 |

The frames after a hide are one content frame each (2 of 92 hides: one Chrome tab switch, one
6 min Chrome minimize): a frame grabbed after recording resumed while `visible_monitors` still
counted the window visible. Each was a frame of Akashic's own UI (see Findings, "Own content
can be captured").

### Schedule and lease

Gaps between consecutive visible reports of one page id, ms (n / median / p95 / max). "never":
nothing confirms the page, the page reports every 250 ms. "halts": confirmed, every 2 s. No
page reported hidden and no page id changed in any of these runs.

| Browser | Mode | Visible and unfocused | Half covered | Fully covered (opaque, page still visible) |
| --- | --- | --- | --- | --- |
| Chrome | never | 226 / 264.4 / 266.2 / 268.3 | 226 / 264.4 / 266.4 / 268.1 | 226 / 264.2 / 266.4 / 272.0 |
| Chrome | halts | 29 / 2007.3 / 2017.4 / 2018.9 | 29 / 2008.5 / 2016.9 / 2018.9 | 29 / 2006.5 / 2018.0 / 2018.0 |
| Zen | never | 236 / 253.2 / 254.0 / 262.6 | 236 / 252.9 / 253.6 / 254.1 | 236 / 253.0 / 253.7 / 254.2 |
| Zen | halts | 29 / 2003.0 / 2003.7 / 2003.8 | 29 / 2003.0 / 2004.1 / 2004.5 | 29 / 2003.2 / 2003.9 / 2003.9 |

Lease, 10 min each ("halts"):

| Browser | State | Gaps n / median / p95 / max | Lease lapses by expiry | Hidden reports |
| --- | --- | --- | --- | --- |
| Chrome | visible and unfocused | 298 / 2008.4 / 2018.0 / 2045.7 | 0 | 0 |
| Chrome | half covered | 298 / 2007.1 / 2017.7 / 2019.4 | 0 | 1 (see Findings, "Own content can be captured") |
| Zen | visible and unfocused | 299 / 2003.1 / 2003.8 / 2005.0 | 0 | 0 |
| Zen | half covered | 298 / 2003.1 / 2003.7 / 2003.9 | 0 | 0 |

Browser killed by `TerminateJobObject` while showing confirmed content, time to
`set_ui_open(False)` (always by the lease expiring, source expire; no process of the job left):

| Browser | n valid | Min | Median | p95 | Max |
| --- | --- | --- | --- | --- | --- |
| Chrome | 10 | 4428.6 | 5593.5 | 5796.3 | 5796.3 |
| Zen | 10 | 4273.5 | 5668.6 | 6628.6 | 6628.6 |

### Failsafe matrix

Five reads per cell; V visible, M minimized, C fully covered, T background tab. `is_own_address`
is the string test on the read address, `shows_own_address` and `judge(...).own_address` are the
real calls on the windows of the job. All 20 reads per row returned an address. The read time is
the raw `WindowsUia.read_url`.

| Browser | Display | Address form | Raw read has a scheme | `normalize_url` not None | `is_own_address` (V / M / C / T) | `shows_own_address` (V / M / C / T) | `judge().own_address` (V / M / C / T) | Read ms: median / max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Chrome | default | tray-opened `/` | none 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 15.0 / 36.7 |
| Chrome | default | typed 127.0.0.1 | none 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 13.6 / 23.8 |
| Chrome | default | typed localhost | none 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 14.0 / 23.6 |
| Chrome | default | used `/open?code=` | none 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 13.9 / 23.6 |
| Chrome | default | other port | none 20 | 20/20 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 13.9 / 25.7 |
| Chrome | scheme shown | tray-opened `/` | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 14.1 / 23.8 |
| Chrome | scheme shown | typed 127.0.0.1 | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 14.1 / 24.8 |
| Chrome | scheme shown | typed localhost | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 14.6 / 23.9 |
| Chrome | scheme shown | used `/open?code=` | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 15.2 / 26.8 |
| Chrome | scheme shown | other port | http 20 | 20/20 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 15.5 / 24.8 |
| Zen | default | tray-opened `/` | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.8 / 40.6 |
| Zen | default | typed 127.0.0.1 | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.5 / 39.9 |
| Zen | default | typed localhost | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.7 / 32.4 |
| Zen | default | used `/open?code=` | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.5 / 47.2 |
| Zen | default | other port | http 20 | 20/20 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 8.5 / 28.8 |
| Zen | scheme shown | tray-opened `/` | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.7 / 32.6 |
| Zen | scheme shown | typed 127.0.0.1 | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.5 / 48.0 |
| Zen | scheme shown | typed localhost | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.6 / 47.4 |
| Zen | scheme shown | used `/open?code=` | http 20 | 20/20 | 5 / 5 / 5 / 0 | 5 / 0 / 0 / 0 | 5 / 0 / 0 / 0 | 8.7 / 30.3 |
| Zen | scheme shown | other port | http 20 | 20/20 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 8.6 / 80.4 |

The five cells per state are counts of 5. `visible_monitors` counted the window visible in V and
T and not in M and C in every cell. The typed and used-code pages (no session) made 0
`Presence.report` calls in every cell; the tray-opened page made 1.

### Failsafe timing

Cells are n / median / p95 / max in ms; 10 runs per browser and form, none missed.

| Browser | Form | Launch to first True of `shows_own_address` (poll 0.1 s) | Hide to first False (poll 1.0 s) | `Presence.report` calls, total over 10 runs |
| --- | --- | --- | --- | --- |
| Chrome | tray-opened `/` | 10 / 316.6 / 775.9 / 775.9 | 10 / 758.7 / 946.8 / 946.8 | 38 |
| Chrome | typed 127.0.0.1 | 10 / 312.0 / 322.6 / 322.6 | 10 / 611.5 / 872.7 / 872.7 | 0 |
| Zen | tray-opened `/` | 10 / 1091.3 / 1255.6 / 1255.6 | 10 / 697.8 / 921.8 / 921.8 | 38 |
| Zen | typed 127.0.0.1 | 10 / 1071.2 / 1400.3 / 1400.3 | 10 / 514.3 / 971.7 / 971.7 | 0 |

## Findings against the chosen values

- **250 ms (unconfirmed).** Gaps were 253-264 ms at the median and at most 272 ms: the 250 ms
  timer plus the request, in every state, including fully covered. No throttling of the timer
  was seen in the visible, half covered and fully covered windows. The confirming reply is the
  first report after the halt, so it comes one unconfirmed period after the first report: 254-
  262 ms after it at the median in the restore and background-tab cases of both browsers. The
  halt itself came at a median of 25-27 ms after t0 in Chrome c and e, 36 ms in Zen e and 110
  ms in Zen c (the fake's 10 ms delay measured 16-19 ms after the first visible report); the
  time the real capture child needs to halt was not measured.
- **2 s (confirmed).** Gaps were 2003-2008 ms at the median and at most 2046 ms over two 10 min
  runs per browser (about 300 gaps each): less than a third of the 6 s lease. No state
  (visible, half covered, fully covered) changed the period.
- **6 s lease.** 0 lease lapses by expiry in 4 runs of 10 min (2 browsers x 2 states). (The one
  page-hidden event of Chrome's half-covered run is under "Own content can be captured".) After a kill, the halt ended 4.3-6.6 s later (median 5.6-5.7 s), never
  later than 7 s: the lease runs from the last report, which comes 0-2 s before the kill, and
  `expire` runs once a second. The plan's "6-7 s" is the upper part of that range.
- **A page covered by another window stays visible to itself (privacy finding 1).** In both
  browsers an opaque topmost window over the whole browser window (and, in Chrome, a Windows
  Forms window of another kind) did not make the page hidden: no `visible: false` report in 20
  of 20 runs (case d, 3 s) and in the 60 s fully covered runs, and the page kept reporting every
  250 ms or 2 s. While the cover was up, `visible_monitors` did not count the window visible
  (case d: verified at three points). So a fully covered Akashic page (for example behind a
  game) keeps the lease alive and recording halted as long as the page is open: screen, audio
  and media stop together, so a game and its voice chat are lost, logged as a `ui_open` gap
  (CAPTURE.md "While Akashic is open"). The presence loop has no hide event for occlusion, only
  for minimize and tab switches. This follows the measured behaviour of Chrome 157 and Zen
  1.24.1b on this PC; no occlusion setting was changed.
- **No stale frame at reveal.** In the 132 valid runs of cases b, c and e (12 of them 6 min
  runs) no content frame was grabbed before the halt, and the page showed the wait note again
  at every reveal (the page conceals itself when hidden, also after 6 min). Case a is left out:
  its probe starts only once the window is found and Chrome's first frame (median 598 ms) came
  after the halt (median 449 ms); how many case a runs had a frame before the halt was not
  kept. One content frame came between the halt and the confirming reply (Zen, case b).
- **Own content can be captured (privacy finding 2).** Every hide (minimize, background tab)
  was reported by the page at a median of 8.6 ms (Chrome minimize; max 10.6), 24.6 ms (Zen
  minimize; max 34.9), 77.5 ms (Chrome tab switch; max 330.5) and 368.4 ms (Zen tab switch; max
  665.2). In 2 of 92 hides (one Chrome tab switch, one 6 min Chrome minimize) one content
  frame was grabbed after `set_ui_open(False)` while `visible_monitors` still counted the
  window visible. The frame was Akashic UI content (the shell, not the wait note): the page
  reports hidden, and recording resumes, before the window stops showing it (the minimize
  animation, or a tab switch not yet composited; in the Chrome tab switch the frame was the
  Akashic tab itself). The time from `set_ui_open(False)` to that frame was not kept. Whether a
  real capture would record it depends on the capture child's resume latency, which was not
  measured. If it does, the child could record a screenshot and OCR of whatever the page showed
  (transcripts, answers), which contradicts the CAPTURE.md guarantee that Akashic's own pages
  never show recorded content while recording runs. The background tab case leaves the window
  visible to `visible_monitors` for the whole hide, so a window that shows another tab is
  captured as soon as the page reports hidden. Also in this group: in Chrome's half-covered
  10 min run the page reported hidden once while its window stayed on screen and was counted
  visible, and became visible again with a new page id; `set_ui_open(False)` came from that
  report, so recording resumed with the Akashic page still showing until the next visible
  report. The time from the hidden report to the next visible report was not kept, the pixel
  probe was not running in that phase (so it is unknown whether content stayed on screen), and
  the cause was not found.
- **Hidden pages are quiet.** 0 visible reports while hidden in every run, except the first
  batch of the 6 min Zen minimize runs (1 per run, 3 of 6 runs overall), not reproduced in the
  second batch and not explained.
- **Reveal latency to content.** Restore, background tab and warm new tab: the confirming reply
  after 0.27-0.40 s and the first content frame after 0.30-0.49 s at the median in Chrome and
  Zen (p95 up to 0.62 s, except Zen restore 1.06 s and Zen warm tab 0.65 s). A cold start of the
  UI is dominated by the browser: confirmed after 0.70 s and content after 0.87 s in Chrome,
  after 2.0 s and 2.4 s in Zen (median).
- **Failsafe.** `shows_own_address` and `judge().own_address` were True for exactly the visible
  windows showing an own-port form (tray-opened, typed 127.0.0.1, typed localhost, used code)
  in both browsers and both displays (V 5/5 of 5 in all 16 cells), and False for the other-port
  server (0/5 in the 4 cells) and for minimized, covered and background-tab windows. The
  address the readers return has no scheme in Chrome by default (`normalize_url` adds one:
  20/20) and `http` in Chrome with the scheme shown and in Zen both ways. A typed address in a
  profile with no session made 0 `Presence.report` calls, so only the failsafe sees it. The
  failsafe first True came 0.3 s after the launch in Chrome and 1.1 s in Zen, the first False
  within one poll period of the hide (median 0.5-0.76 s, max 0.97 s).

## Not run

- Edge (a fresh profile may sign in to the PC's Microsoft account); it stays open in OPEN.md.
  Brave and Firefox.
- The user's real profiles, tabs and windows; `--app` windows; other virtual desktops; the
  capture child's own halt latency (the fake supervisor halts after 10 ms).
- Foreground checks after the minimize, restore and cover actions; the exclusivity scan and
  the focus counters in the kill, schedule, matrix and failsafe-timing phases.
- A covering window of another kind in Zen (the Windows Forms variant was run in Chrome only),
  and an occlusion setting of either browser changed on purpose.
- Case d reveal timing (nothing to reveal: the page never hid), and the cause of the one
  hidden event in Chrome's half-covered lease run and of the visible reports in the first
  batch of the 6 min Zen minimize runs.
- The sample period of the `visible_monitors` sampler was not recorded.
