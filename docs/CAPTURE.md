# Capture

## Monitor frames

A frame is what one monitor showed: each kept window's real pixels at native resolution, where it
was and in its stacking order. Evidence means what the user saw, so the record must not contain
what was never captured and must not claim a window that was not visible. Windows
Graphics Capture (WGC) delivers a window's own pixels, as the compositor drew them, whatever covers
them; Akashic keeps one capture running for every visible window it has judged and kept.

A monitor's picture is composed from the latest WGC frame of each kept window, painted in
stacking order at native resolution, with the desktop and the taskbar as unlisted shell layers at
the bottom. Excluded, pending (no usable frame yet) and held windows are left blank, so the
covered parts of the windows beneath them do not show either. Minimized, cloaked (another virtual
desktop, a suspended app), alpha-0 overlays (WGC returns them as solid black) and fully covered
windows are not captured; a covered window is pending until part of it shows. A pending window is
waited for `PENDING_WAIT_S` (`capture/desk.py`) before the monitor is composed without it, unless
a capture failed on it.

What the composite does not show, accepted: the blurred backdrop behind translucent shell
surfaces, drop shadows and the cursor. While a window is dragged or resized the composite is off
for a moment. Numbers: [window composition](baselines/2026-11-15-window-composition.md).

Each monitor is its own schedule:

- A monitor is composed only when one of its windows sent a frame or the window list changed.
  It is stored only when the picture or the window list differs from the last stored one; tiny
  differences such as a blinking cursor do not count. Change is measured against what was
  actually stored: a dropped or failed write retakes the picture at the next check.
- The monitor holding the focused window is checked on focus, title and input and every few
  seconds. Input also checks the monitor under the cursor (scrolling a side window). Window
  events (show, hide, move, reorder, title) check the monitors they touch. If the window event
  hook is down, every monitor is checked and every hold restarts at once, then at most every 5 s
  (`HOOK_DOWN_RECHECK_S`); reinstall attempts wait 1 s, doubling to 60 s. Held browser windows off
  the busy monitor can stay blank meanwhile.
- Side monitors are checked at least every 30 s, because a media verdict lapses after 60 s and
  every check renews it, even when the picture is unchanged, dropped or failed. The rates are in
  `capture/screen.py` `Settings`.
- A fully black monitor (protected video, some exclusive-fullscreen games) stores no image, only
  its window list and the focused window's UIA text. A monitor showing only excluded windows
  stores nothing and is hidden.

Encoding, OCR and writes run on the `ScreenWriter` thread behind a short bounded queue, so the
capture loop never waits for them. When the queue is full the oldest side-monitor picture is
dropped and its monitor is marked dropped.

A monitor is identified by what Windows reports about the hardware and its connection
(`DisplayConfig`), not by the order it is listed in. The layout (rectangles, scale, negative
coordinates, mixed DPI) is recorded over time. At any moment a monitor is in one state: shown (at
the last check it looked like its latest stored picture; a black monitor counts as shown), hidden,
dropped or failed, or "not recording" read from the capture gaps below. States are known at each
check and assumed between them, and never say why: "hidden" does not say it was a bank.

Why per monitor, not synchronized snapshots: the desk is mostly still and each monitor changes at
its own pace; the timeline shows each monitor's latest frame at or before the chosen moment.
How evidence images are cut from frames is in [RETRIEVAL.md](RETRIEVAL.md).

A window that covers a monitor is a game when Windows reports a Direct3D exclusive-fullscreen app
(`SHQueryUserNotificationState`, read at most once per second), even with a caption or on the
built-in deny-list; otherwise when it has no caption and is not on the deny-list. Precedence: no
process or no covered monitor is not a game, then the user's `games`, then the user's `not_games`,
then D3D evidence, then the borderless rule.

Game mode composes the game's monitor at the game rate and every other monitor at the side rate,
drops input triggers, reads no UIA text and defers OCR to the worker. The verdict reads (browser
caption and address) still run, so side browsers keep publishing verdicts; the UIA timeouts
below apply to them.

Screen text should preserve useful reading order, repeated messages and bilingual content while
removing interface clutter. Neither nonempty UIA text nor a nonblack image proves completeness,
freshness or correct ownership.

The app must work across three or more monitors, including mixed DPI and negative desktop
coordinates. Games must remain responsive while conversations continue to be recorded.

## Which windows are captured

Exclusions are applied in memory before persistence, including screenshots, text, media metadata
and audio. A window with no kept verdict is never captured, so it never reaches disk, in pixels or
text. `capture/verdicts.py` decides; each visible window gets a verdict before its capture starts
and again at every check. Titles, media titles and password fields are never matched against any
list. The accepted exceptions are listed once, in [SECURITY.md](SECURITY.md).

A window is judged by, in this order:

1. **Shell copy surfaces** (Alt-Tab, Snap Assist, Task View, taskbar previews, the Snipping Tool
   overlay) are always excluded: they show copies of other windows.
2. **Process rules**: a built-in password manager or Windows sign-in surface, or an app the user
   excluded. An unknown process, or a UWP frame whose hosted app cannot be resolved, is excluded.
   A UWP window is judged by the app inside its frame.
3. **Private marker**: the browser's own private-mode mark at the end of its title or, for
   Chrome and Brave (where the title never shows it), of its caption read through UIA. An
   unreadable Chrome or Brave caption is excluded. A blank title in other browsers is excluded.
4. **Child windows**: a window containing a child window of an excluded or unknown app is
   excluded; an unreadable child list is excluded.
5. **Ghosts**: a "Not Responding" ghost takes the verdict of the hung window it stands for
   (`HungWindowFromGhostWindow`); unresolved, it is excluded.
6. **Browser main window**: a browser window whose address bar reads and is not DevTools. Its
   address is judged against the built-in finance and password-manager hosts (parsed hosts, never
   substrings), extension pages and the user's address rules. A user rule with a path covers its
   whole site, because a page can repaint before its address changes. An unreadable address is
   kept, once the window's address has been read at least once.
7. **Other browser windows** (picture-in-picture, DevTools, bar-less windows) take the strictest
   verdict of their browser's main windows; in Chrome and Brave they also need a caption that
   reads without a private marker. With no main window known they are excluded. Every browser
   popup is excluded for now (a temporary default until the popups spike).
8. **Owned windows of other apps** (menus, dropdowns, tooltips) take the strictest of their own
   verdict and their owner's. An ownerless popup is excluded when any window of its app, minimized
   or on another desktop included, is excluded.

A window first judged unknown is judged again at every check. Only what a verdict needs (process,
title or caption, address) is read, and none of it is kept or logged.

**Title-change hold.** A kept browser main window that changes title is held (pending, blank)
until its address and private state are read again, because a page excluded by its address can
paint right after its title changes. The hold starts at the title event, or for every kept main
window when events were lost (at most every 5 s while the hook stays down). It ends when a read
that started after the event returns an address different from the last judged one, or after
500 ms and 3 reads that started after the event (`HOLD_MIN_S`, `HOLD_MIN_READS`); an unreadable
address is then kept. Measurements and the open cases:
[title-change hold](baselines/2026-11-16-title-change-hold.md).

**Grab before judge.** Each check grabs the newest frame of every started window before the
verdicts run, so a frame painted under a verdict was grabbed before the read that verdict rests
on. This also protects in-page repaints that no title event announces: a host rule cannot be
crossed in-page, and a user path rule already covers its whole site.

**Verdict changes** take effect before the next pixel. When a kept window turns excluded its
capture stops and its copy is dropped in the same check. When it turns kept again nothing older
than the change is painted; it is pending until a new frame arrives. Excluded and held windows
are content-free rectangles: no title, address, text or pixels, no app row in the window list.
The excluded area is a hidden row with its rectangle only.

**Media.** Media sessions follow the browser rule: every judged browser window feeds
`capture/browser_state.py`, and a browser's media is rejected for 60 s after an excluding or
unknown verdict, or when its verdict is missing or stale. So a private or bank window on a side
monitor also cuts that browser's media. A media title is not a window title. Nothing about an
excluded window is logged.

Pause stops all new capture. The pause is kept across Lock, Quit and restart until the user
resumes. Pause drops the utterance being recorded. Stop, a device change and a device failure
keep it; an unexpected fault drops it. A locked Windows screen and an Akashic pause are
different states: headset conversations can continue while Windows is locked.

Audible processes are read per playback endpoint. Exclusions check every endpoint: an excluded
app audible anywhere drops the segment. A system segment stores the processes audible on the
endpoint it recorded. When that endpoint carries a virtual mixer (`MIXER_PROCESSES`, Elgato Wave
Link), it stores what is audible on every endpoint instead, without the mixer. A poll that lacks
the recorded endpoint makes the sources unknown (`unknown_sources`), and the endpoints are
re-read. Voice chat (`VOICE_APPS`) audible only on another endpoint, and not of an excluded app,
is not recorded. It is written as a `voice_elsewhere` gap. These lists are correlations, not
proof of who produced a word. Preserve that uncertainty in episode associations and answers.

Music-app audio: drop songs, but keep segments with evidence of real speech, such as a podcast
([DECISIONS.md](DECISIONS.md), D7). Today the code still drops all system sound whose only
sources are music apps.

Mechanics live with the code (`capture/verdicts.py`, `capture/exclusions.py`,
`capture/browser_state.py`, `platform/win/uia.py`) and their tests.

## While Akashic is open

The user's Pause, the UI-open halt and the own-address failsafe all set capture's halt: screen,
audio and media stop, the utterance in progress is kept and the worker continues. The UI-open
halt is recorded as a gap with the reason `ui_open` and is never persisted or shown as the
user's Pause.

A UI page reports itself to the local server while it is visible (in any browser, whatever its
address shows) and shows no recorded content until the server confirms the halt. As a failsafe, a
visible main browser window whose readable address is Akashic's loopback port halts capture, and
while that holds only the addresses of visible browser windows are re-read, about once a second,
until none shows it. Akashic's own pages therefore never show recorded content while recording
runs, and no copy of a transcript, excerpt or answer reaches the screen text. The message path is
in [ARCHITECTURE.md](ARCHITECTURE.md).

## Screen text

- The focused window is read through UIA once per tick, with the per-app budget
  (`capture/uia_budget.py`). The text is dropped when the window, its title or its address changed
  during the read.
- Every other kept app window is read with OCR on its whole capture, covered parts included
  ([DECISIONS.md](DECISIONS.md)): reading window by window keeps each window's lines in order,
  attributes every line to its window, and lets an unchanged window reuse its previous text
  (`capture/ocr_gate.py`). A search hit can therefore quote text the stored picture does not show.
- For the focused window the longer of the UIA text and the OCR text is kept.
- Shell, hidden and pending rows are never read: not the desktop, the taskbar, excluded or pending
  windows.
- In game mode the whole capture of each kept window is stored, encrypted, on its window row until
  the worker's `ocr_window` job reads it. Deferred OCR reads a lossy stored image, so its text can
  be slightly worse than a live read.

Why not UIA for every window: it is slow on large pages, it adds work in the apps being read (the
reason UIA reads are rationed per app) and it returns text hidden behind other windows.

## Media and game sessions

A media row opens when a session first plays, never while Windows is locked. It stays open across
pauses, lock and UAC prompts. While paused, its end stays at the pause point. It closes on a title
change, the source going away, an excluding verdict, a pause over 10 min (at the pause point), a
poll gap over 10 s such as sleep (at the last poll; a session still playing gets a new row at the
first unlocked poll), and Akashic's halt or stop. Locking clears the browser verdicts, so a row
whose verdict becomes `url_unknown` is kept while locked and for one poll after. Any excluding
verdict still closes it at once. A game row ends at the last screen tick that saw the game in
front.

## Writes

A failed frame write marks its monitor failed and the next check stores the picture again.
Audio segment writes and media-session closes retry when the database is busy; game rows do not.
A full disk fails writes and is reported as a problem; nothing is deleted to make room.

Stop has a deadline (`STOP_BUDGET_S`, 8 s after the screen loop returns; the supervisor kills at
10 s). Once stop is set every capture connection waits at most 250 ms on a busy database
(`capture/gaps.py`). The screen and audio writers drop what is still queued at their first busy
write after stop (`stop_dropped`; audio drops are `write_failed` gaps). The gap thread retries the
stop record until the deadline. Every capture thread is a daemon and every join uses the
deadline (`StopDeadline`); a thread still running at the deadline is logged by name and
abandoned, and the process exits.

## Health

Capture reports `degraded` (instead of `ready` or `gaming`) while it runs unpaused and a problem
is active. The codes are fixed strings, also listed under `problems` in the `capture`
process_info row and returned as `capture_problems` by `GET /api/status`.

- `audio_unavailable`: audio is enabled but could not start (for example the VAD model is
  missing). Sticky: a pause does not clear it. `[audio] enabled = false` is never a problem.
- `mic_unavailable`, `system_audio_unavailable`: that recorder had no successful device read for
  60 s, counted from the later of its last read and the last resume. A read that times out with
  no data counts as a success.
- `screen_images_failing`: one failed store on a monitor, or a run of 3 checks with failed window
  captures; it lasts until that monitor's next stored picture.
- `screen_disk_full`: one failed store on a full disk.
- `screen_frames_dropped`: a run of 3 drops on one monitor.
- `mic_segments_dropped`, `system_segments_dropped`: a segment of that stream was lost to a full
  writer queue or a failed write in the last 60 s (`DROP_PROBLEM_S`) and after the last resume;
  excluded, music and unknown-source drops are intended and never problems.
- `window_events_down`: the window event hook has been down for 30 s (`HOOK_DOWN_PROBLEM_S`).

Screen problems are reported per monitor as `<code>@Monitor N`, numbered left to right, the same
numbering the timeline shows. Runs count only events after the last resume, and a success or a
pause clears them. Priority is `paused` > `degraded` > `gaming` > `ready`. A pause clears
every non-sticky problem; a resume reports `degraded` only for `audio_unavailable`, and the next
publish catches up with the real state. Problem changes are logged once, codes only: warning level
when problems are active, info level when the set becomes empty.
Down codes come before dropped codes. The thresholds (`GRAB_FAILURE_RUN`, `DEVICE_DOWN_S`,
`DROP_PROBLEM_S`, `HOOK_DOWN_PROBLEM_S` in `capture/health.py`) are not measured.

## Capture gaps

The `capture_gaps` table says when something was not being recorded and why. Rows are
content-free: a closed vocabulary (CHECK constraints) and two integer times. `end_ms IS NULL`
means the gap is still going on. A gap thread of capture writes them on its own connection, so a
busy database never delays capture; events keep their original times and are retried. Nothing
is written before the previous run has been settled. Screen drops, failures and hidden monitors
are monitor states (`storage/monitors.py`), not gaps; "not recording" reads the gaps.

| kind | reason | opens | closes |
| --- | --- | --- | --- |
| `all` | `paused` | pause (also when capture starts paused) | resume |
| `all` | `ui_open` | a UI page visible, or the own-address failsafe | page gone or address gone |
| `screen` | `locked` | `is_locked()` is true | unlock |
| `mic`, `system` | `device_missing` | no device found | first read that returns, or a pause |
| `mic`, `system` | `device_failed` | open, read or loop fault fails | first read that returns, or a pause |
| `mic`, `system` | `audio_unavailable`, `disabled` | start, when audio could not be built or is off | stop |
| `screen_text` | `ocr_unavailable` | start, when no recognizer is installed | stop |
| `all` | `stopped` | start of a clean stop | next start |
| `all` | `crashed` | next start after a kill: last heartbeat to this start | (written closed) |
| `all` | `stalled` | a gap-thread tick more than 30 s after the previous one | (written closed) |
| `mic`, `system` | `excluded`, `unknown_sources`, `music`, `write_failed` | a dropped segment | (written closed) |
| `mic`, `system` | `backlog` | a segment dropped because the writer queue was full | (written closed) |
| `system` | `voice_elsewhere` | voice chat audible on a playback device that is not recorded | (written closed; consecutive polls extend one row) |

Device rows start at the first failure and keep one row while the reason stays the same; a
changed reason closes the old row and opens a new one. A successful open alone does not close
them, a read does. A default-device switch that reopens fine records nothing.

Dropped segments are closed rows. A drop extends the previous row of the same stream and reason
when it starts within 30 s of its end, the writer's last decision on that stream was a drop with
that reason and no segment was stored in between. `backlog` never merges. The segmenter's `short`
and `quiet` drops write nothing.

Capture writes `capture_alive_ms` to `state` every 5 s. On the next start without a `stopped`
row, open rows are closed at the later of the start and that heartbeat, and the time from the
heartbeat to the start is recorded as `crashed`. The stop is written when it begins, before
the audio drain: every open row is closed and `stopped` opened; drops the audio writer reports
while draining are still written afterwards as closed rows. If the database stays busy, the stop
is retried until the stop deadline. Then the pending gap events are dropped (`stop_dropped`) and
no `stopped` is written, so the next start records `crashed` from the last heartbeat. Gaps queue
at most 10,000 pending drops; transitions are always queued. Counters are under `gaps` in the
capture Debug data, and `stop_dropped` under `writer` (screen), `audio.stats` (audio) and `gaps`.

## UI Automation timeouts

`WindowsUia` sets `ConnectionTimeout` to 1000 ms and `TransactionTimeout` to 2000 ms
(`platform/win/uia.py`), so a hung app fails a call instead of blocking the screen loop.
Spike 2026-11-13, Edge and Chrome, a local page of about 30k elements, default UIA timeouts, 5
runs each. Worst times of the readers that run without a budget: `browser_caption` 20 ms,
`read_url` 626 ms (Chrome; Edge 554 ms). Zen (Gecko, throwaway profile, same page, about 20k UIA
nodes, 5 runs): worst `browser_caption` 0 ms (returns at once for Firefox and Zen), `read_url`
14 ms (`_firefox_bar` finds the address bar early). `read_text` took up to 4.5 s on the Chromium
page and 3.1 s in Zen, because it fetched
every node. It now applies the text-type, visible and non-password condition inside the
`FindAll`, so `max_nodes` cuts the matching nodes (Chromium marks scrolled-away nodes
`IsOffscreen`), and visible text far down a long page is kept. Re-measured 2026-11-13 on a page
rebuilt to the description above (30k elements, autofocused password field, scrolled to the
end, throwaway profiles), 5 runs of the full path (filtered `FindAll` plus the loop with
`max_nodes` 3000): Edge worst 1912 ms (median 1864), Chrome worst 1770 ms (median 1740). The
old unfiltered path took 6029 ms (Edge) and 5910 ms (Chrome) and kept only 16 of 59 (Edge)
and 12 of 54 (Chrome) visible text nodes. The call can still approach the 2 s transaction
timeout on pages that large, and then the frame falls back to OCR; its budget only records
time after the call. Zen was not re-measured: the throwaway page exposed no web content in
that run.
The transaction timeout is the next 500 ms step above 3x the worst legitimate call
(1878 ms), with a 1000 ms floor.

The two readers without a budget (`browser_caption`, `read_url`) are also time-boxed per window,
unlike `read_text`, which has the per-app budget. A call that took 900 ms or more
(`SLOW_CALL_MS`, wall time) and returned None backs that window's HWND off for 30 s
(`HWND_BACKOFF_S`), stamped on the capture tick's clock. While it is backed off no UIA call is
made for it and each reader returns its failure value: `browser_caption` None (a Chrome or Brave
window ends `caption_unknown`), `read_url` None (the unreadable-address path: a main window with
no address is kept), `read_text` skipped (OCR gate or empty text). Neither `read_text` nor a slow
call that returned a value starts a backoff. Remaining limit: a legitimate read that takes 900 ms
or more and still returns None (for example a huge page with no omnibox found) backs the window
off for 30 s.

Verdict reads and the focused window's text read run on the one capture thread. With the per-HWND
backoff a hung window costs at most one timed-out UIA call per 30 s, and that call delays every
monitor's checks.

Spike 2026-11-13, Chrome 157 and Edge 157, throwaway profiles, PC locked: a flagless window
returned only the browser's own interface text through UIA, never page text, even after 10 s.
With Chromium's occlusion tracking turned off, the first read enabled web accessibility and the
page was readable from the next read (0.5 s); with `--force-renderer-accessibility` at once.
So a blank page tree means Chromium considers the window occluded, not that UIA is enabled
lazily. Address bar and caption reads worked in every mode. Not yet run unlocked with the window
in front.

## Validation boundaries

WGC captures each window by itself; a failed capture leaves its window pending and is never
replaced by a desktop grab. Controlled Zen checks established window rendering, updates, a
mixed-DPI move and occlusion within that fixture. Those checks do not validate everyday browsers,
Discord, games or protected surfaces. Firefox and Brave (title markers, address bar, caption) are
unverified on this PC; Zen, Chrome and Edge were measured in the
[browser reads](baselines/2026-11-16-browser-reads.md) baseline. French OCR availability and real
bilingual accuracy must be checked on the target machine.

Validate real app layouts, startup accessibility, focus/exclusion transitions, brief text on
secondary monitors and concurrent media ownership. Use the
[manual checklist](../tests/manual/CHECKLIST.md) and
[audit findings](audits/2026-11-10-2.md) to choose checks relevant to a change.
