## Researcher: OCR preprocessing (received)
- No published measurement that any step beyond upscaling helps Windows.Media.Ocr on screen text; apps do at most scaling, padding and background fill.
P1 Pad + edge-colour fill (PowerToys: canvas max(w,64)+16 x max(h,64)+16, fill pixel(0,0), paste at 8,8; HighQualityBicubic, fixed 1.5x) · matters only for small per-block crops [PowerToys BitmapPreprocessor.cs:13-66]
P2 Fixed bicubic/Lanczos scale (PowerToys 1.5x; local 3x Lanczos) [docs/research/windows-ocr-small-text-upscaling.md]
P3 Per-line/per-block crops scaled to ~40 px word height (Text-Grab rule) · 1+N OCR calls unless boxes come from the gate's find_text_box; risks p95 [Text-Grab]
P4 Chroma removal (luma or max channel) · hypothesis; breaks on coloured text over similar-luma background [opinion only]
P5 Binarization (Otsu/Sauvola) · throws away AA information that upscaling uses; Sauvola costly in numpy; no evidence for Windows OCR [Tesseract ImproveQuality docs]
P6 Text-Grab optional filters (brighten, sigmoidal contrast, darken, grayscale, invert) via Magick.NET; call sites unread [Text-Grab MagickHelpers.cs:12-150]
P7 screenpipe Windows adapter: no preprocessing; Otsu only in text-region detection for its gate [screenpipe microsoft.rs:201-233, text_regions.rs:8-91]
