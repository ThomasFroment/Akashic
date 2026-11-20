# Audit 2026-11-17: data collection

Date: 2026-11-17 · Code: `claude/akashic-data-collection-audit-e91ad3` at `1877212e` · Context: Windows 11
Pro 10.0.26200, Python 3.14, comtypes WASAPI, Windows.Media.Ocr, Silero VAD v6, faster-whisper 1.2.1
large-v3 · Goal: the best recorded data before Ask is tuned: private, reliable, fast enough on the
AGENTS.md hardware · Ruled out: retrieval and Ask tuning; changes to TODO.md, OPEN.md and production
code (this audit only) · Spikes ran on the user's PC while it was in use (RTX 4090 24 GB, Ryzen 7
9800X3D; the AGENTS.md target is an RTX 4080 Super 16 GB, so GPU numbers are not quoted as targets)

Scope: everything that decides what is recorded and how clean it is: when screens are captured,
screen text (UIA and OCR), text duplication, transcription, splitting speech into conversations,
removing unwanted audio, and attributing sound to its app. Every item is judged against the full
range of questions AGENTS.md describes (videos, series, games, projects, people, files and names
seen on screen, counts, French, English and mixed), not only its five examples.

Evidence rules: code is cited as `path:line` at the commit above. External claims cite tier-1 or
tier-2 sources and were checked by fact-checker agents; one refuter pass challenged the
recommendations (section 6). Spike numbers come from the harnesses in
[`eval/data-collection-2026-11-17/`](../../eval/data-collection-2026-11-17/README.md). Spikes kept
captured audio in memory and report numbers only.

## 1. Summary

| ID | Area | P | Effort | One line |
| :- | :- | :- | :- | :- |
| DC-01 | Audio attribution | P0 | L | Record each app's sound as its own stream (process loopback) instead of one mixed loopback guessed from 1 Hz peak meters |
| DC-02 | Screen text | P0 | S | Upscale 2x before Windows OCR: 1440p window CER 3.5 % → 0.18 % |
| DC-03 | Unwanted audio | P1 | M | Drop songs and singing with a small audio tagger (CED-mini) instead of process names (D7) |
| DC-04 | Screen text | P1 | S | The focused window skips OCR when UIA returns ≥ 40 characters, even when they are only the address bar |
| DC-05 | Conversations | P1 | S | Any game sound counts as a conversation; require a voice-chat signal (the game holds the mic, or mic turns) |
| DC-06 | Screen text | P1 | S | Deferred (game-mode) captures are lossy WebP q70; lossless WebP is 5x faster to encode and OCRs better |
| DC-07 | Text duplication | P1 | M | Every stored frame repeats each window's whole text; record the lines new since the window's last capture (21 % of text, no new line missed) |
| DC-08 | Conversations | P1 | M | Bound calls by the voice app's microphone session instead of only a 5-minute silence rule |
| DC-09 | Transcription | P1 | M | A segment with a French and an English sentence loses one: English kept 2 %; decoding per language run keeps 97 % |
| DC-10 | Screen text | P2 | M | PP-OCRv6-small reads code and accents far better; adopt it as a second recognizer after a GPU spike |
| DC-11 | Capture timing | P2 | M | Side monitors are checked every 15 s with no content trigger; fast chats and brief content can be missed |
| DC-12 | Screen text | P2 | S | A missing French OCR pack silently degrades every French accent; report it as a capture problem |

The top four by impact:

1. **DC-01 per-app audio streams.** Today all system sound is one mixed loopback. A segment's
   "sources" are every process whose session peak crossed −60 dBFS during it
   (`src/akashic/platform/win/audio.py:71`, `:551`, `src/akashic/capture/audio.py:482-506`). During a
   real 2-minute PAYDAY 3 + Discord session measured live, the game was audible during 85 % of
   Discord speech windows, so every Discord transcript of that period would carry both sources.
   Process loopback ran on this PC on the real game and Discord: perfect separation between two
   test streams, about 2 % of one core for 6 streams with their voice detectors,
   endpoint-independent. It fixes attribution
   ("what did Minlo say", "what video", "what game"), removes the `voice_elsewhere` hole, and lets
   an excluded app be dropped without dropping everyone else's speech.
2. **DC-02 OCR upscale.** Windows OCR (the app's fr-FR recognizer) misreads small UI text: 3.0 % CER
   on French, 2.8 % on English and 34 % on code at 11-18 px. Upscaling brings a full 1440p window
   from 3.5 % to 0.18 % CER, and code to 12 %. Names read off the screen ("the Minecraft Rust
   project", file names, channel names) are exactly this kind of text.
3. **DC-03 + DC-05 unwanted audio.** Silero passes singing: 45 % of MUSDB18 song and a cappella
   clips would be recorded and transcribed today. Game sound turns into "conversations". A 10 MB
   tagger keeps 100 % of test speech (including speech over music at 0 dB) and 1.5 % of non-speech;
   it gates music-app and browser sound only, where songs come from.
4. **DC-09 language runs.** A segment holding a French and an English sentence keeps one of them:
   English words kept 2 % in 20 of 20 test segments; decoding each language run alone keeps 97 %.

## 2. Items

### DC-01 · Per-app system audio (process loopback) · Audio attribution · P0 · L

**Problem.** System audio is recorded from one playback endpoint in loopback
(`platform/win/audio.py:446-465`), so Discord, a browser video, a game and music arrive as one
signal. Attribution is a guess from a separate 1 Hz poll of session peak meters: a process counts as
audible when its peak is ≥ 0.001 (`platform/win/audio.py:71`, `:544-557`; poll interval
`capture/audio.py:76`), and a segment stores every process audible on the recorded endpoint during
it, ± 1 s (`capture/audio.py:354-382`, `:495-501`). Consequences:

- A segment while a game, a video and Discord all play is stored with all three as sources; the
  words are not attributable to any of them (`SPEECH.md` already warns "process participation does
  not identify the author of every word").
- One excluded app audible anywhere drops the whole segment, including friends' speech
  (`capture/audio.py:485-489`).
- Voice chat routed to another endpoint is not recorded at all, only noted as a `voice_elsewhere`
  gap (`capture/audio.py:260-273`, `:334-352`; TODO A5 plans a second loopback).
- The Wave Link mixer case needs a special rule (`MIXER_PROCESSES`, `capture/audio.py:91`, unconfirmed
  in OPEN.md A5).

Live measurement (spike S3, 120 s of the user's real PAYDAY 3 + Discord session, in memory): Discord
held 97 % of the speech windows of the mixed loopback; the game was audible in 85 % of Discord speech
windows; the game alone produced speech-like windows 4 % of the time (voice lines, effects); 10 % of
Discord speech windows were under 10 dB above the game.

**Impact on questions.** Every question anchored on a person, a call, a video or a game depends on
knowing which app spoke: "what did Minlo say this morning", "what video did I watch", "what was said
in that YouTube video about X", "which series", "what solo game did I play" (game voice lines must not
become a call). Mixed sources also pollute speaker identity: a video narrator in a Discord segment
gets enrolled as a "person".

**Recommended solution.** Record system sound as one stream per audible app tree, through WASAPI
process loopback (`ActivateAudioInterfaceAsync` with `VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK` and
`PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE`, Windows 10 build 20348+ [S1][S2]; OBS ships the
same API for "Application Audio Capture" [S4]). Keep the endpoint loopback only as a fallback.

1. `platform/win/audio.py`:
   - `ProcessLoopbackInput(pid)`: activation exactly as the spike's `wasapi.process_loopback_client`
     (PROPVARIANT `VT_BLOB` holding `AUDIOCLIENT_ACTIVATION_PARAMS{type=1, pid, mode=0}`; a comtypes
     `COMObject` handler implementing `IActivateAudioInterfaceCompletionHandler` and `IAgileObject`;
     wait ≤ 5 s on an event). Then `Initialize(SHARED, LOOPBACK | AUTOCONVERTPCM |
     SRC_DEFAULT_QUALITY, 1 s buffer, 0, mono float32 16 kHz, None)`. Spike S1: this exact format is
     accepted; `GetMixFormat` is not needed. Same `read(timeout)` polling as `_WasapiInput`.
   - `WindowsAudioSessions.app_sessions()`: today's per-endpoint session scan, returning for each
     **active** session (state `AudioSessionStateActive`, whatever its peak) `(endpoint_id, pid,
     exe, root_exe)`. `root_exe` is the image name of the highest ancestor that is not a shell or
     service process (a game's voice helper maps to the game). Guard PID reuse with the process
     creation time (`GetProcessTimes`).
2. `capture/ports.py`: `AppAudio` port (`open(pid) -> AudioInput`).
3. `capture/audio.py`: an `AppStreams` owner beside the two recorders.
   - Streams are keyed on the **session's own PID**, opened in include-tree mode: the PID that
     renders is captured directly, without relying on how Windows resolves parent trees (OBS
     maintainers call subprocess capture "hit or miss" [S24]). Spike S2 captured Discord from its
     rendering child and from its main process.
   - Each sampler poll (1 s), for every active session whose `exe` and `root_exe` are not excluded
     by `exclusions.check_process`, open one `StreamRecorder(kind="system", source=exe)` with its
     own Silero state and `Segmenter`. Opening on an *active* session, not on an audible peak, means
     a call's first words after a silence are already in the stream (spike S1: a client opened
     before the target renders captures it once it starts). Excluded apps are never opened, so their
     sound never reaches memory (stricter than today's drop-after-recording).
   - Close a stream (flushing its open utterance, as a device change does today) after
     `APP_IDLE_CLOSE_S = 120` s without an active session, or when the process exits (a target that
     exits leaves a silent stream, not an error).
   - Reopen a stream between utterances every 30 minutes. OBS reported process-loopback audio
     turning noisy after 15 minutes to hours. The cause was a Windows bug fixed in 24H2, but a
     maintainer says edge cases remain, and reopening cleared it [S25]. Spikes here ran ≤ 120 s.
   - `MAX_APP_STREAMS = 8`. Cost per stream: polling (spike S1: 0.4-0.8 % of one core for 1-6
     clients together) plus Silero (measured 0.055 ms of CPU per 32 ms window, 0.17 % of one core
     for a stream that is never silent). Apps beyond the cap are left to the fallback.
   - A stream turns excluded (user edits the list) → close it and drop its open utterance in memory.
4. Fallback endpoint stream: keep today's system recorder, but `SegmentWriter.write` drops a fallback
   segment (gap reason `covered`, never a problem) when every process audible during it
   (`SourceCheck.everywhere`) had a healthy app stream. It also drops it when any app stream stored a
   segment overlapping it, so covered speech is never transcribed twice; an uncovered app's speech
   that overlaps covered speech is lost, by choice. It keeps the segment, with today's sources, when
   an audible app had no stream: activation failed, the cap was reached, or the app is
   **uncaptured**. Uncaptured = its app stream's RMS stayed below 1e-4 (−80 dBFS) for ≥ 5 s while
   its session peak was ≥ 0.01. This is the documented Teams/Communications-category failure, which
   returns near-silence (RMS about 1e-5), not exact zeros [S3]. Discord was captured normally on this
   PC (spike S2), but Teams and other voice apps are untested.
5. Storage (`storage/migrations/0001_schema.sql`, edited in place: no real data exists, as for A9):
   `audio_segments` gains `source_app TEXT` (the exe of the session that rendered; NULL for `mic`
   and for fallback segments), `source_root TEXT` (its `root_exe`, used by episodes so a game's voice
   helper counts as the game) and `capture TEXT NOT NULL CHECK (capture IN ('device','app'))`.
   `sources` keeps its meaning: a one-element array for app segments. Add `covered` to the `capture_gaps`
   reason CHECK and `GapReason`.
6. Continuations and prompts: key `SegmentWriter._last_id` by `(stream, source_app)`; key
   `AudioStore.previous_transcript` (`storage/audio.py:238-247`) by `source_app` too, so the
   prompt context comes from the same app.
7. Speakers: queue the speaker job only for voice-app segments (`VOICE_APPS`) and fallback segments
   with a voice app; not for browser, game or music streams.
8. Mute semantics (spike S1): process loopback is taken after the app's session volume. A tone at
   session volume 0.002 came back at exactly 0.002 × its level; a muted session came back silent. So
   "what the user heard" stays the rule: an app muted in the mixer is not recorded. Document this in
   CAPTURE.md.
9. Retire `MIXER_PROCESSES` and `voice_elsewhere` for apps with a stream: capture is
   endpoint-independent (spike S1: a tone on either active endpoint was captured; [S2]). Keep the
   gap reason for fallback-only voice apps.
10. Health: `app_audio_unavailable` when activation fails for every app for 60 s (codes in
    `capture/health.py`, words in `ui/app.js`).

Failure behaviour: activation error or timeout → that app is "not covered" and the fallback keeps
its sound; a read error closes the stream (utterance kept, like a device failure); a hung activation
never blocks the recorders (separate thread, 5 s wait).

**Alternatives rejected.**
- *Second endpoint loopback for the voice device* (TODO A5): fixes routing only; still mixed.
- *Finer peak polling (100 ms) on the mix*: still a correlation; cannot split overlapping speech.
- *Source separation on the mix* (Demucs, MRX, Bandit): GPU cost per segment, artefacts that can
  raise Whisper's WER [S11], and it cannot name the app.
- *Exclude-mode client per excluded app*: one client excludes one tree only [S2]; several
  exclusions need several mixes.

**Acceptance criteria.**
- Hardware test (new, `tests/hardware/`): two child processes render 440 Hz and 1 kHz at session
  volume 0.002 (inaudible). Each app segment has `source_app` of its own child, and ≥ 99 % of its
  spectral energy at its own tone (spike S1 measured 1.000 / 0.000).
- Manual (CHECKLIST audio): 30 minutes of Discord call + YouTube + a game at once, segments labelled
  by ear: ≥ 98 % of transcribed system segments carry exactly the right single `source_app`.
- Discord routed to a non-default endpoint: 0 `voice_elsewhere` gaps, its speech transcribed.
- An excluded app playing during a call: friends' segments still stored; a writer spy shows no
  sample of the excluded tree ever read.
- Capture process CPU with 6 app streams: ≤ +2 % of one core over today (spike: polling < 1 %,
  Silero 0.17 % per stream).
- Soak: 4 hours of a call plus a game: no stream's noise floor rises (RMS during the app's silent
  sessions stays < 1e-4), no stream silently stops, segment timestamps within 100 ms of the mic's
  for a clap heard on both.

**Dependencies, decisions, packages.** No new package (comtypes). Supersedes TODO A5 "second
loopback", #110 and the per-app stream sub-items; makes the A1 follow-up question (drop browser
audio while a private page is open) a per-stream rule (User decision U2). No conflict with
DECISIONS.md: exclusions stay in memory before disk, and stricter.

**Risks.** Communications-category silence for some voice apps (fallback covers it); in-game voice
chat rendered by the game process cannot be separated from the game, and some games play voice chat
from a helper process [S26] (DC-05 handles both through `source_root` and the microphone signal);
apps using exclusive-mode or spatial-audio paths are untested (fallback); UWP and packaged apps are
untested; long-session noise (reopen above, soak test). Clock drift between streams is not a risk:
each recorder re-anchors its window timestamps to wall time beyond 100 ms
(`capture/audio.py:731-734`).

### DC-02 · Upscale before Windows OCR · Screen text · P0 · S

**Problem.** `WindowsOcr.recognize` only ever shrinks (`platform/win/ocr.py:47-51`); windows are
OCR'd at native size, so 100 %-scaled UI text is 11-13 px high. Spike S4 (text rendered in Segoe UI
and Consolas, light and dark, ground truth known), with the app's recognizer (fr-FR,
`config.py:151`):

| Text (fr-FR recognizer) | Raw CER | 1.5x | 2x |
| :- | :- | :- | :- |
| French UI, 11-18 px | 3.0 % (6.3 % at 11 px) | 2.2 % | 1.25 % (5.5 % at 11 px) |
| English UI, 11-18 px | 2.8 % (8.9 % at 11 px) | 0.28 % (1.0 % at 11 px) | 1.05 % (5.3 % at 11 px) |
| Code (Consolas) | 33.7 % | 14.7 % | 11.9 % |
| Full 2560x1440 window, 75 lines at 12 px | 3.5 % | 1.5 % | 0.18 % |
| Full 3840x2160 window, 80 lines at 13 px | 1.4 % | 2.7 % | 0.09 % |

(The en-US recognizer on English: 2.1 % → 0.08 % at 1.5x, 0.04 % at 2x.) Inverting dark themes
changed nothing. Cost: a sparse 1440p window went from 138 ms to 325 ms + 129 ms of resize at 2x. A
dense one (four columns, about 2,700 words) went from 368 ms to 594 ms at 1.5x and 695 ms at 2x,
resize included.

**Impact.** Every question answered from screen text: project, file, channel and video names, error
messages, prices, counts. A misread name cannot be found by keyword or meaning.

**Recommended solution.** In `WindowsOcr.recognize` (`platform/win/ocr.py`): before recognition,
`scale = min(2.0, OcrEngine.max_image_dimension / max(w, h))`; when `scale > 1`, resize with
`Image.LANCZOS` (measured). Divide each word rectangle by `scale` where it is read, in the
word-grouping loop (`platform/win/ocr.py:62-74`): the gap test there uses pixel constants (24 px), so
the rectangles must be in window pixels before it, not only in `_word_block`. Applies to live OCR and
the deferred `ocr_window` job (same class). The OCR gate (`capture/ocr_gate.py:54-66`) keeps
comparing unscaled crops. `MaxImageDimension` is 10,000 on this PC (read at runtime; Microsoft does
not document it [S6]).

Why 2x: it is best on French, code and both full windows. 1.5x is better only for small English
text read by the French recognizer (0.28 % vs 1.05 %), and worse than raw on the 4K window (2.7 % vs
1.4 %). Adaptive word height (Text-Grab's 40 px target [S7]) costs two passes; PowerToys uses 1.5x
[S8]. The evidence is rendered text (Pillow grayscale antialiasing, not ClearType), so the
acceptance below repeats the comparison on real windows before the factor is frozen.

**Alternatives rejected.** Adaptive two-pass scaling (double cost, no measured gain over 2x);
inversion (no effect); a VLM OCR (rewrites text; built for documents [S9]); the Windows AI
`TextRecognizer` (NPU only; unavailable on this PC [S10]).

**Acceptance.**
- Harness `eval/data-collection-2026-11-17/ocr/ocr_spike.py` with the new `recognize`: mean CER on
  French and English UI text ≤ 1.5 % at 12-15 px, code ≤ 15 %.
- Real windows (hardware test): local FR/EN pages in a throwaway Chromium profile at 100 % and 125 %
  scaling, with the page's UIA text as reference (the 2026-11-13 spike shows it is complete once web
  accessibility is on). Compare raw, 1.5x and 2x; keep 2x unless 1.5x is within 0.2 points of CER.
- Dense 1440p window ≤ 800 ms p95 including the resize (measured 695 ms). Try `Image.BICUBIC` as a
  cheaper resize; keep it only if CER is within 0.05 points.

**Dependencies.** None (Pillow). DC-06 and DC-10 build on it.

**Risks.** Writer-thread time per changed window grows about 3x; the OCR gate limits it to changed
windows. In game mode OCR is deferred anyway.

### DC-03 · Speech/singing gate with an audio tagger · Unwanted audio · P1 · M

**Problem.** The only gates before Whisper are Silero (speech probability ≥ 0.5 opens a segment,
≥ 0.35 keeps it, `capture/audio.py:94-101`), an RMS floor and a minimum of 400 ms of speech.
Speech detectors misclassify singing as speech [S12]. Spike S5 on MUSDB18 7-second stems and FLEURS
speech: today's gate would record 77.5 % of a cappella clips and 12.5 % of full songs, then
transcribe the lyrics. Music-app audio is dropped only by process name (`capture/audio.py:83-88`,
`:502-506`), which also drops podcasts (DECISIONS D7, pending), and browser music (YouTube, Deezer
web) is always kept. Whisper hallucinates on non-speech: 40.3 % of 301,317 non-speech files with
large-v3 [S13].

**Impact.** Lyrics pollute "what was said about X", "what video", and the people and conversation
records; podcasts heard in a music app are lost (D7).

**Recommended solution.** A CED-mini tagger [S14] (AudioSet, 527 classes, mAP 49.0, Apache-2.0 on
its model card; the training repo is GPL-3.0, but only the ONNX weights are used) as a gate in the
worker, before Whisper:

1. Model: `mispeech/ced-mini` at commit `26c3ebcae85d4330f4fc26763f029539a3afcda0`, file
   `model.onnx`, 10,890,069 bytes, SHA-256
   `2a86374dbe1fa03e96b0acf6a02618ac71852d12aa3d72a030b062ff1e5c5434`. Input `waveform`
   (batch, samples) float32 16 kHz; output (batch, 527) **probabilities** (the export already
   applies the sigmoid: applying it again maps everything into [0.5, 0.73]). Register it in
   `pipeline/models.py` as an optional model in Settings › Models. CPU onnxruntime, 2 threads:
   37 ms per 10 s clip (spike S5). No VRAM.
2. Scope: gate only where songs come from. These are segments of a music app (`MUSIC_PROCESSES`)
   or a browser (`BROWSERS`), and, before DC-01, mixed segments whose sources include one of them
   and no voice app. Voice-app, game and mic segments are not gated: the test set held only read
   speech of 2-10 s, nothing like Discord audio, overlap or sub-2 s replies, and D7 concerns music.
   `pipeline/transcribe.py` `TranscribeHandler.run`: for a gated segment, split the samples into
   10 s chunks (last chunk ≥ 2 s, else merged). Per chunk, `speech = max(p[0..5])` (Speech, Male,
   Female, Child speech, Conversation, Narration) and `singing = max(p[27..37])` (Singing to
   Humming). Segment score `s = max over chunks of (speech − singing)`. If `s < 0.2` → set status
   `music` (new CHECK value) and do not run Whisper; keep the raw audio under its retention.
3. Threshold 0.2 (spike S5, 255 clips): keeps 100 % of speech (40 FLEURS FR/EN clips, plus 80 of the
   same over instrumentals at 0 and +10 dB SNR) and 1.5 % of non-speech (5 % of a cappella, 0 % of
   songs, instrumentals or game effects). 0.3 keeps 94 % / 0.7 %. ROC AUC: tagger 0.999, Silero
   speech share 0.892. There is no margin: the lowest speech score was 0.207 (speech over music at
   0 dB), and lowering the threshold to 0.1 still keeps 3 % of non-speech. So 0.2 is a starting
   point to recalibrate on the export (U5), not a tuned value.
4. Store `speech_score REAL` on `audio_segments` for tuning on the export (Part C).
5. Capture: remove the whole-segment music drop by process name (`capture/audio.py:502-506`), so
   music-app segments reach the gate (D7).
6. Missing model → transcribe as today and count `gate_skipped` in the worker status.

**Alternatives rejected.** Process-name rule only (fails D7 and browser music); Silero threshold
(AUC 0.89: at ≤ 5 % false keeps it keeps only 16 % of speech); SR-SAD (singing-robust SAD [S12]:
weights not confirmed available); vocal separation (Demucs/RoFormer extract singing as "vocals");
Whisper's `no_speech_prob` (not a music detector; reviewed in [S13]); AST (86 M parameters, 9x larger, not
measured here).

**Acceptance.** Harness `tag/tag_spike.py`: speech kept ≥ 99 %, songs + a cappella kept ≤ 5 %.
On the A8 export: 100 system segments labelled speech / song / both, precision of the `music` drop
≥ 95 % and recall of real speech ≥ 98 %.

**Dependencies.** None new (onnxruntime). D7 becomes done with it. Works on mixed or per-app
segments (DC-01 makes it cleaner: a game stream's music never mixes into Discord).

**Risks.** Rap and spoken word over a beat score as singing (label 36 "Rapping" is in the singing
set), so they are dropped by design (U7); not in the spike set. Game streams are not gated (DC-05
handles them by source). The chunk max keeps a video whose speech is under music.

### DC-04 · Focused window skips OCR on 40 characters of UIA text · Screen text · P1 · S

**Problem.** For the focused window, `ScreenWriter._read_app` stores UIA text without running OCR
whenever it has ≥ 40 characters (`capture/screen_writer.py:347-352`, `MIN_UIA_CHARS`
`capture/text.py:16`). Edit controls count as content and their value is read
(`platform/win/uia.py:35-63`, `:116-119`). So a Chromium window whose page tree is not exposed
reaches 40 characters from its address bar's name and URL alone. Chromium exposes no page text while
it considers the window occluded, and turns web accessibility on only at the first read (CAPTURE.md,
spike 2026-11-13). The page text is then lost for that frame, and OCR never runs. The same happens to
any app whose UIA tree is mostly chrome. CAPTURE.md says the opposite: "For the focused window the
longer of the UIA text and the OCR text is kept" (`docs/CAPTURE.md:185`).

**Impact.** The focused window is what the user was actually reading. Losing its page text on the
first capture after a focus change hits "what article / page did I read about X" and names on the
page.

**Recommended solution.**
1. `platform/win/uia.py` `read_text`: skip the browser address bar (the element `_omnibox` already
   identifies, class `OmniboxViewViews`, and Firefox's bar); this needs `UIA_ClassNamePropertyId`
   added to the cache request. The URL is already stored in `frame_windows.url`. Step 2 alone
   already fixes the lost page text; step 1 removes URL noise from the stored text.
2. `capture/screen_writer.py` `_read_app`: run the OCR gate for the focused window too, and keep the
   longer of UIA and OCR, as CAPTURE.md states. Keep UIA when its length is ≥ 0.8 × the OCR
   length: UIA has no recognition errors, so near-equal lengths favour it. Remove the early return
   on `MIN_UIA_CHARS`; keep `MIN_UIA_CHARS` only to treat tiny UIA reads as empty.
3. Commit the gate result for the focused window as for the others.

**Alternatives rejected.** Raising `MIN_UIA_CHARS` (any threshold is crossed by long URLs or
toolbars); forcing renderer accessibility on Chromium (a browser flag the app cannot set for the
user's browser).

**Acceptance.** Unit test with `FakeUia` returning only an address-bar Edit with a 60-character URL
and a capture with page text: the stored text is the OCR text. `tests/test_screen_writer.py:528`
asserts today's skip and must be rewritten to the CAPTURE.md rule. Manual: open a page, focus it, check
that the first stored frame's text contains the page body.

**Risks.** About one extra OCR every 5 s while the focused window's text changes (gated).

### DC-05 · Game sound is not a conversation · Conversations · P1 · S

**Problem.** `_is_voice` counts a segment as conversation when a voice app *or the game running at
that moment* is among its sources (`pipeline/episodes.py:256-264`). The game clause is deliberate:
in-game voice chat is one of the user's main conversations (AGENTS.md; TODO #170;
`tests/test_episodes.py:199`). But with mixed sources every segment during a game qualifies, so NPC
voice lines, cutscenes and the user's YouTube video during a solo game become a "<game>
conversation" with "people". Spike S3 measured the game producing speech-like windows 4 % of the
time on its own.

**Impact.** "What solo game did I play" and "who did I talk to" both get false conversations; game
characters get enrolled as speakers. Dropping the clause would lose in-game voice chat instead.

**Recommended solution.** Count game sound as conversation only with evidence that voice chat was
in use, from content-free signals:
- A game segment (DC-01: `source_root` is the running game; before DC-01: the game among the
  sources) is conversation when, during it, the game's process tree or a helper whose `root_exe` is
  the game holds an **active microphone capture session**. In-game voice chat must open the mic;
  solo play does not. This uses DC-08's capture-endpoint scan. Fallback when the game uses
  push-to-talk and releases the mic: a mic segment within 30 s on both sides (turn-taking).
- Otherwise game-stream segments are game audio: add them to the game episode's body, as
  `build_media` adds a video's transcript to its episode (`pipeline/episodes.py:177-215`), so "what
  did the character say" stays answerable.
- `[gaming] voice_games` (User decision U3) forces a game's stream to count as conversation, for
  games whose chat signal the rule misses.

**Alternatives rejected.** Dropping the clause (loses in-game chat, a main use); speaker
recognition (game characters are unknown speakers too); a game exe list alone (misses games whose
chat runs in a helper process [S26]).

**Acceptance.** Episode tests: game-only segments with no game capture session and no mic turns make
no conversation; with a capture-session span of the game, or interleaved mic segments, they do.
`test_voice_chat_inside_a_game_is_a_conversation` keeps passing with a capture span in its fixture.
Spike first (manual): a game with in-game voice chat (open mic and push-to-talk) and a solo game,
reading capture sessions.

**Dependencies.** DC-08 (capture-session scan); DC-01 for the clean version.

### DC-06 · Lossless deferred-OCR captures · Screen text · P1 · S

**Problem.** In game mode each window is stored for later OCR as WebP q70 method 2
(`capture/screen_writer.py:356-357`, `capture/imaging.py:32-33`, `:70-74`). The window-composition
baseline measured OCR recall of 0.89-0.99 for that setting against the original frame
(`docs/baselines/2026-11-15-window-composition.md:194`). Spike S6 (1440p dark window, 75 lines,
fr-FR, 2x upscale):

| Encoding | Size | Encode | CER after OCR |
| :- | :- | :- | :- |
| WebP q70 m2 (today) | 104 KB | 247 ms | 0.25 % |
| WebP lossless, method 0 | 163 KB | 52 ms | 0.09 % (= original) |
| PNG level 1 | 282 KB | 40 ms | 0.09 % |

**Impact.** During games, Discord and browsers on side monitors are OCR'd only from these captures.

**Recommended solution.** Encode deferred captures as lossless WebP (`lossless=True, quality=0,
method=0`) in a new `imaging.encode_capture`. The captures are temporary (deleted by
`finish_capture` after OCR), so the 57 % size increase is transient. Stored monitor frames keep
WebP q70: they are for viewing.

**Acceptance.** Harness `ocr/lossless.py`: deferred OCR CER equals live OCR CER (± 0.02 points);
encode ≤ 80 ms per 1440p window.

**Risks.** A long game session's backlog grows 1.6x on disk until the worker reads it.

### DC-07 · Store each window's new lines · Text duplication · P1 · M

**Problem.** Every stored frame inserts every app window's full text into `frame_windows` and
`frame_windows_fts` (`storage/migrations/0001_schema.sql:61-73`). A window whose text is unchanged
reuses its OCR result (`capture/ocr_gate.py:54-66`) but is written and indexed again whenever any
window of its monitor changes. A chat that gains one line is indexed again whole. Search collapses
only runs of identical rows (`search/keyword.py:116-118`), and the identical-text study handles
embeddings of identical texts only (it ruled near-duplicates out:
`docs/solutions/2026-11-10 - Identical Frame Text Reuse/report.md`, mixed browsing −21 % only).

**Impact.** Near-duplicate rows grow with capture rate, not with what was seen. The first time a
line appeared, which answers "when did Minlo post that link", is not recorded anywhere.

**Recommended solution.** Record, per app window row, the lines that were not in that window's
previous stored capture. This is a collection change; whether search uses it is left to the
retrieval work.
1. `capture/screen_writer.py`: a `LineHistory` (in memory, LRU of 64 windows keyed by
   `hwnd` + `process`) holding the previous capture's `Counter` of normalized lines.
   Normalization: NFKC, casefold, whitespace collapsed; no fuzzy matching. New lines are the
   multiset difference, in original order, so a message repeated verbatim ("gg") is still new each
   time it is added. The first capture of a window (or after a restart) has all lines new.
2. Schema: `frame_windows.text_new TEXT` (the new lines joined by `\n`, empty when none, NULL for
   non-app rows). Clear it on every path that clears `text` (D3: expiry and deletion clear copies),
   and include it in export scope.
3. Indexing stays as it is: FTS and passages keep the full `text`, so every frame with a text stays
   findable at its own time (the identical-text study's contract), and identical texts are
   deduplicated by its C3 link table. `text_new` is a first-seen signal for the timeline, episode
   digests and later retrieval work. Switching FTS or passages to `text_new` is a retrieval change:
   it would hurt time-window questions about lines that stayed on screen, so it needs the B1
   retrieval eval first.
4. Compare normalized lines whatever the text source, so a focus change that switches a window
   between OCR and UIA flags only the lines that differ; expect such switches to mark most lines new.

Spike S7 (Windows OCR fr-FR 2x, a 160-message chat scrolled 20 times by 1-12 lines and sub-line
offsets, each OCR line mapped back to its source message): the recommended normalization and
multiset flagged 199 of 955 lines as new (21 % of the visible text, against 100 % re-indexed today).
It missed none of the 158 truly new lines; the 41 extra flags come from OCR variance between the
two views. A first run with a fuzzy match (difflib ratio ≥ 0.9) flagged only 0.68x as many lines as
an upper bound on the truly new ones. It hides new chat lines that differ from older ones by a name
or a time, so it is rejected. SimHash at Hamming ≤ 3-10 [S15] fails the same way: a changed number
is the content that matters.

**Alternatives rejected.** Fuzzy line or SimHash dedup (loses new lines); dedup at query time only
(index and embedding cost remain); storing diffs instead of full text (evidence views need the
whole text).

**Acceptance.** Unit tests: scroll, append, repeated message, window re-created. Harness
`ocr/novelty2.py` with the production normalizer: 0 truly new lines missed; `text_new` ≤ 30 % of
the visible text after scrolls.

**Dependencies.** DC-02 first (upscaling changes OCR variance). Complements the identical-text C3
method (whole identical texts get an empty `text_new`).

### DC-08 · Bound calls by the voice app's microphone session · Conversations · P1 · M

**Problem.** Conversations are chained while gaps are ≤ 5 minutes, and take mic segments within
30 s of a cluster (`pipeline/episodes.py:41-42`, `:266-306`). Two calls 4 minutes apart merge; a
call with a long silence splits; mic speech to someone in the room 20 s after a call joins it. No
public threshold exists for call segmentation [S16].

**Impact.** "What did we talk about in this morning's call" needs one call = one episode.

**Recommended solution.** Record call spans as content-free rows and cut conversations at them.
1. Capture: the 1 s sampler also enumerates sessions on **capture** endpoints
   (`IAudioSessionManager2` on `eCapture`), and records for each voice app (`VOICE_APPS`) whose
   capture session is `AudioSessionStateActive` a span in a new table
   `call_spans(app TEXT, start_ms, end_ms)`. Spans are opened and closed like gaps, merged across
   ≤ 10 s flaps. Spike S8 (read-only, during the user's Discord call): `discord.exe` held the only
   active capture session; the ConsentStore registry entry for `discord.exe` showed it in use
   (`LastUsedTimeStop = 0`), an undocumented convention.
   Akashic's own microphone session appears in that scan and is ignored (its PID).
2. Episodes: inside a span, a conversation is that app's segments plus mic segments within the span
   (± 5 s). Outside spans, today's rule stands.
3. Retention and Delete all cover `call_spans` (as gaps).

**Acceptance.** Manual (CHECKLIST): join and leave a Discord call twice, 2 minutes apart: two
spans, two conversations. Push-to-talk and voice-activity modes both checked.

**Risks.** Unverified: whether Discord keeps its capture session open outside calls or in
push-to-talk mode. If it does, spans are too long and the 5-minute rule must still split them.

### DC-09 · French–English switching inside one segment · Transcription · P1 · M

**Problem.** Each segment gets one language: the likelier of French and English over the whole
segment, forced for the whole decode (`pipeline/whisper.py:111-135`). Segments end only after 1 s
of silence and last up to 30 s (`capture/audio.py:104-108`), so a French speaker answered in
English, or a bilingual speaker switching between sentences, lands in one segment. Whisper commits
to one language token per window. A vendor FR–EN benchmark on synthetic speech ranked Whisper
large-v3-turbo last, with errors concentrated in the English parts and a tendency to translate when
no language is set [S23].

Spike S9 (faster-whisper 1.2.1, large-v3 int8 on CPU, production decode settings, 20 segments, each
a FLEURS French sentence and an English one in both orders, 0.6 s apart, 12-19 s):

| Decoding | French words kept | English words kept | Segments losing a language (< 50 %) |
| :- | :- | :- | :- |
| Production (detect, force fr/en) | 0.868 | 0.021 | 20 / 20 |
| `language=None` | 0.868 | 0.021 | 20 / 20 |
| Each sentence detected and decoded alone | 0.953 | 0.972 | 0 / 20 |

The English sentence was dropped, not translated. In 2 of 20 segments the French one was mostly
lost too.

**Impact.** Mixed-language calls and videos are the user's daily case (AGENTS.md); whole sentences
vanish from transcripts, so "what did X say about Y" fails silently.

**Recommended solution.** Split a segment into language runs before decoding.
1. `pipeline/transcribe.py` `decode_segment`: for segments ≥ 4 s, find internal pauses ≥ 250 ms
   with Silero v6 (CPU, the capture model; 0.055 ms per 32 ms window measured) and cut there into
   chunks.
   Merge chunks shorter than 1.5 s into a neighbour: language identification is unreliable on very
   short audio.
2. If there are at least 2 chunks, call `detect_language` on each (restricted to fr/en, as
   `_detect`). If all chunks agree, or no chunk reaches p ≥ 0.65 for the other language, decode the
   whole segment as today.
3. Otherwise group consecutive same-language chunks into runs and decode each run with its language
   forced, the same prompt and the same settings. Join the texts in order. Store the language of the
   longest run in `language` and the per-run languages in a new `languages TEXT` (JSON) column.
4. Overlap trimming (`trim_overlap`) and hallucination checks apply to the joined text, as today.

Cost: one encoder pass per chunk for detection plus the run decodes, only on segments ≥ 4 s. That
is in line with "quality before speed" (DECISIONS.md).

**Alternatives rejected.** `language=None` or faster-whisper `multilingual=True` (detection per
30 s window; segments are ≤ 30 s, measured identical); a shorter capture hangover (more segment cuts
mid-sentence for everyone); switching models now (no public FR–EN casual-speech evidence; TODO C6
#111).

**Acceptance.** Harness `cs/cs_spike.py` with a `production_new` condition: ≥ 0.90 recall for both
languages on the 20 mixed segments. On 40 monolingual FLEURS segments: ≤ 5 % split, word error rate
within 0.5 points of today.

**Risks.** Not measured: code-switching *inside* a sentence (English game terms in French speech),
the most frequent kind, which this method leaves to Whisper; two speakers of the same language with
different accents misdetected as different languages (bounded by the 0.65 rule). Read FLEURS
speech is cleaner than Discord audio.

### DC-10 · PP-OCRv6-small as a second recognizer · Screen text · P2 · M

**Problem.** Even at 2x, Windows OCR misreads code (12 % CER with fr-FR, 6.6 % with en-US) and keeps
97-98 % of French accented letters (spike S4).

**Evidence.** Spike S4 with RapidOCR's ONNX exports on CPU (same rendered set): PP-OCRv6-small CER
French 0.02 %, English 0.06 %, code 1.85 %, accents 100 %. PP-OCRv5 Latin: English 13.9 %, code 63 %.
PP-OCRv6-medium: no better than small. Cost: 1.3-1.8 s per sparse 1440p window on CPU (4 or 2
threads), against 0.45 s for Windows OCR at 2x, which reached the same 0.2 % CER on that prose. PaddleOCR
reports +14.4 points on screen text for v6-medium over v5-server [S17].

**Recommended solution.** Not now: after DC-02, Windows OCR matches it on prose. Run a spike of
recognition-only PP-OCRv6-small on the GPU (onnxruntime-gpu, already a dependency): Windows OCR
gives the line boxes, each line crop is resized to height 48 and decoded by `rec_small` (CTC). That
avoids the detector post-processing that brings opencv, shapely and pyclipper with RapidOCR. Adopt
it for windows whose text looks like code (monospace, or editor and terminal processes) if the
export shows code names misread. Models: RapidOCR v3.10.0 exports, `PP-OCRv6_det_small.onnx`
(9,929,594 B, SHA-256 `090f04ab…94f`) and `PP-OCRv6_rec_small.onnx` (21,234,383 B,
`6f327246…884`), pinned in `pipeline/models.py`, never fetched at runtime.

**Rejected.** The RapidOCR package (downloads models from modelscope.cn at runtime through
requests [S18], heavy dependencies); VLM OCR (rewrites text [S9]).

### DC-11 · Content trigger for side monitors · Capture timing · P2 · M

**Problem.** Side monitors are checked every 15 s; the triggers are focus, title, input, window
events and a rate (`capture/screen.py:77-79`, `capture/ports.py:19`). A fast chat on a side monitor
can scroll lines out of view between checks; nothing reacts to content changes in an unfocused
window. screenpipe polls every 3 s with a 1/4-scale histogram [S19]; its own code notes that small
text edits rarely trip it.

**Recommended solution (spike first).** When a kept window on a side monitor delivers a WGC frame
whose text-box signature (`imaging.signature` of `find_text_box`) differs from its last stored one,
request a check of that monitor with trigger `content`, at most every 3 s. Skip windows with a
playing media session (video): they keep the 15 s rate. Spike: CPU of the signature on frame
arrival and stored frames per hour on a normal day; `DirtyRegionMode` (Windows 11 24H2+ [S20])
could give changed rectangles for free.

**Acceptance.** A side-monitor chat posting a line every 2 s for a minute: every line in some stored
`text_new` (DC-07); capture CPU ≤ +3 % of one core.

### DC-12 · OCR language fallback is silent · Screen text · P2 · S

**Problem.** `WindowsOcr` falls back to English with only a log warning when the configured
recognizer is not installed (`platform/win/ocr.py:37-39`). With en-US, French accent recall drops
from 0.97 to 0.63 and French CER doubles (spike S4). Both fr-FR and en-US are installed on this PC
today; OPEN.md A9 recorded the French check skipped during the 2026-11-16 hardware run.

**Recommended solution.** Report `ocr_language_fallback` as a capture problem (sticky, like
`audio_unavailable`), with the Settings hint to add the "Optical character recognition" feature
of French. Keep one fr-FR engine: on English text at 2x it is as good as en-US except at 11 px
(5.3 % vs 0.2 %).

**Acceptance.** Unit test with a fake `available_languages` lacking fr: the problem code appears.

## 3. Considered, no change recommended

- **ASR model.** No public benchmark covers casual French–English voice chat; published French
  numbers are read speech under different normalisations. Large-v3 stays; the comparison belongs to
  TODO C6 #111 on the export. Candidates worth adding to it: `bofenghuang/whisper-large-v3-french`
  (MIT), Cohere Transcribe (Apache-2.0, 2 B), Parakeet-TDT-0.6B-v3 (CC-BY-4.0, word timestamps).
- **Diarization.** With DC-01 the voice-app stream holds only remote speakers and the mic holds the
  user. The speaker work planned in TODO C4 stays the next step. pyannote community-1 (CC-BY-4.0) is
  the permissive upgrade path; DiariZen's weights are CC BY-NC.
- **Audio encoding.** Opus 24 kb/s mono 16 kHz (`storage/opus.py:20`) is adequate for ASR; not
  measured here.
- **Change detection.** Hash then changed-pixel fraction at 1/4 scale (`capture/imaging.py:26-28`,
  `:55-62`) already sees scrolling, which screenpipe's histogram cannot [S19].
- **Silero VAD v6.** Its maintainers' comparison shows the best rejection of noise-only clips among
  the VADs it covers (ESC-50: 0.87 vs 0.61 for v5 [S21]); its weakness is singing, handled by DC-03.

## 4. Implementation order

1. DC-02 (OCR upscale) and DC-12: independent, small.
2. DC-04 (focused window OCR), then DC-06 (lossless captures).
3. DC-01 (per-app streams). It changes `audio_segments`, so it lands before any real recording.
4. DC-08 (call spans and the capture-session scan), then DC-05 (game sound as conversation only with
   a voice-chat signal), on DC-01.
5. DC-03 (tagger gate). Independent of DC-01, but its acceptance on the export is cleaner after it.
6. DC-07 (new lines), after DC-02.
7. DC-09 (language runs), independent; after DC-01 its prompts come from the same app.
8. DC-10 and DC-11: spikes first, after the A8 export shows where text is misread or missed.

Everything in 1-7 changes what is recorded, so it belongs to Part A, before the dataset is recorded.

## 5. User decisions

| # | Decision | Options | Recommended default |
| :- | :- | :- | :- |
| U1 | Apps muted in the Windows mixer | Not recorded (process loopback is post-volume) / record anyway (impossible with this API) | Accept: "what you heard" |
| U2 | A browser's sound while any of its windows is private or excluded | Drop that browser's stream while the verdict holds (60 s, as media) / keep it | Drop: the per-app stream makes this precise (answers the TODO A1 question) |
| U3 | Game sound when the voice-chat signal (game holds the mic, or mic turns) is missing | `voice_games` list forces conversation / never | Empty list; add a game if its chats are missed |
| U4 | Game audio transcripts (NPC lines, cutscenes) | Transcribe and attach to the game episode / drop | Transcribe: costs GPU only for speech-like segments |
| U5 | Speech gate threshold on music-app and browser sound | 0.2 (keeps all test speech, 1.5 % non-speech) / 0.3 (94 % / 0.7 %) | 0.2 now, recalibrated on the export (D7 values speech) |
| U6 | Second OCR engine for code | PP-OCRv6-small on the GPU after a spike / Windows OCR only | Decide on the export |
| U7 | Rap and spoken word over a beat in music apps and browsers | Dropped with songs (tagger counts them as singing) / kept | Dropped |

## 6. Verification

Three fact-checker passes covered 32 claims. Code citations: 9 supported, 3 partial (line numbers
corrected; the address-bar and voice-elsewhere paths confirmed). External claims: 14 supported, 3
partial: PP-OCRv6's screen gain is +14.4 points, not percent; Microsoft gives build 20438 on one
page and 20348 on two; the agile-handler requirement is documented only for pre-Windows-10. Two came
back unchecked (WGC `DirtyRegionMode`, the pyannote and DiariZen licences); they appear only in
DC-11 and section 3, as research leads, not as evidence for a recommendation. One was weak: the ConsentStore `LastUsedTimeStop = 0` convention is undocumented and was not found in the
cited app's code, so DC-08 rests on the capture-session scan and spike S8, not on it.

The refuter passes changed these items:
- DC-01: streams keyed on the session PID, not an inferred root (subprocess capture is unreliable
  [S24]); opened on an active session rather than an audible peak, so first words are kept;
  "uncaptured" detected by RMS below −80 dBFS, not exact zeros (the Communications failure returns
  about 1e-5 [S3]); fallback segments dropped when they overlap a covered segment (no double
  transcription); a 30-minute reopen and a 4-hour soak test for the long-session noise OBS reported
  [S25]. Its CPU concern (Silero up to 1 ms per window) was measured: 0.055 ms here.
- DC-02: production uses the fr-FR recognizer, for which 1.5x beats 2x on small English text; the
  table now shows fr-FR numbers, the dense-window cost, and an acceptance comparing 1.5x and 2x on
  real windows. Rectangles must be unscaled inside the word-grouping loop.
- DC-03: scope narrowed to music-app and browser sound; the 0.2 threshold has no margin on the test
  set and is to be recalibrated; rap is dropped by design (U7).
- DC-05: the game clause is deliberate (in-game voice chat, #170); the fix now requires a
  voice-chat signal instead of removing the clause.
- DC-07: the spike was rerun with the recommended normalization, a multiset and per-line ground
  truth (0 of 158 new lines missed); FTS keeps indexing full text, so `text_new` is a first-seen
  signal and the identical-text contract holds.
- DC-04 and DC-08 stood. Notes added: the test asserting today's skip, the ClassName cache, and
  ignoring Akashic's own microphone session.

## 7. Spikes run

All on the user's PC while it was in use, at below-normal CPU priority, CPU only (no GPU work).
Harnesses: [`eval/data-collection-2026-11-17/`](../../eval/data-collection-2026-11-17/README.md);
no data is committed.

| ID | What | Method | Result |
| :- | :- | :- | :- |
| S1 | Process loopback mechanics | comtypes activation; inaudible test tones (child processes, private non-persistent session at volume 0.002 or muted) | Activation works; 16 kHz mono float with or without AUTOCONVERTPCM accepted; separation 1.000/0.000; exclude mode gives the rest of the mix; endpoint-independent (2 active endpoints); late start captured; exit leaves silence; post-session-volume (muted = silence); 1-6 clients 0.4-0.8 % of one core; Silero 0.055 ms CPU per 32 ms window |
| S2 | Real apps | Per audible session, process loopback on the PID and on its parent | PAYDAY 3 (Unreal) and Discord captured, Discord from child and main process tree |
| S3 | Live overlap | 120 s, mixed endpoint loopback + per-app include and exclude streams, Silero per stream | Discord 97 % of mix speech windows; game audible in 85 % of Discord speech; game speech-like 4 %; 10 % of Discord speech < 10 dB SIR |
| S4 | OCR quality | Rendered FR/EN/code text, 11-18 px, light/dark, fr-FR and en-US, raw/1.5x/2x/inverted; sparse and dense full windows; RapidOCR PP-OCRv5 Latin and v6 small/medium | Tables in DC-02 and DC-10 |
| S5 | Speech vs singing | 40 FLEURS (FR/EN) speech, 40 MUSDB18-7 songs (mix, vocals, accompaniment), speech over accompaniment at 0 and 10 dB, 15 OpenGameArt CC0 effect chains; Silero v6 vs CED-mini | DC-03 |
| S6 | Deferred capture encoding | 1440p dark window, three encodings, OCR at 2x | DC-06 |
| S7 | Line novelty | 20 scrolls of a rendered chat; exact multiset with per-line ground truth (`novelty2.py`), fuzzy matching in a first run (`novelty.py`) | DC-07 |
| S8 | Call state | Read-only: capture-endpoint sessions and ConsentStore during a Discord call | DC-08 |
| S9 | Code-switching | 20 FLEURS FR+EN segments, faster-whisper large-v3 int8 CPU, production settings vs `language=None` vs per-sentence | DC-09 |

## Sources

[S1] AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS (min. Windows 10 build 20348) — https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_process_loopback_params — tier 1 — 2024-02
[S2] ApplicationLoopback sample README (not tied to an endpoint; silence without render streams) — https://github.com/microsoft/Windows-classic-samples/blob/main/Samples/ApplicationLoopback/README.md — tier 1
[S3] Windows-classic-samples issue 414 (Teams captured as silence; Communications-category repro in a later comment) — https://github.com/microsoft/Windows-classic-samples/issues/414 — tier 2 — 2025-12 to 2026-10
[S4] OBS win-wasapi.cpp (VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, INCLUDE_TARGET_PROCESS_TREE) — https://github.com/obsproject/obs-studio/blob/master/plugins/win-wasapi/win-wasapi.cpp — tier 1
[S5] ActivateAudioInterfaceAsync — https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync — tier 1 — 2026-08
[S6] OcrEngine.MaxImageDimension; OcrEngine (one RecognizerLanguage per engine) — https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine — tier 1
[S7] Text-Grab OcrSourceUtilities.cs (idealLineHeight = 40) — https://github.com/TheJoeFin/Text-Grab/blob/main/Text-Grab/Utilities/OcrSourceUtilities.cs — tier 1
[S8] PowerToys TextExtractorService.cs (EnhancedScale = 1.5) — https://github.com/microsoft/PowerToys/blob/main/src/modules/PowerOCR/PowerOCR.Core/Services/TextExtractorService.cs — tier 1
[S9] Do VLMs Read or Rewrite? — https://arxiv.org/html/2607.21617 — tier 1 (preprint) — 2026
[S10] Windows AI APIs, hardware table (Text Recognition: NPU only) — https://learn.microsoft.com/en-us/windows/ai/apis/ — tier 1 — 2026-11-13
[S11] When Denoising Hinders (enhancement raises Whisper WER) — https://arxiv.org/pdf/2603.04710v1 — tier 1 (preprint) — 2026
[S12] Robust Speech Activity Detection in the Presence of Singing Voice (SR-SAD), WASPAA 2025 — https://arxiv.org/abs/2512.09713 — tier 1
[S13] Investigation of Whisper ASR Hallucinations Induced by Non-Speech Audio (40.3 %; Silero pre-filter 21.3 % → 0.2 %) — https://arxiv.org/abs/2501.11378 — tier 1 (preprint) — 2025
[S14] CED-mini model card (Apache-2.0, 9.6 M, mAP 49.0) — https://huggingface.co/mispeech/ced-mini — tier 1 — commit 26c3ebc; training code https://github.com/RicherMans/CED (GPL-3.0)
[S15] Manku, Jain, Das Sarma, Detecting Near-Duplicates for Web Crawling, WWW 2007 — https://research.google/pubs/detecting-near-duplicates-for-web-crawling/ — tier 1
[S16] Stivers et al., Universals and cultural variation in turn-taking, PNAS 2009 (abstract) — https://pubmed.ncbi.nlm.nih.gov/19553212/ — tier 1
[S17] PP-OCRv6 technical report (screen display 82.5 vs 68.1 for v5-server) — https://arxiv.org/html/2606.13108 — tier 1 (preprint) — 2026-06; PaddleOCR 3.7.0 — https://github.com/PaddlePaddle/PaddleOCR
[S18] RapidOCR default_models.yaml and utils/download_file.py (modelscope.cn URLs via requests) — https://github.com/RapidAI/RapidOCR — tier 1 — v3.10.0
[S19] screenpipe frame_comparison.rs and event_driven_capture.rs (1/4 grayscale, hash, Hellinger histogram, 0.05, 3 s) — https://github.com/screenpipe/screenpipe — tier 1 — main@cad7c37, 2026-11-17
[S20] GraphicsCaptureSession.DirtyRegionMode / Direct3D11CaptureFrame.DirtyRegions — https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturedirtyregionmode — tier 1
[S21] Silero VAD quality metrics (ESC-50 v6 0.87, v5 0.61) — https://github.com/snakers4/silero-vad/wiki/Quality-Metrics — tier 1
[S22] faster-whisper transcribe.py (defaults; temperature fallback on log-prob or compression ratio) — https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py — tier 1
[S23] ServiceNow FR-EN code-switching benchmark (synthetic, vendor) — https://huggingface.co/blog/ServiceNow-AI/code-switching — tier 2
[S24] OBS issue 9838 (maintainer: process-loopback subprocess capture "hit or miss") — https://github.com/obsproject/obs-studio/issues/9838 — tier 2
[S25] OBS issue 8064 (Application Audio Capture noisy after long sessions; Windows bug fixed in 24H2, edge cases remain) — https://github.com/obsproject/obs-studio/issues/8064 — tier 2 — closed 2025-10-25
[S26] OBS issue 7187 (a game playing voice chat from a separate process) — https://github.com/obsproject/obs-studio/issues/7187 — tier 2
