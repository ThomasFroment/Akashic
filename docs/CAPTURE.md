# Capture

## Evidence meaning

Screenshots represent the selected app window, never a monitor fallback. Episode images must
belong to the claimed app and moment; a nearby timestamp alone is insufficient.
A window whose pixels cannot be rendered (hung app) is stored text-only, like black
frames, and windows over 16 MP are downscaled to fit.

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
([DECISIONS.md](DECISIONS.md), D2). Built-in finance and password-manager addresses match parsed
hosts, not substrings. Akashic's own UI is always excluded. The detailed mechanics live with the
code (`capture/screen.py`, `capture/exclusions.py`, `capture/browser_state.py`,
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
minutes in game mode). Priority is `paused` > `degraded` > `gaming` > `ready`. A pause clears
every non-sticky problem; a resume reports `degraded` only for `audio_unavailable`, and the next
publish catches up with the real state. Problem changes are logged once, codes only: warning level
when problems are active, info level when the set becomes empty.
The thresholds (`GRAB_FAILURE_RUN`, `DEVICE_DOWN_S` in `capture/health.py`) are not measured.

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
field. `read_text` took up to 4.5 s on the Chromium page and 3.1 s in Zen; its budget only
records time after the call and `max_nodes` applies after the single `FindAll` call, so on pages
that large `read_text` times out and the frame falls back to OCR.
The transaction timeout is the next 500 ms step above 3x the worst legitimate call
(1878 ms), with a 1000 ms floor.

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
