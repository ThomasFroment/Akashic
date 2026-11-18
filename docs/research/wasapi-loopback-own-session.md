# Does a WASAPI loopback capture show up as an audible session, and how should a recorder exclude itself?
Date: 2026-11-19 · Context: Python 3.14, Windows 11 build 26200, comtypes; Akashic `platform/win/audio.py` (`_audible`, `_WasapiInput`) · Goal: stop Akashic listing its own capture process as an audible source of every system segment (audit #187) · Ruled out: none

## Answer
Yes. On the target PC, opening Akashic's own loopback capture on a render endpoint adds a session
of the capturing process to that endpoint's session list, Active, with a peak equal to the mix
(0.251 next to a tone player's 0.251); it goes Inactive at 0.0 when the capture closes [1]. Microsoft
documents only that a loopback stream belongs to a session "treated the same as a capture session",
not that it is listed on the render endpoint or what its meter reads [2][3]. The standard
self-filter is to skip sessions whose `GetProcessId` equals the enumerating process's own pid, as
screenpipe does [4]; in Akashic that process is the one that opens the loopback stream (the real
interpreter under the venv launcher), so `os.getpid()` catches it with no process-tree walk [1][5].
System-sounds sessions already carry pid 0 on this build and are dropped by `_audible` [1][5].

## Findings
### What the session list shows during loopback capture (local spike)
- With a tone playing into VB-CABLE's "CABLE Input" (its session: Active, peak 0.251), opening
  Akashic's `_WasapiInput` loopback on that endpoint adds a session with the capturing process's
  pid, state Active, peak 0.251 [1].
- After the capture closes, that session stays listed as Inactive with peak 0.0 [1].
- Akashic's `WindowsAudioSessions.active_processes` reports `python.exe` for the endpoint while the
  capture is open [1].
- The system-sounds session is listed with pid 0 on Windows 11 26200 [1].
- In the smoke runs, the audible "foreign" session on the recorded endpoint belonged to the
  capture tree's interpreter (the launcher's child), not to the launcher [5].
### What Microsoft documents
- Every stream belongs to exactly one session; a session is Active when it holds a running stream;
  a session with loopback streams "is treated the same as a capture session" (said about settings
  persistence) [2].
- The Loopback Recording page says nothing about sessions, enumeration or meters [3].
- `IAudioSessionControl2::GetProcessId` returns S_OK, or `AUDCLNT_S_NO_SINGLE_PROCESS` when the
  session spans several processes; `AUDCLNT_E_DEVICE_INVALIDATED` is among its failures [6].
### How other tools exclude themselves
- screenpipe's render-endpoint check skips non-active sessions and skips sessions whose
  `GetProcessId()` equals `GetCurrentProcessId()`; a failed `GetProcessId` counts as another app.
  Its comment gives its own notification sounds as the reason [4].
- screenpipe's capture-side scan skips non-active sessions and pid 0 ("system-sounds /
  cross-process session") and drops its own pid or process names; it walks no children for
  self-exclusion, and walks parents only to attribute a target app [7].
- screenpipe warns that `SessionInstanceIdentifier` is per endpoint and rotates when an app
  changes device, so it is not a process identity [7].
- EarTrumpet maps a system-sounds session (`IsSystemSoundsSession() == S_OK`) to pid 0 to work
  around a Windows 10 bug where that session reports taskhostw.exe's pid, and accepts both S_OK
  and `AUDCLNT_S_NO_SINGLE_PROCESS` from `GetProcessId` [8].
- OBS application capture targets a process tree with process loopback
  (`PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`) and does not enumerate sessions to decide
  audibility [9].

## Caveats
- The spike measured one endpoint (VB-CABLE) and one tone; that the peak mirrors the mix rests on
  this measurement alone [1].
- The EarTrumpet system-sounds guard targets a Windows 10 bug; on this Windows 11 build the
  system-sounds session has pid 0 and is already dropped, and a taskhostw pid would never equal
  Akashic's own pid, so it adds nothing to self-exclusion [1][8].
- Whether a per-process loopback (`VAD\Process_Loopback`) client is also listed this way is not
  documented and was not measured [2][3].
- The OBS reading rests on a fetched summary of `win-wasapi.cpp`, not a full read [9].

## Sources
[1] Local spike, loopback self-session on CABLE Input — `eval/quality/spikes/loopback_self_session.py`, `eval/quality/spikes/loopback_self_session.json` — tier 1 — target PC, 2026-11-19
[2] Audio Sessions — https://learn.microsoft.com/en-us/windows/win32/coreaudio/audio-sessions — tier 1 — updated 2025-03-11
[3] Loopback Recording — https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording — tier 1 — updated 2025-04-16
[4] screenpipe `endpoint_has_other_session` — https://github.com/mediar-ai/screenpipe (crates/screenpipe-audio/src/core/device.rs:1588-1626) — tier 1 — clone e282f3e, 2026-11-18
[5] Akashic `_audible`, platform wiring, smoke observer logs — `src/akashic/platform/win/audio.py:544-557`, `src/akashic/platform/win/__init__.py:48-50`, `eval/quality/results/20261118-smoke-s11-target-2220/truth/os.jsonl`, `guard.jsonl` — tier 1 — 2026-11-18
[6] IAudioSessionControl2::GetProcessId — https://learn.microsoft.com/en-us/windows/win32/api/audiopolicy/nf-audiopolicy-iaudiosessioncontrol2-getprocessid — tier 1 — 2024-02-22
[7] screenpipe meeting-process scan — https://github.com/mediar-ai/screenpipe (crates/screenpipe-audio/src/meeting_processes.rs:45-61, 680-725) — tier 1 — clone e282f3e, 2026-11-18
[8] EarTrumpet `AudioDeviceSession.cs` — https://github.com/File-New-Project/EarTrumpet (EarTrumpet/DataModel/WindowsAudio/Internal/AudioDeviceSession.cs:150-290) — tier 1 — clone ca7c682, 2026-11-15
[9] OBS win-wasapi — https://raw.githubusercontent.com/obsproject/obs-studio/master/plugins/win-wasapi/win-wasapi.cpp — tier 1 — master, 2026-11-19
