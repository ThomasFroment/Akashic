# OCR, decode and QUNS cost spike 2026-11-17

Measurements behind TODO A9 ("OCR time of whole windows on 1440p and 4K; decode time of several
frames per moment", F12) and the open cost question of OPEN A4 #150 (what
`SHQueryUserNotificationState` costs per call, cached for 1 s): how long Windows OCR takes on a
whole window and on the live gate's crop, what a deferred `ocr_window` job costs, what it costs to
turn stored frames back into thumbnails for one moment, how WebP compares with AV1 NVENC per frame,
and what the QUNS call costs. Production code ran unchanged (`WindowsOcr`, `OcrGate`,
`OcrWindowHandler`, `imaging.encode_webp`, `FrameStore`, `MonitorLog`, `monitors.stage_at`,
`api.frames._render`, `Win32Windows`); only the split timings copy a function's steps inline.

Privacy and scope. Every image was drawn in memory from synthetic words and shapes; no window,
screen frame or WGC capture was used, no window was opened and no input was sent. The user's data
folder (`%LOCALAPPDATA%\Akashic`) was never read or opened. The only store was a disposable one
(`Paths(<scratchpad>\f12-store)`, `DataKey.generate()`, `db.connect` + `db.migrate`) built under the
session scratchpad and deleted at the end of the run. This report holds numbers, counts, booleans
and labels only: no OCR text, not even the synthetic text. The script was throwaway and is not
kept; no file under `src/`, `tests/`, TODO.md, OPEN.md or the guides changed. Measurement only:
video compaction waits until disk usage demands it.

## Machine

AMD Ryzen 7 7800X3D (8 cores, 16 threads), 32 GB RAM (31.7 GB), RTX 4080 Super (driver 621.01),
Windows 11 build 26200, Python 3.14.10, Pillow 13.0.0 (libwebp 1.6.0), PyAV 19.0.2 (FFmpeg
version info 9.0.2; libavcodec 63.1.102). `av1_nvenc`, `libdav1d` and `av1_cuvid` all exist in this
PyAV build and all opened.

- **OCR.** Installed recognizer languages: `en-US` only. `WindowsOcr("fr")` (production default
  `ocr_language = "fr"`) logs "not installed" and resolves to `en-US`.
  `OcrEngine.max_image_dimension` is 10000.
- **Monitors, left to right:** 2560x1440 at 100%, 3840x2160 at 125% (primary), 2560x1440 at 100%.
- **Other load.** Running `akashic` processes (by process name): 0 at the start and 0 at the end.
  Other agents' spikes or tests ran at the same time (a browser spike was known to run). Total
  machine CPU, sampled every 0.5 s during the final run (this process uses about one core,
  6% of 16 threads, and three threads in the threaded decode cells):

  | Phase | samples | mean % | max % |
  | --- | --- | --- | --- |
  | OCR cells | 267 | 28.5 | 64.6 |
  | Deferred OCR | 57 | 26.3 | 37.7 |
  | Decode | 390 | 40.6 | 81.1 |
  | Encoders | 167 | 42.3 | 59.7 |
  | QUNS | 119 | 34.4 | 51.3 |

  An earlier full run of the same script (it differed only in the 4K moving picture, which then
  had no per-frame noise, and in the recall crops) sampled 14.7 / 13.5 / 28.6 / 31.5 / 24.1% mean
  and gave medians within about 5% of the ones below (1440p dense `recognize` 260 against 273 ms,
  4K dense 404 against 414 ms). The load varies by tens of points between runs, so read the
  timings as a loaded-desk sample, not a quiet-machine floor. The noise was added to make the
  moving picture closer to video, and the left-column recall was added because whole-image OCR
  of the noisy 4K frame was erratic.

## Method

- **Content.** Words drawn from a fixed mixed English/French list of 89 words (31 accented, about
  32-37% of the words drawn; letters such as é, è, à, ô, ç), drawn word by word with Segoe UI (light theme) and Consolas
  (dark theme, monospace) from `C:\Windows\Fonts`, 14 px at 1440p and 18 px at 4K ("125% scale").
  Text sits in one column 1100 px (1440p) or 1450 px (4K) wide, in paragraphs, between a title bar,
  a status bar and a sidebar with icon squares. Content kinds: dense light, dense dark monospace,
  sparse (10 lines), empty (the bars plus three shapes, no text).
- **Repetitions.** 2 warm-up runs, then 20 measured runs per OCR cell, 10 per deferred-job cell,
  32 moments per decode cell (after 2 warm-up moments) and 60-frame sequences for encoding. Cells
  run interleaved (every run goes through all cells; encoders alternate order per frame).
  Statistics are n, median, p95 (nearest rank) and max; time cells are ms, QUNS cells are
  microseconds. A cold first call is reported on its own below. The decoder-open rows come from a
  separate short run (see "Decoder open"), not from the main run.
- **Recall.** Drawn words counted as a multiset against the OCR text's words (case folded).
  "Accented" is the drawn words with a non-ASCII letter; "accent-folded" compares them with
  accents removed on both sides. Computed in memory, only counts were kept.
- **Splits copy production steps inline.** The preparation split is the RGBA conversion, BGRA
  swizzle and `SoftwareBitmap.create_copy_from_buffer`; "recognize_async" is
  `run_until_complete(recognize_async)`; "post" is grouping words into blocks and `render_blocks`.
  The inline text equalled the unchanged `recognize` text in every cell. The gate split calls
  `find_text_box`, `crop` and `signature` and then `recognize` on the crop, as `OcrGate.read` does.

## OCR of whole windows

Every cell n = 20; values are median / p95 / max in ms.

### The unchanged `WindowsOcr.recognize` on the whole window

| Window | Total | Preparation | `recognize_async` | Post (blocks, text) |
| --- | --- | --- | --- | --- |
| 1440p dense light | 272.6 / 278.3 / 285.6 | 32.6 / 36.1 / 36.8 | 233.4 / 241.0 / 255.6 | 2.4 / 3.1 / 4.0 |
| 1440p dense dark mono | 254.3 / 286.2 / 296.1 | 30.9 / 35.5 / 35.9 | 218.7 / 223.7 / 255.8 | 2.2 / 3.1 / 3.2 |
| 1440p sparse | 76.3 / 79.5 / 80.5 | 32.7 / 36.1 / 37.2 | 43.8 / 46.0 / 48.9 | 0.5 / 1.5 / 1.7 |
| 1440p empty | 57.4 / 59.8 / 59.9 | 32.4 / 37.6 / 43.8 | 24.2 / 27.3 / 28.5 | 0.0 / 0.0 / 0.0 |
| 4K dense light | 414.3 / 425.4 / 427.6 | 75.0 / 84.1 / 86.2 | 334.7 / 363.0 / 367.5 | 2.8 / 3.8 / 4.6 |
| 4K dense dark mono | 369.3 / 380.4 / 412.8 | 78.3 / 85.5 / 85.7 | 291.2 / 327.2 / 356.7 | 2.6 / 3.1 / 3.3 |
| 4K sparse | 151.7 / 171.7 / 175.9 | 77.5 / 82.0 / 94.6 | 73.9 / 82.9 / 89.8 | 0.3 / 0.7 / 0.7 |
| 4K empty | 131.3 / 136.6 / 141.6 | 77.5 / 81.2 / 84.6 | 51.6 / 54.0 / 55.4 | 0.0 / 0.0 / 0.1 |

- Preparation is a per-pixel cost independent of content: about 31-33 ms at 1440p and 75-78 ms at
  4K, which is 59% of an empty 4K window's total and 12-21% of a dense one's.
- No 4K image was shrunk: `max_image_dimension` is 10000, so the thumbnail branch of `recognize`
  never ran at 3840x2160.

### The live path `OcrGate.read` (new key each time)

The gate runs OCR on the crop of the cells that look like text. Median / p95 / max ms, n = 20.

| Window | `read` total | `find_text_box` | crop | signature | OCR on the crop | Reuse path (unchanged text) | Crop area | OCR ran |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1440p dense light | 252.3 / 257.3 / 264.7 | 7.1 / 7.8 / 9.0 | 1.0 / 1.3 / 1.3 | 1.4 / 2.0 / 2.2 | 241.4 / 253.5 / 255.5 | 10.0 / 11.6 / 12.0 | 48% | yes |
| 1440p dense dark mono | 234.3 / 240.5 / 281.0 | 6.7 / 7.5 / 8.6 | 1.0 / 1.3 / 1.4 | 1.4 / 1.7 / 1.8 | 226.0 / 240.1 / 246.3 | 9.4 / 11.3 / 11.3 | 48% | yes |
| 1440p sparse | 34.5 / 37.5 / 37.7 | 6.5 / 7.7 / 8.0 | 0.4 / 0.4 / 0.6 | 0.3 / 0.5 / 1.6 | 26.6 / 28.6 / 29.0 | 7.6 / 8.3 / 8.4 | 9% | yes |
| 1440p empty | 26.9 / 28.7 / 35.4 | 6.4 / 7.7 / 7.8 | 0.5 / 0.7 / 0.7 | 0.8 / 1.1 / 1.2 | 16.9 / 18.8 / 21.6 | 8.5 / 10.7 / 11.8 | 29% | yes |
| 4K dense light | 357.9 / 386.3 / 395.1 | 15.5 / 18.1 / 22.1 | 1.9 / 2.3 / 2.3 | 2.8 / 3.1 / 3.4 | 335.3 / 342.9 / 351.7 | 21.3 / 22.8 / 27.7 | 41% | yes |
| 4K dense dark mono | 311.7 / 315.9 / 401.8 | 16.0 / 19.8 / 20.4 | 2.1 / 2.6 / 2.7 | 2.7 / 3.4 / 3.6 | 289.3 / 353.8 / 363.4 | 21.0 / 22.9 / 26.3 | 41% | yes |
| 4K sparse | 45.8 / 48.5 / 52.7 | 15.9 / 18.7 / 18.9 | 0.6 / 0.7 / 0.7 | 0.4 / 0.8 / 1.7 | 29.0 / 30.5 / 31.2 | 16.5 / 19.1 / 20.2 | 4% | yes |
| 4K empty | 34.6 / 35.9 / 36.0 | 15.7 / 17.0 / 17.1 | 0.6 / 0.7 / 1.0 | 0.9 / 1.1 / 1.2 | 16.2 / 17.9 / 18.2 | 17.1 / 18.3 / 19.0 | 13% | yes |

- The reuse path (same key, committed result, same pixels) never ran OCR; it costs what
  `find_text_box` + crop + signature cost: 8-10 ms per window at 1440p and 16-21 ms at 4K.
- The "empty" window is not empty to `find_text_box`: its sidebar and shape borders are vertical
  edges and count as text-like cells, so the gate still ran OCR on a 13-29% crop and found no
  word.
- On a dense window the gate's crop (48% or 41% of the area) saves about 20 ms at 1440p and
  56-58 ms at 4K against OCR of the whole window; on a sparse one it is 2.2 times cheaper at
  1440p (34.5 against 76.3 ms) and 3.3 times at 4K (45.8 against 151.7 ms).

### Word recall (synthetic text, `en-US` recognizer)

| Window | Words drawn | Recall, whole window | Recall, gate crop | Accented words drawn | Accented, exact | Accented, accent-folded | OCR words out | Text chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1440p dense light | 1451 | 79.2% | 79.2% | 532 | 43.2% | 94.5% | 1437 | 9859 |
| 1440p dense dark mono | 1146 | 80.5% | 80.5% | 400 | 44.0% | 93.5% | 1140 | 7769 |
| 1440p sparse | 140 | 82.1% | 82.1% | 46 | 45.7% | 91.3% | 137 | 927 |
| 1440p empty | 0 | n/a | n/a | 0 | n/a | n/a | 0 | 0 |
| 4K dense light | 1840 | 82.0% | 82.0% | 612 | 45.8% | 93.8% | 1840 | 12431 |
| 4K dense dark mono | 1437 | 78.6% | 78.6% | 517 | 40.4% | 95.6% | 1414 | 9612 |
| 4K sparse | 140 | 81.4% | 81.4% | 45 | 42.2% | 93.3% | 140 | 949 |
| 4K empty | 0 | n/a | n/a | 0 | n/a | n/a | 0 | 0 |

- Derived from these counts, the recall on the words without accents is about 100% in every cell;
  the whole loss is on accented words: most (46-55 points) are read without their accent, and
  4-9% are misread in other ways (exact 40-46%, accent-folded 91-96%). The French recognizer is not installed, so this is the `en-US` recognizer reading
  French words.
- The gate crop lost no word against the whole window in any cell.
- The stored text was never cut by `MAX_TEXT_CHARS` (20,000): the largest text was 12,431
  characters, and the uncapped recall equalled the capped recall in every cell.

## Deferred OCR: `OcrWindowHandler.run`

Whole-window captures were written by `FrameStore.add` through `NewWindow.capture_webp` (WebP from
`imaging.encode_webp`, q70 m2, native size), one capture and one `ocr_window` job per run, and
the handler ran unchanged (`Job(id, "ocr_window", key, {"window_id": id}, data_ms, 1)`). A run
covers decrypting the capture, decoding the WebP, OCR and `finish_capture` (database write and
file delete). n = 10 per cell; median / p95 / max in ms.

| Window | Time per job | Stored WebP KB | Recall on the stored copy | Recall on the original |
| --- | --- | --- | --- | --- |
| 1440p dense light | 325.1 / 333.3 / 333.3 | 271 | 79.0% | 79.2% |
| 1440p dense dark mono | 302.9 / 311.6 / 311.6 | 219 | 80.5% | 80.5% |
| 1440p sparse | 112.8 / 131.6 / 131.6 | 32 | 81.4% | 82.1% |
| 1440p empty | 91.6 / 97.2 / 97.2 | 11 | n/a | n/a |
| 4K dense light | 531.7 / 543.5 / 543.5 | 523 | 81.7% | 82.0% |
| 4K dense dark mono | 464.0 / 480.8 / 480.8 | 428 | 78.6% | 78.6% |
| 4K sparse | 225.0 / 245.5 / 245.5 | 55 | 81.4% | 81.4% |
| 4K empty | 200.0 / 215.4 / 215.4 | 19 | n/a | n/a |

- A job costs the whole-window `recognize` plus 34-53 ms at 1440p and 69-117 ms at 4K (decrypt,
  WebP decode, database write); the overhead grows with the stored size. At these medians 1,000
  dense 4K windows take about 8-9 minutes of wall time for one worker.
- The stored copy reads as well as the original: at most 0.7 points less.

## Decode per moment

On the disposable store: three monitors recorded with `MonitorLog.present` (2560x1440,
3840x2160, 2560x1440), 34 moments (2 warm-up, 32 measured) of one frame per monitor written by
`FrameStore.add` (WebP q70 m2; the 4K frame holds text over a moving noisy picture; the content of the 1440p frames was not
recorded, and their 217 KB median is smaller than the 279 KB of the encoder section's dense text
frames, so they are lighter than those). Stored sizes:
1440p median 217 KB, 4K median 668 KB. Frames were first-view costs: no `services.thumb` cache,
but the files had just been written, so the OS file cache was warm. Median / p95 / max in ms.

| Cell | 1440p (n = 64) | 4K (n = 32) |
| --- | --- | --- |
| `FrameStore.image` (decrypt only) | 6.8 / 10.0 / 108.9 | 6.5 / 8.5 / 9.4 |
| Full WebP decode (`Image.open` + `load`) | 42.9 / 46.2 / 51.2 | 113.2 / 127.0 / 129.5 |
| `_render` w = 480, no crop | 68.8 / 76.0 / 105.0 | 144.5 / 158.3 / 231.0 |
| `_render` w = 960, no crop | 129.5 / 138.4 / 151.2 | 211.3 / 234.0 / 244.5 |
| `_render` w = 1920, no crop | 296.0 / 311.4 / 337.5 | 435.5 / 464.8 / 484.1 |
| `_render` w = 960, window crop (1200x800 window, nothing in front) | 136.8 / 145.7 / 148.8 | 223.2 / 253.5 / 293.0 |

`monitors.stage_at` per moment (3 monitors): n = 32, 0.3 / 0.7 / 0.9 ms.

Per-moment totals, n = 32, median / p95 / max in ms: `stage_at`, the frame path lookups, and for
each of the three monitors `FrameStore.image` + `_render` at the given width; sequential, or the
three monitors as three concurrent threads (wall time).

| Width | Sequential | Three threads |
| --- | --- | --- |
| 480 | 287.7 / 296.5 / 296.9 | 156.1 / 170.3 / 172.5 |
| 960 | 473.9 / 496.9 / 500.1 | 235.1 / 258.4 / 264.4 |
| 1920 | 1031.3 / 1071.4 / 1073.8 | 489.2 / 529.7 / 533.6 |

- A moment at w = 960 costs about 0.47 s sequentially and 0.24 s with three threads (2.0x, bounded
  by the 4K frame). `_render` adds to the bare WebP decode about 90 ms (1440p) and 100 ms (4K) at
  w = 960, and about 250 and 320 ms at w = 1920: it also resizes and re-encodes WebP (q70).

## WebP against AV1 NVENC, per frame

60-frame synthetic sequences. 1440p: dense light text, one line changed per frame, a five-line
scroll every 10 frames. 4K: static text with a moving picture area (1920x1080: a pan of 8 px per
frame, per-frame noise of +-10 levels and a moving ellipse). WebP: `imaging.encode_webp` (q70,
m2). AV1 NVENC as in the 2026-11-15 baseline: constqp 90, no B-frames, keyframe every 60, one
packet per frame, fed from BGRA (the build accepted `bgra` directly; no error); the AV1 wall time
includes `VideoFrame.from_ndarray`. 60 packets for 60 frames in both sequences, one keyframe each.
Frames were rendered once and both encoders alternated order per frame. Decoding frame k of the
group means decoding packets 0..k and flushing (5 repeats after one warm-up, n = 5); decoder open
is listed apart.

### 1440p text

| Cell | n | median | p95 | max |
| --- | --- | --- | --- | --- |
| WebP KB/frame | 60 | 278.7 | 285.7 | 286.2 |
| WebP encode wall ms | 60 | 145.6 | 154.1 | 170.7 |
| WebP decode ms | 60 | 49.1 | 51.1 | 72.8 |
| AV1 KB, keyframe | 1 | 459.6 | | |
| AV1 KB, frames 1-59 | 59 | 3.5 | 162.5 | 167.6 |
| AV1 encode wall ms, frame 0 | 1 | 18.9 | | |
| AV1 encode wall ms, frames 1-59 | 59 | 11.2 | 13.7 | 14.8 |
| AV1 decode ms, libdav1d, k = 0 | 5 | 30.6 | 33.3 | 33.3 |
| AV1 decode ms, libdav1d, k = 29 | 5 | 103.7 | 105.5 | 105.5 |
| AV1 decode ms, libdav1d, k = 59 | 5 | 199.3 | 208.9 | 208.9 |
| AV1 decode ms, av1_cuvid, k = 0 | 5 | 32.6 | 34.4 | 34.4 |
| AV1 decode ms, av1_cuvid, k = 29 | 5 | 78.1 | 82.5 | 82.5 |
| AV1 decode ms, av1_cuvid, k = 59 | 5 | 128.3 | 140.6 | 140.6 |

- Mean KB/frame: WebP 278.5, AV1 24.3 (the keyframe counted). The AV1 frames at multiples of 10
  (the scrolls, n = 5) average 162.7 KB, the other 54 frames 3.4 KB.
- CPU per frame (sum over the 60 frames divided by 60, because the Windows CPU clock ticks at
  15.6 ms and a per-frame value is quantized): WebP 134.4 ms (thread = process), AV1 6.5 ms.
- Decoder open: see the "Decoder open" table below.
- OCR word recall at frames 0, 29, 59 (1480, 1501 and 1564 words): original 80.2, 80.3,
  80.8%; WebP decoded 79.7, 79.7, 80.4%; AV1 (libdav1d) decoded 79.9, 80.1, 80.4%. The
  av1_cuvid-decoded copies were not OCR'd.

### 4K moving picture

| Cell | n | median | p95 | max |
| --- | --- | --- | --- | --- |
| WebP KB/frame | 60 | 664.6 | 665.1 | 665.5 |
| WebP encode wall ms | 60 | 343.4 | 359.8 | 373.0 |
| WebP decode ms | 60 | 119.4 | 125.4 | 132.0 |
| AV1 KB, keyframe | 1 | 882.9 | | |
| AV1 KB, frames 1-59 | 59 | 23.0 | 41.5 | 86.6 |
| AV1 encode wall ms, frame 0 | 1 | 26.1 | | |
| AV1 encode wall ms, frames 1-59 | 59 | 20.6 | 23.6 | 24.6 |
| AV1 decode ms, libdav1d, k = 0 | 5 | 73.3 | 77.9 | 77.9 |
| AV1 decode ms, libdav1d, k = 29 | 5 | 352.1 | 355.7 | 355.7 |
| AV1 decode ms, libdav1d, k = 59 | 5 | 594.9 | 601.6 | 601.6 |
| AV1 decode ms, av1_cuvid, k = 0 | 5 | 43.6 | 47.4 | 47.4 |
| AV1 decode ms, av1_cuvid, k = 29 | 5 | 131.9 | 142.6 | 142.6 |
| AV1 decode ms, av1_cuvid, k = 59 | 5 | 238.9 | 248.8 | 248.8 |

- Mean KB/frame: WebP 664.6, AV1 40.1. CPU per frame (sum / 60): WebP 324.5 ms, AV1 12.5 ms.
- OCR word recall of the left text column (967 words, outside the picture; frames 0, 29, 59):
  original 80.4, 80.4, 80.4%; WebP decoded 80.2, 80.2, 80.2%; AV1 (libdav1d) decoded 80.4, 80.4,
  80.4%. The codecs do not change recall on text here.
- OCR of the whole 4K image (1342 words outside the picture) is erratic and unrelated to the
  codec: original 73.1, 10.0, 62.5%; WebP decoded 0.0, 58.6, 80.2%; AV1 decoded 70.3, 80.3, 63.0%.
  With the noisy picture in the image the recognizer sometimes returns almost nothing for the whole
  frame, including the text column that reads at 80% when cropped alone. Do not read the
  whole-image numbers as codec quality.

### Decoder open

Separate short run after the main one (not the original run; the original open figures, 0.9-1.0 ms
for libdav1d and 68.6-69.0 ms for av1_cuvid, were single values without spread and are replaced).
A one-frame random-noise AV1 stream from `av1_nvenc` at each size; `CodecContext.create` +
width/height + `open()` timed, 1 warm-up and 20 measured opens per cell, ms.

| Cell | n | median | p95 | max |
| --- | --- | --- | --- | --- |
| libdav1d open, 1440p | 20 | 3.65 | 4.21 | 4.48 |
| libdav1d open, 4K | 20 | 5.74 | 6.44 | 6.64 |
| av1_cuvid open, 1440p | 20 | 126.3 | 133.2 | 134.6 |
| av1_cuvid open, 4K | 20 | 148.7 | 155.5 | 181.3 |

The two runs differ by a factor of about 2 (av1_cuvid) and 4-6 (libdav1d), so decoder open is only known to
be about 1-6 ms for libdav1d and between 70 and 150 ms for av1_cuvid.

## Cold first calls (ms)

Each is the first call of its kind in the process. The raw data was deleted and the script did not
record which window or frame each first call used, nor whether "first decode of a 1-frame group"
includes the decoder open; read these as order-of-magnitude cold costs.

| Call | ms |
| --- | --- |
| `WindowsOcr(...)` construction | 7.4 |
| First `recognize` | 294.1 |
| First `OcrGate.read` | 254.9 |
| First gate reuse read | 9.4 |
| First `OcrWindowHandler.run` | 336.3 |
| First `monitors.stage_at` | 0.3 |
| First `FrameStore.image` | 5.7 |
| First full WebP decode | 39.6 |
| First `_render` (no crop / with crop) | 66.5 / 120.7 |
| First `encode_webp` (1440p) | 153.8 |
| AV1 NVENC open (1440p / 4K) | 194.4 / 139.9 |
| First AV1 encode (1440p / 4K) | 13.9 / 17.5 |
| First decode of a 1-frame group, libdav1d / av1_cuvid | 32.9 / 125.9 |
| First `_query_d3d_fullscreen` | 0.3 |

Each is a single call (n = 1), so it has no median, p95 or max.

## QUNS cost

Unchanged `Win32Windows._query_d3d_fullscreen()` and `Win32Windows().focused_window()`, in
microseconds. The foreground window was whatever was in front during the run (read in memory,
never kept); `focused_window` returned a window in all 2,000 calls. Warm and cold calls
alternated. "Warm" set `_d3d_read_at = time.monotonic()` before each call so no query ran; "cold"
set it to `-inf` so one query ran.

| Cell | n | median | p95 | max |
| --- | --- | --- | --- | --- |
| `_query_d3d_fullscreen`, back to back | 10000 | 68.5 | 177.8 | 1218.8 |
| `_query_d3d_fullscreen`, 1 s apart | 60 | 200.6 | 298.7 | 480.4 |
| `focused_window`, cache warm | 1000 | 91.0 | 165.6 | 563.4 |
| `focused_window`, cache forced cold | 1000 | 157.8 | 328.0 | 718.8 |

- Means: query 85.8, warm 102.8, cold 182.5 microseconds.
- Returned values: the call returned `False` in 10,000 of 10,000 back-to-back calls and
  `focused_window.exclusive_fullscreen` was `False` in all 1,000 cold calls. The raw
  `(HRESULT, state)` pair was `(0, 2)` in all 10,000 back-to-back calls and in all 60 calls
  1 s apart: always success, state 2 (`QUNS_BUSY`: Windows saw a full-screen window or
  presentation mode). State 3 (D3D fullscreen) was never seen. The desk had a full-screen window
  during the final run (not one the spike set up, cause unknown). An earlier 500-call dry run of
  the same script saw `(0, 5)` throughout (`QUNS_ACCEPTS_NOTIFICATIONS`), so the state value
  depended on what was on the desk, not on the call.
- Calls 1 s apart cost more than back-to-back ones (median 200.6 against 68.5 us), which is the
  cost a cached query pays when it does run. A 100 ms spacing was not measured.
- At the ~10 Hz focus poll, a bound from the cells that apply: without a cache at most
  10 x (91.0 + 200.6) = 2.9 ms/s, 0.29% of one core; with the 1 s cache about
  9 x 91.0 + 291.6 = 1.1 ms/s. The cache saves at most about 0.18% of one core.

## Where the numbers contradict a claim

- **`platform/win/ocr.py` docstring, "~10-60 ms per frame".** The whole-window `recognize` took
  57 ms on an empty 1440p window, 76 ms on a sparse one, 254-273 ms on dense 1440p, 131 ms on an
  empty 4K window, 152 ms on a sparse one and 369-414 ms on dense 4K. Only an empty 1440p window
  fits. The live gate, which reads the crop, took 16-29 ms on empty or sparse crops (inside the
  claim) and 226-335 ms on dense ones. The claim holds for small or sparse crops, not for whole
  dense windows. A later docs feature should update the docstring.
- **The 1 s cache comment in `platform/win/windows.py` and CAPTURE.md ("read at most once per
  second").** The comment says the shell query "need not" run at the ~10 Hz poll. Measured, the
  query costs 69-200 us per call, and the cache saves at most about 0.18% of one core (bound
  above). The 1 s cache is not needed for cost; keeping it is harmless. OPEN A4 #150's cost question ("the cost of the call,
  cached 1 s") is answered as above for an ordinary desk, not for games.
- **`imaging.py` comment, "266 ms per 4K frame at quality 70, method 2": not contradicted.** The
  4K frame of this run (664 KB, noisy picture, loaded machine) took 343 ms median, against the
  baseline's 180 KB frame, so the two are not like for like. The 1440p text frame took 146 ms
  (2026-11-15 baseline: 120-150 ms).

## Limits

- Synthetic content only: text drawn word by word, offscreen images rather than WGC frames. The
  dense pages (1,150-1,840 words) are plainer than a real window and read at 79-82%; real text
  with other fonts, sizes, colours and backgrounds will differ.
- English-only recognizer: French is not installed, so every recall figure is `en-US` reading a
  mixed English/French text. The recall (about 100% on non-accented words, 40-46% exact and 91-96%
  accent-folded on accented ones) and the timings may differ with the French recognizer.
- One desk, with other agents' work running at the same time (26-42% total CPU on average
  during the final run); QUNS state was 2 here and 5 in the dry run, so the desk was not in one
  fixed state. The "not run" list covers only what the spike set up: no game, mpv/MPC-BE, browser
  F11, RDP, alt-tab or side-monitor case.
- The av1_cuvid-decoded copies were not OCR'd, so recall after av1_cuvid decoding is unmeasured.
- Whole-window OCR of a window with a noisy picture can return almost nothing (0-80% on the same
  text beside a noisy picture). Not verified on real game frames, though deferred `ocr_window`
  jobs OCR whole game windows.
- Decoder open comes from a separate short run on a noise stream; the two runs disagree (see
  "Decoder open").
- The 4K moving picture is procedural (pan, noise, an ellipse), not video. Its noise made the
  whole-window OCR erratic. The AV1 sizes (40 KB/frame at 4K) are for this picture, not for real
  video; the 2026-11-15 baseline measured 93 KB/frame on a real moving 4K monitor.
- Decode cells ran on files just written (warm OS file cache). Frames of the three monitors
  within one moment shared one machine; the threaded cell shows three-thread wall time only.
- Per-frame CPU for encoders is a sum divided by 60 because the Windows CPU clock is coarse.
- AV1 decode of frame k includes decoding the 0..k frames before it and a flush; frame counts
  returned were k + 1 for every decode.
- Not measured: disk per normal day, normal-day window counts, VRAM, the real post-game backlog,
  any encoder change or video compaction.
