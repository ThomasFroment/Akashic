# Title-change hold spike 2026-11-16

Revised 2026-11-16: the "rule 2" and "rule 3" below refer to the numbering of the deleted design,
now [CAPTURE.md](../CAPTURE.md) "Which windows are captured". "None is implemented" for the open
cases is outdated: site-wide user path rules exist ([DECISIONS.md](../DECISIONS.md) row 18).

Revised 2026-11-18: "row 18" above is the DECISIONS.md row "Exclusions for now" (path rules cover
their whole site); the rows have moved since.

Revised 2026-11-17: F5 never reported the `OBJID_CLIENT` `NAMECHANGE` (Chrome's carry a negative
idChild, which F5 drops), and F17 removed the unused branch, so it stays undelivered; see
[title-hold pages](2026-11-17-title-hold-pages.md), "The -4 events and F5" and "Decision for F17".

Measurements behind TODO A9 (F9) of the monitor-capture rules ([CAPTURE.md](../CAPTURE.md)),
rule 3: when a kept browser window changes its title, how long after the title event do the
address and the new pixels arrive, and what would a hold that starts at the title event have
saved. Test windows were disposable Chrome and Zen processes started by the spike with
throwaway profiles and a page server on 127.0.0.1 inside the spike; the user's browsers and
windows were not touched. Page B ("excluded") was excluded by a user URL rule through the real
`Exclusions.check_url`. Titles, addresses and pixels were compared in memory and dropped; the
result files hold timestamps, classes and counts only. The scripts were throwaway and none is
kept; no file under `src/` or `tests/` changed.

## Machine

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01, as in the F8 report), Windows 11
build 26200, Python 3.14.10. Monitors as in the F8 report; the test window was 1200x800 on a
2560x1440 monitor at 100%. Cases 1-7 and the cold runs ran on the left one; cases 4b and 8 ran
on the right one (2560x1440, 100%) after the placement rule changed. The refresh rate of the
monitors was not recorded.

| Browser | Version |
| --- | --- |
| Chrome | 157.0.8103.51 |
| Zen | 1.24.1b (`application.ini` `Version`) |

## Method

- **Pages.** A (kept): green, title "AKF9 page A", `http://127.0.0.1:<port>/a`. B (excluded):
  magenta with the text "AKF9 SECRET", `/secret...`, excluded by the user rule
  `*127.0.0.1:<port>/secret*`; `api_origin` was on another port so loopback was not `own_ui`.
  The server sent no `no-store` header. A page action was released by a long-poll `/go` that the
  spike answered; t0 is the monotonic time just before the response was written. Case 1b's t0 is
  the `Popen` call; case 5's is the moment before `SendInput`.
- **Recorders** (one process, five threads, all stamped with `time.monotonic()`, the clock of the
  F5 events): the production `Win32WindowEvents` (F5) drained every 3 ms, keeping the `title`
  events of the test window; a raw `SetWinEventHook` on `EVENT_OBJECT_NAMECHANGE` for the
  browser's process with any `idObject` and any window (classified as root window, child window,
  other object of the root, or no window); a 2 ms `GetWindowTextW` poll that also checked the
  foreground window and `WindowFromPoint` at the viewport centre every 10 ms; a loop of the
  unchanged `WindowsUia.browser_caption` and `WindowsUia.read_url` (one read = both calls),
  values normalized by `normalize_url` and judged by `Exclusions`; and `WgcCaptures.start(hwnd,
  0)` with a 2 ms `latest_seq` poll that grabs on change and classifies the centre 50% of the
  frame (green of A, magenta of B within +-40; a frame shows B when 2% of the centre is magenta).
  The frame `at` is the arrival time of the production frame; the grab time is when the poller
  copied it.
- **Validity.** A run is invalid when, between t0 - 0.3 s and the end of the run, the foreground
  window was not the test window, the root window at the viewport centre was not the test
  window, or the F5 hook reported unhealthy. A run where B never appeared is "failed". Input is
  sent only for case 5 (Ctrl+PageDown) and only when the foreground window is the test window at
  that moment. 22 valid runs per case and browser; case 8 counts only runs where the B page
  reported `pageshow.persisted` true.
- **Exclusivity.** Before each case and before each cold run a read-only scan of
  python/pythonw/chrome/msedge/zen/firefox processes counted any whose command line points at
  another session's `Temp\claude\` folder or runs `synthetic_window.py`: 27 scans, 0 found; the
  spike never waited.
- **Cases.** 1a `window.open` from A; 1b Chrome `chrome.exe --user-data-dir=<ours> URL`; 2
  `location.href` to B with A's title; 3 `pushState` to the B address, then the paint on the
  next animation frame; 4 paint B, then `pushState` two animation frames later; 4b (added) paint
  B, then `pushState` 100 ms later; 5 Ctrl+PageDown to an already loaded B tab; 6 navigation to B
  without a `<title>`, title set by script 1 s after load; 7 A sets its own title every 2 s, no
  navigation; 8 B, then A, then `history.back()` to B. Case 8 uses timer-driven pages with no
  long-poll (a page with the aborted long-poll was never restored from the cache by Zen, in
  every try; plain pages were), t0 is the `/mark` response that precedes `history.back()`;
  the first Chrome version of case 8 (poll pages) was discarded and the case rerun for both
  browsers. Zen's new tab case is `window.open` only.
- **Profiles.** Chrome flags `--no-first-run --no-default-browser-check --disable-sync
  --disable-background-networking --disable-component-update --disable-popup-blocking
  --disable-search-engine-choice-screen --window-position --window-size`; Zen `-no-remote
  -profile` with a `user.js` turning off telemetry, updates, safe browsing, the welcome screen,
  Firefox accounts and the popup blocker. A crop of each browser window was looked at once
  (deleted afterwards): no sign-in, sync or account notice appeared in either.
- **Simulation.** Over each run's recorded timeline, from t0 on: a frame is judged by the first
  read that starts after its grab ("literal"); the verdict is excluded when that read returned
  B's address, kept otherwise (also for `None`); a leak is a frame showing B that is kept.
  (a) is that alone. (b) holds every frame with `at >= event.at` until the first read that
  started after the event ends. (c) holds until a read that started after the event returns an
  address different from the one last judged before the event, or the bound is reached: both T
  elapsed and N reads that started after the event completed ("both"; "either" is the earlier of
  the two). A `None` read counts toward N only. Held frames are released and judged either
  literally or by the read that ended the hold ("release-read judge"). Case 8 was cut at 1.4 s,
  before the restored page navigates away on its own.

## Results

Statistics are median / p95 (nearest rank) / max; "-" means no value. 22 runs per cell unless
stated.

### Times (ms from t0): median / p95 / max

| Case | Browser | Valid (invalid, failed) | F5 title event (n) | Raw root NAMECHANGE | Title poll 2 ms | First read returning B (end) | Viewport changed | B pixels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1a `window.open` | chrome | 22 (0, 0) | 6.2 / 7.4 / 8.2 (22) | 6.2 / 7.4 / 8.2 | 7.7 / 8.9 / 10.6 | 30.6 / 105.0 / 111.5 | 21.6 / 29.9 / 31.2 | 21.6 / 29.9 / 31.2 |
| 1a `window.open` | zen | 22 (0, 0) | 15.4 / 17.4 / 21.5 (22) | 15.4 / 17.4 / 21.4 | 16.5 / 17.8 / 22.6 | 37.0 / 43.4 / 89.6 | 23.7 / 36.6 / 36.9 | 46.7 / 51.5 / 85.7 |
| 1b `chrome.exe URL` | chrome | 22 (0, 0) | 50.8 / 55.9 / 69.8 (22) | 50.8 / 55.9 / 69.8 | 52.2 / 56.8 / 71.0 | 81.0 / 92.9 / 155.5 | 71.5 / 76.7 / 95.4 | 71.5 / 76.7 / 95.4 |
| 2 same-title navigation | chrome | 22 (0, 0) | - (0) | - | - | 29.4 / 36.2 / 38.7 | 24.4 / 33.5 / 36.6 | 24.4 / 33.5 / 36.6 |
| 2 same-title navigation | zen | 22 (0, 0) | 11.1 / 12.1 / 12.1 (2) | 11.1 / 12.1 / 12.1 | 12.4 / 13.8 / 13.8 | 21.2 / 27.3 / 27.9 | 35.3 / 42.6 / 47.4 | 35.3 / 42.6 / 47.4 |
| 3 pushState, address first | chrome | 22 (0, 0) | - (0) | - | - | 15.8 / 26.8 / 29.4 | 8.4 / 23.9 / 24.1 | 8.4 / 23.9 / 24.1 |
| 3 pushState, address first | zen | 22 (0, 0) | - (0) | - | - | 8.0 / 12.5 / 12.5 | 7.4 / 10.6 / 10.8 | 7.4 / 10.6 / 10.8 |
| 4 pushState, paint first | chrome | 22 (0, 0) | - (0) | - | - | 24.0 / 31.7 / 31.8 | 6.5 / 12.2 / 16.7 | 6.5 / 12.2 / 16.7 |
| 4 pushState, paint first | zen | 22 (0, 0) | - (0) | - | - | 8.9 / 12.6 / 13.7 | 7.7 / 11.0 / 11.1 | 7.7 / 11.0 / 11.1 |
| 4b paint, address 100 ms later | chrome | 22 (0, 0) | - (0) | - | - | 122.4 / 137.1 / 137.4 | 3.0 / 12.1 / 12.4 | 3.0 / 12.1 / 12.4 |
| 4b paint, address 100 ms later | zen | 22 (0, 0) | - (0) | - | - | 108.5 / 111.1 / 113.2 | 4.8 / 8.2 / 8.9 | 4.8 / 8.2 / 8.9 |
| 5 switch to loaded tab | chrome | 22 (0, 0) | 8.1 / 9.6 / 9.7 (22) | 8.1 / 9.6 / 9.8 | 9.7 / 10.8 / 10.9 | 25.8 / 90.1 / 96.1 | 17.7 / 21.8 / 22.4 | 17.7 / 21.8 / 22.4 |
| 5 switch to loaded tab | zen | 22 (0, 0) | 8.6 / 10.1 / 10.9 (22) | 8.6 / 10.1 / 10.9 | 10.1 / 12.0 / 13.2 | 12.4 / 16.0 / 16.1 | 26.7 / 36.7 / 41.1 | 26.7 / 36.7 / 41.1 |
| 6 title set 1 s late | chrome | 22 (0, 0) | 11.3 / 12.5 / 12.6 (22) | 11.3 / 12.5 / 12.6 | 12.8 / 14.8 / 14.8 | 31.8 / 37.1 / 37.9 | 23.0 / 27.1 / 30.9 | 23.0 / 27.1 / 30.9 |
| 6 title set 1 s late | zen | 22 (0, 0) | 13.9 / 15.9 / 16.2 (22) | 13.9 / 15.9 / 16.2 | 14.7 / 17.2 / 17.3 | 25.4 / 30.0 / 30.5 | 33.8 / 38.4 / 40.5 | 33.8 / 38.4 / 40.5 |
| 7 own title every 2 s | chrome | 22 (0, 0) | 201.4 / 203.5 / 204.5 (22) | 201.4 / 203.5 / 204.5 | 203.1 / 205.2 / 206.3 | - | - | - |
| 7 own title every 2 s | zen | 22 (0, 0) | 3.0 / 3.4 / 3.5 (22) | 3.0 / 3.4 / 3.5 | 4.0 / 5.1 / 5.6 | - | - | - |
| 8 bfcache back | chrome | 22 (0, 0) | 6.5 / 7.3 / 7.9 (22) | 6.5 / 7.3 / 7.8 | 7.5 / 8.9 / 9.4 | 23.6 / 28.4 / 29.5 | 20.8 / 28.7 / 32.4 | 20.8 / 28.7 / 32.4 |
| 8 bfcache back | zen | 22 (0, 0) | 8.8 / 10.9 / 13.4 (22) | 8.8 / 10.9 / 13.4 | 10.2 / 11.8 / 14.6 | 22.1 / 28.3 / 33.8 | 24.1 / 27.8 / 29.5 | 24.1 / 27.8 / 29.5 |

### Order counts (22 runs each)

| Case | Browser | Title before pixels | Title after pixels | No title event | Address before pixels | Address overlaps pixels | Address after pixels |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1a `window.open` | chrome | 22 | 0 | 0 | 3 | 16 | 3 |
| 1a `window.open` | zen | 22 | 0 | 0 | 21 | 1 | 0 |
| 1b `chrome.exe URL` | chrome | 22 | 0 | 0 | 3 | 18 | 1 |
| 2 same-title navigation | chrome | 0 | 0 | 22 | 7 | 15 | 0 |
| 2 same-title navigation | zen | 2 | 0 | 20 | 22 | 0 | 0 |
| 3 pushState, address first | chrome | 0 | 0 | 22 | 3 | 17 | 2 |
| 3 pushState, address first | zen | 0 | 0 | 22 | 8 | 14 | 0 |
| 4 pushState, paint first | chrome | 0 | 0 | 22 | 0 | 16 | 6 |
| 4 pushState, paint first | zen | 0 | 0 | 22 | 5 | 17 | 0 |
| 4b paint, address 100 ms later | chrome | 0 | 0 | 22 | 0 | 0 | 22 |
| 4b paint, address 100 ms later | zen | 0 | 0 | 22 | 0 | 0 | 22 |
| 5 switch to loaded tab | chrome | 22 | 0 | 0 | 4 | 17 | 1 |
| 5 switch to loaded tab | zen | 22 | 0 | 0 | 22 | 0 | 0 |
| 6 title set 1 s late | chrome | 22 | 0 | 0 | 4 | 17 | 1 |
| 6 title set 1 s late | zen | 22 | 0 | 0 | 22 | 0 | 0 |
| 7 own title every 2 s | chrome | 0 | 0 | 22 | 0 | 0 | 0 |
| 7 own title every 2 s | zen | 0 | 0 | 22 | 0 | 0 | 0 |
| 8 bfcache back | chrome | 22 | 0 | 0 | 6 | 16 | 0 |
| 8 bfcache back | zen | 22 | 0 | 0 | 11 | 11 | 0 |

### Address lag, ms (first B read end minus the reference): median / p95 / max

| Case | Browser | After the title event | After the first B pixels |
| --- | --- | --- | --- |
| 1a `window.open` | chrome | 24.4 / 98.7 / 105.8 | 5.4 / 86.2 / 96.1 |
| 1a `window.open` | zen | 21.2 / 26.5 / 68.1 | -10.9 / -1.7 / 3.9 |
| 1b `chrome.exe URL` | chrome | 29.8 / 40.5 / 104.6 | 8.9 / 17.7 / 83.9 |
| 2 same-title navigation | chrome | - | 6.2 / 14.6 / 17.8 |
| 2 same-title navigation | zen | 12.5 / 17.9 / 17.9 | -14.1 / -2.6 / -2.2 |
| 3 pushState, address first | chrome | - | 10.2 / 20.5 / 22.9 |
| 3 pushState, address first | zen | - | 1.5 / 6.3 / 7.2 |
| 4 pushState, paint first | chrome | - | 18.3 / 24.4 / 25.6 |
| 4 pushState, paint first | zen | - | 1.4 / 6.7 / 9.3 |
| 4b paint, address 100 ms later | chrome | - | 119.3 / 133.8 / 134.5 |
| 4b paint, address 100 ms later | zen | - | 103.2 / 107.8 / 108.4 |
| 5 switch to loaded tab | chrome | 17.7 / 81.7 / 86.5 | 6.2 / 73.7 / 74.4 |
| 5 switch to loaded tab | zen | 3.6 / 6.3 / 6.7 | -16.1 / -7.7 / -5.2 |
| 6 title set 1 s late | chrome | 20.0 / 26.2 / 27.1 | 8.5 / 15.2 / 19.1 |
| 6 title set 1 s late | zen | 11.7 / 15.4 / 15.9 | -8.2 / -3.3 / -3.1 |
| 7 own title every 2 s | chrome | - | - |
| 7 own title every 2 s | zen | - | - |
| 8 bfcache back | chrome | 17.1 / 21.9 / 23.5 | 3.7 / 8.9 / 12.2 |
| 8 bfcache back | zen | 13.2 / 18.8 / 25.0 | -0.0 / 7.9 / 8.2 |

### Leaks: runs with a leaked frame / leaked B frames (T=500 ms, N=3)

| Case | Browser | (a) grab-then-judge | (b) first read, release-read judge | (b) literal judge | (c) both, release-read judge | (c) literal judge | (c) either | None address reads (runs) | None caption reads (runs) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1a `window.open` | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 2 (2) | 0 (0) |
| 1a `window.open` | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |
| 1b `chrome.exe URL` | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 1 (1) | 0 (0) |
| 2 same-title navigation | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | 0 (0) |
| 2 same-title navigation | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |
| 3 pushState, address first | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | 0 (0) |
| 3 pushState, address first | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |
| 4 pushState, paint first | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | 0 (0) |
| 4 pushState, paint first | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |
| 4b paint, address 100 ms later | chrome | 22 / 44 | 22 / 44 | 22 / 44 | 22 / 44 | 22 / 44 | 22 / 44 | 0 (0) | 0 (0) |
| 4b paint, address 100 ms later | zen | 22 / 22 | 22 / 22 | 22 / 22 | 22 / 22 | 22 / 22 | 22 / 22 | 0 (0) | n/a (by design) |
| 5 switch to loaded tab | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 1 (1) | 2 (2) |
| 5 switch to loaded tab | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |
| 6 title set 1 s late | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | 0 (0) |
| 6 title set 1 s late | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |
| 7 own title every 2 s | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | 0 (0) |
| 7 own title every 2 s | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |
| 8 bfcache back | chrome | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | 0 (0) |
| 8 bfcache back | zen | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 (0) | n/a (by design) |

### Runs with a leak over all cases 1-3, 4, 5, 6, 8 (the 4b stress case apart), by bound; both, release-read judge

| T ms | N=1 | N=2 | N=3 | N=5 |
| --- | --- | --- | --- | --- |
| 100 | 0 of 330 | 0 of 330 | 0 of 330 | 0 of 330 |
| 250 | 0 of 330 | 0 of 330 | 0 of 330 | 0 of 330 |
| 500 | 0 of 330 | 0 of 330 | 0 of 330 | 0 of 330 |
| 1000 | 0 of 330 | 0 of 330 | 0 of 330 | 0 of 330 |

### Pending of the first B-pixel frame, ms: median / p95 / max

| Case | Browser | (a) | (b) T=0, N=1 | (c) T=500, N=3 |
| --- | --- | --- | --- | --- |
| 1a `window.open` | chrome | 42.9 / 86.2 / 96.1 | 23.6 / 86.2 / 96.1 | 23.6 / 86.2 / 96.1 |
| 1a `window.open` | zen | 19.2 / 22.8 / 25.6 | 14.8 / 21.3 / 22.8 | 15.8 / 486.3 / 489.3 |
| 1b `chrome.exe URL` | chrome | 34.4 / 52.8 / 83.9 | 29.0 / 37.6 / 83.9 | 29.0 / 37.6 / 83.9 |
| 2 same-title navigation | chrome | 33.4 / 46.4 / 48.2 | 33.4 / 46.4 / 48.2 | 33.4 / 46.4 / 48.2 |
| 2 same-title navigation | zen | 19.6 / 22.5 / 22.6 | 19.6 / 22.5 / 22.6 | 19.6 / 22.5 / 22.6 |
| 3 pushState, address first | chrome | 35.8 / 45.2 / 46.4 | 35.8 / 45.2 / 46.4 | 35.8 / 45.2 / 46.4 |
| 3 pushState, address first | zen | 18.8 / 22.1 / 22.1 | 18.8 / 22.1 / 22.1 | 18.8 / 22.1 / 22.1 |
| 4 pushState, paint first | chrome | 37.8 / 42.6 / 43.3 | 37.8 / 42.6 / 43.3 | 37.8 / 42.6 / 43.3 |
| 4 pushState, paint first | zen | 18.0 / 20.7 / 22.0 | 18.0 / 20.7 / 22.0 | 18.0 / 20.7 / 22.0 |
| 4b paint, address 100 ms later | chrome | 37.2 / 43.5 / 45.0 | 37.2 / 43.5 / 45.0 | 37.2 / 43.5 / 45.0 |
| 4b paint, address 100 ms later | zen | 19.6 / 22.1 / 22.6 | 19.6 / 22.1 / 22.6 | 19.6 / 22.1 / 22.6 |
| 5 switch to loaded tab | chrome | 38.5 / 90.2 / 93.5 | 24.4 / 90.2 / 93.5 | 24.4 / 90.2 / 93.5 |
| 5 switch to loaded tab | zen | 17.2 / 20.9 / 21.3 | 16.9 / 20.9 / 21.3 | 17.2 / 21.3 / 480.5 |
| 6 title set 1 s late | chrome | 34.2 / 45.2 / 46.4 | 20.0 / 32.8 / 33.6 | 20.0 / 32.8 / 33.6 |
| 6 title set 1 s late | zen | 19.9 / 23.2 / 25.3 | 15.7 / 23.2 / 25.3 | 15.7 / 23.2 / 25.3 |
| 8 bfcache back | chrome | 42.3 / 46.6 / 48.8 | 22.9 / 27.3 / 27.9 | 22.9 / 27.3 / 27.9 |
| 8 bfcache back | zen | 20.4 / 25.0 / 28.4 | 15.1 / 24.1 / 25.0 | 12.4 / 24.1 / 25.0 |

### Hold length per title event, ms: median / p95 / max (events; ended by changed address / bound)

| Case | Browser | (b) T=0, N=1 | (c) T=500, N=3 | (c) T=1000, N=3 |
| --- | --- | --- | --- | --- |
| 1a `window.open` | chrome | 35.9 / 96.9 / 105.8 (44; 44 / 0) | 35.9 / 96.9 / 105.8 (44; 44 / 0) | 35.9 / 96.9 / 105.8 (44; 44 / 0) |
| 1a `window.open` | zen | 21.2 / 29.0 / 38.4 (44; 40 / 4) | 21.9 / 500.0 / 500.0 (44; 41 / 3) | 21.9 / 1000.0 / 1000.0 (44; 41 / 3) |
| 1b `chrome.exe URL` | chrome | 41.9 / 55.8 / 104.6 (44; 44 / 0) | 41.9 / 55.8 / 104.6 (44; 44 / 0) | 41.9 / 55.8 / 104.6 (44; 44 / 0) |
| 2 same-title navigation | chrome | no title event | no title event | no title event |
| 2 same-title navigation | zen | 15.1 / 17.9 / 17.9 (4; 4 / 0) | 15.1 / 17.9 / 17.9 (4; 4 / 0) | 15.1 / 17.9 / 17.9 (4; 4 / 0) |
| 3 pushState, address first | chrome | no title event | no title event | no title event |
| 3 pushState, address first | zen | no title event | no title event | no title event |
| 4 pushState, paint first | chrome | no title event | no title event | no title event |
| 4 pushState, paint first | zen | no title event | no title event | no title event |
| 4b paint, address 100 ms later | chrome | no title event | no title event | no title event |
| 4b paint, address 100 ms later | zen | no title event | no title event | no title event |
| 5 switch to loaded tab | chrome | 34.4 / 100.2 / 100.7 (22; 22 / 0) | 34.4 / 100.2 / 100.7 (22; 22 / 0) | 34.4 / 100.2 / 100.7 (22; 22 / 0) |
| 5 switch to loaded tab | zen | 11.8 / 14.5 / 14.9 (22; 21 / 1) | 12.1 / 14.9 / 500.0 (22; 21 / 1) | 12.1 / 14.9 / 1000.0 (22; 21 / 1) |
| 6 title set 1 s late | chrome | 28.9 / 40.5 / 43.3 (44; 22 / 22) | 271.6 / 500.0 / 500.0 (44; 22 / 22) | 521.6 / 1000.0 / 1000.0 (44; 22 / 22) |
| 6 title set 1 s late | zen | 15.1 / 22.4 / 23.1 (44; 22 / 22) | 261.5 / 500.0 / 500.0 (44; 22 / 22) | 511.5 / 1000.0 / 1000.0 (44; 22 / 22) |
| 7 own title every 2 s | chrome | 28.0 / 33.8 / 36.1 (22; 0 / 22) | 500.0 / 500.0 / 500.0 (22; 0 / 22) | 1000.0 / 1000.0 / 1000.0 (22; 0 / 22) |
| 7 own title every 2 s | zen | 10.5 / 14.9 / 15.6 (22; 0 / 22) | 500.0 / 500.0 / 500.0 (22; 0 / 22) | 1000.0 / 1000.0 / 1000.0 (22; 0 / 22) |
| 8 bfcache back | chrome | 36.8 / 41.5 / 41.8 (22; 22 / 0) | 36.8 / 41.5 / 41.8 (22; 22 / 0) | 36.8 / 41.5 / 41.8 (22; 22 / 0) |
| 8 bfcache back | zen | 14.8 / 22.1 / 27.4 (22; 21 / 1) | 15.1 / 25.0 / 27.4 (22; 22 / 0) | 15.1 / 25.0 / 27.4 (22; 22 / 0) |

### Case 7 pending cost per own title change (one change every 2 s)

| Browser | Bound | Hold ms (median / p95 / max) | Share of the time pending |
| --- | --- | --- | --- |
| chrome | T=0 N=1 | 28.0 / 33.8 / 36.1 | 1.4% |
| chrome | T=100 N=1 | 100.0 / 100.0 / 100.0 | 5.0% |
| chrome | T=250 N=2 | 250.0 / 250.0 / 250.0 | 12.5% |
| chrome | T=500 N=3 | 500.0 / 500.0 / 500.0 | 25.0% |
| chrome | T=1000 N=3 | 1000.0 / 1000.0 / 1000.0 | 50.0% |
| chrome | T=1000 N=5 | 1000.0 / 1000.0 / 1000.0 | 50.0% |
| zen | T=0 N=1 | 10.5 / 14.9 / 15.6 | 0.5% |
| zen | T=100 N=1 | 100.0 / 100.0 / 100.0 | 5.0% |
| zen | T=250 N=2 | 250.0 / 250.0 / 250.0 | 12.5% |
| zen | T=500 N=3 | 500.0 / 500.0 / 500.0 | 25.0% |
| zen | T=1000 N=3 | 1000.0 / 1000.0 / 1000.0 | 50.0% |
| zen | T=1000 N=5 | 1000.0 / 1000.0 / 1000.0 | 50.0% |

### Raw NAMECHANGE on objects the F5 filter drops (idObject -4, OBJID_CLIENT, on the top-level window)

| Case | Browser | Runs with such an event | First such event, ms | Runs where it precedes the first B pixels | Root-window events (F5 passes) |
| --- | --- | --- | --- | --- | --- |
| 1a `window.open` | chrome | 22 | 3.5 / 4.8 / 5.4 | 22 | 22 |
| 1a `window.open` | zen | 22 | 13.6 / 16.4 / 18.4 | 22 | 22 |
| 1b `chrome.exe URL` | chrome | 22 | 50.5 / 55.5 / 69.3 | 22 | 22 |
| 2 same-title navigation | chrome | 22 | 9.0 / 10.1 / 10.1 | 22 | 0 |
| 2 same-title navigation | zen | 22 | 14.6 / 18.1 / 26.8 | 22 | 2 |
| 3 pushState, address first | chrome | 22 | 1.8 / 3.6 / 4.1 | 21 | 0 |
| 3 pushState, address first | zen | 0 | - | 0 | 0 |
| 4 pushState, paint first | chrome | 22 | 7.0 / 12.1 / 13.3 | 6 | 0 |
| 4 pushState, paint first | zen | 0 | - | 0 | 0 |
| 4b paint, address 100 ms later | chrome | 22 | 108.5 / 115.2 / 116.6 | 0 | 0 |
| 4b paint, address 100 ms later | zen | 0 | - | 0 | 0 |
| 5 switch to loaded tab | chrome | 22 | 6.3 / 7.8 / 7.9 | 22 | 22 |
| 5 switch to loaded tab | zen | 22 | 13.6 / 20.4 / 22.9 | 22 | 22 |
| 6 title set 1 s late | chrome | 22 | 9.1 / 10.2 / 10.6 | 22 | 22 |
| 6 title set 1 s late | zen | 22 | 15.7 / 20.2 / 22.3 | 22 | 22 |
| 7 own title every 2 s | chrome | 22 | 0.9 / 2.1 / 2.5 | n/a | 22 |
| 7 own title every 2 s | zen | 22 | 2.0 / 2.2 / 2.2 | n/a | 22 |
| 8 bfcache back | chrome | 22 | 4.8 / 5.6 / 6.1 | 22 | 22 |
| 8 bfcache back | zen | 22 | 13.2 / 17.2 / 18.7 | 22 | 22 |

### Cold first read (fresh browser, the reader released at t0; case 2 navigation), ms

| Browser | Run | Read start | Read end (B) | B pixels |
| --- | --- | --- | --- | --- |
| chrome | 1 | 0.5 | 49.1 | 28.3 |
| chrome | 2 | 2.5 | 42.8 | 29.9 |
| chrome | 3 | 2.1 | 48.5 | 35.0 |
| zen | 1 | 2.3 | 48.9 | 66.5 |
| zen | 2 | 1.0 | 40.4 | 52.9 |
| zen | 3 | 0.5 | 38.6 | 48.0 |

- chrome warm, case 2: first read returning B ends at 29.4 / 36.2 / 38.7 (start -3.2 / 14.7 / 17.4).
- zen warm, case 2: first read returning B ends at 21.2 / 27.3 / 27.9 (start 1.1 / 14.9 / 18.9).

## What the numbers say

- **Title events.** 200 runs (cases 1a, 1b, 5, 6, 8; Zen case 2 in 2 runs; case 7 aside) had
  an F5 `title` event, and in all 200 it came before the first B pixels (0 after). Chrome's event is 6-11 ms after t0 for in-tab changes and
  51 ms for the command-line tab (process hand-off included); Zen's is 9-15 ms. The 2 ms poll saw
  the same change 1-2 ms after the event in every case. Chrome cases 2, 3, 4, 4b changed no
  title: 0 of 88 runs had a title event. Zen case 2 had one in 2 of 22 runs, cases 3, 4, 4b none.
- **Same-document title (case 7).** Chrome delivered the F5 event 201.4 ms (max 204.5) after the
  script set the title; Zen 3.0 ms. The title event is therefore not a signal of when the page
  changed in Chrome.
- **Address.** The first read returning B ended 8.0-37.0 ms after t0 at the median, except the
  Chrome command-line tab (81.0) and 4b (the address is set 100 ms late). The slow tails are
  Chrome `window.open` 105.0 p95 / 111.5 max, the command-line tab 155.5 max, and tab switch
  96.1 max. In the slowest run (1a) the cause was one read that began 5.7 ms before t0, ran
  96 ms and returned `None`; there were 4 `None` address reads in 4 Chrome runs and 2 `None`
  caption reads in 2 Chrome runs, none in Zen. A read that began before a frame's grab never
  judges that frame. The worst lag of the first B read behind the
  title event is 105.8 ms (Chrome 1a), behind the first B pixels 96.1 ms (Chrome 1a); Zen's
  worst are 68.1 and 3.9 ms. Zen's address is read before the pixels in most runs.
- **Pixels.** First B pixels 3.0-8.4 ms after t0 for in-page changes, 17.7-35.3 ms for
  navigations and tab switches, 46.7 ms for a Zen new tab; Chrome 1b 71.5 ms.
- **Leaks.** Grab-then-judge alone (a) leaked 0 frames in every case except 4b. (b) and (c) leaked
  0 in the same cases, at every grid point (T 100-1000 ms, N 1-5, "both", literal or
  release-read judge, 330 runs per grid point). 4b leaked in 22 of 22 runs in both browsers (44
  frames in Chrome, 22 in Zen) under (a), (b) and (c) alike: there is no title event, so the
  hold never starts. In this data the hold prevented no leak that grab-then-judge did not
  already prevent; it changes only the pending time.
- **Pending.** Under (a) the window waits for the read that follows the first B frame: 17-43 ms
  median, at most 96.1 ms. Under (c) with T=500 ms the first B frame is released when the
  address changes (35.9 ms median hold in Chrome 1a, 21.9 in Zen 1a; maximum 105.8 ms in
  Chrome) except when the last judged address was already B (3 of 44 holds in Zen 1a, 1 of 22
  in Zen 5), which run to the bound.
- **Late title (case 6).** The second title event, 1.0-1.2 s after the page appeared, started
  a second hold on a page already judged excluded: 22 of 44 holds in each browser ran to the
  bound.
- **Cold.** A fresh browser's first read ends 42.8-49.1 ms after t0 in Chrome and 38.6-48.9 ms in
  Zen (3 runs each), against a warm median of 29.4 and 21.2 ms in case 2. No cold read was `None`.
- **Raw hook.** Every raw `NAMECHANGE` of the browser's process was on the test window itself:
  `idObject` 0 (`OBJID_WINDOW`, what F5 passes) or -4 (`OBJID_CLIENT`, which F5 drops); none on a
  child window and none without a window. The -4 events were absent before t0 in all runs (a
  settled window is silent). They fire in Chrome on every navigation, also when no title changes:
  case 2 at 9.0 ms (22 of 22 runs, before the pixels in all 22), case 3 at 1.8 ms (before the
  pixels in 21 of 22), case 7 at 0.9 ms, 200 ms before Chrome's title event; but in case 4 they
  precede the pixels in 6 of 22 runs only and in 4b arrive at 108.5 ms, with the address. Zen sends
  none for in-page changes (cases 3, 4, 4b); for navigations they come 13-16 ms after t0, before
  the pixels in 22 of 22 runs (case 2 too, where it has no title event in 20 of 22 runs).
  Flag for F5: its `id_object != OBJID_WINDOW` filter drops the only signal that Chrome gives
  for a same-title navigation.

## Chosen bound

T = 500 ms and N = 3 reads, "both" (the plan default of 1 s is also leak-free).

- Margin: T is 4.7 times the worst address lag behind the title event (105.8 ms, Chrome 1a) and
  5.2 times the worst behind the pixels (96.1 ms); 7.3 times Zen's worst (68.1 ms). Three reads
  take 63.8 ms in Chrome and 26.1 ms in Zen at the median (the hold of the "either" rule in
  case 7), so T binds and N matters only when reads stall.
- Pending cost in case 7, a kept page that changes its title every 2 s without navigating (the
  address never changes, so every hold runs to the bound): 500 ms per change, 25.0% of the time
  pending, in both browsers; T = 1000 ms gives 50.0%, T = 250 ms 12.5%, T = 100 ms 5.0%; ending
  at the first read (b) 1.4% in Chrome and 0.5% in Zen.
- 1 of 200 first holds (Zen 1a, any T and N) ended on an intermediate address (the new tab shows
  a different address before B's) 40 ms before the first read returning B ended; no frame leaked
  because the B frames came later and were judged by their own reads.

## Implementation rules the data forces

1. Start the hold at the F5 `title` event with `at >= event.at`; the title event preceded the
   pixels in 200 of 200 runs that had one.
2. A frame is never painted on the hold's end alone: it is judged by a read that started after
   its grab (the release read, or the first read after the grab). The two judges gave the same
   leaks here (0); the release-read judge is the one that does not depend on a stale read.
3. Do not hold a window whose last judged address is already excluded (hold runs to the bound in
   4 of 66 events in Zen cases 1a and 5, and in 44 of 88 in case 6).
4. The `None` address read does not end the hold; it counts toward N (4 `None` reads, all in
   Chrome, all one stalled read).
5. A hold that ends on any changed address may end on an intermediate one (Zen new tab, 1 of 22
   runs); rule 2 covers it.
6. A kept-to-kept title change costs the full bound whenever the address does not change (case
   7). Chrome's same-document title events arrive 200 ms late, so the hold also starts late.

## Open

Fail-closed defaults proposed; none is implemented (F10, F17).

1. **Case 4 and 4b, in-page paint before the address.** No title event, so no hold; in 4b the B
   pixels precede the address by 119.3 ms (Chrome) and 103.2 ms (Zen) and leaked in 22 of 22 runs
   in each browser; in case 4 (two animation frames) 0 leaks, the address read ended after the
   pixels by 18.3 ms (Chrome median, 6 of 22 runs with the read starting after them). Default: a
   user URL rule that has a path also holds, or excludes, every page of the same host on in-page
   changes (a path rule becomes a host rule while the window is on that host). `pushState` cannot
   change the origin, so host rules (banks, password managers) cannot be crossed in-page: only a
   rule with a path can be.
2. **Chrome cases 2 and 3, no title event.** 0 of 44 runs had a title event; the address read
   ended 6.2 ms (case 2) and 10.2 ms (case 3) after the pixels at the median, and 0 of 44
   leaked. These rest on grab-then-judge alone. Default: as for 1, and the -4 `NAMECHANGE`
   (precedes the pixels in 22 of 22 and 21 of 22 runs) starts the same hold if F5 is changed to
   report it.
3. **Chrome case 4, -4 event.** It precedes the pixels in 6 of 22 runs, so no hold start exists
   for it either; default as for 1.
4. **Same-document title delay.** Chrome's title event comes 200 ms after the change, so a
   page that retitles and leaks in one step has no hold for the first 200 ms; not measured with
   a leaking page. Default: the -4 event as above.

## Limits

- Fresh profiles, tiny local pages, one tab (two for cases 1, 5, 8), no extensions; Chrome and
  Zen only; the user's real pages and tab counts untested. 22 runs per cell: p95 is the second
  largest value.
- The address read is one UI Automation read of 8-18 ms; its sampling instant lies inside the
  read, so "address before pixels" has about that resolution ("overlap" counts reads that began
  before and ended after the pixels).
- t0 includes the server write, network and script start (about 1-3 ms); case 1b includes the
  process hand-off of `chrome.exe`.
- The monitors' refresh rate was not recorded; the animation-frame gap of cases 3 and 4 (a few
  ms) depends on it. Case 4b is the only case with a set gap.
- No page produced both a title event and an address lagging the pixels; the hold is simulated
  against the measured lags, not against such a page.
- The simulated frames are those that the 2 ms poller saw; WGC may coalesce frames. The kept
  after excluded direction is only the pending cost in case 7.
- Cases 4b and 8 ran on another monitor than the rest. A cold run is a fresh browser, three per
  browser. The first Chrome run of case 8 (long-poll pages) was discarded; the numbers are from
  the rerun.
- Edge, Brave, private windows, typing in the address bar, hung browsers, read cost in general
  and real websites were not covered.
