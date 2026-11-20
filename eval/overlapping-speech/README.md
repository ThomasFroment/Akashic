# Overlapping speech (#110)

Harness and prototypes behind `docs/solutions/2026-11-20 - Overlapping Speech/report.md`.
Frozen research code (see `eval/README.md`).

Report candidates map to the prototype folders:

| Report | Folder | Variant |
| :- | :- | :- |
| Today (reference) | `baseline/` | today's transcribe and speakers jobs replayed on the stored segments |
| C1 per-app stems + call residual, strict exclusion | `P1-per-process-streams/` | `detail.json` `runs_strict_excl` / `runs_nopause_strict_excl` (also `shadow_p1` in P3's `detail.json`) |
| C1a P1 as first built | `P1-per-process-streams/` | `detail.json` `runs` / `runs_nopause`, `metrics.txt`, `side.png` |
| C4 overlap-gated separation | `P2-gated-separation/` | separation on CPU (default); `P2_SEP=cuda` = the GPU variant (`cost-candidate-gpu.json`) |
| C6 stems, then gated separation on every stream | `P3-stems-plus-separation/` | `P3_LANGSPLIT=0`: `detail.json` `p3_no_langsplit`; the synthetic set was replayed this way |
| C6 + language split (relaxes the language non-goal) | `P3-stems-plus-separation/` | default: `detail.json` `runs` / `runs_nopause`, `metrics.txt`, `side.png` |

C2 (complementary include/exclude), C3 (Wave Link mixer endpoints) and C5 (packed Whisper window)
were not built; the report's Explored section gives the reasons.

## Files

- `harness/core.py`: loads a finished quality run (truth via `eval/quality/score.py`, store via
  `score.open_store`), re-renders each harness source from the player logs as a simulated
  per-process stream (`ctx.process_stream`), and gives a candidate its context: decoded stored
  audio, production Whisper decode with GPU accounting, the speakers job, capture-side CPU timing.
  A candidate is a module with `process(segments, ctx) -> list[Transcript]`.
- `harness/run.py`: replays a candidate on the standard and day runs and the synthetic set, scores
  it with the quality scorer's own `m7` and `m8_m9`, and writes `metrics.txt` (one line, keys
  documented in its docstring), `detail.json` and `side.png`. `--only-cost` re-measures cost only.
- `harness/cost.py`: GPU seconds per recorded hour, VRAM peak and capture CPU on a fixed 6-minute
  window (the densest overlapped system speech of the standard run), 9 reps, fresh process.
- `harness/synthetic.py`: the synthetic overlap set (two corpus talkers, overlap 0-1 x SIR -6/0/+6 dB
  x EN/FR/FR-EN/mixed, music or game bed, talkers on one or two processes), exact references.
- `harness/errors.py`, `harness/diagnose.py`: word-level error attribution (overlap kind, stream,
  error type, fix route); `diagnose.py` writes `diagnosis.json`.
- `harness/fidelity.py`: how faithful the simulated per-process streams are (`fidelity.json`).
- `harness/draw.py`: the six-panel `side.png` (Pillow). `harness/example_per_process.py`: an
  oracle API check, not a proposal.
- `baseline/baseline.py`: today's pipeline as a candidate.
- `P1-per-process-streams/`: `candidate.py` (C1); `p1_run.py` (driver: harness scoring, the
  no-paused scoring, the strict exclusion rescoring); `spike.py` + `spike.json` (the real
  subtraction spike on VB-CABLE and the DT-770 endpoint: depth per 0.5 s block, FIR lengths,
  process-client CPU); `cost_locked.py` (cost under the GPU lock, `cost-same-session.json`);
  `figs.py` (figures); `probe.py` (store gap reasons, numbers only); `baseline-rescore/` (today
  rescored with the no-paused rule).
- `P2-gated-separation/`: `candidate.py` (C4); `evaluate.py` (driver); `export.py` (ONNX export of
  MossFormer2_SS_16K in a throwaway venv); `make_test.py`, `check_onnx.py`, `cuda_check.py`,
  `time_torch.py`, `bench_sep.py` (export check and separation real-time factor, `bench.json`,
  `check_onnx.json`); `locked_cost.py` (`cost-*.json`); `finalize.py` (`metrics.txt`, figures);
  `smoke.py`, `test_score.py` (pre-flight); `musts.json` (today vs P2, both scorings);
  `model/provenance.json` (pinned URL, commit, sizes, sha256 of the checkpoint and the export).
- `P3-stems-plus-separation/`: `candidate.py` (C6 + language split; `P3_LANGSPLIT=0` for C6);
  `sep.py` (frozen copy of P2's candidate, loads P2's exported model); `p3_run.py` (driver, with
  P1 replayed as a shadow on the same capture front end); `langsplit_check.py` (mixed-row check);
  `ls_census.py` (`ls-census.json`, language-split GPU on whole runs); `geometry.py` (row
  geometry around mixed utterances); `cost_p3.py` (`cost-same-session.json`); `finalize.py`
  (`metrics.txt`, `side.png`, figures, `detail["musts"]`, `detail["cost_model"]`); `spike.json`
  (copy of P1's, read by the candidate); `test_score.py` (pre-flight).
- `log-*.txt`: the drivers' printed numbers.

## Run

From the repository root. Every scoring command needs the quality runs' stores
(`eval/quality/local/runs/<id>/`, this PC only). P2 and P3 need the separation model: download
`last_best_checkpoint.pt` from the pinned URL in `P2-gated-separation/model/provenance.json`,
check its sha256, then run `export.py` in a throwaway venv with the packages listed there (the
model code is read from `_resources/references/ClearerVoice-Studio`, not committed). The model was
approved for measurement only.

```powershell
uv run python eval\overlapping-speech\harness\run.py eval\overlapping-speech\baseline\baseline.py
uv run python eval\overlapping-speech\harness\diagnose.py
uv run python eval\overlapping-speech\harness\fidelity.py
uv run python eval\overlapping-speech\P1-per-process-streams\p1_run.py eval\overlapping-speech\P1-per-process-streams\candidate.py eval\overlapping-speech\P1-per-process-streams
uv run python eval\overlapping-speech\P1-per-process-streams\cost_locked.py
uv run python eval\overlapping-speech\P2-gated-separation\evaluate.py
uv run python eval\overlapping-speech\P2-gated-separation\locked_cost.py
uv run python eval\overlapping-speech\P2-gated-separation\finalize.py
uv run python eval\overlapping-speech\P3-stems-plus-separation\p3_run.py eval\overlapping-speech\P3-stems-plus-separation
uv run python eval\overlapping-speech\P3-stems-plus-separation\ls_census.py
uv run python eval\overlapping-speech\P3-stems-plus-separation\cost_p3.py
uv run python eval\overlapping-speech\P3-stems-plus-separation\finalize.py
```

A full replay takes about an hour per candidate. The cost scripts take a `gpu.lock` file in this
folder and wait until no other model process holds the GPU. `spike.py` plays inaudible test tones
into VB-CABLE and quiet ones into the default endpoint.

Not committed (see `.gitignore`): the model checkpoint and its ONNX export, the export venv, the
harness calibration cache (`harness/cache/`), test mixtures (`*.npz`) and any audio. Nothing here
holds transcript or captured text: numbers, process names and harness source labels only. The
scripts were run from `_dev/invent/overlapping-speech/`; the copies here have their repository-root
paths adjusted to this folder's depth and were not rerun from it.
