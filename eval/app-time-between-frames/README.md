# App time between frames (#196)

Harness and candidates behind `docs/solutions/2026-11-19 - App Time Between Frames/report.md`.
Frozen research code (see `eval/README.md`).

- `brief.md`: problem, goals, fidelity, budget, non-goals, mechanism catalog. `candidates.md`: the
  six candidates with fact-check, prior-art and refuter notes (`*-raw.md`: the agents' returns).
- `harness/`: `run.py` replays the quality runs' encrypted stores (DPAPI, this Windows account) through
  a candidate `bands(conn, start_ms, end_ms, now_ms, ctx)` and the unchanged `eval/quality/score.py`
  M5 scorer; `synthetic.py` builds nine in-memory timelines (static read, lock, sleep, pause, end of
  recording, away, video without input, controller game, voice call while away); `core.py` gives the
  simulated foreground and idle records; `draw.py` draws `side.png`.
- `baseline/`, `explored/` (fixed-cap and monitor-confirmation holds), `C1/`, `C3/`, `C4/` (best fit):
  `metrics.txt`, `detail.json`, `side.png` and the scripts used for fixes and robustness checks.
- `refuter/`: the refuter's numbers-only checks on the stores and the SystemExecutionState spike.

Outputs hold numbers and the harness apps' exe names only; nothing captured is printed or written.
The replay needs the quality runs' stores in `eval/quality/local/runs/` (gitignored, this PC only).

```powershell
uv run python eval\app-time-between-frames\harness\run.py eval\app-time-between-frames\C4\c4.py
```
