# Quality of what Akashic records and derives: audit 2026-11-18

Final report (Phase 6) of the [2026-11-18 quality audit](progress.md). Date: 2026-11-20. Production
code: the audit's base (`src/` unchanged since 7d6d213). Ask and answer quality are out of scope.

## Verdict

Akashic records the right apps and keeps captured content private, and clean speech is transcribed
well. Four things stop it from answering the target questions reliably today:

1. **A long day breaks it.** Transcription stopped for good after two hours of model swapping on
   the day run (5.5 h of speech waited for a manual restart), and capture's GPU memory grows about
   0.6 GB per hour.
2. **Episodes are wrong.** "What solo game did I play" and "what video did I watch" read episodes,
   and those miss or split most games (game F1 0.29-0.30) and, on the day run, most videos (video
   F1 0.22). Conversations list 14-27 people where 4 spoke.
3. **Mixed sound is mistranscribed.** Speech that overlaps a video or game reads at WER 0.50-0.58,
   against 0.05-0.12 for clean speech.
4. **Small things on screen are lost.** One-word chat replies fall below the change threshold
   (all missed on synthetic frames, 131 of 356 at the first check on real windows), and text at
   11 px or less reads at CER 0.23-0.52.

Each has a measured fix candidate (below). None has yet run as Akashic code: every "after" number
is a replay, a spike or a synthetic set.

## How it was measured

- **Harness:** `eval/quality/` drives the real desktop from a seeded scenario: pages with known text
  at known sizes, videos, a synthetic Direct3D game, the real Outer Wilds, voice-chat and mic speech
  from public corpora, Pauses, excluded and private windows with planted canaries. It writes its
  own truth logs and an independent OS observer's log, runs Akashic on a disposable encrypted
  store, and scores what Akashic stored. Metric definitions: [scope.md](scope.md).
- **Runs** on the target PC (Ryzen 7 7800X3D, RTX 4080 Super, 4K 125 % primary plus two 1440p):
  smoke ×2 (5 min), standard ×2 (1.1 h), day ×1 (6.9 h). Standard 0315 ran on a partly broken
  harness and the day run needed one manual worker restart; see the
  [baseline](../../baselines/2026-11-18-quality.md) for what each run covers.
- **Scorer:** fixed and rescored on 2026-11-20 (wrapped OCR lines, truth clipped to the recorded
  span and split at Pauses, one stored row per truth piece, Pause speech left out). Before the fix,
  screen-text error read about twice as high and game rows looked worse than they are.
- **Studies:** 33 + 21 code findings ([audit.md](audit.md), #187-#240), 8 research reports
  (`docs/research/`), 6 method studies with measured candidates (`docs/solutions/2026-11-19*`,
  `2026-11-20*`), and a ranked synthesis checked by two refuters and two fact-checkers
  ([synthesis.md](synthesis.md)).
- **Not covered:** lock, sleep, audio-device change and UAC (attended runs only; not run), a real
  Discord call, a clean same-seed pair for the 5 % repeatability rule, exclusive-fullscreen
  re-entry.

## Results by dimension

Values: standard 0315 / standard 1818 / day 0547, rescored. "Good" holds for the target questions
as measured; "weak" works but limits answers; "broken" gives wrong or missing answers.

| # | Dimension | Measurement | Values | State |
| :- | :- | :- | :- | :- |
| 1 | When the screen is captured | change recall (≥ 5 s), focused, game borderless; useless frames on the primary | 0.59 / 0.67 / 0.69; 0.72 / 0.77 / 0.87; 0.04-0.12; 0.10-0.23 | weak: one-word replies and badges are never stored (#208); game mode stores one frame per 60 s by design |
| 2 | App tagging | label accuracy; focused row | 0.98-1.00; 1.00 | good |
| 3 | Screen text | CER all; ≤ 11 device px; 1440p; 4K; UIA; FTS recall of shown lines; duplicate characters | 0.08 / 0.07 / 0.11; 0.23 / 0.27 / 0.52; 0.15-0.18; 0.01; 0.01; 0.72 / 0.80 / 0.83; 0.95-0.96 | weak: small text and 1440p (#190); everything stored per frame (#101) |
| 4 | Browser and media identity | URL; video title; series and episode; game name | 0.69 / 0.92 / 0.77; 1.00; 1.00; 1.00 | good for titles; URL weak |
| 5 | Timeline | untyped holes; duration error; Pause typed | 582 / 571 / 5,240 s; 0.35 / 0.18 / 0.48; 0.98-1.00 | broken: sessions end at their last frame (#196) |
| 6 | Media and game sessions | media start error p50; media row splits (day); game rows per truth piece; game end error max; Outer Wilds after the Pause | 0.36-1.62 s; 12; 1 / 1 / 3.67; 516 / 518 / 703 s; missed on both standard runs | broken for games: rows close while the game runs (#197) |
| 7 | Sound per application | segments listing Akashic itself; leak other than self; speech-tag accuracy | 257 / 253 / 2,074 (nearly all); 0 / 11.7 / 25.6 s; 0.99 | weak: Akashic's own session in every source list (#187) |
| 8 | Voice, segments, speakers | voice recall system / mic; DER; authors wrong / unknown (day) | 0.94-0.98 / 0.98-1.00; 0.22 / 0.23 / 0.16; 46 / 293 of 1,811 | weak: unknown authors and fragmented profiles |
| 9 | Transcription | WER EN; FR; mixed; overlapping speech; hallucinated words; term hits | 0.05-0.11; 0.08-0.11; 0.15-0.17; 0.53 / 0.58 / 0.50; 0; 0.60 / 0.50 / 0.75 | good when clean; broken under overlap (#110) |
| 10 | Privacy | canary hits; plaintext at rest; outbound connections | 0; 0; 0 on every run | good (export not scanned) |
| 11 | Episodes | F1 video; game; conversation; titles; distinct people (4 truth speakers); useless episodes | 1.00 / 1.00 / 0.22; 0.29 / 0.29 / 0.30; 1.00 / 0.67 / 0.75; 0.67 / 0.60 / 0.52; 14 / 16 / 27; 0.25-0.37 | broken for games, people and day-long video |
| 12 | Index coverage and delay | transcripts in FTS; transcribe delay p50; embed delay p50 | all; 16 / 17 / 9,950 s; 209 / 319 / 48 s | broken on the day run (transcription stopped) |
| 13 | Life-event loss | seconds lost after a Pause and a busy disk | 0 | good for what ran; lock, sleep and device change not run |
| G1 | CPU | capture p95 (% of a core); worker p95 | 31-34; 95-98 | as expected |
| G2 | VRAM | capture p95; worker p95; llama-server p95 | 1.0 / 1.0 / 4.4 GB; 4.6-4.8 GB; 4.7 GB | broken over a day: capture grows about 0.6 GB/h |
| G3 | Game frame time | synthetic borderless p99 with / without Akashic | 1.13 / 0.61, 1.56 / 0.63, 0.59 / 0.38 ms | small in absolute terms on a light synthetic scene |
| G4 | Disk | store growth per day | 4.2 / 4.9 / 4.2 GB | about 1.5-1.8 TB a year kept forever by default |

## What to fix, in order

Full evidence, costs and open conditions per item: [synthesis.md](synthesis.md). The order puts
first what makes a day of data survive, then what serves the most example questions.

| # | Fix | Evidence it works | First check |
| :- | :- | :- | :- |
| R1 | Keep transcription alive: give the embedding server a small context (it sets none, so it takes its model's full context; it holds 4.7 GB for a 0.6 B model), stop evicting a model on every job-kind change, restart the worker after repeated load failures | Day-run diagnosis; Whisper leaks about 8 MiB of VRAM per reload (spike) | A day run with no manual restart, transcribe delay p50 near 17 s |
| R2 | Bound capture VRAM: keep window captures alive across game-mode switches; restart the capture process past a VRAM threshold | 81.8 MiB leaked per capture start/stop (spike); growth 10x faster near game-mode switches | Capture VRAM flat over a day run |
| R3 | Games and videos: one game row per process lifetime (held process handle, in-front ledger, exclusion first), video episodes from chains of media rows, Pause splits; drop solo-game NPC voices as calls (#200) | Replays: rows per session 1.25, F1 video and game 1.00, titles 0.86 ([report](../../solutions/2026-11-20%20-%20Game%20Sessions%20And%20Episodes/report.md)) | Standard and day reruns |
| R4 | Skip Akashic's own audio session by process id (#187) | Spike: own loopback session is listed Active at the mix's peak | M7.1 segments listing Akashic itself: 0 |
| R5 | App time: store foreground switches as band edges, cut by an away span from idle, audio, speech and game presence | Replays: holes 15-39 % → ≤ 0.4 %, duration error 0.05-0.12 on standard and day (upper bound: switch rows simulated from truth) ([report](../../solutions/2026-11-19%20-%20App%20Time%20Between%20Frames/report.md)) | The combination measured on the runs |
| R6 | Overlapping speech: per-app process-loopback streams, the call as the remainder, FR/EN split in long unclear rows | Simulated per-app streams: overlap WER 0.58 → 0.11 (standard), 0.50 → 0.16 (day), measured with a separation stage that never fired ([report](../../solutions/2026-11-20%20-%20Overlapping%20Speech/report.md)) | A real Discord call with a video playing (voice-call streams are silent in per-process capture on this PC) |
| R7 | Small screen changes: exact clusters of the few moved pixels, minus regions that change at every check | Real windows: 1 of 356 one-word replies missed (today 131), no caret or clock stores ([report](../../solutions/2026-11-19%20-%20Small%20Screen%20Change%20Detection/report.md)) | One-word reply and badge recall in a harness run |
| R8 | Small text OCR: one call on a mosaic of line strips, small strips enlarged, ClearType strips rebuilt from their subpixels | Real Edge, Chrome and Zen windows: ≤ 11 px CER 0.53 → 0.005-0.007, +50 ms p95 per call ([report](../../solutions/2026-11-20%20-%20Small%20Text%20Windows%20OCR/report.md)) | A live run (stored WebP frames cannot show it) |
| R9 | People and authorship: a method study of speaker-profile fragmentation and episode people | Not studied: 14-27 people for 4 speakers; window tail and 30 s seam leave turns unknown | Study first |
| R10 | Repeated screen text in search: merge hits per window run at query time, then a line store if needed | Study: top-5 slots repeating one window 0.73-0.78 → 0-0.06 with the line store ([report](../../solutions/2026-11-20%20-%20Screen%20Text%20Duplication%20And%20Noise/report.md)) | Query-time merge measured |
| R11 | Names and terms in transcripts | Not studied: term hits 0.50-0.75 | Study first |

Smaller fixes that need no study (#191, #193, #194, #198, #199, #202, #203, #206) and the tests,
architecture and creep findings (#207-#240) are queued in TODO.md `## Audit 2026-11-18`.

## What this audit cannot tell yet

- Whether any fix holds in the running app: each needs its harness rerun once it ships (standard,
  then day).
- Real voice calls, lock, sleep, audio-device change and UAC.
- Repeatability: a clean same-seed standard pair on the current harness.
- How much of the user's real screen text is 9-10 px, where most of the OCR gain lies.

## Documents

- [scope.md](scope.md): dimensions and metric definitions.
- [audit.md](audit.md): code findings #187-#240.
- [synthesis.md](synthesis.md): the ranked recommendations with evidence and verification.
- [progress.md](progress.md): phases, the user's binding answers, run notes.
- [Baseline](../../baselines/2026-11-18-quality.md): every measured value, before and after the
  scorer fix.
- `docs/research/`: the 8 Phase 3 reports; `docs/solutions/`: the 6 Phase 4 studies.
- `eval/quality/`: the harness; `eval/quality/results/`: truth logs and scores per run.
