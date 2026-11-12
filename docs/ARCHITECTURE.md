# Architecture

## Boundaries and rationale

The application shell owns unlock, user interaction and child-process supervision. Capture
records lightweight evidence; a separate worker performs deferred processing and model inference.
This isolation keeps a model crash or expensive job from taking capture down with it.
Capture stays CPU-only; the worker owns heavy inference resources.

Windows integration belongs behind small interfaces. Core code decides exclusions, persistence
and encryption; platform adapters report facts. Keep business rules out of Windows glue and
avoid adding a service or framework where local code and the existing queue suffice.

The persistent queue separates recording from processing so work survives interruption and can
wait while the user plays a game. Recovery must avoid both lost evidence and jobs that starve
all later work. A refactor must preserve evidence links and make stalled processing observable.

## Source map

All code is under `src/akashic/`. Packages only; read the modules for the rest.

| Process | Entry point | Packages it uses |
| --- | --- | --- |
| App shell: unlock, tray, local UI server, supervision | `app.py` (`supervisor.py`, `tray.py`, `unlock*.py`) | `api/` (HTTP handlers) serving `ui/` (static HTML/JS/CSS) |
| Capture (CPU only) | `capture/main.py` | `capture/` (screen, media, audio, exclusions) |
| Worker: deferred jobs and models | `pipeline/main.py` | `pipeline/` (queue runner, transcription, speakers, episodes, embeddings, Ask) |

Shared by all: `storage/` (encrypted database, media files, migrations, retention), `search/`
(time parsing, planner, keyword/semantic/hybrid ranking, answer building), `platform/win/`
(Windows API adapters), and the top-level `config.py`, `ipc.py`, `log.py`, `offline.py`.

## Resource ownership

The worker owns model lifecycles. Serial jobs alone do not bound memory: resident models,
allocators, child processes and other applications all contribute. Coordinate model release and
measure actual RAM/VRAM and responsiveness before claiming a memory improvement.

Background processing should yield to games and interactive use. Explicit requests may process
stored memories while capture is paused, but must not resume recording. Avoid promises of
immediate unload or preemption without evidence from the actual implementation.

## Lifecycle guarantees

Capture starts only after unlock. Lock and quit must stop all writers and prevent a concurrent
restart from reviving them. Pausing, restarting and deleting data must preserve the user's
recording choice, including while children are starting.

Full-data deletion requires an exclusive boundary with recording, processing and API writers;
it is not ordinary age-based retention. See [STORAGE.md](STORAGE.md) for the contract.
Implementation gaps and proposed structural changes belong to the [audits](audits/).
