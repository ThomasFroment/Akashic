# Akashic

A personal, fully local memory of what you see, hear and say on your Windows PC.
Nothing leaves the machine. Personal, non-commercial project. See [AGENTS.md](AGENTS.md) for the
constraints and [docs/README.md](docs/README.md) for the documentation map.

Akashic records the screen (app-window screenshots plus the text on them), what plays, which
games run, and speech from the microphone and system sound. It transcribes speech in French and
English, tells voices apart, groups everything into episodes (videos, calls, games, browsing,
work) and answers questions about them with a local language model, citing the moments it used.

## Requirements

- Windows 11 and PowerShell.
- An NVIDIA driver (target: RTX 4080 Super). No CUDA toolkit is needed.
- [uv](https://docs.astral.sh/uv/), the only tool you install. It fetches Python 3.14 and every
  dependency into the project's `.venv`.

## Install

```powershell
winget install --id=astral-sh.uv -e      # once; then open a new terminal
git clone <this repository> Akashic
cd Akashic
$env:UV_CONCURRENT_DOWNLOADS = "1"
$env:UV_CONCURRENT_INSTALLS = "1"
$env:UV_CONCURRENT_BUILDS = "1"
uv sync --no-cache
```

Installing and downloading models fetch only public files. The running app talks only to
`127.0.0.1`.

**Optional: French OCR.** When an app exposes no text through UI Automation, Windows OCR reads the
screen. French is the default recognizer (it reads English well too). Without the French pack the
app falls back to English and logs a warning. To add it, run once in an elevated PowerShell (or
add French under Settings › Time & language):

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~fr-FR~0.0.1.0"
```

## Run

```powershell
uv run akashic
```

1. **First run:** choose a passphrase. **If you lose it, your data is unrecoverable**: there is
   no reset and no recovery key.
2. **Every run:** enter the passphrase. Nothing is captured until you unlock.
3. The UI opens in your browser and an icon appears in the system tray. Left-click it to reopen
   the UI (always open the UI from the tray: links are one-time). The tray menu has
   Pause/Resume, **Lock** (stops capture, forgets the key, asks for the passphrase again) and
   Quit. Settings > Startup can start Akashic when you sign in.
4. **Models:** Settings > Models lists every model the current settings need (speech, speakers,
   embeddings, the llama.cpp runtime and the chat model, about 11 GB). Nothing downloads until you
   press a button, and files are pinned and checked. Offline or by hand:
   `uv run python scripts\fetch_models.py`. Audio is recorded and queued until the speech model
   is there.

To try it without touching your real data:

```powershell
$env:AKASHIC_DATA_DIR = "$env:TEMP\akashic-test"
uv run akashic
```

**Portable copy:** `.\packaging\build.ps1` writes `dist\Akashic\` (Python, dependencies, CUDA
libraries). Copy the folder anywhere, run `Akashic.cmd`, and download models from Settings >
Models. See [docs/PACKAGING.md](docs/PACKAGING.md).

## Privacy

- No telemetry, uploads or cloud APIs. The only outbound requests are public model downloads you
  ask for.
- Database, screenshots and audio are encrypted at rest; the key exists only in memory while
  unlocked. Design and limits: [docs/SECURITY.md](docs/SECURITY.md).
- Excluded apps, windows and addresses (`[exclusions]` in `config.toml`, password managers,
  private browser windows, banking sites, password fields) are dropped before anything is
  written. Boundaries: [docs/CAPTURE.md](docs/CAPTURE.md).
- To decide when to capture, Akashic reads only the time of your last input
  (`GetLastInputInfo`), never which key was pressed.

## Where things live

Everything is in `%LOCALAPPDATA%\Akashic\` (or `AKASHIC_DATA_DIR`):

| Path | Contents |
|---|---|
| `db\akashic.db`, `db\keyfile` | encrypted database; your data key wrapped by your passphrase |
| `media\` | encrypted screenshots and (under `media\audio\`) speech recordings |
| `models\`, `runtime\` | models and `llama-server.exe` (re-downloadable) |
| `logs\` | `app.log`, `capture.log`, `worker.log` (counts and errors, never captured content) |
| `config.toml` | settings with comments, also editable in Settings; restart after editing by hand |

Back up the whole folder, **including `db\keyfile`**, and remember the passphrase. Retention is
`forever` by default; Settings > Retention deletes data explicitly.

## Development

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1   # run before committing
```

Format, lint, type-check and tests. `scripts\selftest.ps1` is a separate hardware smoke test
(real audio devices, OCR, GPU). Set `AKASHIC_CAPTURE=0` to start with capture idle. Manual checks:
[tests/manual/CHECKLIST.md](tests/manual/CHECKLIST.md). Agents: start with [AGENTS.md](AGENTS.md).

## Troubleshooting

- *`'uv' is not recognized`:* open a new terminal, use `.\akashic.ps1`, or run
  `. .\scripts\env.ps1` to put `uv` on this session's PATH.
- *"Could not unlock":* there is no recovery. Check keyboard layout and caps lock.
- *Nothing captured, or something seems stuck:* open Settings > Debug (Overview, then Logs).
- *An app running as administrator shows no UI Automation text:* Windows blocks reading
  elevated windows from a normal process; OCR is used instead.
- *Sound from a program is missing:* set `[audio] system_device` to part of the name of the
  playback device you actually listen on.
