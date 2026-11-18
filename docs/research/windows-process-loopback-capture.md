# Can per-process loopback capture separate voice chat, game and browser audio on Windows 11?
Date: 2026-11-19 · Context: Python 3.14, Windows 11 build 26200, comtypes 1.5; Akashic `platform/win/audio.py`; earlier spike `docs/baselines/2026-11-17-process-loopback.md` · Goal: decide whether Akashic can record voice chat, game and browser sound as separate streams (audit: leakage M7, overlapping-speech WER 0.75; TODO A5 #110) · Ruled out: none

## Answer
Per-process loopback works on the target PC from Python/comtypes with the Microsoft
ApplicationLoopback sample's parameters: a -15 dBFS tone is captured at exactly -15.0 dBFS, both
when targeting the rendering process and when targeting its parent (tree inclusion through a
launcher works), while an unrelated process and the capturer's own tree give silence [1][2]. It
does not capture streams rendered in the Communications category: the same tone with
`AudioCategory_Communications` comes back as silence, as a report on the same build found [1][3].
WebRTC's Windows audio code sets that category, and an OBS user reports application capture
failing on Discord [4][5], so Discord voice is likely unreachable this way. Per-process streams
therefore fit games, browsers and media apps (non-Communications renderers, targeted at the tree
root); voice chat stays on endpoint loopback, either the recorded endpoint or a dedicated one
such as Wave Link's "Voice Chat" if Discord plays there, unless a real Discord call shows otherwise
[1][6].

## Findings
### Local positive control (target PC, VB-CABLE, inaudible)
- Settings: `VAD\Process_Loopback`, include tree, PCM16 stereo 44.1 kHz,
  LOOPBACK | EVENTCALLBACK | AUTOCONVERTPCM, buffer 0, event-driven, agile completion handler [1][2].
- Tone child, category Other, targeted directly: RMS -15.0 dBFS, peak -12.0 (what it rendered) [1].
- Same child, target = its venv launcher (parent): RMS -15.0 dBFS, so descendants are included [1].
- Target = an unrelated silent process: RMS -96.7 dBFS (±1 LSB floor) [1].
- Target = the capturing process, an ancestor of the renderer: RMS -96.7 dBFS, so the capturer's
  own tree is not captured [1].
- Category Communications, targeting the renderer or its launcher: RMS -96.7 dBFS [1].
- 300 packets in 3 s in every case, none flagged SILENT, so packet arrival says nothing about
  sound [1].
### What Microsoft documents
- Minimum Windows 10 build 20348; include mode captures the render streams of the target process
  and its child processes, exclude mode everything else [7].
- The sample activates the virtual device with a VT_BLOB `AUDIOCLIENT_ACTIVATION_PARAMS`, an agile
  completion handler called on an MTA thread, and the Initialize settings above [2].
- A target with no render streams gives silence; capture is not tied to an endpoint [8].
### Working implementations
- The Rust `wasapi` crate documents that `get_mixformat`, `is_supported`, `get_device_period` and
  `get_current_padding` are not implemented for this client and that `get_buffer_size` returns
  garbage, as the 2026-11-17 spike saw [9][6].
- bozbez/win-capture-audio uses LOOPBACK | EVENTCALLBACK, a 5 s buffer, `SetEventHandle` after
  `Initialize`, and skips SILENT packets [10].
- OBS application audio capture targets a window's process tree in include mode [11].
### The Communications category and voice apps
- Issue #414: Teams desktop captured as silence through process loopback while device loopback
  worked; a commenter on build 10.0.26200 measured Other at RMS ~0.116 and Communications at
  ~0.00001 with the session unmuted, RAW mode and effects off [3].
- WebRTC's Windows audio device module sets `AudioCategory_Communications` [4].
- An OBS forum report says application audio capture does not capture Discord while window audio
  does [5].

## Caveats
- One run per case, one tone, one render endpoint (CABLE Input) [1].
- The 2026-11-17 spike's all-zero result is not reproduced at -15 dBFS with the sample's settings;
  its cause is not isolated (it captured float32 at -90 to -102 dBFS, on the default endpoint, with
  different flags and buffer), so "level" is a hypothesis [1][6].
- Discord's real stream category was not measured; the WebRTC setting and the OBS report are
  indirect, and Discord may use its own audio stack [4][5].
- The Communications report in #414 is one practitioner's measurement on one driver, without a
  Microsoft reply; the local spike is its only confirmation found [1][3].
- Chromium renders from a child audio-service process; only tree inclusion through one launcher
  level was measured, not a real browser tree [1][11].
- Whether Discord plays on the Wave Link "Voice Chat" endpoint is unconfirmed (OPEN A5 #151) [6].

## Sources
[1] Local positive control, process loopback — `eval/quality/spikes/process_loopback_control.py`, `eval/quality/spikes/process_loopback_control.json` — tier 1 — target PC, 2026-11-19
[2] ApplicationLoopback sample, `LoopbackCapture.cpp` — https://github.com/microsoft/Windows-classic-samples (Samples/ApplicationLoopback/cpp/LoopbackCapture.cpp:51-137, 207-242, 344-418) — tier 1 — main, 2026-11-19
[3] Windows-classic-samples issue #414 — https://github.com/microsoft/Windows-classic-samples/issues/414 — tier 2 — comment of 2026-10-28
[4] WebRTC `core_audio_utility_win.cc` — https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_device/win/core_audio_utility_win.cc — tier 1 — main, 2026-11-19
[5] OBS forum: Application audio capture does not capture Discord — https://obsproject.com/forum/threads/application-audio-capture-does-not-capture-discord.180888/ — tier 2 — accessed 2026-11-19
[6] Akashic process loopback spike and audio rules — `docs/baselines/2026-11-17-process-loopback.md`, `OPEN.md` A5 #151 — tier 1 — 2026-11-17
[7] AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS — https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_process_loopback_params — tier 1 — updated 2024-02
[8] Application loopback audio capture sample page — https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/ — tier 1 — 2026-02-19
[9] wasapi-rs `new_application_loopback_client` — https://github.com/HEnquist/wasapi-rs (src/api.rs:810-913) — tier 1 — default branch, 2026-11-19
[10] bozbez/win-capture-audio `audio-capture-helper.cpp` — https://github.com/bozbez/win-capture-audio (src/audio-capture-helper.cpp:18-66, 97-150) — tier 1 — default branch, 2026-11-19
[11] OBS win-wasapi — https://raw.githubusercontent.com/obsproject/obs-studio/master/plugins/win-wasapi/win-wasapi.cpp — tier 1 — master, 2026-11-19
