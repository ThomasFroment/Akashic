# Monitor capture and multi-monitor timeline

**Status:** proposed, 2026-11-15; checked by measurements on the user's PC, independent
refutations and reviews ·
**Milestone:** [TODO.md](../../TODO.md) A9 · **Decisions:** the 2026-11-14 and 2026-11-15 rows
of [DECISIONS.md](../DECISIONS.md), which are binding; each section below applies them ·
**Evidence:** [window composition](../baselines/2026-11-15-window-composition.md).

This document explains what changes, why, and which rules the change must keep. It is not an
implementation guide: modules, schemas, routes and algorithms are decided while building, in
code and tests. Once A9 lands, its lasting rules move into CAPTURE.md, SECURITY.md, STORAGE.md,
RETRIEVAL.md, ARCHITECTURE.md and UI.md, and this file is deleted.

## Problem

Akashic records app windows, not screens. For each monitor it renders one chosen window (the
focused one, or the one at the monitor's centre), plus the window under the cursor on input, even
if other windows cover them. That has two consequences:

- **The record is not what the user saw.** A video in a corner, a Discord chat beside the
  browser, a second window on the same monitor: none of these is in the record unless it was a
  chosen window at that moment. Covered parts of a chosen window are recorded as if visible.
- **The timeline cannot show the desk.** The timeline is a grid of single-window thumbnails. The
  user works on three or more monitors and wants to scroll through the day and see all of them at
  once, laid out as they sit on the desk.

The target questions in [AGENTS.md](../../AGENTS.md) often ask about something that was *on
screen* rather than *focused*: "what video did I watch yesterday afternoon" while working on
another monitor, or what was in Discord during a call.

## Words used here

- **Verdict:** Akashic's keep-or-exclude decision for one window, from the exclusion rules.
- **WGC** (Windows.Graphics.Capture): the Windows API that delivers a window's own pixels, as
  drawn by the compositor, whatever covers it.
- **Composite:** a monitor picture built by painting each kept window's latest WGC frame in
  stacking order. **Pending:** a window with no usable frame yet, painted solid.
- **Redact:** paint an area of the image over with a solid colour, in memory, before it is saved.
- **UIA** (UI Automation): asking a window for its text through Windows' accessibility interface.
  **OCR**: reading text from the image.
- **Owned popup:** a menu, dropdown or tooltip window that belongs to another window.
- **Copy surface:** a window that shows copies of other windows' pixels, such as Alt-Tab or a
  taskbar preview.
- **Cloaked:** a window Windows lists as visible but does not draw, such as a window on another
  virtual desktop or a suspended app.
- **Visible window:** a top-level window on the current desktop, not minimized or cloaked, with
  at least one pixel on screen. A fully covered window is not visible: it needs no verdict and
  no capture until part of it shows, and it is pending until then.

## Goals

1. A frame is **what one monitor showed**: each kept window's real pixels at native resolution,
   where it was and in its stacking order.
2. A **timeline that shows every monitor at once**, at their real relative positions and sizes,
   that the user scrolls through time.
3. **What the exclusion rules exclude is never captured**, so it never reaches disk, in pixels or
   text, except the accepted exceptions below. A window whose app or private state cannot be read
   is treated as excluded. An unreadable address alone is not a reason.
4. Evidence stays **attributable**: an episode or answer about an app shows that app, and only
   what the user saw of it.
5. Capture stays light enough for games and daily use on three or more monitors. When this
   conflicts with native resolution, the game wins: encoding and OCR wait, the game never does.
   A spike reports the game frame-rate loss; the user decides whether it is acceptable.

## Non-goals

- Lanes on the time strip other than app bands (speech, media, games, gaps): later.
- App sessions built from visible but unfocused windows (#149): enabled by this design, not part
  of it.
- UIA text beyond the focused window.
- Exclusions by window or media title: dropped for now, to be added back later.
- Video compression of stored frames, or any change to retention (still forever, no storage
  warning). The spikes measured video storage; the user decides on it with the disk-per-day
  numbers.

## Design

### A frame is one monitor, composed from its windows

Akashic keeps one WGC capture running for every visible window it has judged and kept. Windows
sends that capture a frame only when the window's content changes. A monitor's frame is composed
from those frames: each kept window's latest frame, painted at its place in stacking order, the
desktop at the bottom. Excluded windows have no capture; their visible area is painted solid.

The frame is stored at native resolution, with today's image quality. It keeps the monitor's
identity and its place on the desktop at that time, and the list of kept app windows visible on
it, with where each one was and which one had focus.

A composite never contains what was not captured, so an excluded window cannot show through a
translucent window, around an animation or under a window being moved. Measured on the user's
PC: the composite matched the real screen on static monitors (taskbar aside) within 0.2% of
pixels at a 24-level tolerance, and a translucent, an overlapping or a capture-excluded window on
top of an excluded one leaked nothing.

What the composite does not show, accepted: the blurred backdrop behind translucent shell
surfaces such as the taskbar, Task View, drop shadows and the cursor. A new window appears after
its first frame (49-79 ms measured; 76-297 ms when many captures start one after another); until
then it is pending. While a window is dragged or resized the composite is off by 3.5-5.7% and
9-14% of the monitor respectively, and back in line within 0.5 s.

Windows to leave out of the composite:

- **Fully transparent layered windows** (overlays such as the NVIDIA overlay): WGC returns them
  as solid black, which would hide the monitor.
- **Cloaked and minimized windows:** they are not on screen.

Monitors are captured independently:

- The monitor with the focused window follows input and focus changes and is refreshed every few
  seconds.
- Side monitors are checked at least every 30 s. Every check refreshes the verdicts used by media
  logging, even when its frame is unchanged, dropped or failed. A private or bank window cuts its
  browser's media for 60 s after it was last seen, so checking twice as often keeps it cutting
  even when a check runs late.
- Input on another monitor (scrolling a side window) refreshes the monitor under the cursor.
- A hung window cannot delay composition: its capture keeps its last frame and the app is not
  asked to draw. Reading a hung browser's caption and address is still to be measured.

A check composes a monitor only when one of its windows sent a new frame or the window list
changed; otherwise nothing changed and nothing is done. A composed frame with no visible change
from the monitor's last stored picture is not stored (as today, tiny differences such as a
blinking cursor do not count as a change). Change is always measured against the last picture
actually stored: a frame that is dropped, fails to encode or fails to write marks the monitor
dropped or failed, and the next check takes the picture again. As today, a fully black monitor
(protected video, some exclusive-fullscreen games) stores no image, only its window list, plus
UIA text if the focused window is on it.

**Why per monitor, not synchronized snapshots of every monitor:** the desk is mostly still, and
each monitor changes at its own pace. The timeline shows, for each monitor, its latest frame at or
before the chosen moment.

### Encoding never holds up capture

Encoding a native 4K frame is slow: today's WebP setting took 266 ms per 4K frame and 120-150 ms
per 1440p frame, on one CPU thread. So encoding runs apart from the capture loop:

- Composition, with its solid areas, happens in memory before a frame leaves the capture loop.
  Nothing is written unencrypted.
- Frames wait for encoding in a short, bounded queue (a raw 4K frame is about 33 MB). When the
  queue is full, the oldest waiting side-monitor frame is dropped (if there is none, the oldest
  frame) and its monitor is marked "dropped". The capture loop never waits for the encoder.

### Monitors have a stable identity and a recorded layout

A monitor is identified by what Windows reports about the hardware and its connection, not by the
order Windows lists it in, so it keeps its identity across restarts and when another one is
plugged in (two identical monitors may only be told apart by their connector). The record also
keeps, over time, which monitors were present, where they sat on the desktop (including negative
coordinates and mixed DPI) and when they were being recorded.

### Every moment of every monitor has a known state

At any moment, each monitor is in one of these states:

- **shown:** at the last check the monitor looked the same as its latest stored picture (or that
  picture was just stored). The timeline shows how old that check is. A black monitor counts as
  shown, with no picture.
- **hidden:** the monitor showed only excluded windows (a private window filling it).
- **dropped:** encoding could not keep up.
- **failed:** capturing, encoding or writing the picture failed.
- **not recording:** paused, locked, Akashic not running, or the monitor absent. These periods
  will be recorded by A7's capture gaps (TODO A7); the monitor state reads them from there.

States are known at each capture attempt and assumed between attempts. They never say why. A
label such as "hidden: bank" at 23:10 would itself record that the user was banking then, so the
state says only "hidden".

Health follows each monitor: repeated failures or drops on one monitor are reported even while
the others capture fine.

### Privacy: excluded windows are never captured

This is the core of the design. As today, an excluded window is simply never captured; the rules
below decide which windows are excluded, keep their pixels from arriving through other windows,
and say what is accepted.

1. **Every visible window gets a verdict before its capture starts, and again at every check.** A
   window is excluded for exactly these reasons, and no others:
   - its app: a built-in password manager or Windows sign-in surface, or an app the user
     excluded. A UWP app is judged by the app inside its frame window, not by the frame host;
   - a browser window in private (incognito) mode;
   - Akashic's own interface, recognized by its local address (localhost on Akashic's port);
   - a browser whose address can be read and matches the built-in bank and password-manager
     sites or one of the user's address exclusions.

   What that leaves out:

   - **Never matched against any list:** window titles, media titles, password fields.
   - **Still read:** a browser's own private-mode marker at the end of its title or caption
     (rule 1; an unreadable one falls under rule 2). Chrome shows it only in the caption that
     UIA reads, not in the window title.
   - **Kept when unreadable:** a browser address (no bar, the user typing in it, a failed read),
     Akashic's own pages included.
2. **Unknown means excluded.** These are treated as excluded:
   - a window whose app cannot be read, or a UWP frame window whose app cannot be resolved;
   - a browser window whose private state cannot be read (a Chrome or Brave window whose caption
     is unreadable, a blank title);
   - a kept window containing a child window of another app that is excluded or unknown.

   Picture-in-picture and DevTools windows take the verdict of their browser window: their own
   title never shows "InPrivate", and DevTools shows the page's content and traffic. A window
   first judged unknown is judged again at every check, so it does not stay hidden once its
   state can be read.
3. **A verdict change takes effect before the next pixel.**
   - When a kept window becomes excluded, its capture stops and its last frame is discarded.
   - When it becomes kept again, nothing older than the change is painted; it is pending until a
     new frame arrives.
   - **A browser window that changes title is held pending until its address and private state
     are read again.** A page that is excluded by its address paints after its title changes:
     measured in Chrome, the title changed at 59-64 ms and the first excluded pixels arrived at
     74-120 ms, in 5 of 5 runs. The hold starts from a title-change event, not from polling, and
     ends when a fresh read gives the new address (or its timeout makes the window unknown).
4. **Popups follow their owner; browser credential popups are always redacted.**
   - A menu, dropdown, suggestion list or tooltip is its own window. An owned popup takes the
     verdict of the window that owns it. Measured: Chrome and Zen draw `<select>` options,
     suggestions and tooltips in such windows, and they showed the excluded page's content in the
     composite until they followed their owner.
   - A popup whose owner cannot be found is redacted if any window of its app, minimized or on
     another desktop included, is excluded or unknown. In a browser it is always redacted,
     because it may belong to a private window.
   - Browser surfaces that are not part of the page are redacted whatever the page's verdict:
     extension popups (password managers above all), saved-password and card autofill, and
     save-password bubbles.
   - A "Not Responding" ghost window, drawn by Windows with the hung window's last picture, takes
     the verdict of the window it stands for. If that cannot be resolved, it is redacted.
5. **Shell copy surfaces are always redacted.** Alt-Tab, Snap Assist, Task View, taskbar previews
   and the Snipping Tool overlay show copies of other windows. Measured with an excluded window
   open: Alt-Tab, Snap Assist and the snipping overlay put 28,700, 93,400 and 3.7 million marker
   pixels into the composite until they were redacted. Task View never appeared in the composite,
   and the taskbar-preview result was inconclusive; both are on the list anyway. They are
   redacted always, not only while an excluded window exists, because the snipping overlay is a
   frozen copy that can outlive the window it shows (not reproduced yet).
6. **Redaction is solid.** Blur was rejected: it can leak large text and layout.
7. **Nothing about an excluded window is kept.** Its area produces no text, and it is left out of
   the frame's window list: no app, title or address, only the fact that an area was hidden.
8. **A monitor showing only excluded content stores no frame**, only the "hidden" state.

Deferred OCR needs no second check: it reads only the stored image, which never contained an
excluded window.

**Accepted exceptions** (to be listed in SECURITY.md when A9 lands):

- **Titles are never checked; an unreadable address is kept** (rule 1). A bank, password-manager
  or other sensitive page is excluded only through its readable address, so it is recorded while
  the user types in its bar, in picture-in-picture or as an installed web app. Akashic's own pages
  are recorded in the same cases. Their screen text then copies Akashic's transcripts, excerpts
  and answers, so deleting or expiring a transcript (D3) no longer removes every copy of it.
- **Password fields are not checked.** A page or app is recorded while the user types a password;
  the password itself normally shows as dots, but the page around it is kept.
- **Copies of other windows outside the shell.** A kept app that shows a copy of another window's
  pixels or text is captured as it appears: screen-share and OBS previews, remote desktop, the
  Magnifier and zoom tools, clipboard history, jump lists, Live Captions, or simply an app
  displaying a screenshot of an excluded window. The shell's own copy surfaces are redacted
  (rule 5); detecting every other one is not possible in general.
- **Notifications** are captured like any other window.
- **Background browser media** stays logged when the browser is minimized, fully covered or on
  another virtual desktop, and for a background tab of a visible window: only the active tab's
  address can be read.

**Changes to existing rules:**

- Title exclusions (built-in finance and password-manager titles, the user's title list, media
  title matches, the title match for Akashic's own pages) go, and so does the
  focused-password-field check. The user can add exclusions back later.
- The address fallback goes: today an unreadable address is judged by the title, and excluded
  when the user has address exclusions. Now it is kept. Zen shows decoded addresses, and a space
  must not make them unreadable, since unreadable now means kept.
- Media logging keeps today's per-browser rule: an excluding or unknown verdict on any visible
  window of a browser, or a missing or stale verdict for the focused browser, rejects that
  browser's media for 60 s. Verdicts now exist for every visible browser window, so a private or
  bank window on a side monitor also cuts that browser's media. This is stricter, and accepted.

### Text: UIA for the focused window, OCR for each other window

- The **focused window** is read through UIA, as today, with the same time limits and per-app
  backoff.
- **Every other visible window** is read with OCR, **one window at a time, on the part of it that
  was visible**. Reading window by window keeps each window's lines in order (side-by-side windows
  are not interleaved), lets a window that did not change keep its previous text without new OCR,
  and attributes every line to the window it came from. A window that sent no new frame needs no
  new OCR.
- Redacted and pending areas and the desktop are never read.
- **The window, not the frame, is the unit of text.** Search, its app filter, passages and text
  reuse work on each window's text with that window's app, title and address. A match in one
  window is never returned under another window's app, and a phrase never matches across two
  windows. The Identical Frame Text Reuse method (#101) assumes one title and address per frame,
  so it must be re-evaluated for windows.

**Why not UIA for the other windows:** it would give better text, but it is slow on large pages
(close to two seconds on a large Chromium page), it adds work in the apps being read (which is why
UIA reads are already rationed per app), and it returns text hidden behind other windows. OCR of
the visible area matches what was on screen.

### Everything downstream reads windows, not "the frame's app"

Today a frame has one app, title and address, and many features rely on that: app sessions, the
episode image, search and its passages, the app filters of search and Ask, the "on screen in …"
wording of answers, and the lookup of a playing video's address. With monitor frames, these read
the frame's list of windows and the text attributed to each window. App sessions still follow the
focused window, so the timeline's app bands mean what they mean today.

### Evidence shows only what the user saw of the app

An episode, a search hit or an answer citing a window shows **only that window's visible part**:
the frame cropped to the window, with every pixel that belonged to something else (another window
on top, the desktop) masked out. The image is exactly what the user saw of that app at that
moment, nothing more. If the claimed app was not visible on the frame, there is no image.

Because unchanged frames are not stored, a window that stayed the same on a side monitor may have
no frame inside a later episode. The latest stored frame of that monitor is valid evidence for
the moment as long as the monitor was "shown" at every check since, and the window is on it.
Any other earlier or nearby frame is not a substitute.

### Games

Game detection is unchanged. The game window is captured like any other window. During a game,
the game's monitor is composed at the game rate and the other monitors at their usual slower
rate, with no UIA reads and OCR deferred to the worker, so the game stays responsive. Deferred
OCR follows the same rules as live OCR (window by window, visible parts only), but reads the
stored image, which is lossy, so its text can be slightly worse than a live read.

### Timeline

- **Stage:** every monitor present at the chosen moment, drawn at its real relative position and
  size, scaled to fit the page. A shown monitor displays its latest frame at or before that
  moment, with the age of the last check; any other state is drawn as that state, never as an old
  frame. The monitor with the focused window is marked. Hovering shows the windows on a frame.
- **Time strip:** the day, with bands coloured by the focused app, zoomable from the whole day
  down to seconds. Scrolling or dragging moves through time; the keyboard steps frame by frame.
  Day navigation and the app filter stay; the filter highlights the app's bands rather than hiding
  monitors.
- **Opening a moment** (a click on the stage, or a link from Ask or an episode) shows the stage at
  that exact instant, with the cited monitor enlarged and the cited window outlined. Every moment
  has its own address, so Back returns to it.
- Scrolling must stay smooth with several native frames per moment, and image work must not
  block other API calls (TODO A9 Timeline).
- It follows [UI.md](../UI.md): calm, local assets only, no framework.

### Storage and deletion

Window lists, titles and addresses are captured content: encrypted at rest like everything
else, covered by retention and by "Delete all". Removing screenshots keeps the text and window
lists. Expiring screen text removes each window's text and, as today (D3), the excerpts and
answers that cite it. The window list, with its titles and addresses, is deleted with the frame,
once both its screenshot and its text have expired. Monitor layouts and states follow the screen
retention category.

Native resolution on three or more monitors costs much more disk than today's 1920 px window
frames, on the system drive by default. Extrapolated from short samples with today's WebP
setting: 42 MB per active hour for a 1440p monitor of changing text, 22-64 MB per monitor in a
150 s composite sample (flattered by large painted-over areas), and 650 MB per hour for a 4K
monitor showing video at one frame per second. An hour of normal use is still to be measured;
the user re-decides resolution, format and where the store lives with that number. The spikes
found AV1 video on the GPU encoder about 2 times smaller on video and 5 times smaller on text, at
a tenth of the CPU; but an unchanged monitor costs more as video (a keyframe is larger than a
WebP still), and FFmpeg's NVENC creates a CUDA context in the capture process. Filling the
system drive breaks Windows, not only Akashic. The retention decision (forever, no warning) is
unchanged. When the disk is full, writes fail: capture reports it as a problem, as it does for
other write failures, and never deletes history to make room.

There is no real data yet, so the format changes in place and the store is recreated; nothing
from window capture carries over.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| UIA for every visible window | See "Text" above: cost to the capture loop and the apps, text that was not visible. |
| One OCR pass over the whole monitor | Lines of side-by-side windows interleave; any change anywhere means OCR of everything; attribution by position is weaker. |
| Synchronized snapshots of all monitors | More work at every trigger, heaviest during games, for little information. |
| Keep 1920 px | Small text unreadable when zooming in; the user chose native. |
| Exclude by title, or redact when the address cannot be read | Titles rarely name a bank or a vault and do name harmless videos about them; an address bar being edited, a missing bar and a failed read look the same. |
| Blur, or dim the rest of a crop | Blur can leak text; dimming still shows other windows in evidence for one app. |
| Detect every copy of another window | Impossible in general. The shell's own copy surfaces are a short, known list and are redacted (rule 5); other apps' copies are an accepted exception. |

## Risks and open questions

Measured so far: [window composition](../baselines/2026-11-15-window-composition.md). Each
remaining item is answered on the real PC before the matching part is built; measurements go to a
dated report in [baselines](../baselines/), and the guides keep only the rules.

- **The WGC package.** Python reaches WGC through the `winrt` projections for Graphics.Capture
  and DirectX, which are new packages: the user approves them first (what they do, network calls,
  the alternative).
- **Capture border.** No border appeared for this unpackaged process on Windows 11 build 26200,
  but Microsoft documents turning it off as needing a capability and a prompt. Re-check on each
  Windows update; a border on every captured window would be visible to the user.
- **Undocumented calls.** `HungWindowFromGhostWindow` maps a ghost to its hung window on this
  build; it is exported but undocumented. If it fails, ghosts are redacted.
- **Popups and shell surfaces not yet seen.** Password-manager extension popups, autofill and
  save-password bubbles, the IME and emoji panels, credential prompts, popups drawn by another
  process (WebView2), notifications, elevated windows, taskbar-preview redaction, and an
  unexplained 1,292 marker pixels when the Start key opened PowerToys Command Palette.
- **The title-change hold.** Same-title and in-page navigations, switching to an already-loaded
  tab, titles set late by script, back/forward cache, and Zen.
- **Games, DRM and HDR.** Frame-rate loss in a game running below its frame cap (frame times,
  not frames per second), exclusive fullscreen, protected video, HDR output.
- **Cost on a real day.** Number of windows, VRAM of their captures (+360 MB for 42 small
  windows), CPU of copying frames (13.7% of one core for three busy windows at 10 frames per
  second, under 1% at 1 frame per second), composition time, disk per normal day, and decoding
  several native frames per moment while scrolling the timeline.
- **Monitor identity.** Must survive restarts and replugging; check two identical monitors.

## What this replaces

Rendering of the selected window with PrintWindow, with its hang protection and 1920 px cap;
capturing the window under the cursor, replaced by refreshing the monitor under the cursor; title
exclusions and the title fallback for unreadable addresses. The TODO A9 header lists the items
this makes obsolete.
