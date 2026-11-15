# Screen capture and encoding spike 2026-11-15

**Archived.** Whole-monitor capture was dropped in favour of per-window composition on
2026-11-15. S1, S1b and the rules quoted below refer to the 2026-11-14 draft of the
monitor-capture design.

Exploratory spike for TODO A9 S1/S1b, before the method is chosen. It compares
whole-monitor grabs, per-window capture composed into a monitor image, and still-image versus
video encoding, on the user's PC with their normal windows open. Pixels and OCR text stayed in
memory; this report holds numbers only. The scripts were throwaway, in an isolated spike venv
(dxcam 0.3.0, windows-capture 2.0.1, winrt Graphics.Capture projections); none is kept, and
none of these packages is approved for the app.

## Machine and desk

Ryzen 7 7800X3D, 32 GB RAM, RTX 4080 Super (driver 621.01), Windows 11 build 26200, Python
3.14.10, PyAV 19.0.2 (its FFmpeg ships NVENC H.264/HEVC/AV1, SVT-AV1, x264, x265, dav1d,
cuvid), Pillow 13.0.0 (libwebp 1.6.0, AVIF available).

Monitors, left to right: 2560x1440, 3840x2160 (primary), 2560x1440, 15.7 MP in all
(63 MB as raw BGRA). Nine visible top-level windows; per monitor one or two windows with
visible pixels plus the taskbar and the desktop. During the 120 s recording the 4K monitor was
playing moving content (every 1 s sample changed; median 62% of 64 px tiles changed), the right
monitor showed text that changed 9 times (median 1% of tiles), the left one did not change.
That is one sample of one desk: a mostly-text day will look like the side monitors, video like
the 4K one.

## 1. Whole-monitor grab

Median wall time per grab, 15 grabs each, process CPU at Windows' 15.6 ms resolution.

| Method | 1440p | 4K | Notes |
| --- | --- | --- | --- |
| GDI BitBlt | 16.6 ms | 35.9 ms (p90 41) | CAPTUREBLT changes nothing measurable. Cost is waiting on DWM. |
| DDA (persistent duplication, copy to RAM) | 2.3 ms | 4.8 ms | An unchanged monitor answers "no new frame" in 0.2 ms. |
| WGC monitor, one-shot session | 65 ms | 72 ms | Session start dominates; one-shot is the wrong use. |
| WGC monitor, 3 persistent sessions at 1 Hz | 0.3% of one core in all | | Frames arrive only when something was presented. |

- DDA and WGC both give a frame time (DDA `LastPresentTime`, WGC `SystemRelativeTime`), which
  is what the "picture newer than the verdicts" rule needs. Not yet validated against a window
  listing.
- With the border turned off, no capture border appeared on screen for this unpackaged
  process (edge pixels of the side monitors identical with sessions running and stopped).
- DWM's composition counters (`DwmGetCompositionTimingInfo`) did not move on this build, so
  they can't stand in for game frame-rate loss. **Game impact is unmeasured** and needs a
  real game.

## 2. Per-window capture composed into the monitor

Every visible window captured on its own, then painted bottom-to-top in z-order into a monitor
canvas, compared with a DDA grab of the same monitor (pixels differing by more than 24 levels).

| Method | Per monitor | Composite vs real screen |
| --- | --- | --- |
| PrintWindow (today's renderer) for every visible window | 117-146 ms; 10-40 ms per window, the desktop window (19 MP, spans all monitors) 80-110 ms | 0.0-0.2% on the two static monitors |
| WGC per window, 8 persistent sessions | compose 3-6 ms; sessions 1.2-3.3% of one core (with a copy of every frame) | 0.0% on the two static monitors |

The 4K monitor differed by 17-30%, but two DDA grabs of it 0.5-1 s apart already differ by
31-44% (moving content), so this measures time, not composition error.

- A window that does not change gets no new WGC frame (1-2 frames in 10-20 s); changing
  windows get one per minimum interval.
- Starting 8 window sessions one after another: first frames after 76-297 ms. A new window, menu
  or tooltip is missing from the composite until its session delivers.
- Not measured: whether DWM-drawn copies (taskbar thumbnails, Alt-Tab, Task View) appear in
  any window's WGC capture; menus and tooltips; shadows and acrylic backdrops; a hung app's
  window; exclusive-fullscreen games; protected video.

## 3. Encoding

Recorded with DDA at 1 Hz for 120 s, keeping changed frames as the current rule does (4K: 120,
right: 9, left: 1). Quality is Windows OCR word recall against OCR of the original frame
(same engine, multiset of words) plus PSNR. The 4K monitor showed only about 45 words, so its
recall is noisy; the side monitors showed about 510-600 words.

### Still images, per frame

| Encoder | 4K KB | 4K enc ms | 1440p text KB | 1440p enc ms | 1440p OCR recall | Decode ms (4K / 1440p) |
| --- | --- | --- | --- | --- | --- | --- |
| WebP q70 m2 (today's setting, native size) | 180 | 266 | 154-230 | 119-149 | 0.89-0.99 | 96 / 43-58 |
| WebP q70 m0 | 241 | 166 | 174-262 | 79-91 | 0.90-0.99 | 101 / 41-54 |
| WebP q70 m4 | 171 | 741 | 150-224 | 295-378 | 0.90-0.99 | 103 / 38-48 |
| JPEG q85 4:2:0 | 562 | 24 | 342-531 | 9-12 | 0.89-0.99 | 33 / 11-14 |
| JPEG q90 4:4:4 | 912 | 32 | 587-791 | 13-15 | 0.94-0.99 | 42 / 14-18 |
| AVIF q60 speed 10 | 215 | 89 | 178-284 | 40-46 | 0.89-0.99 | 85 / 38-45 |
| PNG level 1 (lossless) | 5113 | 164 | 445-1506 | 41-70 | 1.00 | 85 / 15-33 |

Today's setting costs 266 ms per native 4K frame and 120-150 ms per 1440p frame, all of it on
one CPU thread.

### Video, one stream per monitor (no B-frames, one packet per stored frame)

NVENC encodes from BGRA directly (colour conversion on the GPU); CPU encoders need a CPU
conversion first. "KB/frame" is the stream size divided by stored frames, keyframe included.

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

A single-frame stream is just a keyframe: on the unchanged left monitor AV1 NVENC qp90 was 316
KB against 230 KB for WebP (OCR 0.94 against 0.91).

Same frames as AV1 NVENC **intra only** (each frame independent, like a still), 3 frames per
monitor:

| | 4K KB | 1440p text KB | 1440p OCR recall |
| --- | --- | --- | --- |
| WebP q70 m2 | 182 | 157-223 | 0.89-0.98 |
| AV1 intra qp90 | 173 | 237-304 | 0.93-0.99 |
| AV1 intra qp120 | 141 | 204-250 | 0.90-0.99 |

So AV1 intra is about WebP's size and quality at a tenth of the CPU. The size gain comes from
frames referring to earlier ones.

### Keyframe interval and random access (AV1 NVENC qp90, 40 frames at 1 Hz, unchanged frames included)

| Keyframe every | 4K KB/frame | 1440p text KB/frame | Decode up to the last frame of a GOP, 4K: dav1d / cuvid | 1440p: dav1d / cuvid |
| --- | --- | --- | --- | --- |
| 1 (intra) | 196 | 239 | 22 / 99 ms | 24 / 79 ms |
| 10 | 122 | 24 | 174 / 108 ms | 38 / 85 ms |
| 30 | 118 | 12 | 476 / 159 ms | 61 / 104 ms |
| 600 | 116 | 6 | 620 / 187 ms (40 frames) | 74 / 111 ms |

cuvid includes about 80 ms of decoder start; sequential playback costs about 15 ms (dav1d) or
3 ms (cuvid) per 4K frame, 1-3 ms per 1440p frame.

### GPU resources

- Three AV1 NVENC sessions (4K + 2 x 1440p) take about 1.0-1.2 GB of VRAM in all, about 0.4 GB
  for the first one (`surfaces=1` saves about 0.2 GB in all). Opening a session takes 90-150 ms.
  FFmpeg's NVENC fed from system memory creates a CUDA driver context. This conflicts with the
  "capture loads no CUDA" rule.
- Encoding the three monitors in parallel: about 20 ms per frame per monitor, 25 ms of CPU per
  full desk.

## 4. What one hour costs on disk (extrapolated, not measured)

Per-frame sizes above times this sample's change rate, before encryption overhead:

| Monitor in this sample | Stored frames/h | WebP q70 m2 | AV1 NVENC qp90, long GOP |
| --- | --- | --- | --- |
| 4K with moving content, one frame per second | 3600 | 650 MB | 335 MB |
| 4K with moving content, one frame per 3 s | 1200 | 216 MB | not measured (P-frames grow with the gap; at most the intra size, 173 KB = 208 MB) |
| 1440p text, changes every ~13 s | ~270 | 42 MB | 8 MB |
| 1440p unchanged | ~1 | 0.2 MB | 0.3 MB |

## Open

- Game frame-rate loss for DDA, WGC and NVENC (a real game, `docs/TOOLING.md` real-data rules).
- DDA and WGC with HDR, protected video and exclusive fullscreen; DDA access loss on mode
  changes and the secure desktop.
- Per-window WGC: DWM-drawn copies, menus, tooltips, shadows, acrylic, hung apps, session
  churn over a normal day.
- Video storage: P-frame size with the real capture schedule (a frame every few seconds,
  input bursts); deletion and retention at GOP granularity; encrypted packet storage.
