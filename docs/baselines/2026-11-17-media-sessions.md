# Media sessions spike 2026-11-17

Measurements behind TODO F18 (one open media row per playing session, keyed by
`(app_id, title, artist)`): what the Windows media-session list
(GlobalSystemMediaTransportControls, "GSMTC", read the way `WindowsMedia._read` in
`src/akashic/platform/win/media.py` reads it) holds when one browser plays media in several tabs
and windows, and whether `MediaTracker.poll` ("the first (current) wins") loses a player. Test
browsers were disposable processes started by the spike with throwaway profiles and a page
server on 127.0.0.1; the user's browsers and profiles were not touched. A session counts as the
spike's when its title carries the run's random nonce; sessions that are not the spike's were
only counted, and compared in memory with the current session, per poll; never stored or
controlled. This report and the raw result file hold
numbers, counts, booleans and synthetic labels only (tab A1, tab A2, window B tab B1). The script
was throwaway and none is kept; no source file under `src/` changed.

## Machine

Windows 11 build 26200, Python 3.14.10. Monitors, left to right: 2560x1440 at 100% (x -2560),
3840x2160 at 125% (primary, x 0), 2560x1440 at 100% (x 3840). Every spike window sat on the
right monitor, whose rect as returned by `right_monitor()` is left 3840, top 0, right 6400,
bottom 1440.

| Browser | Version |
| --- | --- |
| Chrome | 157.0.8103.79 |
| Zen | 1.24.1b (`application.ini` `Version`) |

Edge, Brave and Firefox were not measured. One session that is not the spike's (the PC's own
player) was in the list before every launch and in every one of the 1323 polls; it is the only
other session ever seen.

## Method

- **Runs.** Three runs per browser, each with a fresh throwaway profile, a fresh launch and a
  fresh nonce; profiles were deleted afterwards (6 of 6 deleted). All 6 runs completed all 10
  steps (no run was dropped). An earlier script version, with A2 as a command-line tab, was
  discarded; every number here comes from the 6 runs of the final version.
- **Launch.** Each browser process was created suspended with STARTUPINFO `SW_SHOWNOACTIVATE`,
  put in a Job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, then resumed; the job handle
  was closed in a `finally`. Chrome flags: `--no-first-run --no-default-browser-check
  --disable-search-engine-choice-screen --disable-sync`, `--window-position`/`--window-size` on the
  right monitor, `--autoplay-policy=no-user-gesture-required --disable-popup-blocking`,
  `--disable-background-timer-throttling --disable-renderer-backgrounding
  --disable-backgrounding-occluded-windows`. Zen: `-no-remote -profile <dir>` with a `user.js`
  (autoplay allowed: `media.autoplay.default=0`, `media.autoplay.blocking_policy=0`;
  `dom.disable_open_during_load=false`; `media.hardwaremediakeys.enabled=true`; budget timer
  throttling off and `dom.min_background_timeout_value` 100; default-browser check, telemetry,
  updates and welcome screen off) and a preset `xulstore.json` that put the window on the right
  monitor.
- **Media.** A local server on 127.0.0.1 (port chosen by the OS) served the pages and a
  stdlib-generated 16-bit mono PCM WAV of 61 s, all zero (checked before serving), with range
  requests. Each page has an `<audio loop>` at volume 1, not muted, and sets
  `navigator.mediaSession.metadata` to title `<nonce> <label> <n>` and artist `<nonce>`. Pages
  fetched `GET /cmd?label=...` every 250 ms and ran `play`, `pause`, `title <n>`, `same-title`,
  `open`, `blank` or `close`, then reported to `/ack`. Every command in every run was acked
  (no ack failure, no `play()` rejection).
- **Windows and tabs.** Tab A1 was the only URL on the command line. Tab A2 and window B were
  opened by A1 with `window.open` from a server command (A2 as a tab, B as a popup-style window
  with `left/top/width/height` inside the right monitor); B1 is the page in window B. Placing:
  every browser window was checked with `GetWindowRect` (left >= 3840 and inside the monitor);
  `SetWindowPos(SWP_NOACTIVATE|SWP_NOZORDER)` and `ShowWindow(SW_SHOWNOACTIVATE)` were ready but
  no window needed a move (0 moves). No `SendInput`, `keybd_event`, `try_play`, `try_pause` or
  other control call on any session; no process outside the job was opened, suspended or killed.
  The only call on a window outside the job was one `SetForegroundWindow`, which gave the
  foreground back after Chrome's steal in run 1.
- **Poll.** Every 250 ms: `get_sessions()` read like `WindowsMedia._read` plus
  `get_current_session()`. Per step: wait up to 5 s from the last ack for the poll list of
  spike sessions to change and stay unchanged for four polls ("no change" when it runs out),
  then hold 3 polls. "Latency" is the time from the page's ack (server clock) to the end of the
  first poll of the final unchanged stretch; each latency includes up to 250 ms of waiting for
  the next poll.
  Step 8 has two phases (set the same title and artist on A1 and B1, then play both); its latency
  is the second phase's.
- **Steps.** 1 A1 plays. 2 A2 plays (A1 still playing). 3 B1 plays in a second window. 4 A2
  pauses. 5 A1 changes its metadata title. 6 A2 resumes. 7 all pause. 8 A1 and B1 given the same
  title and artist, both play. 9 A2's tab navigates away to a blank page. 10 window B closes.
- **Focus.** Polls where the foreground window belonged to a spike process: Chrome 0 of 519,
  Zen 0 of 804. Foreground steals (the foreground turning to a spike window, seen outside the
  poll loop while the browser started): Chrome 1 in 3 runs (run 1), given back at once
  (`SetForegroundWindow`, 1 of 1 restored); Zen 0. Samples with a spike window outside the right
  monitor: 0 in all 6 runs (Zen too, thanks to the preset window position).

## Sessions per poll

Cells pool the polls of the 3 runs for the step (from the first command of the step to the end of
its 3 hold polls). "Polls with two or more" counts polls that listed two or more spike sessions
of the browser. Chrome: 519 polls, 0 of them with two or more, at most 1 session in any poll.

**Chrome**

| Step | Spike sessions per poll (min / median / max) | Labels seen | Polls with two or more |
| --- | --- | --- | --- |
| 1 A1 plays | 0 / 1 / 1 | A1 | 0 of 27 |
| 2 A2 plays | 1 / 1 / 1 | A1, A2 | 0 of 27 |
| 3 B1 plays in window B | 1 / 1 / 1 | A2, B1 | 0 of 24 |
| 4 A2 pauses | 1 / 1 / 1 | B1 | 0 of 72 |
| 5 A1 changes title | 1 / 1 / 1 | B1 | 0 of 74 |
| 6 A2 resumes | 1 / 1 / 1 | A2, B1 | 0 of 25 |
| 7 all pause | 1 / 1 / 1 | A2 | 0 of 25 |
| 8 A1 and B1 same title, both play | 1 / 1 / 1 | A2, SAME | 0 of 98 |
| 9 A2 navigates away | 1 / 1 / 1 | SAME | 0 of 72 |
| 10 window B closes | 1 / 1 / 1 | SAME | 0 of 75 |

The 0 in step 1 is the polls before the first one that showed the session. "SAME" is the label of
the title set in step 8, shared by A1 and B1.

**Zen**

| Step | Spike sessions per poll (min / median / max) | Labels seen | Polls with two or more |
| --- | --- | --- | --- |
| 1 to 10 (every step) | not run | not run | not run |

In Zen 804 polls (3 runs) never listed a spike session. Every command was acked, A1's page
fetched the audio and its `play()` resolved, and the windows were on the right monitor, so the
browser ran; only the session is missing. Pages A2 and B1 did not fetch the audio file in Zen
(possibly served from the media cache; not checked). Zen is "not run" in every cell.

## What GSMTC exposes per session

| Property | Chrome | Zen |
| --- | --- | --- |
| `SourceAppUserModelId` | present | not run |
| Title | present (the page's `MediaMetadata` title) | not run |
| Artist | present | not run |
| Playback status | present (`playing`, `paused` seen) | not run |
| Session id | none: the session object has no id member | not run |

The session object's members (from the API, not from the sessions) are the AUMID, playback
info, timeline properties, media properties (title, artist, album artist, album title, album
track count, genres, playback type, subtitle, thumbnail, track number) and the `try_*` control
calls; none of them is an id.

| Boolean | Chrome | Zen |
| --- | --- | --- |
| AUMID is the same for every tab and window of the browser | true (1 distinct AUMID over all 3 runs, tabs A1 and A2 and window B) | not run |
| No property tells two sessions of the browser apart except title and artist | true by the API above (the spike set only title and artist); not exercised, because Chrome never listed two sessions | not run |
| AUMID differs between Chrome and Zen | not run (Zen published no session to compare) | not run |

## Switching behaviour

"Label shown" is the session list at the end of the step's hold (the same in all 3 runs, one
session each). "First is current": over the 3 hold polls of each run (step 8: 6, two phases), how often the
first entry of `get_sessions()` (`MediaTracker.poll` keeps the first entry per app, so a session
of another app at index 0 does not hide the browser's) was the same session as
`get_current_session()` (compared by AUMID, title and artist). Latency is median / max over the 3
runs of ms from the page's ack to the first poll that showed the final state.

**Chrome**

| Step | Label shown | First is current | Latency (ms, median / max) | No change |
| --- | --- | --- | --- | --- |
| 1 A1 plays | A1 playing | 0 of 9 | 239 / 280 | 0 of 3 |
| 2 A2 plays (A1 still playing) | A2 playing | 0 of 9 | 219 / 233 | 0 of 3 |
| 3 B1 plays in window B | B1 playing | 0 of 9 | 171 / 174 | 0 of 3 |
| 4 A2 pauses | B1 playing | 0 of 9 | - | 3 of 3 |
| 5 A1 changes its title | B1 playing | 0 of 9 | - | 3 of 3 |
| 6 A2 resumes | A2 playing | 0 of 9 | 155 / 226 | 0 of 3 |
| 7 all pause | A2 paused | 9 of 9 | 188 / 249 | 0 of 3 |
| 8 A1 and B1 same title, both play | SAME playing | 18 of 18 | 235 / 251 | 0 of 3 |
| 9 A2 navigates away | SAME playing | 9 of 9 | - | 3 of 3 |
| 10 window B closes | SAME playing | 9 of 9 | - | 3 of 3 |

**Zen**

| Step | Label shown | First is current | Latency (ms, median / max) | No change |
| --- | --- | --- | --- | --- |
| 1 to 10 (every step) | not run | not run | not run | not run |

- Chrome publishes one session for all its tabs and windows. In steps 1 to 7 it followed the
  player that started or resumed last: A1, then A2, then B1, then A2 again after the resume. From
  step 8 on A1 and B1 share the label SAME, so steps 8 to 10 cannot tell which player the session
  follows. Pausing a player that
  is not the one shown (step 4), retitling a player that is not the one shown (step 5),
  navigating a tab away and closing a window (steps 9, 10) did not change the list.
- The first entry of `get_sessions()` was the PC's own other session in all 99 hold polls that
  had a spike session (0 of 99 a spike session), while `get_current_session()` was the spike
  session in 54 of them (steps 1 to 6) and the PC's other session in the other 45 (steps 7 to
  10). So "first is current" is true only where both were the PC's other session. This is a
  property of that PC's session list, not of Chrome. Within Chrome the one session is trivially
  first.

## Same title and artist (step 8)

Before step 8 all players were paused and the one session showed A2 paused. Giving A1 and B1 the
same title and artist did not change the list; after both played, all 3 runs showed exactly one
session, `SAME playing`, in every poll (0 of 98 polls with two). The
`(app_id, title, artist)` collision of two players could not occur: Chrome never listed two
sessions to collide.

## Verdict for F18

**F18 is not needed (every one of the 519 Chrome polls listed at most one spike session).** This
held with two or three players at once (A1 and A2 in step 2, all three in steps 3 and 6, A1 and
B1 in step 8). The single session follows the tab or window that last started or
resumed playing and keeps showing it while the others play or pause, so a row per
`(app_id, title, artist)` with the first entry wins sees the same session as the system does;
what it misses is the other players behind that session, which GSMTC never lists.

Zen published no session in 804 polls (3 runs), so nothing was measured there; the cause is
open (see Limits). In step 8 the collision case did not occur in Chrome (one session,
`SAME playing`).

## Limits

- Fresh disposable profiles, local silent media (all-zero PCM), no extensions, no real sites. A
  real player (an ad, a site that uses several audio elements, a site with its own session
  handling) may behave differently. Zen's missing session may come from the silent audio
  (not tested with audible audio, which the spike rules out) or from Zen's media-key setup; the
  cause is not known.
- A tab given only on the command line stays a background tab, and in both browsers its media
  never started loading (`readyState` 0, no audio request, `play()` never settled). A first
  attempt with A2 as a command-line tab was discarded for that reason (see Method); A2 was then opened by A1
  with `window.open`, which makes it the visible tab of the window (the browser's own behaviour,
  not a switch by the spike). A1 played in the background from step 2 on, so "A1 hidden while
  playing" is covered and "A2 played first, A1 second" is not.
- No foreground-tab or foreground-window switching by the spike; B is a popup-style window
  opened by script, not a normal browser window.
- One PC session list that is not the spike's (1 session in every poll): the "first is current"
  numbers depend on it and could differ on another PC.
- Polls every 250 ms: each latency includes up to 250 ms of waiting for the next poll; changes
  shorter than a poll were not seen. Steps 8 to 10 cannot tell A1 from B1 (same label). Three runs per browser, one version each (Chrome 157.0.8103.79, Zen 1.24.1b).
- The 3 runs showed the same labels, counts and "no change" outcomes in every step; only
  latency varied (median / max above).
- Foreground steals were possible and were tolerated: Chrome took the foreground once while it
  started (run 1) and the foreground was given back at once; Zen never.
