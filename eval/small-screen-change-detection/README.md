# Small screen change detection (#208)

Harness and candidates behind `docs/solutions/2026-11-19 - Small Screen Change Detection/report.md`.
Frozen research code (see `eval/README.md`).

- `brief.md`: problem, goals, fidelity, budget, non-goals, mechanism catalog. `candidates.md`: the
  six candidates with refuter, fact-check and prior-art notes.
- `harness/`: `run.py` (synthetic placement sweep, timing, one-hour 4K check streams),
  `baseline.py` (today's `differs`), `scenes.py`, `hcommon.py` (candidate contract), `plot.py`,
  `live_replay.py` (real desktop, GDI grab, counts only, never writes captured content).
- `baseline/`, `C2/`, `C3/` (`fix_taskbar/` is the measured variant), `C5/` (best fit): each with
  `metrics.txt`, `details.json`, `side.png` and the scripts used for its fix rounds.
- `refuter/`: the refuter's sweeps and timings. `live_counts.json`: the 10-minute real-desktop counts.

All content is synthetic (Pillow, Segoe UI); nothing here reads the Akashic data directory.

```powershell
uv run python eval\small-screen-change-detection\harness\run.py eval\small-screen-change-detection\C5\c5.py <outdir> [--quick] [--no-seq]
uv run python eval\small-screen-change-detection\harness\live_replay.py --seconds 600 eval\small-screen-change-detection\C5\c5.py
```

The sweep caches today's results in `harness/cache/` (gitignored). The live replay captures the
user's real screen in memory: run it only with the user's agreement.
