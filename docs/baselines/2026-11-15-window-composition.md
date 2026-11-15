# Window composition spike 2026-11-15

Measurements behind the [monitor-capture design](../designs/2026-11-14-monitor-capture.md) (TODO
A9): each monitor's picture is composed from per-window Windows.Graphics.Capture (WGC) captures,
and an excluded window is never captured. Tested on the user's PC with their normal windows, plus
test windows driven by script and browser popups clicked by the user. Pixels and OCR text stayed
in memory; this report holds numbers only. Scripts were throwaway (isolated spike venv,
windows-capture 2.0.1 for WGC, PyAV 19.0.2 for encoding); none is kept, and none of these
packages is approved for the app. An independent refuter and two code reviews went over the
scripts and results; their limits are included below.

## Machine and desk

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01), Windows 11 build 26200, Python
3.14.10, PyAV 19.0.2 (its FFmpeg ships NVENC H.264/HEVC/AV1, SVT-AV1, x264, x265, dav1d, cuvid),
Pillow 13.0.0 (libwebp 1.6.0). Monitors, left to right: 2560x1440 at 100%, 3840x2160 at 125%
(primary), 2560x1440 at 100%. One sample of one desk: a mostly-text day looks like the side
monitors, video like the 4K one.

## Method

- **Compositor.** A tracker lists visible top-level windows in stacking order every 50 ms and
  keeps one WGC capture per kept window (border off, 100-1000 ms minimum interval). Composing a
  monitor paints each window's latest frame bottom to top. An excluded window gets no capture;
  its visible area is painted solid. A window without a frame yet is painted "pending".
- **Ground truth.** A copy of the real screen (DXGI Desktop Duplication) of the same monitor at
  the same moment.
- **Leak detector.** Test windows and a test page are painted with large magenta/orange blocks
  and the words "AKASHIC SECRET 7Q4Z". A leak is magenta-ish pixels (also when tinted or shrunk
  into a thumbnail) or OCR words containing SECRET. The user's own content produced 0-436
  magenta-ish pixels per monitor. The detector misses a marker blended with its orange blocks,
  and real content is only detected through the marker.
- **Exclusion rules modelled.** Marker windows; browser private mode (title marker, or for Chrome
  and Edge the UIA caption, as `browser_caption` does); an excluded address read through UIA;
  password-manager and user-excluded processes. A simplified copy of `exclusions.py`.
- **Input.** Scripted input went only to the test windows. Shell shortcuts went through the
  computer-use tool, browser clicks were made by the user.

## Fidelity

| Case | Composite vs screen |
| --- | --- |
| Static monitor without the taskbar (the two side monitors) | 0.00-0.2% of pixels differ (tolerance 24 levels) |
| Window straddling the 125% and 100% monitors | identical on the 100% side; same marker pixel count on both |
| Monitor with a playing video | differs as much as two screen copies 0.5 s apart (moving content) |
| Taskbar | 98% of its pixels off: WGC returns the bar without its blurred backdrop |
| Fully transparent overlays (NVIDIA overlay, a WebView2 window; layered, alpha 0) | WGC returns solid black; must be skipped, or one on top blacks out the monitor |
| Task View | on screen, absent from the composite |
| A kept app's menu | in the composite within 150 ms (9 of 10 words) |
| A new window | first frame 49-79 ms after its capture starts; 76-297 ms when 8 captures start one after another |
| Dragging at 60 Hz / resizing at 30 Hz | 3.5-5.7% / 9-14% of the monitor off while moving; 0.3% / 0.03% 0.5 s later |
| Restore after minimize | matches 314 ms later |

The composite can show content no longer on screen for a moment: a window listed as visible
whose last frame is stale (a dismissed preview, for example).

## Privacy

Magenta-ish pixels (or SECRET words) of an excluded test window, on the real screen and in the
composite.

| Scenario | On screen | In the composite |
| --- | --- | --- |
| Excluded window alone | 317,971 | 29 (baseline) |
| Opaque kept window over part of it | 176,381 | 18 |
| 50% translucent kept window over it | 176,437 | 18 |
| Capture-excluded kept window (`WDA_EXCLUDEFROMCAPTURE`) over it | 176,437 | 18 (that window gives no frame: pending) |
| Restore animation | 317,960 | 18 |
| Zen private window | 3.87 M | 0 |
| Edge InPrivate, Chrome Incognito | marker on screen | 0 (Chrome's marker is only in the UIA caption, not the title) |
| Proton Pass desktop app | (not looked at) | excluded by process |
| UWP Calculator excluded as a user app | | 100% of its window painted over when judged by its hosted app `calculatorapp.exe`; its frame window belongs to ApplicationFrameHost.exe |

Leaks found, and the rule that closed each:

| Leak | Without the rule | With the rule |
| --- | --- | --- |
| Alt-Tab (`XamlExplorerHostIslandWindow`, explorer.exe) | 28,726 | 0 |
| Snap Assist (same host) | 93,424 | 0 |
| Win+Shift+S overlay (frozen copy of the desk, `XamlWindow` of snippingtool.exe) | 3.71 M | 0 |
| Taskbar thumbnail preview | 25,070 | inconclusive (scenario labels lagged the actions) |
| "Not responding" ghost (class `Ghost`, dwm.exe, no owner) | 175,363 | 24, mapped to its hung window by `HungWindowFromGhostWindow` |
| The excluded app's own Win32 menu (`#32768`, same process, owned) | 8 SECRET words | 0, judged with its app |
| A tool or owned popup of an excluded app, main window visible or minimized | ~148,000 | 0, verdict per process over all its windows, minimized ones included |

- Copy surfaces were redacted only while an excluded window existed. That condition is wrong:
  the snipping overlay is a frozen copy that can outlive the window it shows, so they must be
  redacted always. Not reproduced, because another excluded window stayed open.
- A process-wide popup rule over-redacts browsers: normal and private windows share one process.
  Popups take their owner window's verdict; the process rule is for popups with no owner.
- The Start key opened PowerToys Command Palette here and added 1,292 marker pixels against a
  0-160 baseline, unexplained.
- Copies drawn by other apps (Discord Go Live and OBS previews, Magnifier, Phone Link, Game Bar,
  remote desktop, an app showing a screenshot) are not windows of the shell and are not covered.
  The Claude app showing screenshots of the marker put a steady 46,700 marker pixels into the
  composite during the tests.
- `IncludeSecondaryWindows` (through windows-capture) did not draw a Win32 menu into its owner's
  frame.

### Browser popups (clicked by the user)

Two windows showed the marker page: Chrome, excluded by its address, and a Zen private window. In
each, the user opened the dropdown, typed into a field with suggestions, right-clicked, and
hovered a `title` tooltip.

- **Every popup was its own top-level window** with the popup style and owned by the browser
  window:
  - Chrome: `Chrome_WidgetWin_1`, the dropdown 307x146, suggestions 209x139, the tooltip 147x25,
    the context menu 431x510;
  - Zen: `MozillaDropShadowWindowClass`, four popups.
- **None was judged excluded by its own rules** (no private marker, no address).
- **Without the popup rule they leaked page content into the composite.**
  - The Chrome dropdown leaked 27,344 marker pixels and 4 SECRET words (its options carry the
    page's styling).
  - Chrome's suggestions and tooltip leaked 2 SECRET words each; Zen's popups 2 words.
  - The context menus showed no page content.
- **With the popup rule**, every popup was painted over: 0 marker pixels and 0 SECRET words.
- **The Proton Pass extension popup produced no new window** in the Zen private window. It is
  either not enabled in private windows or was not opened, so it is still untested.

### A page navigating to an excluded address

A kept Chrome window switches to a page whose address is excluded. Opening a URL with
`chrome.exe` adds a tab to the running window. Five runs:

- The title changed 59-64 ms after the switch.
- The first window frame with excluded pixels came at 74-120 ms.
- The address was readable through UI Automation at 69-73 ms. In one run the first read took
  222 ms, and 5 excluded frames arrived before it.

So holding a browser window's frames from its title change until its address is read again
would have stopped all of them here. The margin can be about 10 ms. The hold needs a
title-change event hook, not polling, and a re-read that loops until the address changes.

Not tested: switching to an already-loaded tab, same-title or in-page navigations, titles set
late by script, back/forward cache, Zen.

## Robustness and cost

- **Capture border:** none on screen with the border off for this unpackaged process (26 yellow
  pixels around the window before and during capture; 5,866 with the border on). Microsoft
  documents the border opt-out as needing a capability and a prompt, so this may change.
- **Hung app:** its capture keeps the last frame; composing is not delayed (33 ms).
- **Two captures of the same window** (Akashic plus a screen share, say): both got 58-60 fps.
- **Many windows:** 42 captures of 420x300 windows took +362 MB of VRAM. Frame pools grow with
  window size (a 4K window is 33 MB per buffer). Composing a 1440p monitor with 32 windows took
  128 ms in numpy, 25 ms typically. A verdict pass over every top-level window takes 0.4 ms.
- **Large busy windows:** three windows redrawing at 30 Hz (2560x1400 twice, 3800x2050), capture
  process only (not DWM or the GPU):

  | Minimum interval | Frames/s | Count only | Copy every frame to RAM |
  | --- | --- | --- | --- |
  | 100 ms | 28.6 | 5.0 % of one core | 13.7 % |
  | 1000 ms | 2.9 | 0.2 % | 0.9 % |

- **Game:** DREDGE at 4K, capped at 60 fps, held 60 fps median with per-window capture,
  composing and AV1 NVENC once per second; one second dipped to 48. Weak evidence: the counter
  counted compositor frames in 1 s buckets, and the game had GPU headroom. A GPU-bound game with
  frame times is still needed.

Rules the implementation needs, from the reviews:

- **Verdict flip to excluded:** stop the window's capture and drop its last frame. On a flip
  back, paint nothing older than the flip.
- **Re-judge at every check.** A window judged excluded or unknown at first sight (an empty
  title, a caption not readable yet) must not stay hidden forever.
- **Popups:** an owned popup takes its owner's verdict. An ownerless popup is hidden when its
  process has an excluded or unknown window; a browser popup with no owner always, and so are
  extension, autofill and save-password popups.
- **Browser side windows:** DevTools and picture-in-picture take their browser window's verdict.
- **Unknowns are hidden:** a process that cannot be read, an unresolved UWP frame host, or a
  child window from another process inside a kept window.
- **Same verdict rules as today:** from `exclusions.py`, with D2 marker matching and the full
  process lists.
- **No UI Automation under the composition lock:** reads can take seconds on a hung window.

## Storage and encoding

### Encoders on recorded screen frames

Screen frames recorded at 1 Hz for 120 s, keeping changed frames as the current rule does (4K:
120, right: 9, left: 1; the 4K monitor was playing moving content). Quality is Windows OCR word
recall against OCR of the original frame, plus PSNR. The 4K monitor showed only about 45 words,
so its recall is noisy; the side monitors showed about 510-600 words.

Still images, per frame:

| Encoder | 4K KB | 4K enc ms | 1440p text KB | 1440p enc ms | 1440p OCR recall | Decode ms (4K / 1440p) |
| --- | --- | --- | --- | --- | --- | --- |
| WebP q70 m2 (today's setting, native size) | 180 | 266 | 154-230 | 119-149 | 0.89-0.99 | 96 / 43-58 |
| WebP q70 m0 | 241 | 166 | 174-262 | 79-91 | 0.90-0.99 | 101 / 41-54 |
| WebP q70 m4 | 171 | 741 | 150-224 | 295-378 | 0.90-0.99 | 103 / 38-48 |
| JPEG q85 4:2:0 | 562 | 24 | 342-531 | 9-12 | 0.89-0.99 | 33 / 11-14 |
| JPEG q90 4:4:4 | 912 | 32 | 587-791 | 13-15 | 0.94-0.99 | 42 / 14-18 |
| AVIF q60 speed 10 | 215 | 89 | 178-284 | 40-46 | 0.89-0.99 | 85 / 38-45 |
| PNG level 1 (lossless) | 5113 | 164 | 445-1506 | 41-70 | 1.00 | 85 / 15-33 |

Today's setting costs 266 ms per native 4K frame and 120-150 ms per 1440p frame, on one CPU
thread.

Video, one stream per monitor (no B-frames, one packet per stored frame). NVENC encodes from BGRA
directly; CPU encoders need a CPU colour conversion first. "KB/frame" includes the keyframe.

| Encoder | 4K moving KB/frame | 4K enc wall / CPU ms | 1440p text KB/frame | 1440p CPU ms | 1440p OCR recall | 4K OCR / PSNR |
| --- | --- | --- | --- | --- | --- | --- |
| AV1 NVENC constqp 90 | 93 | 21 / 11 | 28 | 3-5 | 0.99 | 0.97 / 41.3 |
| AV1 NVENC constqp 120 | 63 | 21 / 11 | 24 | 5 | 0.99 | 0.94 / 40.3 |
| AV1 NVENC constqp 150 | 41 | 21 / 10 | 19 | 5 | 0.98 | 0.88 / 38.9 |
| HEVC NVENC qp30 4:2:0 | 56 | 25 / 10 | 28 | 5 | 0.99 | 0.94 / 40.1 |
| HEVC NVENC qp30 4:4:4 (CPU conversion) | 62 | 29 / 45 | 34 | 30 | 0.99 | 0.94 / 42.2 |
| H.264 NVENC qp26 | 102 | 26 / 12 | 27 | 5 | 0.99 | 0.95 / 40.2 |
| SVT-AV1 preset 8 crf35, screen tools | 54 | 20 / 306 | 17 | 127 | 0.99 | 0.96 / 39.4 |
| x265 ultrafast crf26 4:4:4 | 178 | 77 / 338 | 77 | 122 | 1.00 | 0.98 / 45.8 |
| x264 veryfast crf23 4:4:4 | 294 | 59 / 169 | 48 | 57 | 1.00 | 0.98 / 45.4 |

- A keyframe costs more than a WebP still: on an unchanged monitor AV1 NVENC qp90 was 316 KB
  against 230 KB. As intra-only frames, AV1 NVENC is about WebP's size and quality (173 against
  182 KB at 4K, 237-304 against 157-223 KB at 1440p) at a tenth of the CPU; the gain comes from
  frames referring to earlier ones.
- Keyframe interval and random access (AV1 NVENC qp90, 40 frames at 1 Hz): every 10 frames, 122
  KB/frame at 4K and 24 at 1440p; every 30, 118 and 12; every 600, 116 and 6. Decoding up to the
  last frame of a 30-frame group takes 476 ms (dav1d) or 159 ms (cuvid) at 4K, 61 or 104 ms at
  1440p.
- Three AV1 NVENC sessions (4K + 2 x 1440p) take about 1.0-1.2 GB of VRAM; opening one takes
  90-150 ms. FFmpeg's NVENC fed from system memory creates a CUDA driver context, which the
  "capture loads no CUDA" rule forbids today.

### On real composites

150 s of composites, keeping a frame only when a window on that monitor sent a new frame and the
picture changed. On quiet monitors 121-136 of 150 checks skipped composing because no window had
sent anything. Private test windows covered much of two monitors, which flatters every encoder.

| Per monitor | WebP q70 m2 (today) | AV1 NVENC intra | AV1 NVENC, keyframe every 60, qp90 | qp120 |
| --- | --- | --- | --- | --- |
| left (5 frames kept) | 22 MB/h | 30 MB/h | 7 MB/h | 5 MB/h |
| centre 4K (15) | 30 MB/h | 22 MB/h | 3 MB/h | 2 MB/h |
| right, active (24) | 64 MB/h | 90 MB/h | 7 MB/h | 6 MB/h |

Encrypting each packet with AES-GCM costs under 0.3 ms.

### One hour, extrapolated

| Monitor | Stored frames/h | WebP q70 m2 | AV1 NVENC qp90, long keyframe interval |
| --- | --- | --- | --- |
| 4K with moving content, one frame per second | 3600 | 650 MB | 335 MB |
| 1440p text, changing every ~13 s | ~270 | 42 MB | 8 MB |
| 1440p unchanged | ~1 | 0.2 MB | 0.3 MB |

An hour of normal use, with OCR of stored frames, is the number to decide on.

## Open

- The Proton Pass extension popup, autofill and save-password bubbles, IME and emoji panels,
  credential prompts, WebView2 popups, notifications (the test toast did not appear), elevated
  windows, taskbar-preview redaction.
- The title-change hold beyond new tabs (see above).
- A game running below its frame cap, exclusive fullscreen, DRM video, HDR.
- A normal day: windows, capture VRAM, frame-copy CPU, composition time, disk.
