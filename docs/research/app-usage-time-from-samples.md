# How should sparse frames and foreground observations become continuous app time, and what should end the hold?
Date: 2026-11-19 · Context: Python 3.14, Windows 11 build 26200, SQLCipher; Akashic `pipeline/sessions.py` (`IDLE_GAP_MS` 5 min, a session ends at its last stored frame), `storage/gaps.py`, `storage/monitors.py`; harness `eval/quality/` smoke runs · Goal: app sessions with no untyped holes and no time counted across idle, lock, sleep or away (audit #196, M5.1, M5.3) · Ruled out: none

## Answer
Hold each app session past its last frame instead of ending it there, as every surveyed tracker
holds the last observation [1][2][3][4]. Bound the hold by what Akashic already stores, not by a
fixed cap: up to the next session's start, at most one check interval past the last "unchanged"
confirmation of the last frame's monitor (`monitor_states.checked_ms`), and never into a stored
no-recording gap, past the end of recording, or past a game's stored end [5][6]. On the two smoke
runs this takes holes from 115.8 / 109.7 s to 1.6 / 0.3 s and duration error from 0.587 / 0.570
to 0.231 / 0.320, without new overlap [6]. A fixed cap of 60 s or more scored nearly the same
only because the runs held no long static screen; on a static page frames stop, so any fixed cap
leaves a hole [5][6]. Lock, sleep (stored as `stalled`), pause and stop already end the hold
through stored gaps; input idle has no record yet, and needs a presence override for video and
controller play, as ActivityWatch's audible-tab and always-active options do [5][2][7].

## Options
| Rule | Holes (s, runs 2220 / 2241) | Duration error | What ends it | Fit |
| :- | :- | :- | :- | :- |
| Today: end at the last frame | 115.8 / 109.7 [6] | 0.587 / 0.570 [6] | last frame | in place |
| Hold to next session, cap 5 s | 83.0 / 76.8 [6] | 0.501 / 0.485 [6] | cap | too short for 15 s and 60 s check intervals |
| Hold to next session, cap 60-300 s | 0.55 / 0.0 [6] | 0.318 / 0.321 [6] | cap, gaps, end of recording | leaves a hole after a static stretch longer than the cap [5] |
| Hold to next session, bounded by the monitor's last unchanged check + 5 s and the game's end | 1.6 / 0.3 [6] | 0.231 / 0.320 [6] | confirmation, gaps, game end, end of recording | uses stored rows only |
| screenpipe: gap under 300 s goes to the earlier frame, else 0 [3] | not replayed | not replayed | 300 s, whole gap dropped | drops the whole tail after a long static read |
| ActivityWatch: 1 s heartbeats, 5 s flood, minus AFK [1][2] | not replayed | not replayed | AFK at 180 s, back-dated | needs a 1 s foreground poll and an idle record |

## Findings
### Akashic today
- A session is one run of focused window rows with the same process, title and URL; it ends at
  its last stored frame and closes after 5 min without a new frame [8].
- Frames are stored only when the picture changed; the focused flag, process and title are part of
  the desk signature, so a focus switch stores a frame after a 0.3 s debounce, except during game
  mode, when triggers are cleared and the focused non-game monitor is checked every 15 s [5].
- `monitor_states` keeps a "shown" run per monitor whose `checked_ms` advances on every
  unchanged check; `latest_valid_frame` already reads it to prove a frame is still on screen [5].
- A wall-clock jump over 30 s on the gap thread's 1 s tick is written as an `all/stalled` gap, so
  sleep is already a stored gap [5].
- Focusing an excluded window changes the previous window's focused flag, so a frame is stored with
  no focused app row; the hold can end there without a new signal [5].
### Other trackers
- aw-watcher-window polls every 1 s and sends heartbeats with pulsetime 2 s; after an excluded poll
  the next heartbeat uses pulsetime 0 so the excluded time is not absorbed [1].
- The server merges a heartbeat into the last event only if the data is equal and it starts within
  pulsetime of that event's end [1].
- aw-watcher-afk reads GetLastInputInfo every 5 s and turns AFK after 180 s, back-dated to the last
  input; on Windows its lock check is a stub returning False [1].
- The canonical query floods window and AFK events (5 s; gaps between different events split at
  the midpoint), keeps `not-afk`, and intersects window time with it; options add audible browser
  tabs or an always-active app pattern [2].
- screenpipe credits the gap between consecutive frames to the earlier frame's app when it is
  under 300 s; a longer gap and the last frame count 0 [3].
- arbtt counts every sample as its sample interval (60 s default) and excludes samples tagged
  inactive; WakaTime joins heartbeats within a 15 min timeout; ManicTime relabels the last 10 min
  as away once 10 min pass without input [4].
### Windows presence signals
- GetLastInputInfo is per session and can be fed by SendInput; Valve says its overlay gamepad
  emulation and the Steam Input API produce no events it sees, while an Xbox controller probably
  does [7].
- GUID_SESSION_USER_PRESENCE reports present, not present or inactive as a power-setting
  notification; whether video or gamepad play keeps it present is undocumented [7].
- Listing other processes' power requests (what `powercfg /requests` shows) needs admin rights [7].

## Caveats
- The smoke runs contain no idle period, no sleep, no lock and no long static screen, so the idle
  cut, the presence override and the confirmation bound against a long read are unmeasured [6].
- The replay matches each band to its last frame's monitor by start, end and process; the 5 s
  check interval used for the bound is the focused monitor's, not the 15 s or 60 s of the others [6].
- In game mode the next app's first frame can trail the real switch by about 5-15 s; the
  game-end cut covers it only when the game session's end is stored on time [5].
- WakaTime's "no time added at the ends" is implied by its FAQ, not stated; RescueTime's 5 min
  threshold rests on a search summary; Toggl and Timing publish no rules [4].
- ActivityWatch sleep handling is inferred from code, not documented [1].

## Sources
[1] ActivityWatch watchers and server merge — `_resources/references/aw-watcher-window/aw_watcher_window/config.py:16`, `main.py:31-40, 289-296, 420-446`; `aw-server-rust/aw-transform/src/heartbeat.rs:12-57`; `aw-core/aw_transform/heartbeats.py:26-56`; `aw-watcher-afk/aw_watcher_afk/config.py:8-13`, `afk.py:39-63, 100-186`, `windows.py` (https://github.com/ActivityWatch) — tier 1 — default branches, 2026-11-19
[2] ActivityWatch flood and canonical query — `_resources/references/aw-core/aw_transform/flood.py:10-12`, `aw-server-rust/aw-transform/src/flood.rs`, `aw-webui/src/queries.ts:196, 227-248` — tier 1 — default branches, 2026-11-19
[3] screenpipe activity summary — `_resources/references/screenpipe/crates/screenpipe-engine/src/routes/activity_summary.rs:39, 703-805, 1505-1519` — tier 1 — clone, untagged, 2026-11-19
[4] arbtt https://raw.githubusercontent.com/nomeata/arbtt/master/src/Stats.hs, https://raw.githubusercontent.com/nomeata/arbtt/master/doc/arbtt.xml; WakaTime https://wakatime.com/faq; ManicTime https://blog.manictime.com/articles/may-2025/away-time/; RescueTime https://help.rescuetime.com/article/54-what-does-rescuetime-do-when-i-am-away-from-my-computer — tier 1 (RescueTime tier 3) — accessed 2026-11-19
[5] Akashic capture and storage — `src/akashic/capture/screen.py:82, 361-363, 443, 459-461, 513-541, 593-595`, `src/akashic/capture/desk.py:263-297`, `src/akashic/capture/debounce.py:16`, `src/akashic/capture/gaming.py:68`, `src/akashic/capture/gaps.py:32, 241-245`, `src/akashic/storage/monitors.py:1-9, 89-99, 177-216` — tier 1 — 2026-11-19
[6] Session hold replay on the smoke runs — `eval/quality/spikes/session_hold_replay.py`, output `eval/quality/spikes/session_hold_replay.json`; scorer `eval/quality/score.py:555-590`; runs `eval/quality/results/20261118-smoke-s11-target-2220`, `-2241` — tier 1 — target PC, 2026-11-19
[7] Windows presence — https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo; https://steamcommunity.com/groups/SteamClientBeta/discussions/3/3887226156980206582; https://learn.microsoft.com/en-us/windows/win32/power/power-setting-guids; https://github.com/diversenok/Powercfg — tier 1 / 2 — accessed 2026-11-19
[8] Akashic session derivation — `src/akashic/pipeline/sessions.py:1-13, 25, 53-74` — tier 1 — 2026-11-19
