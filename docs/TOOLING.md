# Tooling and validation

Use the native Windows environment and PowerShell-compatible commands. Interpreter, package
versions and tool configuration belong in `pyproject.toml` and `uv.lock`.

## Quality bar

Use ruff for formatting/lint, strict pyright and pytest. Keep overall coverage at least 80% and
security-critical behaviour thoroughly tested. Windows platform glue in `platform/win/` may use
`# pyright: basic` where bindings lack stubs, but public interfaces remain typed and security
decisions stay in core code. Verify real platform behaviour through hardware tests and the
[manual checklist](../tests/manual/CHECKLIST.md).

Run the checks before committing (manually; there is no git hook):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1
```

It runs ruff format, ruff check, pyright and pytest through `uv run`, which may sync the
environment first. Hardware tests (`-m hardware`, `scripts\selftest.ps1`) are separate from the
regular suite; read the self-test script before running it because it uses real devices and
models. Development tools and tests may use the network; captured, derived and identifying user
data must remain local.

The user removed the development runner's memory/process/thread/priority limits and mandatory
model preflight. Do not restore them without asking. Production resource ownership is a separate
concern ([ARCHITECTURE.md](ARCHITECTURE.md)).

## Testing on real data

Never open the live data folder (`%LOCALAPPDATA%\Akashic`). Real-data testing uses an export the
app writes, re-encrypted with a separate passphrase that the user gives the agent
([DECISIONS.md](DECISIONS.md); the export feature is not built yet). Results and any copies stay
local and encrypted. Until then, and always for deletion, upgrade and failure tests, use
disposable encrypted stores.

## Known defects and measurement

Strict expected-failure tests assert the intended behaviour of known audit defects. When a fix
produces XPASS, remove the marker and retain the regression. Verify a failure's actual cause
before marking it; an unrelated crash is not a known-defect result. Use `pytest -rx` to inspect
open defects.

Keep brute-force oracles and planner accuracy floors passing when replacing an algorithm. Compare
against the latest applicable [baseline](baselines/); read scripts for current options rather
than copying their interfaces here. Use a separate model/runtime folder. Keep development and
held-out sets distinct; do not tune on held-out questions, and score a frozen candidate once on a
fresh set. `scripts/eval_retrieval_scale.py` measures recall@10 and MRR of old evidence among
10,000 invented episodes in words, vectors and hybrid modes; run it after retrieval or ranking
changes.

Synthetic contracts, model smoke tests and real-data quality are different evidence. Validate
capture, transcription, retrieval, answer support and resource use at their own boundaries;
coverage or an aggregate Ask score cannot substitute for these measurements.

## Approved dependencies

The approved runtime dependencies are those in `pyproject.toml` today: argon2-cffi, av, comtypes,
cryptography, fastapi, faster-whisper, librosa, numpy, nvidia-cublas-cu12, onnxruntime-gpu,
Pillow, pystray, sqlcipher3, torch, transformers, uvicorn and the `winrt-*` packages, plus the
official llama.cpp `llama-server.exe` downloaded as a model runtime. Development tools: pyright,
pytest, pytest-cov, ruff. Anything else needs the user's approval first.

PyTorch, Transformers and librosa exist for the Qwen3-ASR and Canary speech models. Load pinned
local artifacts with offline settings and no remote code. Having a library does not authorize
using its network/file helpers or adding a new service. Do not reintroduce llama-cpp-python as a
runtime dependency; the chosen integration uses the official executable.
