# Process loopback spike 2026-11-17

Measurements behind TODO A5 (#110) of the audio rules ([CAPTURE.md](../CAPTURE.md)): whether
the per-process WASAPI loopback (`ActivateAudioInterfaceAsync` on `VAD\Process_Loopback`) can be
opened from Python on a COM-multithreaded thread the way production sets COM up, what the
resulting `IAudioClient` returns, whether it follows a process tree and a late child, whether a
muted session and a very quiet session reach it, how fast it starts, and what polling it costs.
Only processes started by the spike were targeted (a synthetic tree of tone renderers, all in a
Job object with `KILL_ON_JOB_CLOSE`, started with `CREATE_NO_WINDOW`, killed in a `finally`). No
user process was targeted and nothing the user hears or says was captured. Each process wrote
at most -90.1 dBFS, but streams played together: the summed endpoint mix could reach about -78
dBFS in the -90.1 tree runs (four streams) and about -84 dBFS in the CPU runs (two streams), and up to -89.96 dBFS in the -102 dBFS tree runs, above
the -90 dBFS per-sample rule (see Safety). Captured samples stayed in memory; this report and the
raw result files hold numbers, counts, booleans, HRESULTs and fixed labels only (no device names,
no user process names, no captured content). The script was throwaway and is not kept; no file
under `src/` changed.

**Headline: the activation path works and is fast, but no sample of any process-loopback stream
was ever non-zero.** With tones at -102 and -90.1 dBFS, muted and unmuted, every packet was zero
and none was flagged `SILENT`. Whether process loopback follows a tree, a late child or an
outsider cannot be decided from this run (see Muted and -90 dBFS).

## Machine

AMD Ryzen 7 7800X3D 8-Core Processor, Windows 11 build 26200 (10.0.26200), Python 3.14.10,
comtypes 1.5.0, numpy 2.5.5, QPC frequency 10 000 000 Hz. Default render endpoint mix format
(`GetMixFormat`): tag 65534 (`WAVE_FORMAT_EXTENSIBLE`, `cbSize` 22), 2 channels, 48 000 Hz,
32 bits, block align 8; `GetDevicePeriod` default 100 000 hns (10 ms), minimum 30 000 hns (3 ms).

## Method

- **Run command.** `uv run --isolated --no-project --python 3.14 --with comtypes --with numpy
  python -I spike.py main --phases ...`. One file, three roles (`main`, `root`, `render`);
  children are started with the same interpreter and talk to `main` over stdin/stdout pipes (PIDs,
  `GO`/`SPAWN`/`STOP`, QPC onsets, rendered peaks). Every PID used is the one the process reports
  itself (`os.getpid()`), not the PID of the launcher `subprocess` returned.
- **COM.** `sys.coinit_flags = 0` before comtypes is imported, `CoInitializeEx(0)` on every
  thread that uses COM; each stream is used by the one thread that opened it. Interface
  definitions for `IAudioClient`, `IAudioCaptureClient`, `WAVEFORMATEX`, `PROPVARIANT` are copied
  from `src/akashic/platform/win/audio.py`; `IAudioRenderClient`, `ISimpleAudioVolume`,
  `IActivateAudioInterfaceAsyncOperation`, `IActivateAudioInterfaceCompletionHandler` and
  `IAgileObject` exist in the script only.
- **Activation.** `ActivateAudioInterfaceAsync("VAD\\Process_Loopback", IID_IAudioClient,
  VT_BLOB AUDIOCLIENT_ACTIVATION_PARAMS{type 1, pid, mode}, handler)`, mode 0 include tree, 1
  exclude tree. The handler is a comtypes `COMObject` (completion handler plus `IAgileObject`)
  that calls `GetActivateResult` and sets an event; the operation object is kept until the
  event or the 5 s timeout. "Call to completion" is `perf_counter` before the call to the moment
  the handler ran.
- **Formats.** All three set-ups use shared mode, a 1 s buffer, and these flags:
  1. mono float32 16 kHz, `AUTOCONVERTPCM | SRC_DEFAULT_QUALITY | LOOPBACK`, polling (production);
  2. the same plus `EVENTCALLBACK` (event handle set before `Start`, wait then drain);
  3. stereo float32 48 kHz, the same flags as 1, polling.
  Getters were called after `Initialize`; `GetMixFormat` also before. Packets were read for 3 s
  from a tree whose child rendered a tone.
- **Synthetic tree.** R (`root`, renders nothing) -> A (tone f1) -> A1 (tone f2), an outsider O
  started directly by the spike (tone f3), and B, a late child of R, spawned about 4 s after the
  tones began and rendering from its first buffer (tone f4). Tones: f1 1013 Hz, f2 1721 Hz, f3
  2593 Hz, f4 4099 Hz (all inside 500-6000 Hz, no harmonic relations). Targets, captured at the
  same time by two include-mode streams (mono 16 kHz, 10 ms poll): R and A1.
- **Rendering.** Each render process opens its own stream on the default render endpoint: shared
  mode, stereo float32 48 kHz with `AUTOCONVERTPCM | SRC_DEFAULT_QUALITY | NOPERSIST`, its own
  (default per-process) session, a 40 ms fill target, `Start` before the first write. Tone level
  per stream: -102 dBFS peak in the main runs (four streams in phase sum to -89.96 dBFS, just above -90; the
  real phase-summed peak was not measured), and -90.1 dBFS peak in a second set of runs (each stream below -90 dBFS, the sum of
  four can reach -78 dBFS). The peak is the maximum absolute value of the float samples written,
  reported by each process.
- **Mute.** In the muted runs each process calls `ISimpleAudioVolume::SetMute(TRUE)` after
  `Initialize` and before `Start`, then `GetMute`, and refuses to start when it does not read
  TRUE. Unmuted runs set and read FALSE.
- **Analysis.** A tone's level is a single-bin Goertzel on the captured 16 kHz samples of a 4 s
  window with a Hann window, expressed as the amplitude of a sine (dBFS peak; floor clamped at
  -240). A tone is "present" at or above the rendered level minus 6 dB (-108 dBFS in the -102
  runs, -96.1 in the -90.1 runs). The windows start 1.5 s after B's first non-silent buffer (all
  four tones playing) or, for "before B", 1 s after the last of A, A1 and O started and end 0.2 s
  before B started. Non-zero samples count the float samples that are not exactly 0.0 (a packet
  flagged `SILENT` counts as zeros).
- **Latency.** Three runs on a tree R -> A, one include stream on R polled every 1 ms; the child
  sends the QPC taken after it released its first non-silent buffer; the capture side takes
  `GetBuffer` QPC position (100 ns units) plus the sample offset of the first non-zero sample.
- **CPU.** The capture process's user plus kernel time (`GetProcessTimes`) over 60 s as a
  percentage of one core, for the production read (poll 10 ms, `GetNextPacketSize`/`GetBuffer`
  copy/`ReleaseBuffer`, as `_WasapiInput.read`) on an include stream of R with A and A1
  rendering at -90.1 dBFS, followed by the same loop with no stream open (sleep 10 ms only).
  Three runs of each, alternating. Total machine CPU is read from `GetSystemTimes` over the same
  60 s.
- **Exclude and exited target.** Exclude mode only activated and initialized, never `Start` or
  `GetBuffer`. The exited target is a spike child that exited before the activation; the spike
  held its own `OpenProcess` handle (`SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION`) so the PID
  could not be reused; no `Start`, no reads.

## Activation

n = 20 calls per mode against R, which was alive. "Callback" is the completion handler arriving
within the 5 s timeout. Times are call to completion in ms; the handler ran in well under a
millisecond, so these are close to the cost of the call itself.

| Mode | Succeeded (activation result S_OK and `IAudioClient` obtained) | Callback within 5 s | HRESULT of each failure | Median ms | p95 ms | Max ms |
| --- | --- | --- | --- | --- | --- | --- |
| include tree (0) | 20 / 20 | 20 / 20 | none | 0.06 | 0.10 | 0.84 |
| exclude tree (1) | 20 / 20 | 20 / 20 | none | 0.05 | 0.07 | 0.07 |

## Formats

One run per set-up (tree R -> A, A rendering at -102 dBFS), include mode on R. `Initialize`
HRESULT `OK` is S_OK. `0x80004001` is `E_NOTIMPL`. The packet counts are over 3 s.

| Set-up | `Initialize` | `GetMixFormat` | `GetBufferSize` | `GetStreamLatency` | `GetDevicePeriod` | Frames per packet (median, min-max) | Packets | Packets flagged `SILENT` | Non-zero samples |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1. mono f32 16 kHz, polling | OK (0.19 ms) | `0x80004001` | 160 frames | `0x80004001` | `0x80004001` | 160 (160-160) | 300 | 0 | 0 |
| 2. 1 plus `EVENTCALLBACK` | OK (0.09 ms) | `0x80004001` | 160 frames | `0x80004001` | `0x80004001` | 160 (160-160) | 300 | 0 | 0 |
| 3. stereo f32 48 kHz, polling | OK (0.09 ms) | `0x80004001` | 480 frames | `0x80004001` | `0x80004001` | 480 (480-480) | 300 | 0 | 0 |

- `GetMixFormat` returned `E_NOTIMPL` both before and after `Initialize`.
- With 1 s requested, the buffer was 160/480 frames (10 ms) of the requested format; packets are
  10 ms (100 per second) in all three.
- The event-driven set-up delivered the same 300 packets in 3 s as polling.
- The 1 s buffer duration was passed to all; only one value was tried.

## Include tree

Four tones played in the tree (A under R, A1 under A, the outsider O, B added to R late). Level in
dBFS of the single-bin Goertzel over the 4 s window with all four playing, verdict against the
stated threshold, non-zero samples in that window, captured/rendered ratio (linear tone amplitude
over the rendered peak the process reported). Expected: on R f1, f2, f4 present and f3 absent;
on A1 only f2 present. -102 dBFS runs, not muted:

| Target | Tone | Level dBFS | Verdict (present at >= -108) | Expected | Non-zero samples in window | Captured / rendered peak |
| --- | --- | --- | --- | --- | --- | --- |
| R | f1 (A) | -240.0 | absent | present | 0 | 0.0 |
| R | f2 (A1) | -240.0 | absent | present | 0 | 0.0 |
| R | f3 (O) | -240.0 | absent | absent | 0 | 0.0 |
| R | f4 (B, late) | -240.0 | absent | present | 0 | 0.0 |
| A1 | f1 (A) | -240.0 | absent | absent | 0 | 0.0 |
| A1 | f2 (A1) | -240.0 | absent | present | 0 | 0.0 |
| A1 | f3 (O) | -240.0 | absent | absent | 0 | 0.0 |
| A1 | f4 (B, late) | -240.0 | absent | absent | 0 | 0.0 |

-240.0 is the floor: the window is exactly zero. The same table at -90.1 dBFS (present at >=
-96.1) is identical: all eight tones -240.0, absent, 0 non-zero samples, ratio 0.0. The "before B"
windows (3.15 s on R, 3.16 s on A1) are also all zero in both runs.

Verdict: **the expected pattern cannot be judged: no tone was present, not even A1's own tone f2
on A1.** What the
run shows is that sample flow at these levels is zero; it cannot say whether the tree, the late
child or the outsider rule works.

Per target, packets after the tones started: R 1041 (-102) and 1042 (-90.1); A1 1036 and 1042;
0 flagged `SILENT`; 0 non-zero samples; captured peak 0.0.

## Idle tree

Before any tone started (nothing rendering in R, A, A1, O; B did not exist yet), 3.0 s from the
return of `Start`, include on R and on A1:

| Run | Target | Packets | Packets per second | Share flagged `SILENT` | Non-zero samples |
| --- | --- | --- | --- | --- | --- |
| -102 dBFS tree | R | 299 | 99.7 | 0.0 | 0 |
| -102 dBFS tree | A1 | 299 | 99.7 | 0.0 | 0 |
| -90.1 dBFS tree | R | 299 | 99.7 | 0.0 | 0 |
| -90.1 dBFS tree | A1 | 299 | 99.7 | 0.0 | 0 |

Packets arrive at 100 per second from the start with an idle tree and are never flagged `SILENT`:
silence is delivered as real zero packets, so "a packet arrived" says nothing about sound.

## Muted and -90 dBFS

Muted session first: the same tree with every process's session muted by
`ISimpleAudioVolume::SetMute(TRUE)`. `GetMute` read TRUE in 4 of 4 processes (A, A1, O, B) before
each render stream started, at both levels. Then the same tree at the same levels with no mute.

| Level per stream | Session | Include target | Packets after tones started | Non-zero float samples | Captured peak |
| --- | --- | --- | --- | --- | --- |
| -102 dBFS | muted | R | 1043 | 0 | 0.0 |
| -102 dBFS | muted | A1 | 1041 | 0 | 0.0 |
| -102 dBFS | not muted | R | 1041 | 0 | 0.0 |
| -102 dBFS | not muted | A1 | 1036 | 0 | 0.0 |
| -90.1 dBFS | muted | R | 1042 | 0 | 0.0 |
| -90.1 dBFS | muted | A1 | 1040 | 0 | 0.0 |
| -90.1 dBFS | not muted | R | 1042 | 0 | 0.0 |
| -90.1 dBFS | not muted | A1 | 1042 | 0 | 0.0 |

Verdict: **neither the muted nor the unmuted -90 dBFS run shows non-zero samples (also none at
-102 dBFS), so sample flow, and with it the mute behaviour, cannot be verified without sound.**
The same reader code and the same flags give 160-frame packets at the expected rate, so the
stream itself runs. The cause of the all-zero capture is open; candidates, unranked:
- the process-loopback path itself;
- the level (-90.1 dBFS is about one 16-bit LSB);
- the reader;
- the render streams went to the default render endpoint, which may route through a virtual
  device;
- `AUTOCONVERTPCM` and format conversion on either side (set-up 3, at the mix format, ran only at
  -102 dBFS, so format is not ruled out at -90.1).

One option to separate them is a positive control at an audible level, which this spike does not
render. Another is an endpoint-loopback read of the same quiet tones, which separates level from
path but also reads other audio playing in memory (a privacy cost).

## Latency

Three runs, tree R -> A, include on R, poll 1 ms, A rendering at -90.1 dBFS.

| Run | Activation ms | `Initialize` ms | `Start` call ms | Start to first packet ms | First packet flags | Packets before the tone started | Onset latency (child release QPC to first non-zero sample QPC) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.75 | 0.23 | 0.04 | 12.4 | 0 | 149 | not measurable |
| 2 | 0.09 | 0.13 | 0.05 | 10.7 | 0 | 149 | not measurable |
| 3 | 0.09 | 0.12 | 0.04 | 11.7 | 0 | 149 | not measurable |

The child reported its first non-silent buffer release in each run; no non-zero sample was ever
captured within the 1.5 s after it, so there is no onset time. The first packet arrived 10.7-12.4
ms after `Start`, about one 10 ms packet period.

## CPU

Capture process user + kernel time over 60 s as a percentage of one core, and the machine's total
CPU over the same 60 s (all cores, every process).

| Run | With stream: capture % of one core | With stream: packets | With stream: machine CPU % | No stream: capture % of one core | No stream: machine CPU % |
| --- | --- | --- | --- | --- | --- |
| 1 | 0.391 | 5998 | 6.2 | 0.130 | 9.4 |
| 2 | 0.260 | 5999 | 8.6 | 0.000 | 6.6 |
| 3 | 0.339 | 5998 | 10.2 | 0.026 | 6.8 |

The stream loop costs about 0.26-0.39% of one core against 0.00-0.13% for the sleep-only loop;
the mean difference is 0.28 points of one core. The packets carried zeros (the cost of copying
real samples is the same size: 640 bytes per packet). The machine CPU column swings between 6
and 10% without relation to the stream, which is the background load of another spike running on
the PC at the same time; the capture process's own time is not affected by that load except
through scheduling.

## Exclude and exited target

Exclude mode against the live R: activation and `Initialize` only, in each set-up; never `Start`
or `GetBuffer`, so no audio of other processes was captured. **What exclude mode contains was
not checked.**

| Set-up | Activation | `Initialize` | `GetMixFormat` | `GetBufferSize` | `GetStreamLatency` | `GetDevicePeriod` |
| --- | --- | --- | --- | --- | --- | --- |
| 1. mono f32 16 kHz, polling | OK | OK (0.16 ms) | `0x80004001` | 160 frames | `0x80004001` | `0x80004001` |
| 2. 1 plus `EVENTCALLBACK` | OK | OK (0.09 ms) | `0x80004001` | 160 frames | `0x80004001` | `0x80004001` |
| 3. stereo f32 48 kHz, polling | OK | OK (0.07 ms) | `0x80004001` | 480 frames | `0x80004001` | `0x80004001` |

Exited target: a spike child (R-like, rendering nothing) was asked to stop and had exited
(`WaitForSingleObject` on the spike's own handle returned signalled) before each call; the spike
kept the handle open so the PID could not be reused. Set-up 1, no `Start`:

| Mode | Activation call | Activation result | `Initialize` |
| --- | --- | --- | --- |
| include tree | S_OK, completed in 0.10 ms | S_OK (0x00000000) | OK |
| exclude tree | S_OK, completed in 0.06 ms | S_OK (0x00000000) | OK |

An exited PID is accepted by both the activation and `Initialize`: neither reports that the target
is gone, so a stream on a dead process cannot be told from a live silent one by those calls.

## Safety

Each item is enforced or checked by the script:

- Rendered samples, per process: the largest absolute value any render process wrote was -102.0
  dBFS in the main runs and -90.1 dBFS in the second set (limit -90 dBFS), muted phases included.
  The script asserts each process's amplitude is at or below the limit and reports the written
  peak.
- **Summed endpoint mix could exceed the -90 dBFS rule.** The -90.1 dBFS tree runs, muted and
  unmuted, played four streams at once (A, A1, O, B); the mix could reach about -78.1 dBFS. The CPU
  runs played A and A1 together, up to about -84.1 dBFS. The -102 dBFS runs sum to at most -89.96
  dBFS for four streams, just above -90 (in-phase bound; real peak not measured). Muted runs
  The endpoint mix was not measured; muted runs had `GetMute` TRUE in every process.
- Render streams use `AUDCLNT_STREAMFLAGS_NOPERSIST` (asserted in the render code).
- Every process was started with `CREATE_NO_WINDOW` and assigned to a per-scenario Job object with
  `KILL_ON_JOB_CLOSE`; each scenario ends in a `finally` that sends `STOP`, terminates the job
  and checks the job's active process count. The count read 0 in the runs whose results were
  saved last (earlier runs of the same files were overwritten when a later phase list was run).
- Include mode only targeted PIDs reported by processes the spike started; the counter of
  include targets that were not (the script raises) read 0.
- Captured samples stayed in memory; nothing was written to a file but numbers.
- No file under `src/` changed by this spike, and the script is not in the repository.

## Limits

- **Zero samples.** Nothing was non-zero at -102, -90.1, muted or not, so the tree, late-child
  and outsider behaviour, the onset latency and the mute behaviour are not measured. A stream
  that delivers 100 non-`SILENT` zero packets per second is also what an idle tree gives. The
  cause is open, unranked: the process-loopback path, the level, the reader, routing of the
  render streams through a virtual default endpoint, `AUTOCONVERTPCM` and format conversion on
  either side (set-up 3 ran only at -102 dBFS). One option is a positive control at an audible
  level (not rendered here, by design); another is an endpoint-loopback read of the same quiet
  tones, which separates level from path but also reads other audio in memory.
- **Exclude mode contents not checked.** Only activation, `Initialize` and the getters ran.
- **The Discord tree is not measured** (TODO A5 #110 stays open for it), nor Wave Link routing
  nor in-game voice.
- **Synthetic tones are not voice.** Pure tones at one level through one render path; real voice
  chat, compression and a game's own sessions were not used.
- **One endpoint.** The default render endpoint with a 48 kHz 32-bit float mix; other endpoints,
  other formats and an endpoint change while a stream is open were not tried.
- **Sizes.** n = 20 per activation mode (p95 is the second-largest), three runs of latency and
  CPU, one run per format set-up, one tree per level. The activation times are sub-millisecond
  and were taken against a tree that was already running; a first activation after boot or with
  a busy audio service was not measured.
- **Level.** The tones at -90.1 dBFS are about one 16-bit LSB; the engine's treatment of values
  below that was not isolated from the process-loopback path.
- **Background load.** A browser spike ran on the PC at the same time as the CPU runs, so the
  absolute CPU numbers carry that load; the total machine CPU column shows it (6-10%). The
  capture process's own time is the number to compare, and it is small against the noise.
- **PIDs.** The spike's children run behind a launcher process; targets were the PIDs the
  children reported. Whether a target given as the launcher's PID would include its child was
  not tried.
- **Polling rates.** The CPU runs use production's 10 ms poll; the latency runs use a 1 ms poll
  so the first-packet time has about 1 ms resolution.
