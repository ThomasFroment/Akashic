# How should a screen recorder decide a frame changed, so a one-word reply is kept and a blinking caret is not?
Date: 2026-11-19 · Context: Python 3.14, numpy, Pillow, winrt projections, Windows 11 build 26200; Akashic `capture/imaging.py` (`signature`, `differs`), per-window Windows.Graphics.Capture (WGC) composed per monitor; monitors 3840x2160 at 125 % and 2560x1440 at 100 % · Goal: keep a frame when a small meaningful change appears (audit #208, M1.1) without storing one per caret blink · Ruled out: none

## Answer
Akashic's test misses every one-word reply and badge: on synthetic 1440p and 4K frames
`differs` returned False for all 40 such changes and True only for the 4 short sentences, because
1e-4 of the frame is 24 reduced pixels at 1440p and 52 at 4K [1][2]. No pixel count fixes this on
both monitors: across sub-pixel placements a 4K/125 % caret moves 6-12 reduced pixels while a
13 px "ok" moves as few as 9 [2][3]. Shape separates them: the caret's moved area is 1-2 reduced
pixels wide in every placement, every word and badge at least 3 wide and 3 high [2]. The
candidate: keep the hash early exit and the 1/4 gray frame, and call a frame different when, in
some 16x16 tile, the moved pixels span at least 3 columns and 3 rows, over four tile grids offset
by (0,0), (0,8), (8,0) and (8,8); with only two grids a word cut across tile edges was missed in up
to 80 of 4096 placements, with four in none, and the caret never fired [3]. Apply it to the OCR
gate's comparison too, or a kept frame still carries the old text [3]. It costs 2.2-2.6 ms per
4K compare against 1.9 ms today [3]. Surveyed recorders use global scores blind to a word and
cover small changes with input triggers, a 30 s idle capture and an exact hash of the text crop
[4][5][6][7][8].

## Options
| Test | One-word reply | Caret blink | Cost per 4K frame | Fit |
| :- | :- | :- | :- | :- |
| Today: > 1e-4 of reduced pixels moved > 16 levels | missed 40/40 [2] | ignored [2] | compare 1.53 ms + signature 6.7 ms [2] | in place |
| Per-tile count ≥ 6 | caught (min 6 at 1440p, 9 at 4K) [2] | counted at 4K (6-12) [3] | 1.69 ms [2] | refuted [3] |
| Per-tile span ≥ 3 x 3, four offset grids (candidate) | caught in 4096/4096 placements [3] | ignored in 4096/4096 [3] | 2.55 ms (2.22 ms with two grids) [3] | numpy only |
| Global histogram (screenpipe, Hellinger > 0.05 at 1/4) | missed (score barely moves) [4][5] | ignored | low | blind to small text |
| Global SSIM ≥ 0.9 drop (OpenRecall) | missed [7] | ignored | full resolution | blind to small text |
| ORB match ratio > 0.7 skip (Windrecorder) | missed [8] | ignored | ORB at full resolution | needs OpenCV |
| Exact hash of the text crop (screenpipe OCR gate) | caught [6] | caught (any pixel) | hash only | gates OCR, not storage |
| WGC dirty regions | region of what the app redrew [9] | reported (4x24 rect) [9] | free, from the OS | Windows 11 24H2+ (inferred) [10] |

## Findings
### Akashic today
- `signature` is a blake2b digest of the frame reduced 4x to grayscale; `differs` returns False on
  equal digests, otherwise True when more than 1e-4 of pixels moved by more than 16 levels:
  above 23.04 pixels at 2560x1440 and 51.84 at 3840x2160 [1].
- On synthetic page frames (dark and light), "ok", "gg", "lol" at 13-16 CSS px and a 16 px badge
  were missed in all 40 cases; the 4 short sentences were detected; the caret was ignored [2].
- Across all 16 sub-block placements per case: at 1440p the caret moved 5 pixels, 1 wide, words
  6-18, at least 3 wide and 3 high; at 4K the caret moved 6-12 pixels, 1-2 wide, words 9-28, at
  least 4 wide and 3 high; badges at least 4x4 [2].
- `differs` also gates OCR on window crops (`ocr_gate.py:77-80`). On a "same" verdict the gate
  returns the previous text with the new signature, and the writer stores it
  (`ocr_gate.py:62-63`, `screen_writer.py:285-286, 359-365`), so on a large OCR-read window a
  one-word reply can be in the stored image but not in the search text; on a small crop the 1e-4
  rule fires on 2 moved pixels [3].
- A refuter's sweep over all 64x64 full-resolution placements (every tile alignment) found the
  two-grid form, (0,0) and (8,8), missing one-word replies in 10-80 of 4096 placements, and the
  four-grid form missing none, with the caret firing in 0 of 4096 at both scales and themes [3].
### Other recorders
- screenpipe hashes the 1/4 thumbnail as an early exit, else scores a Hellinger histogram distance;
  factor 6 was dropped because it made scrolling dense log pages invisible, and SSIM was dropped
  for CPU [4].
- screenpipe's legacy loop skips below 0.02 unless 10 s passed; the event-driven loop triggers
  above 0.05 checked every 3000 ms, plus click, keystroke and clipboard triggers, a 200 ms
  minimum interval and a 30 s idle capture; a comment admits a slide can sit uncaptured when the
  visual detector does not trip [5].
- screenpipe's OCR gate crops to the padded union of text regions in the focused window and
  compares an exact hash of (width, height, BT.601 gray >> 3); "deliberately NO other
  heuristics", since geometry fingerprints, stability waits and region-scoped hashes skipped wrongly [6].
- OpenRecall drops a frame whose global (unwindowed) SSIM with the previous one is at least 0.9 [7].
- Windrecorder captures every 3 s and skips when the ORB match ratio exceeds 0.7, or when OCR text
  similarity exceeds 0.7 [8].
### OS change signals
- With `DirtyRegionMode` REPORT_ONLY on a WGC capture of a Tk window: no frame arrived while idle;
  a one-word change reported one rect covering the whole 502x32 label, a badge 20x20, a 2 px
  caret 4x24 [9]. Dirty regions follow what the app redrew, not which pixels changed.
- The `GraphicsCaptureDirtyRegionMode` page lists ReportOnly (0) and ReportAndRender (1) without
  describing them and is versioned only for build 26100 and later [10].
- Desktop Duplication reports dirty and move rects that may include unmodified pixels when
  coalesced, and a pointer-only update has zero AccumulatedFrames and LastPresentTime [11].

## Caveats
- All pixel numbers come from Pillow-drawn grayscale text; ClearType, video, animation and
  scrolling are not represented, and the false-trigger rate on real screens is unmeasured [2][3].
- The four-grid result and the span timing come from the refuter's in-memory runs, not a
  committed script; the committed spike measures the bounding box of one change [2][3].
- A change of the same shape that is not meaningful counts as a change: a seconds timer, a spinner
  or a seconds clock would store a frame at every check (5 s on the focused monitor, 15 s on the
  others); the default taskbar clock adds about one frame per minute per monitor; a playing video
  already differs today; a block cursor (opt-in) was not measured [3].
- The WGC dirty-region availability (UniversalApiContract 19, build 26100) is inferred from the
  page's versioning, not stated [10]; the spike ran on one Tk window, and its test window took
  focus, which explains one full title-bar redraw in the data [9].
- OpenRecall was read through a fetch summary, not line by line [7].

## Sources
[1] Akashic change test — `src/akashic/capture/imaging.py:26-62` — tier 1 — 2026-11-19
[2] Small-change spike (synthetic 1440p and 4K frames, placement sweep, 4K timing) — `eval/quality/spikes/change_detection_small.py`, output `eval/quality/spikes/change_detection_small.json` — tier 1 — target PC, 2026-11-19
[3] Refuter analyses and in-memory sweeps (per-tile count, 64x64 placement sweep, span timing); OCR gate and check intervals — `src/akashic/capture/ocr_gate.py:62-63, 77-80`, `src/akashic/capture/screen_writer.py:285-286, 359-365`, `src/akashic/capture/screen.py:80-81, 519-541`, `src/akashic/capture/desk.py:267-268` — tier 1 — 2026-11-19
[4] screenpipe `frame_comparison.rs` — `_resources/references/screenpipe/crates/screenpipe-screen/src/frame_comparison.rs:76-85, 378-379` (https://github.com/screenpipe/screenpipe) — tier 1 — clone, untagged, 2026-11-19
[5] screenpipe capture loops — `_resources/references/screenpipe/crates/screenpipe-screen/src/core.rs:171-334`, `crates/screenpipe-engine/src/event_driven_capture.rs:556-618, 1530, 1960` — tier 1 — clone, 2026-11-19
[6] screenpipe OCR gate — `_resources/references/screenpipe/crates/screenpipe-capture/src/ocr_gate.rs:5-43`, `crates/screenpipe-screen/src/text_regions.rs:124-135` — tier 1 — clone, 2026-11-19
[7] OpenRecall `screenshot.py` — https://raw.githubusercontent.com/openrecall/openrecall/main/openrecall/screenshot.py — tier 1 — main, 2026-11-19
[8] Windrecorder `capture.py`, `record.py:521`, `config_default.json:94` — https://github.com/yuka-friends/Windrecorder — tier 1 — main, 2026-11-19
[9] WGC dirty-region spike — `eval/quality/spikes/wgc_dirty_regions.py`, output `eval/quality/spikes/wgc_dirty_regions.json` — tier 1 — target PC, 2026-11-19
[10] GraphicsCaptureDirtyRegionMode, Direct3D11CaptureFrame.DirtyRegions — https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturedirtyregionmode, https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.direct3d11captureframe.dirtyregions — tier 1 — accessed 2026-11-19
[11] DXGI_OUTDUPL_FRAME_INFO — https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/ns-dxgi1_2-dxgi_outdupl_frame_info — tier 1 — 2024-02-22
