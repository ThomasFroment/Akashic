# Phase 0: scope of the recorded and derived data quality audit

One table row per dimension, with the metric, where ground truth comes from, the existing harness
and its latest baseline, the matching method report, and the TODO/OPEN items that cover it.
Ask and answer quality are out of scope. Code references are modules; findings with line numbers
come in Phase 2.

"Driver log" is the `eval/quality/` driver's own action log (what it opened, focused, showed and
played, with monotonic and wall times). "OS log" is the harness's independent observer:
`EVENT_SYSTEM_FOREGROUND`/`EVENT_OBJECT_NAMECHANGE` hooks, `GetForegroundWindow` polled at 20 Hz,
per-endpoint audio sessions (`IAudioSessionManager2`) polled at 10 Hz, lock/sleep/device-change
notifications. Akashic's own tables are never truth; they are what is scored.

## Metric definitions (refined)

| Id | Metric | Definition |
| --- | --- | --- |
| M1.1 | Change recall | Visible changes the driver made (a page, text block, chat line or image shown for ≥ its dwell) whose content is present in a stored frame of the right monitor taken while it was shown, / all such changes. Split by focused / unfocused monitor, and by game mode. |
| M1.2 | Frames per hour | Stored frames per monitor per hour, split into useful (a frame that carries at least one change, M1.1) and useless (no driver change since the previous stored frame of that monitor). Useless-frame ratio = useless / all. |
| M1.3 | Capture delay | Time from a driver change to the first stored frame that shows it; p50 / p90 / max per monitor role. |
| M2.1 | Label accuracy | Per stored app window row: `process` (and resolved app for UWP/ghost/PWA) equals the driver's truth for that HWND. Per frame: the focused row is the OS log's foreground window at the frame time (± 100 ms). |
| M2.2 | Unknown rate | Share of truth-visible, non-excluded window-seconds that Akashic stored as hidden/unknown (excluded only because the app could not be resolved), per case (UWP, PWA, launcher, elevated, ghost). |
| M3.1 | CER / WER of screen text | Character and word error rate of the stored text of a window against the driver's rendered text (the scenario page knows every string it drew), after NFC, case and whitespace folding; reported by language (FR, EN, mixed), font size (≤ 11 px, 12-15, ≥ 16), theme, monitor resolution, and source (UIA, OCR live, OCR deferred). Accent-folded CER reported beside exact CER. |
| M3.2 | Duplicate-character ratio | Stored characters in a window's text history beyond the first stored copy of each distinct line, / all stored characters, per hour. Measures the cost of re-storing unchanged text. |
| M3.3 | Unique-text recall | Distinct driver text lines (each with a nonce word) found by Akashic's own FTS (`frame_windows_fts`) at the end of the run, / lines shown long enough to be captured. |
| M4.1 | Identity accuracy | For each truth item: browser URL of the stored row (exact after normalisation), tab title, video title, channel, series name + season + episode, game name. Accuracy = right / truth items; wrong and missing reported separately. |
| M5.1 | Hole seconds | Seconds of the run where the timeline (`app_sessions` → `app_bands`) shows no app and no typed gap, while truth says an app was in front and recording was on. |
| M5.2 | Overlap seconds | Seconds covered by more than one app band, or by a band and a gap at once. |
| M5.3 | Duration error | Per (app, truth focus run): \|stored duration − truth duration\|; reported as total absolute error / truth time, and per-app relative error for the top apps. |
| M5.4 | Gap typing | Truth non-recording periods (lock, sleep, pause, UI open, device missing) found as a gap of the right reason, by seconds. |
| M6.1 | Boundary error | Per media or game session: \|start − truth start\| and \|end − truth end\| (s); split counts (one truth session stored as several rows) and merges. |
| M7.1 | Leakage | Seconds of a stored system segment's audio attributed to process P while truth says P was silent; and, for each segment, share of energy from processes not in its stored sources (from the driver's per-process playback logs). |
| M7.2 | Attribution accuracy | Stored segment sources and speech tag (`you`, `voice chat`, `game`, `media`) against truth (which processes played speech during the segment, which stream the words came from). |
| M8.1 | Voice detection | Speech recall (truth speech seconds covered by a stored segment) and false-alarm seconds per hour (stored segment seconds with no truth speech), per stream and condition (under game sound, music, overlap). |
| M8.2 | DER | Diarization error rate (missed + false alarm + confusion, 250 ms collar) on system segments against truth turns. |
| M8.3 | Authors | Per transcript segment: correct / wrong / unknown author against truth speaker; returning speakers mapped to the same profile across sessions (profile purity and fragmentation). |
| M9.1 | WER / CER per language | Corpus reference vs stored transcript, per FR / EN / mixed, per condition (clean, under game sound, overlap, continuation cut). Failures count as full deletions. |
| M9.2 | Hallucinated words per hour | Words in transcripts of segments whose audio has no truth speech, plus inserted words not aligned to the reference, per hour of audio. |
| M9.3 | Term hits | Recall of the scenario's names and terms (speaker names, game and project names) in transcripts where truth speech contains them. |
| M10.1 | Canary hits | Count of planted canary strings (excluded pages, private windows, excluded app, excluded audio) found anywhere: SQL (every table, FTS shadow tables), media files after decryption, logs, thumbnails cache, temp folders, export. Must be 0. |
| M10.2 | Plaintext at rest | Files under the data dir (and temp dirs written by Akashic) that are not the encrypted formats, or whose bytes contain a canary or a scenario nonce. Must be 0. |
| M10.3 | Outbound connections | TCP/UDP connections from Akashic processes (capture, worker, shell, llama-server) to non-loopback addresses during the run (`GetExtendedTcpTable`/`GetExtendedUdpTable` polled at 2 Hz). Must be 0. |
| M11.1 | Boundary F1 | Episode spans vs truth activities (video, game session, call, work or browsing block), matched at IoU ≥ 0.5; precision, recall, F1 per kind. |
| M11.2 | Title accuracy | Episode title names the truth item (video title, game, call app + people, project) under normalised match. |
| M11.3 | People accuracy | `episode_people` against truth speakers of the call. |
| M11.4 | Useless-episode rate | Episodes that match no truth activity, or duplicate one already matched, / all episodes. |
| M12.1 | Index coverage and delay | Share of stored text and transcripts present in FTS and passages/vectors at end of run, and time from capture to searchable (#124). Added dimension. |
| M13.1 | Life-event loss | Seconds of truth activity lost (no frame, no segment, no typed gap) within 60 s after each life event (lock, sleep, pause, device change, busy disk). Added dimension. |
| G1-G4 | Guardrails | Akashic CPU (% of one core, p50/p95, per process), VRAM (MiB, per process, NVML/`nvidia-smi`), game frame-time loss (p50/p99 frame time and fps of the synthetic game and Outer Wilds, with vs without Akashic, same scene), disk (bytes written per hour, store growth per day extrapolated). |

## Dimensions

| # | Dimension | Metrics | Ground truth | Code under test | Existing harness, latest baseline | Method report | TODO / OPEN |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | When the screen is captured | M1.1-M1.3, G1, G4 | Driver log of every visible change (page id, monitor, time); OS log | `capture/screen.py`, `desk.py`, `compose.py`, `screen_writer.py`, `debounce.py`, `uia_budget.py` | None end to end. Spikes: `baselines/2026-11-15-window-composition.md` (disk MB/h per monitor, VRAM per capture), `2026-11-17-uia-under-load.md` (check delay with a stalled browser) | none | TODO A9 "A normal day" (windows, VRAM, frame-copy CPU, side-monitor check time, post-game backlog), A9 disk per day, A3 faster text reader; OPEN A9 F19 (late reads, text wait per interval, worker churn) |
| 2 | App tagging per window and frame | M2.1, M2.2 | Driver log (it starts every process and knows each HWND's exe/AUMID), OS foreground log | `capture/verdicts.py`, `exclusions.py`, `platform/win/windows.py`, `platform/win/process.py`; `pipeline/episodes.app_label` | Hardware tests `tests/hardware/test_native_exclusions.py` (pass/fail only); `2026-11-18-focus-surfaces.md` (popups), `2026-11-16-browser-reads.md` | none | OPEN A9: ghost mapping undocumented API, Brave/Firefox unmeasured; OPEN A4 #150 (QUNS on real games/players) |
| 3 | Text: UIA vs OCR, accuracy, de-duplication | M3.1-M3.3 | Scenario pages render known strings (with nonce words) at known sizes, themes, languages; the driver logs them | `capture/text.py`, `ocr_gate.py`, `platform/win/uia.py`, `platform/win/ocr.py`, `pipeline/ocr_windows.py`, `storage/frames.py` | `2026-11-17-ocr-decode-quns.md` (accent exact 40-46 %, folded 91-96 %, en-US OCR only), `2026-11-17-large-page-text.md` (UIA read times); hardware OCR test (French skipped) | `2026-11-10 - Identical Frame Text Reuse` (#101, vectors only) | TODO B7 #101; OPEN A9 F12 (French OCR, noisy whole-window OCR); OPEN A4 #21 (Electron virtualized lists) |
| 4 | Browser and media identity | M4.1 | Driver log: URL, tab title, video title, channel, series/season/episode of each played item; game name | `capture/media.py`, `platform/win/media.py`, `storage/sessions.find_url`, `capture/verdicts.py` (address reads), `capture/gaming.py` | `2026-11-17-media-sessions.md` (Chrome one session per browser, Zen none in 804 polls); `2026-11-16-browser-reads.md` | none | OPEN A6 F13 (Zen publishes no session; Edge/Brave/Firefox unmeasured); no item parses series/season/episode (none exists in code) |
| 5 | Timeline continuity and durations | M5.1-M5.4 | OS foreground log + driver focus log; driver life-event log | `pipeline/sessions.py` (`IDLE_GAP_MS` 5 min), `api/timeline.py`, `storage/gaps.py`, `capture/gaps.py` | none | `2026-11-10 - App Run Single Pass` (#103, speed only) | OPEN A7 #143 (gap rows not checked in the running app); TODO C5 #149 (visible-window sessions) |
| 6 | Media and game session boundaries | M6.1 | Driver log: play/pause/stop/seek times, game start/quit/alt-tab; OS log | `capture/media.py`, `capture/gaming.py`, `storage/sessions.py` | `2026-11-17-media-sessions.md`; unit tests `test_media_tracker.py`, `test_sleep_clocks.py` | none | OPEN A6 #146 #167 (lock/UAC during a video, running app); OPEN A4 #147, #150 |
| 7 | Sound separated per application | M7.1, M7.2 | Driver's per-process playback logs (each fake process plays known clips at known times); OS audio-session log | `capture/audio.py` (sources, music drop, `voice_elsewhere`), `platform/win/audio.py`, `search/transcripts.py` (`speech_tag`) | `2026-11-17-process-loopback.md` (activation works, all-zero capture, undecided) | none | TODO A5 second loopback, #110 process loopback, per-app streams; OPEN A5 #151 (Wave Link mixer, Discord routing) |
| 8 | Voice detection, segmentation, speakers | M8.1-M8.3 | Corpus clips with speaker ids and word timings (MFA-free: clip boundaries are truth turns), driver play log | `capture/audio.py` (`Segmenter`), `capture/vad.py`, `pipeline/speakers.py`, `storage/speakers.py` | `2026-11-17-continuation-speech.md`; `eval/segment-speaker-attribution` (today authorship 0.701, 7.94 % wrong; C7 0.927 / 3.17 %) | `2026-11-10 - Segment Speaker Attribution` | TODO B1 local speaker comparison (D6), C4 #132-#136; OPEN A5 F15 (quiet gate on fresh windows) |
| 9 | Transcription | M9.1-M9.3 | Corpus reference transcripts (MLS French, LibriSpeech), spliced mixed clips, the scenario's term list | `pipeline/transcribe.py`, `whisper.py`, `speech.py` | `eval/transcript-hallucination-rejection` (today recall 0.727, false rejections 0.86 %; C7 0.921); 2026-11-11 baseline has no WER | `2026-11-10 - Transcript Hallucination Rejection` | TODO C1 #117, C2 #128 #129 #138 #141, C3 #130 #137 #139 #140 #43; B1 STT comparison panel |
| 10 | Privacy | M10.1-M10.3 | Planted canaries (driver knows every string, pixel marker and audio clip it put behind an exclusion) | `capture/verdicts.py`, `exclusions.py`, `browser_state.py`, `capture/audio.py` (excluded drops), `storage/crypto.py`, `log.py`, `api/frames.py` thumbnails, `offline.py` | `2026-11-16-title-change-hold.md`, `2026-11-17-title-hold-pages.md` (Zen leak once at 600 ms), `2026-11-17-presence-timing.md` (2 of 92 hides), `2026-11-18-focus-surfaces.md`; `tests/test_capture_privacy.py`, `test_network_guard.py` | none | TODO A9 UI-open grace; A1 browser audio while private page open; OPEN A9 F10, F11, F21 |
| 11 | Episodes | M11.1-M11.4 | Driver activity log (video, game, call with speakers, work/browsing block, project name) | `pipeline/episodes.py`, `pipeline/sessions.py` | none on recorded data; `eval/conversation-clustering-sweep` (C4 0/0/0), `eval/app-run-single-pass` (speed) | `2026-11-10 - Conversation Clustering Sweep`, `App Run Single Pass` | TODO B5, C5 #149 #168 #170 #171 #172; OPEN B3 #30 |
| 12 | Index coverage and delay (added) | M12.1 | Akashic's own stored rows (coverage is internal consistency), driver times (delay) | `pipeline/embed.py`, FTS triggers, `pipeline/runner.py`, `scheduler.py` | none | none | TODO C1 #124, B8 #34 #37; B3 #143 queue OCR for windows without text |
| 13 | Life-event loss (added) | M13.1 | Driver life-event log + OS lock/sleep/device notifications | `capture/gaps.py`, `capture/audio.py` (device change), `capture/media.py` (sleep) | none (unit tests only) | none | OPEN A3 #23 #160 #161 #162 F3 F4, A7 #143, A2 #60 #163 |

## Guardrails

| Id | Metric | Truth / probe | Existing baseline |
| --- | --- | --- | --- |
| G1 | CPU of capture, worker, shell (p50/p95 % of one core) | `GetProcessTimes` per Akashic process at 1 Hz | `2026-11-11.md` CPU (old code), `2026-11-15-window-composition.md` compose times |
| G2 | VRAM per process (MiB) and total | `nvidia-smi --query-compute-apps` / NVML at 1 Hz | window composition (+362 MB for 42 captures; AV1 1.0-1.2 GB) |
| G3 | Game frame-rate loss | Synthetic game logs every `Present` interval; Outer Wilds via PresentMon-free method: DXGI frame statistics are not reachable from outside, so the harness measures Outer Wilds only by its GPU time share and the synthetic game for frame times | none; TODO A9 "Ask the user: acceptable game frame-rate loss" |
| G4 | Disk per hour and per day | Store size before/after, bytes written by capture (`GetProcessIoCounters`) | window composition MB/h per monitor |

## Earlier evidence that binds the harness

- DECISIONS rule: agents use real data only through an export. The user's 2026-11-18 answer
  allows the harness's own disposable store to record the real desktop while the harness runs;
  the live data folder is never opened.
- Only en-US OCR and English SAPI voices are installed; French OCR accuracy can be measured only
  as the app runs today (fallback to en-US), which is itself the baseline.
- Zen publishes no media session (`2026-11-17-media-sessions.md`): browser-media identity cells
  for Zen are expected to fail and are scored, not skipped.
