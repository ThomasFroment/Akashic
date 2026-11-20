# Screen text duplication and noise

Harness and prototypes behind `docs/solutions/2026-11-20 - Screen Text Duplication And Noise/report.md`.
Frozen research code (see `eval/README.md`).

Report candidates map to the prototype folders: C1 = `P1-line-store-lossless/` (best fit),
C1a-C1c = the P1 parameter sweep (`P1-line-store-lossless/sweep.json`), C2 = `P2-line-store-consensus/`,
C2a-C2c = P2 configurations in `P2-line-store-consensus/sweep2.json`. C3 was not built.

- `harness/shared.py`: loads a finished quality run's stored window texts in memory (truth via
  `eval/quality/score.py`, store via `score.open_store`), the candidate contract (`process`,
  optional `prepare` and `step`), line labelling against driver truth, M3.1 / M3.2 / M3.3, the token
  classes behind the noise metrics. Nothing it reads is printed or written.
- `harness/replay.py`: runs a candidate over the three runs and the synthetic set; rebuilds an
  in-memory FTS5 from its kept lines; writes `metrics.txt`, `details.json` (numbers only) and
  `side.png` into the output folder.
- `harness/synth.py`: synthetic window sequences with exact truth (content, volatile content, chrome,
  volatile chrome, garbage), built from the harness sentence corpus `eval/quality/assets/sentences.json`.
- `harness/lexicon.py`: builds the FR/EN word list behind the round-1 noise metric from FLEURS
  transcripts in the repo and the Whisper large-v3 vocabulary of a quality run's model folder.
- `harness/diagnose.py`: where today's stored characters go (`diagnosis/*.json`, `diagnosis/summary.txt`).
- `harness/plot.py`: the `side.png` layout (Pillow). `harness/selftest/`: two harness self-tests
  (`line_dedupe.py`, `drop_odd.py`), not candidates.
- `baseline/cand.py`: today (one FTS row per stored frame window).
- `P1-line-store-lossless/`: `cand.py` (C1), `run_p1.py` (harness plus the user's definitions:
  M3.2 over stored rows, M3.1 on rebuilt frames, misread counts, per-line truth link, rebuild check),
  `figs.py`, `diag_mis.py` (where the remaining misreads sit).
- `P2-line-store-consensus/`: `cand.py` (C2), `score_v2.py` (misread counts and per-line truth link
  for any candidate: `v2_baseline.json`, `v2_p1.json`, `v2_p2.json`), `m31_rebuilt.py`, `round2.py`
  (appends the round-2 fields to `metrics.txt`, draws `fig-round2.png`), `sweep.py`, `sweep2.py`,
  `noise_floor.py`, `debug_drop.py` (`drop_causes.json`), `figs.py`.
- `fig_misread_count.py`: the report's `fig-misread-count.png` (matplotlib, numbers from the json files).

The two scoring tools count misreads slightly differently: `run_p1.py` reads today 538 / 7654 / 44363
and P1 89 / 448 / 3121; `score_v2.py` reads today 523 / 7635 / 50611, P1 90 / 482 / 3527, P2 65 / 419 / 2765.

Not committed: the quality runs' stores (`eval/quality/local/runs/<id>/`, this PC only), which every
scoring command needs; `harness/lexicon/words.txt` (444 KB, derived from the Whisper vocabulary
file, rebuilt by `lexicon.py`); `harness/synthetic/windows.jsonl` (3.5 MB, rebuilt by `synth.py`);
figures and `side.png` files (copies used by the report are in its folder). Neither generated file
is covered by `.gitignore`. Nothing here holds captured text: numbers, run ids, configuration names
and code only.

Run from the repository root:

```powershell
uv run python eval\screen-text-duplication-noise\harness\lexicon.py
uv run python eval\screen-text-duplication-noise\harness\synth.py
uv run python eval\screen-text-duplication-noise\harness\diagnose.py smoke standard day
uv run python eval\screen-text-duplication-noise\harness\replay.py eval\screen-text-duplication-noise\baseline\cand.py eval\screen-text-duplication-noise\baseline
uv run python eval\screen-text-duplication-noise\P1-line-store-lossless\run_p1.py eval\screen-text-duplication-noise\P1-line-store-lossless --reps 9
uv run python eval\screen-text-duplication-noise\P1-line-store-lossless\run_p1.py $env:TEMP\p1-guard-off --set bad_need_unstable=False --no-synth
uv run python eval\screen-text-duplication-noise\P1-line-store-lossless\figs.py
uv run python eval\screen-text-duplication-noise\harness\replay.py eval\screen-text-duplication-noise\P2-line-store-consensus\cand.py eval\screen-text-duplication-noise\P2-line-store-consensus --reps 9
uv run python eval\screen-text-duplication-noise\P2-line-store-consensus\score_v2.py eval\screen-text-duplication-noise\P2-line-store-consensus\cand.py eval\screen-text-duplication-noise\P2-line-store-consensus\v2_p2.json
uv run python eval\screen-text-duplication-noise\P2-line-store-consensus\m31_rebuilt.py
uv run python eval\screen-text-duplication-noise\P2-line-store-consensus\round2.py
uv run python eval\screen-text-duplication-noise\P2-line-store-consensus\sweep2.py eval\screen-text-duplication-noise\P2-line-store-consensus\sweep2.json
uv run --with matplotlib python eval\screen-text-duplication-noise\fig_misread_count.py eval\screen-text-duplication-noise fig-misread-count.png
```

`replay.py`, `run_p1.py` and `round2.py` overwrite `metrics.txt` and `details.json` in the given
folder. P1's `sweep.json` was assembled from `run_p1.py --set` runs (token guard off:
`bad_need_unstable=False`; minority readings: `minority_unk=True`, `minority_line=True`; all guards
off: both `*_need_unstable=False`, `minority_unk=True`, `lm_pct=0.05`). P2 tunables are overridden
with `P2_<NAME>` environment variables (`sweep.py`, `sweep2.py`).
