# Small text Windows OCR (#190)

Harness and candidates behind `docs/solutions/2026-11-20 - Small Text Windows OCR/report.md`.
Frozen research code (see `eval/README.md`).

- `brief.md`: problem, goals, fidelity, budget, non-goals, harness findings, mechanism catalog.
  `candidates.md`: six candidates with fact-check, prior-art and refuter notes (`*-raw.md`: agent returns).
- `harness/`: `corpus.py` renders synthetic pages off-screen with headless Edge (ClearType and
  grayscale) and Pillow at 9-18 CSS px, scales 1.0 and 1.25, dark/light/blue themes;
  `run.py` scores a candidate (`prepare` / `merge` / `read` / `store` / `load`) on the live,
  deferred and replay paths; `replay.py` re-OCRs stored frames of the quality runs (numbers only);
  `shared.py` (Windows OCR, CER with `score.fold`/`best_line_cer`, wrap-tolerant "join" metric).
- `baseline.py` (today's path), `explored/up3x_lanczos.py`, `C1/` (strip mosaic), `C2/` (best fit:
  strip mosaic with ClearType subpixel rebuild; `step_a.txt` is the method comparison), `C3/`.
- `crosstab_m31.py`: cross-tab of the quality scorer's M3.1 rows.

Not committed: `harness/corpus/` (29 MB of rendered pages, gitignored), rebuilt by
`uv run python eval\small-text-ocr\harness\corpus.py` in about 3 minutes. The replay needs the
quality runs' stores in `eval/quality/local/runs/` (this PC only). No window is shown.

```powershell
uv run python eval\small-text-ocr\harness\run.py eval\small-text-ocr\C2\c2.py [--no-replay] [--renderers chromium]
```
