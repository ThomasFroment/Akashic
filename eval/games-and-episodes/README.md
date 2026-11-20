# Game sessions and episodes (#188, #192)

Harness and prototypes behind `docs/solutions/2026-11-20 - Game Sessions And Episodes/report.md`.
Frozen research code (see `eval/README.md`).

Report candidates map to the prototype folders: C1 = `P1-lifetime-provenance-chain/pausesplit/`
(best fit), C1a = `P1-lifetime-provenance-chain/` (as first built), C1b = `rowep/`, C1c = `p600/`,
C1d = `fallback/`, C2 = `P2-focus-identity-reopen/` with `P2_END=alive` (`metrics-alive.*`),
C2a = the same with `P2_END=front` (`metrics.*`), C3 = `P3-video-chains/`.

- `harness/ge.py`: loads a finished quality run (truth via `eval/quality/score.py`, store via
  `score.open_store`) into an in-memory shadow store without titles, URLs or captured text, and
  builds the candidates' context (simulated 100 ms foreground checks, process lifetimes from the
  observer's ~1 s record, stored gaps, monitors). `ge.replay` replays today's game mode.
- `harness/harness.py`: scores a candidate module (`game_rows`, `episodes`, `is_game`,
  `writes_name`, `SIGNALS`) with today's scorer; writes `metrics.txt`, `metrics.json`, `side.png`
  next to the candidate.
- `harness/realwin.py`: the real-window detection table (`realwin.json`): opens each case
  fullscreen, records what `gaming.py` reads plus extra signals and their cost, then closes it.
  It creates renamed copies of the interpreter (`QualityGame.exe`, `QualityVault.exe`) in
  `harness/exes/` at run time; they are not committed. Note: `realwin.json` records
  `under_steam_root: false` for Outer Wilds while `spike_handles.json` reads `true`.
- `harness/launchers.py`: Steam, Epic and GOG install folders and GameConfigStore exes (booleans
  only). `harness/diagnose.py`: writes `diagnosis.json` (why today's rows split).
- `P1-lifetime-provenance-chain/post.py`: runs the harness on a P1 candidate, then adds the front
  ledger, game-mode time, chain counts, changed-parts derivation cost and the clipped+split
  scoring (truth clipped to the recorded span, split at stored Pauses). `spike_handles.py`: the
  real process-handle spike (`spike_handles.json`); it starts the harness game and Outer Wilds.
- `P2-focus-identity-reopen/sweep.py`: pulsetime and end-mode sweep (`sweep.json`, figures).
  `clipped_alive.py`: the clipped+split scoring of `P2_END=alive` (`metrics-alive-clipped.txt`).
- `P3-video-chains/run.py`: chain gap sweep, incremental check, adversarial set, figures.
- `baseline/`, `baseline_replay/`: today's stored rows, and today's logic replayed on the checks.

Not committed: the quality runs' stores (`eval/quality/local/runs/<id>/`, this PC only), which
every scoring command needs; the figures and `side.png` files (copies used by the report are in
its folder). Nothing here holds window titles, URLs or captured text: numbers, exe names, window
classes and the harness's generated video titles only.

Run from the repository root:

```powershell
uv run python eval\games-and-episodes\harness\harness.py eval\games-and-episodes\baseline\candidate.py
uv run python eval\games-and-episodes\P1-lifetime-provenance-chain\post.py eval\games-and-episodes\P1-lifetime-provenance-chain\pausesplit\candidate.py --figs
uv run python eval\games-and-episodes\P1-lifetime-provenance-chain\post.py eval\games-and-episodes\P1-lifetime-provenance-chain\candidate.py --figs
$env:P2_END = 'alive'; uv run python eval\games-and-episodes\harness\harness.py eval\games-and-episodes\P2-focus-identity-reopen\candidate.py
uv run python eval\games-and-episodes\P2-focus-identity-reopen\clipped_alive.py
uv run --with matplotlib python eval\games-and-episodes\P2-focus-identity-reopen\sweep.py
uv run python eval\games-and-episodes\P3-video-chains\run.py
uv run python eval\games-and-episodes\harness\diagnose.py
```

`harness.py` and `post.py` overwrite `metrics.txt` and `metrics.json` in the candidate's folder;
the P2 `END=alive` results were renamed to `metrics-alive.*` by hand. `realwin.py` and
`spike_handles.py` open real windows and games on the desktop.
