# Capture

## Evidence meaning

Screenshots represent the selected app window, never a monitor fallback. Episode images must
belong to the claimed app and moment; a nearby timestamp alone is insufficient.

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
resumes. A locked Windows screen and an Akashic pause are different states:
headset conversations can continue while Windows is locked.

Media-session metadata and audible-process lists are correlations, not proof of which window
or process produced a particular word. Endpoint loopback mixes sound and may miss audio routed
elsewhere. Preserve that uncertainty in episode associations and answers.

Music-app audio: drop songs, but keep segments with evidence of real speech, such as a podcast
([DECISIONS.md](DECISIONS.md), D7). Today the code still drops all system sound whose only
sources are music apps.

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
