# Capture

> **Being replaced in part** by the [monitor-capture design](designs/2026-11-14-monitor-capture.md)
> (TODO A9). Where they differ, the design and the 2026-11-14 and 2026-11-15 rows of DECISIONS.md
> win.

## Evidence meaning

Screenshots represent the selected app window, never a monitor fallback. Episode images must
belong to the claimed app and moment; a nearby timestamp alone is insufficient.
A window whose pixels cannot be rendered (hung app) is stored text-only, like black
frames, and windows over 16 MP are downscaled to fit.
Input also captures the unfocused window under the cursor, on any monitor, OCR only. A window
that is focused, or owned by the focused window, is never captured as unfocused. Each target has
its own rate gate; both are checked together, so a trigger defers at most once and then captures
both.

A window that covers a monitor is a game when Windows reports a Direct3D exclusive-fullscreen app
(`SHQueryUserNotificationState`, read at most once per second), even with a caption or on the
built-in deny-list; otherwise when it has no caption and is not on the deny-list. Precedence: no
process or no covered monitor is not a game, then the user's `games`, then the user's `not_games`,
then D3D evidence, then the borderless rule.

Game mode takes the game screenshot once per interval and drops input triggers. The unfocused
window on each other monitor is captured every 15 s, with no UIA text and OCR deferred to the
worker. The browser caption and URL readers still run on side browsers (each UIA request times
out after 2 s, 1 s to connect; up to 4 reader calls per side browser). The game window and the
focused window are never taken as side windows, and no side capture happens while the focus
monitor is unknown.

Screen text should preserve useful reading order, repeated messages and bilingual content while
removing interface clutter. Prefer accessibility text with OCR as a fallback. Neither nonempty
UIA text nor a nonblack image proves completeness, freshness or correct ownership.

The app must work across three or more monitors, including mixed DPI and negative desktop
coordinates. Prefer targeted, demand-driven capture over continuous desktop processing. Games
must remain responsive while conversations continue to be recorded.

## Privacy and source boundaries

Apply exclusions in memory before persistence, including screenshots, text, media metadata and
audio. Recheck changing targets conservatively; separate OS reads are not an atomic snapshot.
Unreadable metadata must not silently authorize recording excluded content: an unreadable
password-field state, private-mode marker, app identity or audible-process list skips the
evidence. The few accepted exceptions are listed once, in [SECURITY.md](SECURITY.md).

Browser rules: only the browser's own address bar counts as its URL, never an in-page field.
Private-mode markers count only as the browser's own title suffix. Password-manager title
patterns apply only to non-browser apps, so a video titled "Bitwarden vs 1Password" is kept
([DECISIONS.md](DECISIONS.md), D2 of 2026-11-11, superseded by the 2026-11-14 exclusions row,
TODO A9). Built-in finance and password-manager addresses match parsed hosts, not substrings.
Akashic's own UI is always excluded. The detailed mechanics live with the code (`capture/screen.py`, `capture/exclusions.py`, `capture/browser_state.py`,
`platform/win/uia.py`) and their tests.

Media sessions follow the screen loop's per-window verdict: a browser's session is rejected while
an excluding verdict is fresh, and a missing or stale verdict for the focused browser rejects it
too. A browser window the screen loop never examined (minimized, covered) has no verdict, and
its media is logged: an accepted exception ([SECURITY.md](SECURITY.md)). A media title is not a
window title.

Pause stops all new capture. The pause is kept across Lock, Quit and restart until the user
resumes. Pause drops the utterance being recorded. Stop, a device change and a device failure
keep it; an unexpected fault drops it. A locked Windows screen and an Akashic pause are
different states: headset conversations can continue while Windows is locked.

Audio segment writes and media-session closes retry when the database is busy; screen frames
and game rows do not.

Media-session metadata and audible-process lists are correlations, not proof of which window
or process produced a particular word. Endpoint loopback mixes sound and may miss audio routed
elsewhere. Preserve that uncertainty in episode associations and answers.

Music-app audio: drop songs, but keep segments with evidence of real speech, such as a podcast
([DECISIONS.md](DECISIONS.md), D7). Today the code still drops all system sound whose only
sources are music apps.

## Health

Capture reports `degraded` (instead of `ready` or `gaming`) while it runs unpaused and a problem
is active. The codes are fixed strings, also listed under `problems` in the `capture`
process_info row and returned as `capture_problems` by `GET /api/status`.

- `audio_unavailable`: audio is enabled but could not start (for example the VAD model is
  missing). Sticky: a pause does not clear it. `[audio] enabled = false` is never a problem.
- `mic_unavailable`, `system_audio_unavailable`: that recorder had no successful device read for
  60 s, counted from the later of its last read and the last resume. A read that times out with
  no data counts as a success.
- `screen_images_failing`: the last 3 grabs all failed (`error`, `loop_error`,
  `image_unavailable`, `stored_no_image`, `unchanged_no_image`), all after the last resume. Any
  success or a pause clears the record. Locked, excluded, no-window and similar outcomes are
  neutral.

Detection delay: the problems are evaluated by the publisher thread every 1 to 5 s, so a problem
shows up 3 failed grabs plus up to one publish interval later (about 15 s idle, up to about 3
minutes in game mode on one monitor, 3 game grabs at 60 s). With other monitors, side grabs
every 15 s join the same failure run, so failures show sooner when the side grabs fail too. A
good side grab also clears failed game grabs, so a failure limited to the game screenshot may
not surface. Priority is `paused` > `degraded` > `gaming` > `ready`. A pause clears
every non-sticky problem; a resume reports `degraded` only for `audio_unavailable`, and the next
publish catches up with the real state. Problem changes are logged once, codes only: warning level
when problems are active, info level when the set becomes empty.
The thresholds (`GRAB_FAILURE_RUN`, `DEVICE_DOWN_S` in `capture/health.py`) are not measured.

## Capture gaps

The `capture_gaps` table says when something was not being recorded and why. Rows are
content-free: a closed vocabulary (CHECK constraints) and two integer times. `end_ms IS NULL`
means the gap is still going on. A gap thread of capture writes them on its own connection, so a
busy database never delays capture; events keep their original times and are retried. Nothing
is written before the previous run has been settled. Screen drops, failures and hidden monitors
are not here (A9 monitor states).

| kind | reason | opens | closes |
| --- | --- | --- | --- |
| `all` | `paused` | pause (also when capture starts paused) | resume |
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
while draining are still written afterwards as closed rows. Gaps queue at most 10,000 pending
drops; transitions are always queued. Counters are under `gaps` in the capture Debug data.

## UI Automation timeouts

`WindowsUia` sets `ConnectionTimeout` to 1000 ms and `TransactionTimeout` to 2000 ms
(`platform/win/uia.py`), so a hung app fails a call instead of blocking the screen loop.
Spike 2026-11-13, Edge and Chrome, a local page of about 30k elements with an autofocused
password field, default UIA timeouts, 5 runs each. Worst times of the readers that run without
a budget: `focused_is_password` 18 ms, `browser_caption` 20 ms, `read_url` 626 ms (Chrome; Edge
554 ms). Zen (Gecko, throwaway profile, same page, about 20k UIA nodes, 5 runs): worst
`focused_is_password` 34 ms, `browser_caption` 0 ms (returns at once for Firefox and Zen),
`read_url` 14 ms (`_firefox_bar` finds the address bar early). The password field was not the
focused element in the Zen run, so `focused_is_password` there did not read a Gecko password
field. `read_text` took up to 4.5 s on the Chromium page and 3.1 s in Zen, because it fetched
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

The three readers without a budget (`focused_is_password`, `browser_caption`, `read_url`) are
also time-boxed per window. A call that took 900 ms or more (`SLOW_CALL_MS`, wall time) and
returned None backs that window's HWND off for 30 s (`HWND_BACKOFF_S`), stamped on the capture
tick's clock. While it is backed off no UIA call is made for it and each reader returns its
failure value: `browser_caption` None (Chrome and Brave end as `excluded:caption_unknown`),
`read_url` None (the unreadable-address path), the password check None
(`excluded:password_unknown`, or `window_changed` when the backoff starts mid-attempt),
`read_text` skipped (OCR gate or empty text). So a hung focused Edge window costs one timed-out
call per 30 s instead of about five per attempt. Other windows and monitors are unaffected.
Neither `read_text` nor a slow call that returned a value starts a backoff. Remaining limit: a
legitimate read that takes 900 ms or more and still returns None (for example a huge page with
no omnibox found) excludes the window for 30 s.

Spike 2026-11-13, Chrome 157 and Edge 157, throwaway profiles, PC locked: a flagless window
returned only the browser's own interface text through UIA, never page text, even after 10 s.
With Chromium's occlusion tracking turned off, the first read enabled web accessibility and the
page was readable from the next read (0.5 s); with `--force-renderer-accessibility` at once.
So a blank page tree means Chromium considers the window occluded, not that UIA is enabled
lazily. Address bar and caption reads worked in every mode. Not yet run unlocked with the window
in front.

## Validation boundaries

Controlled Zen checks established window rendering, updates, a mixed-DPI move and occlusion
within that fixture. Those checks do not validate everyday browsers, Discord, games or protected
surfaces. Firefox and Zen layouts (title markers, address bar) and Brave's caption are unverified
on this PC. French OCR availability and real bilingual accuracy must be checked on the target
machine.

Validate real app layouts, startup accessibility, focus/exclusion transitions, brief text on
secondary monitors and concurrent media ownership. Unsupported rendering must fail safely;
do not restore desktop fallback. Use the [manual checklist](../tests/manual/CHECKLIST.md) and
[audit findings](audits/2026-11-10-2.md) to choose checks relevant to a change.
