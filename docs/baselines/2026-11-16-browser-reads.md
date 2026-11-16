# Browser reads spike 2026-11-16

Measurements behind TODO A9 (F8, the hung side browser, and the F25 prerequisite) of the
monitor-capture rules ([CAPTURE.md](../CAPTURE.md)): how long the production
readers `WindowsUia.browser_caption` and `WindowsUia.read_url` take on every visible browser,
whether they recognise the address in each window kind, whether they serialize across threads,
and what a hung side browser does. Test windows were disposable browser processes started by
the spike with throwaway profiles and a local page server on 127.0.0.1; the user's browsers,
windows and desktops were not touched. Reads were compared in memory with the expected test
address and only the result was kept; this report and the raw result files hold numbers, counts,
booleans and window-kind labels only (window class names, address schemes from a fixed list).
The scripts were throwaway and none is kept; no source file under `src/` changed.

## Machine

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01), Windows 11 build 26200, Python
3.14.10. Monitors, left to right: 2560x1440 at 100%, 3840x2160 at 125% (primary), 2560x1440 at
100%; every test window sat on the left 1440p monitor.

| Browser | Version |
| --- | --- |
| Chrome | 157.0.8103.51 |
| Edge | 157.0.4341.60 |
| Zen | 1.24.1b (`application.ini` `Version`; Gecko 159.0.1, build 20261106124806) |

Brave and Firefox are not installed and were not measured. The windows' owner process was the
browser process in each (no `--type=` or `-contentproc` on its command line); for Zen that is
the parent process.

## Method

- **Readers.** Every read went through the unchanged `WindowsUia.browser_caption` and
  `WindowsUia.read_url`, with their 1000 ms connection and 2000 ms transaction timeouts.
  Chrome and Edge use the `chromium` family, Zen the `firefox` family (so its caption reader
  returns `None` at once by design, 0.0 ms measured). A cell counts a read as "address
  returned" when `read_url` gave a value, "match" when that value after `normalize_url`
  equals the expected address (scheme dropped, percent-decoded, trailing slash dropped), and
  "caption read" when the caption was non-empty. Statistics are n, median, p95 (nearest rank)
  and max in ms.
- **Instances.** Four browser processes: Chrome profile A, Chrome profile B (two
  `--user-data-dir` profiles), Edge and Zen (`-no-remote -profile`). Chromium flags:
  `--no-first-run --no-default-browser-check --disable-search-engine-choice-screen
  --disable-sync`. The page is a plain local page; the kind tests use a page with a playing
  video. Windows were laid out in a 2x2 grid of 1260x700 so none covered another.
- **States.** Visible: normal window on the side monitor, not focused. Covered: an opaque
  black window of the spike fully over the test window (checked with `WindowFromPoint` at
  three points). Minimized: `ShowWindow(SW_MINIMIZE)`. Other desktop: the whole virtual
  desktop switched by script (SendInput Win+Ctrl+D), the test window checked to report
  `DWMWA_CLOAKED` (value 2 in all cases), and the temporary desktop closed with Win+Ctrl+F4
  only while the windows were cloaked and the desktop count was one higher (the PC had one
  desktop before and after; the closing sent no key when the check failed). Each state:
  2 s settle, one first read (caption then address), then 20 warm reads of each reader.
- **Cold reads.** Three launches per browser; first reader call 2 s after the window
  appeared, caption first (launches 1, 3) or address first (launch 2).
- **Window kinds.** Per browser, five reads of each reader per cell. F11 by SendInput. Video
  fullscreen, PiP and the popup by a key press on the page after a click in it (a trusted user
  activation): the page opens `requestFullscreen`, `requestPictureInPicture` or `window.open`
  with `popup` on the keys F, P and O. Zen PiP by Ctrl+Shift+]. DevTools by Ctrl+Shift+I in
  Chrome and Edge (Edge asks a question on F12) and F12 in Zen. Dock state is preset in the
  profile (`devtools.preferences.currentDockState`, Zen `devtools.toolbox.host`). `--app=`
  for Chrome and Edge. Where a kind opens another top-level window, both that window and its
  parent were read; each case except `--app` was checked once by a crop of the test window during the run (the
crops were deleted).
- **Keystroke guard.** Before any keystroke or click the spike checks that the foreground
  window is the test window and skips the case otherwise ("not run"); no case was skipped.
- **Parallel reads.** One persistent thread per window, each with its own `WindowsUia`
  created after `CoInitializeEx(COINIT_MULTITHREADED)`. A round is one caption read and one
  address read per window. Sequential and parallel rounds alternate over the same windows
  (20 measured rounds after 2 warm-up rounds); a parallel round releases all threads on a
  barrier and ends when the last finishes.
- **Hung browser.** The browser process (Zen: its parent) is suspended with
  `NtSuspendProcess`; the window is on the side monitor and not focused (a healthy
  browser's window is in front). A second thread, with its own `WindowsUia`, reads a healthy
  browser every 50 ms. On the main thread both readers are timed against the hung window,
  then both against the healthy window; the process is resumed in a `finally`; reads of the
  hung window repeat until one returns an address. Healthy browser: Edge for hung Chrome,
  Chrome profile B for hung Edge and Zen. Three runs per browser.

## Read times by state

Cells give n / median / p95 / max in ms. "First read" is the first read after the 2 s settle
that follows the state change. Zen's caption reader returns at once by design (0.0 ms is
under 0.01 ms). Every address read in this table returned a value matching the expected
address (20 of 20 in every cell).

| Process | State | Caption: n / median / p95 / max | Caption first read | Address: n / median / p95 / max | Address first read |
| --- | --- | --- | --- | --- | --- |
| Chrome profile A | visible | 20 / 5.3 / 6.0 / 6.3 | 15.6 | 20 / 12.5 / 13.8 / 14.5 | 12.6 |
| Chrome profile A | covered | 20 / 5.3 / 7.0 / 7.3 | 5.9 | 20 / 12.6 / 15.7 / 17.8 | 12.4 |
| Chrome profile A | minimized | 20 / 5.4 / 7.1 / 11.7 | 5.9 | 20 / 13.6 / 16.1 / 25.7 | 17.3 |
| Chrome profile A | other desktop | 20 / 1.8 / 2.0 / 2.0 | 2.5 | 20 / 8.8 / 9.7 / 10.0 | 9.7 |
| Chrome profile B | visible | 20 / 5.3 / 6.1 / 6.7 | 15.5 | 20 / 12.4 / 15.1 / 15.6 | 13.6 |
| Chrome profile B | covered | 20 / 6.3 / 8.3 / 10.7 | 9.9 | 20 / 15.9 / 21.0 / 24.0 | 18.4 |
| Chrome profile B | minimized | 20 / 5.0 / 5.7 / 5.8 | 6.1 | 20 / 12.4 / 14.4 / 14.6 | 12.5 |
| Chrome profile B | other desktop | 20 / 1.8 / 2.0 / 2.2 | 1.8 | 20 / 8.8 / 10.0 / 10.5 | 9.3 |
| Edge | visible | 20 / 5.8 / 6.8 / 6.9 | 16.1 | 20 / 15.2 / 18.5 / 18.9 | 16.8 |
| Edge | covered | 20 / 5.2 / 5.5 / 5.6 | 5.8 | 20 / 14.0 / 16.0 / 16.7 | 13.1 |
| Edge | minimized | 20 / 5.0 / 5.4 / 5.4 | 5.6 | 20 / 13.1 / 15.2 / 16.8 | 14.9 |
| Edge | other desktop | 20 / 1.9 / 2.4 / 2.6 | 2.0 | 20 / 10.6 / 13.4 / 14.6 | 10.5 |
| Zen | visible | 20 / 0.0 / 0.0 / 0.0 (returns at once by design) | 0.0 | 20 / 8.2 / 9.5 / 9.9 | 45.1 |
| Zen | covered | 20 / 0.0 / 0.0 / 0.0 (returns at once by design) | 0.0 | 20 / 8.0 / 9.0 / 10.4 | 8.3 |
| Zen | minimized | 20 / 0.0 / 0.0 / 0.0 (returns at once by design) | 0.0 | 20 / 8.1 / 8.8 / 9.0 | 9.4 |
| Zen | other desktop | 20 / 0.0 / 0.0 / 0.0 (returns at once by design) | 0.0 | 20 / 3.6 / 4.0 / 4.1 | 4.1 |

Cold first read after launch (ms), three launches per browser. Launches 1 and 3 read the
caption first, launch 2 the address first; the reader called second ran on a browser the first
call had already attached to.

| Process | Launch 1: caption, then address | Launch 2: address, then caption | Launch 3: caption, then address |
| --- | --- | --- | --- |
| Chrome profile A | 24.2 -> 15.9 | 23.1 -> 4.9 | 13.9 -> 12.6 |
| Chrome profile B | 15.3 -> 12.6 | 22.8 -> 5.8 | 14.8 -> 12.7 |
| Edge | 14.6 -> 14.9 | 25.1 -> 5.2 | 14.9 -> 13.4 |
| Zen | 0.0 -> 51.3 | 48.0 -> 0.0 | 0.0 -> 37.2 |

- No state slowed a read: covered, minimized and other-desktop windows read as fast as
  visible ones (an other-desktop read was the fastest). No read returned `None` or raised.
- The slowest read in the table is 25.7 ms, against the 900 ms `SLOW_CALL_MS`.
- Zen's first address read after launch or after the first visible read is the slowest of
  its reads (37-51 ms).

## Address recognition by window kind

Five reads per cell: address returned / matches the expected address after `normalize_url`
/ caption read, and the median address read time. For a kind that opens another top-level
window, the parent window and the new window are both listed. Zen's caption count is 0/5 in
every cell because its caption reader returns `None` by design.

| Kind | Chrome | Edge | Zen |
| --- | --- | --- | --- |
| normal | address 5/5, match 5/5, caption 5/5, 12.0 ms | address 5/5, match 5/5, caption 5/5, 13.9 ms | address 5/5, match 5/5, caption 0/5, 8.4 ms |
| F11 fullscreen | address 5/5, match 5/5, caption 5/5, 15.4 ms | address 5/5, match 5/5, caption 5/5, 16.9 ms | address 5/5, match 5/5, caption 0/5, 4.8 ms |
| video fullscreen | address 5/5, match 5/5, caption 5/5, 16.3 ms | address 5/5, match 5/5, caption 5/5, 18.5 ms | address 5/5, match 5/5, caption 0/5, 4.8 ms |
| PiP | parent window: address 5/5, match 5/5, caption 5/5, 11.6 ms<br>PiP window (Chrome_WidgetWin_1): address 0/5, match 0/5, caption 0/5, 4.9 ms | parent window: address 5/5, match 5/5, caption 5/5, 13.9 ms<br>PiP window (Chrome_WidgetWin_1): address 0/5, match 0/5, caption 0/5, 4.9 ms | parent window: address 5/5, match 5/5, caption 0/5, 8.2 ms<br>PiP window (MozillaDialogClass): address 0/5, match 0/5, caption 0/5, 4.4 ms |
| docked DevTools | parent window: address 5/5, match 5/5, caption 5/5, 18.3 ms<br>no new top-level window | parent window: address 5/5, match 5/5, caption 5/5, 15.9 ms<br>no new top-level window | parent window: address 5/5, match 5/5, caption 0/5, 8.2 ms<br>no new top-level window |
| undocked DevTools | parent window: address 5/5, match 5/5, caption 5/5, 14.6 ms<br>DevTools window (Chrome_WidgetWin_1): address 5/5, match 0/5, caption 5/5, 15.5 ms, scheme devtools | parent window: address 5/5, match 5/5, caption 5/5, 15.7 ms<br>DevTools window (Chrome_WidgetWin_1): address 5/5, match 0/5, caption 5/5, 13.7 ms, scheme devtools | parent window: address 5/5, match 5/5, caption 0/5, 7.4 ms<br>DevTools window (MozillaWindowClass): address 0/5, match 0/5, caption 0/5, 9.1 ms |
| `--app` | address 5/5, match 5/5, caption 5/5, 10.7 ms | address 5/5, match 5/5, caption 5/5, 12.0 ms | n/a: Zen has no app-window mode |
| `window.open` popup | parent window: address 5/5, match 5/5, caption 5/5, 11.7 ms<br>popup (Chrome_WidgetWin_1): address 5/5, match 5/5, caption 5/5, 11.0 ms, no scheme in the value | parent window: address 5/5, match 5/5, caption 5/5, 13.6 ms<br>popup (Chrome_WidgetWin_1): address 5/5, match 5/5, caption 5/5, 12.7 ms, no scheme in the value | parent window: address 5/5, match 5/5, caption 0/5, 7.9 ms<br>popup (MozillaWindowClass): address 5/5, match 5/5, caption 0/5, 5.9 ms, scheme http |

- Caption read time stayed 4.7-6.3 ms (median) in every Chrome and Edge cell.
- The Chrome and Edge address bar stayed in the accessibility tree in F11 and video
  fullscreen (the value still matched). Zen's did too.
- The PiP window never gives an address (`None` in 5 of 5, Chrome, Edge, Zen). Its caption is
  also empty.
- A Chrome or Edge **undocked DevTools window returns a value** that is not the page address:
  it carries the `devtools` scheme. Zen's toolbox window returns `None`.
- A `window.open` popup gives its own address in all three. Docked DevTools is part of the
  parent window and opened no new top-level window.

## Private-window captions

Chrome Incognito and Edge InPrivate windows (a second window of the profile started with
`--incognito` / `--inprivate`), 20 reads per cell. The caption is the accessible name of
`BrowserRootView`; the marker is `Exclusions.is_private_title` on it.

| Window | State | Caption read | Private marker by `is_private_title` | Caption median ms |
| --- | --- | --- | --- | --- |
| Chrome Incognito | visible | 20/20 | 20/20 | 5.7 |
| Chrome Incognito | covered | 20/20 | 20/20 | 5.7 |
| Chrome Incognito | minimized | 20/20 | 20/20 | 4.5 |
| Chrome Incognito | other desktop | 20/20 | 20/20 | 1.9 |
| Edge InPrivate | visible | 20/20 | 20/20 | 5.8 |
| Edge InPrivate | covered | 20/20 | 20/20 | 5.8 |
| Edge InPrivate | minimized | 20/20 | 20/20 | 5.3 |
| Edge InPrivate | other desktop | 20/20 | 20/20 | 1.8 |

The normal Chrome and Edge windows of the read-time table had caption read 20/20 and the
private marker 0/20 in every state.

## Zen decoded addresses

Five reads per cell on five address shapes. The page behind each is a local file; the table
holds counts only. "Matches" is the same test as above.

| Address shape | Browser | Bar has a literal space | `normalize_url` returns None | Bar matches the expected address | Bar shows `%` | Bar shows non-ASCII | Bar shows `+` | Read ms (median) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `%20` in the path | Zen | 5/5 | 0/5 | 5/5 | 0/5 | 0/5 | 0/5 | 7.9 |
| `%20` in the path | Chrome | 0/5 | 0/5 | 5/5 | 5/5 | 0/5 | 0/5 | 11.6 |
| `%20` in the path | Edge | 0/5 | 0/5 | 5/5 | 5/5 | 0/5 | 0/5 | 13.1 |
| `%20` in the query | Zen | 5/5 | 0/5 | 5/5 | 0/5 | 0/5 | 0/5 | 8.6 |
| `%20` in the query | Chrome | 0/5 | 0/5 | 5/5 | 5/5 | 0/5 | 0/5 | 11.8 |
| `%20` in the query | Edge | 0/5 | 0/5 | 5/5 | 5/5 | 0/5 | 0/5 | 14.1 |
| `+` in the query | Zen | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 | 5/5 | 7.9 |
| `+` in the query | Chrome | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 | 5/5 | 13.5 |
| `+` in the query | Edge | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 | 5/5 | 13.4 |
| non-ASCII path | Zen | 0/5 | 0/5 | 5/5 | 0/5 | 5/5 | 0/5 | 7.2 |
| non-ASCII path | Chrome | 0/5 | 0/5 | 5/5 | 0/5 | 5/5 | 0/5 | 12.1 |
| non-ASCII path | Edge | 0/5 | 0/5 | 5/5 | 0/5 | 5/5 | 0/5 | 14.7 |
| plain | Zen | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 | 0/5 | 8.0 |
| plain | Chrome | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 | 0/5 | 12.8 |
| plain | Edge | 0/5 | 0/5 | 5/5 | 0/5 | 0/5 | 0/5 | 13.5 |

- Zen shows `%20` decoded to a literal space in the path and in the query; Chrome and Edge keep
  `%20`. A non-ASCII path is shown decoded in all three. `+` is left as it is in all three.
- Today's `normalize_url` returned a value for every Zen read, including the two with a
  literal space (0 of 5 `None`).

## Parallel reads

Per-thread `WindowsUia` with COM initialised as MTA. A round is one caption read and one
address read per window; each thread reads one window. 20 measured rounds per row. "Parallel
wall / sequential wall" is the median wall time of a parallel round over the median wall time
of the sequential rounds interleaved with it; four equal calls on four threads would give 0.25
(two threads: 0.5).

**Windows of one browser process (four windows of Chrome profile A)**

| Threads | Call ms sequential (median / p95) | Call ms in parallel (median / p95) | Wall per round, sequential (median ms) | Wall per round, parallel (median ms) | Sum of the parallel calls per round (median ms) | Parallel wall / sequential wall |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 9.2 / 13.5 | 9.2 / 13.5 | 18.2 | 18.3 | 18.3 | 1.01 |
| 2 | 9.1 / 13.3 | 13.7 / 21.2 | 36.3 | 28.5 | 56.6 | 0.79 |
| 4 | 9.2 / 13.5 | 28.3 / 41.4 | 72.6 | 55.2 | 218.9 | 0.76 |

**Windows of different processes (Chrome A, Chrome B, Edge, Zen; one window each, added in that order)**

| Threads | Call ms sequential (median / p95) | Call ms in parallel (median / p95) | Wall per round, sequential (median ms) | Wall per round, parallel (median ms) | Sum of the parallel calls per round (median ms) | Parallel wall / sequential wall |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 8.6 / 12.4 | 8.5 / 12.7 | 17.2 | 17.3 | 17.3 | 1.0 |
| 2 | 8.7 / 13.1 | 13.3 / 18.6 | 35.3 | 27.3 | 53.9 | 0.77 |
| 4 | 8.7 / 16.7 | 27.6 / 41.9 | 73.9 | 60.2 | 209.7 | 0.81 |

Verdict: **serializes: yes, in both set-ups**, mostly. Threads run without error (no
exception, every call returned), but four threads finish a round in 0.76-0.81 of the
sequential time instead of about 0.25, and each call takes about three times as long in
parallel (9 to 28 ms median). One process or four makes no difference. The measurement cannot
tell which layer queues the calls (Python's interpreter lock around the comtypes work, the
UI Automation client, or the browsers' own UI threads).

## Hung side browser

The process is suspended with `NtSuspendProcess`; the window is on a side monitor and not
focused (the foreground window was never the target). Result kind is what the reader gave:
`None` or the type of an uncaught exception. No exception escaped in any run.

| Browser | Run | Caption: ms, result, reaches 900 ms | Address: ms, result, reaches 900 ms | Healthy browser, same thread right after (caption / address ms) | Healthy browser, other thread during (n calls, median / max ms) | First successful read after resume (ms) |
| --- | --- | --- | --- | --- | --- | --- |
| Chrome | 1 | 1005.1, none, yes | 1013.5, none, yes | 5.6 / 13.5 | 31, 14.5 / 15.7 | 14.4 |
| Chrome | 2 | 1001.7, none, yes | 1011.7, none, yes | 6.5 / 13.5 | 31, 14.4 / 21.0 | 19.9 |
| Chrome | 3 | 1009.3, none, yes | 1013.7, none, yes | 6.9 / 22.2 | 31, 13.8 / 17.7 | 15.7 |
| Edge | 1 | 1014.4, none, yes | 1014.6, none, yes | 5.4 / 11.8 | 32, 12.8 / 15.3 | 15.6 |
| Edge | 2 | 1004.0, none, yes | 1010.5, none, yes | 5.5 / 13.3 | 31, 14.3 / 23.4 | 15.7 |
| Edge | 3 | 1000.6, none, yes | 1011.7, none, yes | 6.4 / 12.5 | 32, 12.5 / 20.2 | 16.6 |
| Zen (parent process) | 1 | 0.0, none, no (returns at once by design) | 1008.1, none, yes | 5.8 / 11.6 | 16, 12.5 / 13.4 | 8.8 |
| Zen (parent process) | 2 | 0.0, none, no (returns at once by design) | 1005.8, none, yes | 6.4 / 12.5 | 16, 12.6 / 13.1 | 9.1 |
| Zen (parent process) | 3 | 0.0, none, no (returns at once by design) | 1007.0, none, yes | 5.7 / 11.3 | 16, 12.4 / 13.6 | 8.4 |

- Each Chromium reader on a hung window took 1000.6-1014.6 ms, the 1000 ms connection
  timeout and not the 2000 ms transaction timeout, and returned `None`. All of them reach
  `SLOW_CALL_MS` (900 ms), so `UiaBudget.record_failure` would back the window off for `HWND_BACKOFF_S`
  (30 s) after the first read (it is not run here). Without backoff, a capture pass that reads both readers of a hung
  Chromium window spends about 2.0 s; a hung Zen window about 1.0 s.
- A healthy browser read right after the timeout on the same thread took the normal 5-7 ms
  and 11-22 ms. A healthy browser read on another thread during the hang took a median 12.4-14.5
  ms and at most 23.4 ms, 0 of 236 calls over 100 ms.
- The first read after resume succeeded on the first attempt, 8.4-19.9 ms.

## Limits

- Fresh disposable profiles, one tab, no extensions, on tiny local pages: not the user's
  real profiles, tab counts or pages. A large real page may read slower; this was not
  measured. `read_text` and page-text timing were not covered.
- Five reads per kind cell, 20 per state cell: with n = 20, p95 is the second-largest read.
  Three runs per hung case and three cold launches per browser.
- The hang is a suspended process, which freezes every thread of it; a browser busy in a
  long task or deadlocked in one thread was not tested.
- A Chromium browser keeps its address bar in the accessibility tree in fullscreen and
  F11, but that is what these versions did, not a promise.
- PiP and the popup depend on a key press on the page; Zen's PiP key worked with the US key
  code on this PC's keyboard layout.
- The parallel test shows serialization, not its cause; a run with one process per
  reader would separate the Python lock from the UI Automation layers.
- The title-change hold (F9), ghost-window mapping, extension, autofill, save-password and
  credential popups were not covered.
- Edge launched in the first runs of this spike (before `--disable-sync` was added) signed
  its throwaway profile in with the PC's Microsoft account and showed a sync notice, so test
  history of the local pages may have been uploaded to that account. The runs reported here
  used `--disable-sync` (Chrome too); every table comes from runs that had it.
- Test pages, the popup and the video were served by `python -m http.server` on 127.0.0.1.
