# Method-study material

The data and code behind the nine reports in `docs/solutions/` (the 2026-11-10 method studies
run on the audits' queued problems). Each `eval/<study>/` folder holds the study's harness, the
reference copy of today's code (`ref_today.py` or `baseline_today.py`), the candidates, the
fixture builders with their seeds, the labelled question sets, and each candidate's results
(`metrics.txt`, `details.json`, `side.png`).

- **Frozen research code.** These are prototypes as they were measured, not maintained code: they
  are excluded from ruff and pyright and are not part of the app. Read a report before reusing
  its candidate; a candidate is a measured method, not a drop-in patch.
- **No user data.** Everything is invented or public (licences in each study's README). Nothing
  here reads the Akashic data directory.
- **Large data is rebuilt, not committed.** Models, raw downloads, generated audio, per-speaker
  banks and the vector stores (about 24 GB in total) are rebuilt by each study's scripts from
  pinned public sources. `NOT_COPIED.txt` in each folder lists what was left out and its size.
  The rebuilt files are gitignored.

The baseline of 2026-11-11 (tests, Ask, planner sets, timings) is separate: see
`docs/baselines/2026-11-11.md` and `tests/data/planner/`.

## Environment

```powershell
py -3.14 -m venv eval\.venv
eval\.venv\Scripts\python.exe -m pip install -r eval\requirements.txt
```

Run each command from the study's own folder unless its README says otherwise (harnesses import
their sibling modules). The repository's `src/` is imported read-only through a path computed
from the file location.

## The nine studies

"Verified" means the numbers were re-obtained from this folder on 2026-11-11, with the committed
data only. Timings always differ from run to run; identity and accuracy figures matched exactly.

| Study and report | Key numbers (report) | Folders | Verified 2026-11-11 |
| :- | :- | :- | :- |
| `app-run-single-pass` (App Run Single Pass, #103) | Today 1,857 ms on 50k visits; C1 15.7 ms, byte-identical on 10,002 cases × 4 hash seeds | `ref_today.py`, `C1/cand.py` | Identity: yes (ref and C1) |
| `conversation-clustering-sweep` (#102) | Today fails 1,165 / 9 / 40 fuzz, edge and replay checks; C4 0 / 0 / 0, 5.0 ms at N = 20,000 | `ref_today.py`, `oracle.py`, `C4/cand.py` | Yes (ref and C4) |
| `exact-vector-top-k-scan` (#100) | C3 0 mismatches outside NaN scope over 612 queries, 252-431 ns/row | `ref_today.py`, `C3/cand.py` | Yes at 100k rows; the 1M store was not rebuilt |
| `identical-frame-text-reuse` (#101) | C3 result slots 36/36 (today 17/36), 89.7 % fewer vectors on the video fixture | `ref_today.py`, `C3/cand.py` | Yes (ref and C3) |
| `windowed-episode-vector-scan` (#104) | C2 0.089 ms warm for a 1-day window; today 14 tie-order mismatches | `ref_today.py`, `C2/cand.py` | Yes (ref and C2) |
| `conversational-time-windows` (#179-#181) | Today 19.0 % on the fresh set; C7 rules 0.926; C9 (model slots) 0.931; C3 0.753 | `baseline_today.py`; C7 = `R2rules/` with `CTW_GAZ=set`; C9 = `R2slots/`; C3 = `C3fix/` | Yes, C9 from the cached model replies |
| `thread-follow-up-field-inheritance` (#175) | Today keywords 0.618 on the fresh set; rule path 0.903; with the model relation tag 0.933 | `harness.py --baseline today`; `R2C2/` (rule path, and model path with `R2C2_LLM=1`) | Yes, model path replayed from `R2C2/calls_heldout3.jsonl` |
| `transcript-hallucination-rejection` (#128, #129, #138, #141) | Today recall 0.727, false rejections 0.86 %; C7 recall 0.921, false rejections 0.86 %, unclear 9.8 % | `ref_today.py`; C7 = `C5_decode.py` | Yes, from the committed decode and VAD caches |
| `segment-speaker-attribution` (#132-#134) | Today 0.701 authorship, 7.94 % wrong authors; C7 0.927 / 3.17 %; C9 0.902 / 2.88 % | `candidates/reference.py`; C7 = `candidates/R2span_C1.py`; C9 = `out/R2seam_C2_calib_t1` | Scoring only: all 24 `out/*/details.json` re-scored to their `metrics.txt`. Attribution needs the rebuilt fixture |

Report candidate names (C1, C7, ...) are the ones used in `docs/solutions/` and the synthesis;
folder names are the prototype names. Each report's "Reproduce" line gives the exact command.

## Rebuilding the large data

Each study's README gives the full commands. In short:

- **Speed studies** (`exact-vector-top-k-scan`, `windowed-episode-vector-scan`): `build_db.py`
  writes the synthetic encrypted stores into `cache/` (seeded; the 100k and 1M-row stores of the
  top-k study take about 2.3 GB together). The other three speed studies generate their fixtures in memory from fixed seeds.
- **`transcript-hallucination-rejection`**: `fetch_data.py` (about 2.2 GB of public audio and the
  pinned Whisper large-v3), `build_fixture.py` (1.6 GB int16 fixture), `run_production.py` (the
  app's own Segmenter, VAD and Whisper on the GPU; rewrites `data/cache/`). The committed
  `data/cache/segments.jsonl`, `clips.jsonl` and `vad/` already hold every decode statistic the
  candidates read, so scoring needs none of this.
- **`segment-speaker-attribution`**: download the corpora and models listed in its README into
  `data/` (LibriSpeech, MLS French, the app's two pinned ONNX models), then `prepare_sources.py`
  and `make_fixture.py` (seed 20261110). `candidates/R2span_derive.py` rebuilds
  `data/models_derived/`. The CAM++ and alternative ResNet34 models used by the C6 scripts have
  no fetch script; their sources are in the report.
- **LLM-backed steps** (time-window model slots, follow-up model tag): only needed to refresh
  the cached model replies. Fetch the chat model into a folder of your own with
  `scripts\fetch_models.py --data-dir <folder> gemma-4-12b-qat-q4`; the llama.cpp runtime comes
  with a run without keys (which fetches every model) or from an existing install. Start the
  server on loopback with a fresh key, as the app does:
  `llama-server.exe --host 127.0.0.1 --port <port> --no-webui --offline --models-dir <folder>\models --models-max 1 --models-preset <preset.ini>`
  with `LLAMA_API_KEY` set. The scripts read `port.txt` and `key.txt` from their folder
  (`R2slots/`) or the `R2C2_LLM_URL` / `R2C2_LLM_KEY` variables (`R2C2/`).

## Things to know before running

- Write new results under a new name (`--name`, or a new candidate folder). Several harnesses
  write into committed folders by default:
  - `conversational-time-windows/harness.py` without `--candidate` overwrites `out/baseline/`;
  - `exact-vector-top-k-scan/harness.py` rewrites `cache/today_timing_<size>.json` unless
    `--no-today` is passed;
  - `thread-follow-up-field-inheritance/harness.py` writes into `baselines/<name>/` or the
    candidate's folder;
  - both vector studies' `build_db.py` write the store's absolute path into `cache/*.json`.
  Revert those files (`git checkout -- <file>`) after a local run.
- **Frozen files.** `frozen.sha256` records the hash of each candidate when it was frozen before
  the fresh-set scoring. Files hashed with CRLF line endings keep their exact bytes
  (`.gitattributes`). `conversational-time-windows/R2slots/fill_cache.py` no longer matches its
  hash: its path-to-`src` line was changed by one directory level when the study moved here.
- The study code was moved from its original local folder one directory level up; the expressions
  that locate `src/` were adjusted accordingly and the absolute paths stored in results were
  replaced with `eval\`. No other change was made to the code or the results.
