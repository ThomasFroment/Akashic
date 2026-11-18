# Does upscaling before Windows.Media.Ocr help small UI text, and how?
Date: 2026-11-19 · Context: Python 3.14, Windows 11 build 26200, winrt Windows.Media.Ocr (en-US, fr-FR), Pillow; Akashic `platform/win/ocr.py`, `capture/ocr_gate.py` · Goal: cut Akashic's small-text OCR error (audit #190) · Ruled out: cloud OCR services

## Answer
Yes, for text below about 11 px: on Windows OCR, rendered Segoe UI text at 9-10 px reads at CER
0.62-0.73 at native size and 0.021-0.025 after a 3x Lanczos upscale (2x Lanczos: 0.04-0.06); from
11 px up native size is already near perfect, so the gain is confined to the smallest text [1].
Lanczos beat bicubic, and bilinear was clearly worst at every factor [1]. OCR time grows far less
than pixel count (7 ms → 16 ms median at 3x on these blocks) [1]. Prior art agrees on the direction:
Text-Grab rescales to a 40 px average word height measured by a first 1x pass [2], PowerToys
applies a fixed 1.5x HighQualityBicubic [3][4], and Tesseract guidance puts the failure floor at
about a 10 px x-height [5]. Cap the result at `OcrEngine.MaxImageDimension` (10000 px on the target
PC) [1][6]. Upscaling explains only part of the measured 1440p error: Akashic's 1440p rows at ≥ 16
device px still average CER about 0.39, so another cause (likely the lossy WebP copy deferred OCR
reads, #201) must be separated before the gain is booked [7].

## Findings
### Windows OCR on rendered UI text (local measurement)
- CER by font size, averaged over FR/EN/mixed and dark/light themes: 9 px 0.727 at 1x, 0.063 at
  2x Lanczos, 0.025 at 3x Lanczos; 10 px 0.623, 0.037, 0.021; 11 px 0.006 at 1x; 12-16 px ≤ 0.005
  at 1x [1].
- At ≤ 11 px by language, 1x → 3x Lanczos: EN 0.414 → 0.020, FR 0.514 → 0.013, mixed 0.429 → 0.014 [1].
- Filters at 2x for 9 px: Lanczos 0.063, bicubic 0.095, bilinear 0.238 [1].
- 4x was no better than 3x (9 px: 0.046 Lanczos) [1].
- Median OCR time per block: 7.1 ms at 1x, 10.2 ms at 2x, 16.0 ms at 3x, 22.1 ms at 4x; time per
  megapixel falls as scale rises (283 → 59 ms/Mpix at 3x), so cost is far below linear in pixels
  for these sparse blocks [1].
- `OcrEngine.MaxImageDimension` reads 10000 on the target PC [1]; Microsoft documents the property
  without a value or any scaling guidance [6].
### How Windows-OCR apps prepare images
- Text-Grab averages word bounding-box heights from a first OCR pass, sets scale = 40 / average
  ("Ideal Line Height is 40px"; it measures word height), defaults to 4x when no words are found,
  and caps the scale by `MaxImageDimension` with no lower clamp, so it can also shrink large text [2].
- Text-Grab scales with a WPF `TransformedBitmap`/`ScaleTransform` with no explicit interpolation [2].
- PowerToys Text Extractor uses a fixed 1.5x (1.0 in single-word mode, or less to fit
  `MaxImageDimension`) with `InterpolationMode.HighQualityBicubic`, and pads sides under 64 px [3][4].
### OCR engines in general
- Tesseract: about 20 px x-height at 10 pt/300 dpi; below 10 px x-height "very little chance of
  accurate results"; an LSTM ceiling near 30 px x-height [5].
- A 2014 screen-text test on Tesseract cut word errors from 8.33 % to 1.24 % by upscaling; 3x beat
  2x and 4x, and ImageMagick's Triangle (bilinear) filter was best among those tried [8].
- Low-resolution printed text: bicubic upscaling to 300 dpi is a competent Tesseract baseline; an
  ensemble of upscalings with fine-tuned models cut CER about 64 % at 60 dpi [9].
### How much of Akashic's measured error this addresses
- Smoke run 2220: CER 0.41 at ≤ 11 device px (n 23), 0.155 at ≥ 16 px (n 49), 1440p 0.404 (n 39),
  4K 0.038 (n 33); no 12-15 px row [7]. Even if every small-font row is on 1440p, the 1440p rows at
  ≥ 16 px must average CER ≥ ~0.39, which clean text at that size does not explain [1][7].
- Deferred OCR reads a WebP quality-70 re-decode of the window (`capture/screen_writer.py:356-357`,
  `capture/imaging.py:32, 70-74`), and the scorer adds each row to both its resolution and its
  source split, with no cross-tab stored (`eval/quality/score.py:495-499`) [7].
- The ≤ 11 bucket is in device px, so 9 CSS px on the 4K monitor (11.25 device px) lands in it too [7].

## Caveats
- The local sweep used Pillow-rendered grayscale anti-aliased Segoe UI on flat backgrounds, short
  blocks of four lines; real captures carry ClearType colour fringes, other fonts, icons, busy
  backgrounds and whole-window layouts. Treat its factors as the starting grid, not the answer [1].
- A height-driven rule (Text-Grab) costs a first 1x OCR pass per window, 250-415 ms on dense
  windows (`platform/win/ocr.py:2-3`); a fixed factor or a height estimate from Akashic's previous
  read of the same window avoids it [2].
- A 20 px word-box target corresponds to about a 10 px x-height, Tesseract's failure floor, not its
  target; 40 px word height (Text-Grab) is the better-supported point [2][5].
- The Tesseract guidance is the version-3 FAQ; the 2014 filter ranking is Tesseract-only and is
  contradicted for Windows OCR by the local sweep (bilinear worst) [1][5][8].
- No source documents Windows OCR's own text-height sensitivity; a Microsoft Q&A answer reports a
  2x nearest-neighbour upscale fixing tiny bitmaps, anecdotally [10].

## Sources
[1] Local sweep, Windows.Media.Ocr on rendered UI text — `eval/quality/spikes/ocr_scale_sweep.py`, results `eval/quality/spikes/ocr_scale_sweep.json` — tier 1 — target PC, 2026-11-19
[2] Text-Grab `OcrSourceUtilities.cs` (`GetIdealScaleFactorForOcrResult`) and `ImageMethods.cs` (`ScaleBitmapUniform`) — https://github.com/TheJoeFin/Text-Grab (Text-Grab/Utilities/OcrSourceUtilities.cs:540-585, ImageMethods.cs:142-166) — tier 1 — master, 2026-11-19
[3] PowerToys `TextExtractorService.cs` — https://github.com/microsoft/PowerToys (src/modules/PowerOCR/PowerOCR.Core/Services/TextExtractorService.cs:16-104) — tier 1 — main, 2026-11-19
[4] PowerToys `BitmapPreprocessor.cs` — https://github.com/microsoft/PowerToys (src/modules/PowerOCR/PowerOCR.Core/Imaging/BitmapPreprocessor.cs:13-60) — tier 1 — main, 2026-11-19
[5] Tesseract FAQ (old) — https://github.com/tesseract-ocr/tessdoc/blob/main/tess3/FAQ-Old.md — tier 1 — accessed 2026-11-19
[6] OcrEngine.MaxImageDimension — https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine.maximagedimension — tier 1 — page updated 2025-11-21
[7] Akashic smoke baseline and scorer — `docs/baselines/2026-11-18-quality.md:32-37`, `eval/quality/results/20261118-smoke-s11-target-2220/scores.json:288-341`, `eval/quality/score.py:495-499` — tier 1 — 2026-11-18
[8] Improving OCR accuracy (stb-tester) — https://stb-tester.com/blog/2014/04/14/improving-ocr-accuracy — tier 2 — 2014
[9] End-to-end OCR for ultra-low-resolution printed text — https://ar5iv.labs.arxiv.org/html/2105.04515 — tier 1 (preprint) — arXiv 2105.04515
[10] Windows.Media.Ocr won't scan anything if width and height are small — https://docs.microsoft.com/en-us/answers/questions/692284/windowsmediaocr-won39t-scan-anything-if-width-ando-1.html — tier 2 — accessed 2026-11-19
