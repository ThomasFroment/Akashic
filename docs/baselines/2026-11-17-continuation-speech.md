# Continuation segments on real speech 2026-11-17

How the production `Segmenter` (`src/akashic/capture/audio.py`) judges the part that follows a 30 s
cut ("continuation": `continues=True`, 62 seeded overlap windows plus fresh windows), measured on
public read speech in French and English. Questions: does a continuation get kept whenever it has
at least one fresh speech window and a voiced RMS at or above the floor, is it dropped `short` only
with 0 fresh windows, and does the quiet gate behave when the level falls. The recordings were
public-domain audiobooks fetched from archive.org; nothing was played aloud, no window opened
(console Python only). The script, the downloads, the decoded audio and the raw result JSON stayed
in a scratch folder and are not kept; no file under `src/` or `tests/` changed. This report holds
numbers, source identifiers and checksums only, no transcript and no quoted text.

## Machine

Ryzen 7 7800X3D, 31.7 GB RAM, Windows 11 build 26200 (10.0.26200), Python 3.14.10. Measured against the Segmenter and
`Recorder._consume` of commit 1877212 (uncommitted changes to
`src/akashic/capture/audio.py` at the time touched neither).

| Package | Version |
| --- | --- |
| onnxruntime | 1.31.1 |
| numpy | 2.5.4 |
| av (PyAV) | 19.0.2 |
| faster-whisper | 1.2.1 |

VAD model `silero_vad_v6.onnx` from faster-whisper 1.2.1, sha256
`4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2`. The whole run (4 recordings,
5 gains, 37 trials x 9 tails x 5 gains) took 211 s on 4 processes.

## Method

- **Recordings.** LibriVox chapters on archive.org (public domain), 64 kb/s MP3, one item folder
  each, decoded with PyAV exactly as `decode_opus` does (`av.AudioResampler(format="flt",
  layout="mono", rate=16000)`, float32). URL pattern `https://archive.org/download/<item>/<file>`.
  The sha256 is of the downloaded file; sizes matched the item metadata.

| Id | Language | archive.org item | File | sha256 | Duration s |
| --- | --- | --- | --- | --- | --- |
| fr2 | French | `trois_mousquetaires_0810_librivox` | `troismousquetaires_02_dumas_64kb.mp3` | `cc3658fe8714187092244e840ef58bd082cd0fdaa8e96c9ad7682b7669ddb2fa` | 1694.9 |
| fr3 | French | `comte_monte_cristo_jg_librivox` | `montecristo_003_dumas_64kb.mp3` | `497cee0f91cbd66705a4c7552cdd20c62451262047022b6ec07debaa2ffd4ab0` | 1464.5 |
| en1 | English | `tom_sawyer_librivox` | `TSawyer_07-08_twain_64kb.mp3` | `2aba4d61522f77ee9e4edb4584f670ee1ef77d4b208695d46309b17ed649fa66` | 1280.7 |
| en3 | English | `moby_dick_librivox` | `mobydick_004_007_melville_64kb.mp3` | `7c777677073ea3855b871c8ab1a35580a1c360e5e8d695945c03865f51594828` | 1630.0 |

  Two first-choice sources were replaced (see Limits): fr1 (2 cuts only) by fr3, en2 (a constant
  DC offset) by en3. Each kept recording has at least 3 natural cuts: 8, 11, 40 and 47.
- **Feed loop** (what `Recorder._consume` does). One production `SileroVad`
  per run, `reset()` only at the start of a run (a run is one stream), never between utterances.
  The recording is cut into 512-sample windows (the last partial window, under 512 samples, is unused); an
  all-zero window gets probability 0.0 without calling the model; timestamps advance 32 ms per window
  from 0; gain is `np.clip(x * gain, -1, 1)` and skipped at 1.0, as in `_consume` for the
  microphone. Block boundaries and the clock are not simulated (the Segmenter is pure).
- **Segmenter.** Production `Segmenter(AudioSettings(), stats, "mic")` with the defaults: vad_start
  0.5, vad_continue 0.35, energy_floor 0.015, hangover 1000 ms, preroll 300 ms, min_speech 400 ms,
  max 30000 ms, overlap 2000 ms (62 windows = 1984 ms), mic_gain 1.0. The instance's `_utterance`
  and `_cut` are wrapped to record, for every candidate it judges, start, end, `continues`, whether
  it came from a cut, fresh windows (speech windows after the seeded ones), voiced windows, the
  voiced RMS (same formula as the Segmenter), the RMS of the fresh voiced windows alone, whether the
  last window is voiced, and the verdict (`kept`, `short`, `quiet`); the wrapper calls the original
  and returns its result unchanged. "Continuation" counts every candidate with `continues=True`,
  kept or dropped; "first" is every other candidate.
- **Structural checks**, for every kept continuation and the kept utterance right before it:
  `start_ms == previous.end_ms - 1984`, `end_ms > previous.end_ms`, previous length at most
  `max_segment_ms + 32` (30032 ms; a cut is at 938 windows = 30016 ms), plus samples = span x 16.
- **Short-tail trials.** From the gain-x1 natural run, cuts whose utterance was kept and whose last
  window before the cut is voiced (eligible: 7, 10, 33, 40 per recording), up to 10 per recording
  evenly spread: 37 trials. A trial re-feeds the audio from the window-aligned start of the chain
  (start of the first utterance minus 300 ms), with a fresh detector. Pass 1 finds where this fresh
  detector cuts (the cut nearest the natural one, within 2 s). The real audio is then truncated at
  that cut + X ms, X in {0, 32, 64, 96, 160, 224, 320, 400, 640}, followed by 2 s of Gaussian
  filler noise at the recording's own 5th-percentile window RMS (zero windows excluded; none
  existed), which is 0.0007 (fr2), 0.0028 (fr3), 0.0012 (en1) and 0.0005 (en3). Then the
  Segmenter output for the part after the cut is read. The filler is scaled with the gain, as the
  whole recording is. X = 0 is the control. One noise draw per trial, shared by all X and gains.
  **Deviation from the plan, explained:** the plan truncated at the natural cut and counted trials
  whose cut landed on the same sample. A fresh detector cut on that same sample in only 10 of 37
  trials (see Results), so truncating at the natural cut would have left most trials with the real
  speech cut at a place that is not a cut. Truncating at the fresh detector's own cut keeps every
  tail right after a real cut of this detector; 5 of 37 own cuts end on an unvoiced window, so
  they fail the selection rule the trials were built on. The same-sample count is still reported.
- **Gain sweep.** The same four recordings (natural run) and the same 37 trial specs, re-run at
  gains x1, x0.5, x0.25, x0.125, x0.0625. Silero is not scale invariant, so cuts, own cuts and
  chains differ per gain; trial specs are fixed from the x1 run and each gain finds its own cut.
- **Statistics.** Median is the ordinary median; p5 and p95 are nearest rank
  (`sorted[ceil(q n) - 1]`); max and min are exact.

## Results

### Natural run, gain x1

| Recording | Kept utterances | 30 s cuts | Continuations (kept) | Fresh ms of a continuation: median / p95 / max (min) | Fresh 1-12 windows (kept) | 0 fresh (dropped `short`) | First: kept / short / quiet | Continuation: kept / short / quiet |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fr2 | 116 | 8 | 8 (8) | 3536 / 19200 / 19200 (1504) | 0 (0) | 0 (0) | 108 / 0 / 0 | 8 / 0 / 0 |
| fr3 | 111 | 11 | 11 (10) | 6560 / 18848 / 18848 (0) | 0 (0) | 1 (1) | 101 / 0 / 0 | 10 / 1 / 0 |
| en1 | 50 | 40 | 40 (40) | 24464 / 25856 / 26528 (2048) | 0 (0) | 0 (0) | 10 / 0 / 0 | 40 / 0 / 0 |
| en3 | 69 | 47 | 47 (45) | 25280 / 26400 / 26624 (0) | 0 (0) | 2 (2) | 24 / 0 / 0 | 45 / 2 / 0 |
| all | 346 | 106 | 106 (103) | 24464 / 26240 / 26624 (0) | 0 (0) | 3 (3) | 243 / 0 / 0 | 103 / 3 / 0 |

The smallest non-zero fresh speech of a natural continuation is 1248 ms (39 windows).

### Structural checks (kept continuations)

| Gain | Kept utterances (natural) | Violations, natural runs | Violations, trial runs |
| --- | --- | --- | --- |
| x1 | 346 | 0 | 0 |
| x0.5 | 348 | 0 | 0 |
| x0.25 | 191 | 0 | 0 |
| x0.125 | 0 | 0 | 0 |
| x0.0625 | 0 | 0 | 0 |

### Short tail after a cut, gain x1

37 trials per X (7 fr2, 10 fr3, 10 en1, 10 en3). A fresh detector found its own cut in 37 of 37;
the cut was on the natural sample in 10 of 37 (fr2 5, fr3 0, en1 1, en3 4); the offset of the own
cut from the natural one was -9 to +1 windows (the natural cut is later by up to the preroll
length). The own cut's last window was voiced in 32 of 37.

| X ms | Kept | Dropped `short` | Dropped `quiet` | Fresh windows >= 0.35: median / min | Old rule (fresh ms >= 400) would drop | Kept with 0 fresh | `short` with fresh > 0 | `quiet` with RMS >= floor | Kept with RMS < floor |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 (control) | 8 | 29 | 0 | 0 / 0 | 37 | 0 | 0 | 0 | 0 |
| 32 | 34 | 3 | 0 | 1 / 0 | 37 | 0 | 0 | 0 | 0 |
| 64 | 35 | 2 | 0 | 2 / 0 | 37 | 0 | 0 | 0 | 0 |
| 96 | 35 | 2 | 0 | 3 / 0 | 37 | 0 | 0 | 0 | 0 |
| 160 | 36 | 1 | 0 | 5 / 0 | 35 | 0 | 0 | 0 | 0 |
| 224 | 36 | 1 | 0 | 7 / 0 | 36 | 0 | 0 | 0 | 0 |
| 320 | 37 | 0 | 0 | 10 / 3 | 34 | 0 | 0 | 0 | 0 |
| 400 | 37 | 0 | 0 | 13 / 6 | 6 | 0 | 0 | 0 | 0 |
| 640 | 37 | 0 | 0 | 20 / 6 | 2 | 0 | 0 | 0 | 0 |
| all 333 | 295 | 38 | 0 | | 261 | 0 | 0 | 0 | 0 |

Kept by recording, X = 0 / 32 / 64 / 96 / 160 / 224 / 320 / 400 / 640: fr2 1, 6, 7, 7, 7, 7, 7,
7, 7 of 7; fr3 5, 9, 9, 9, 10, 10, 10, 10, 10 of 10; en1 1, 9, 9, 9, 9, 9, 10, 10, 10 of 10; en3 1,
10 x 8 of 10. At X = 0, the 8 kept continuations had 1 to 8 fresh windows (fresh counts of the
other 29: 0); the filler noise was the only audio after the cut. Of the 295 kept continuations, 223
would have been dropped by the old rule; the 38 `short` drops are the same ones the old rule drops. Without the 5 trials whose own cut ends
on an unvoiced window (fr3 3, en1 2), the fresh-window median / min at X = 320, 400 and 640 stay at
10 / 3, 13 / 6 and 20 / 6: the minimum does not come from those cuts.

Voiced RMS of the fresh windows alone next to the whole voiced RMS, gain x1, median over the
continuations with fresh > 0:

| X ms | n | Fresh windows alone | Whole voiced |
| --- | --- | --- | --- |
| 32 | 34 | 0.0376 | 0.0567 |
| 64 | 35 | 0.0303 | 0.0546 |
| 96 | 35 | 0.0298 | 0.0549 |
| 160 | 36 | 0.0344 | 0.0546 |
| 224 | 36 | 0.0377 | 0.0558 |
| 320 | 37 | 0.0413 | 0.0560 |
| 400 | 37 | 0.0433 | 0.0549 |
| 640 | 37 | 0.0450 | 0.0552 |

### Quiet check, natural runs (voiced RMS, median / p5 / p95 / max)

| Gain | Group | Candidates | Voiced RMS | Kept | `quiet` | `short` | `quiet` with RMS >= 0.015 | Kept with RMS < 0.015 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| x1 | first | 243 | 0.0673 / 0.0425 / 0.0926 / 0.1164 | 243 | 0 | 0 | 0 | 0 |
| x1 | continuation | 106 | 0.0473 / 0.0327 / 0.0767 / 0.0894 | 103 | 0 | 3 | 0 | 0 |
| x0.5 | first | 255 | 0.0337 / 0.0206 / 0.0466 / 0.0585 | 255 | 0 | 0 | 0 | 0 |
| x0.5 | continuation | 97 | 0.0238 / 0.0169 / 0.0381 / 0.0447 | 93 | 2 | 2 | 0 | 0 |
| x0.25 | first | 340 | 0.0154 / 0.0090 / 0.0227 / 0.0292 | 178 | 161 | 1 | 0 | 0 |
| x0.25 | continuation | 17 | 0.0167 / 0.0133 / 0.0223 / 0.0223 | 13 | 3 | 1 | 0 | 0 |
| x0.125 | first | 355 | 0.0078 / 0.0045 / 0.0114 / 0.0146 | 0 | 353 | 2 | 0 | 0 |
| x0.125 | continuation | 0 | - | 0 | 0 | 0 | 0 | 0 |
| x0.0625 | first | 351 | 0.0039 / 0.0023 / 0.0056 / 0.0073 | 0 | 349 | 2 | 0 | 0 |
| x0.0625 | continuation | 0 | - | 0 | 0 | 0 | 0 | 0 |

Cut pairs (a cut candidate and the part that follows it, which starts 1984 ms before the cut end):

| Gain | Pairs | Split verdicts | Part before the cut dropped `quiet` -> next part judged a new start (`continues=False`) | Continuation fresh windows (natural): total / median / with 0 fresh |
| --- | --- | --- | --- | --- |
| x1 | 106 | 3 (kept then dropped `short`) | 0 of 0 | 63357 / 764.5 / 3 (of 106) |
| x0.5 | 99 | 6 (4 kept then dropped: 2 `short`, 2 `quiet`; 2 dropped then kept) | 2 of 2 (cut dropped `quiet`) | 56805 / 757 / 2 (of 97) |
| x0.25 | 98 | 6 (4 kept then dropped: 1 `short`, 3 `quiet`; 2 dropped then kept) | 81 of 81 (80 of them a first utterance) | 4965 / 223 / 1 (of 17) |
| x0.125 | 97 | 0 | 97 of 97 | 0 / - / 0 |
| x0.0625 | 97 | 0 | 97 of 97 | 0 / - / 0 |

The continuation fresh-window totals are over different cuts per gain and are not comparable.
Natural continuations dropped `quiet` (x0.5: 2, x0.25: 3) had a voiced RMS of 0.01333 to 0.01498;
the fresh windows alone of 1 of the 5 were at 0.01513 (RMS 0.01498 whole), the rest below.

Natural continuations, fresh voiced windows alone against the floor (0.015):

| Gain | Kept | Kept with fresh windows alone < floor | `quiet` | `quiet` with fresh windows alone >= floor |
| --- | --- | --- | --- | --- |
| x1 | 103 | 0 | 0 | 0 |
| x0.5 | 93 | 2 | 2 | 1 |
| x0.25 | 13 | 0 | 3 | 0 |

### Quiet check, short-tail trials, all 9 tails per gain

Continuations are the parts after the trial's own cut; "evaluated" is the trials in which the
fresh detector found a cut within 2 s of the natural one (same-sample count in brackets).

| Gain | Trials | Evaluated (same sample) | Cut kept | Cut dropped `quiet` (next part a new start) | Continuation kept / `short` / `quiet` | `quiet` with RMS >= floor | Kept with RMS < floor |
| --- | --- | --- | --- | --- | --- | --- | --- |
| x1 | 333 | 333 (90) | 333 | 0 (0) | 295 / 38 / 0 | 0 | 0 |
| x0.5 | 333 | 279 (63) | 279 | 0 (0) | 233 / 33 / 13 | 0 | 0 |
| x0.25 | 333 | 252 (63) | 117 | 135 (135) | 73 / 12 / 32 | 0 | 0 |
| x0.125 | 333 | 252 (45) | 0 | 252 (252) | 0 / 0 / 0 | 0 | 0 |
| x0.0625 | 333 | 252 (9) | 0 | 252 (252) | 0 / 0 / 0 | 0 | 0 |

(The "trials" column counts 37 trials x 9 tails; "evaluated" are those with a cut found.)

| Gain | Fresh windows alone: median RMS | Whole voiced: median RMS (same, fresh > 0) | Continuations kept whose fresh windows alone are < floor | `quiet` continuations whose fresh windows alone are >= floor | Pairs with the same cut as x1 | Of those, fresh-window count differs from x1 | Delta (this gain minus x1): median, min to max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| x1 | 0.0369 | 0.0553 | 45 of 295 | 0 of 0 | - | - | - |
| x0.5 | 0.0203 | 0.0289 | 84 of 233 | 6 of 13 | 261 | 69 | 0, -2 to +7 |
| x0.25 | 0.0136 | 0.0180 | 35 of 73 | 9 of 32 | 225 | 97 | 0, -5 to +5 |
| x0.125 | - | - | - | - | 180 | 107 | +1, -6 to +5 |
| x0.0625 | - | - | - | - | 135 | 102 | +2, -7 to +7 |

(At x0.125 and x0.0625 the pairs with the same cut are parts judged as new starts, since the part
before the cut is dropped.) Split verdicts in the trials with X >= 160 (kept then dropped, by reason):
x1: 2 `short`; x0.5: 7 `quiet`, 2 `short`; x0.25: 20 `quiet` (cut kept, continuation dropped),
plus 3 where the cut was dropped `quiet` and the next part (a new start) was kept.

Old rule (fresh ms >= 400) counterfactual over the evaluated continuations: x1 would drop 261 of
333 (the new rule drops 38); x0.5 216 of 279 (new rule 33 `short`); x0.25 88 of 117 (new rule 12).

## What the numbers say

- **The finding holds on real speech.** Over the 333 trials at x1 (37 cuts x 9 tails) and the 106
  natural continuations, every continuation with at least one fresh window at or above 0.35 and a
  voiced RMS at or above the floor was kept (0 kept with 0 fresh windows, 0 `short` with fresh > 0).
  Every `short` drop of a continuation had 0 fresh windows: 38 of 38 in the trials, 3 of 3 in the
  natural run. At the same time 0 continuations were dropped `quiet` with an RMS at or above 0.015 and 0
  were kept below it, at every gain, natural and trials (the "expected 0" cases are 0).
- **What the old rule would have cost.** In the natural run no continuation had 1 to 12 fresh
  windows, so the old rule would have changed 0 of 106 natural verdicts (the smallest non-zero
  fresh speech is 1248 ms). The change matters for the tails that real cuts do not produce often in
  read speech: of the 333 synthetic tails, the old rule drops 261 and the new rule 38; 223
  continuations carrying 1 to 12 fresh windows are kept by the new rule only.
- **X = 0 is not "nothing".** With the real audio ending at the cut and 2 s of low-level noise
  after, 8 of 37 continuations were kept, with 1 to 8 fresh windows. Either the recurrent state of
  Silero carries over for a few windows after speech stops, or the noise trips the model; the run
  cannot tell them apart. fr3 gives 5 of the 8 keeps and has both the loudest filler (0.0028) and
  the DC offset (see Limits), so a third cause is possible: zero-mean filler appended to audio with
  an offset puts a step at the truncation point. A kept continuation then re-sends 2 s of already transcribed speech plus
  that tail to Whisper (the overlap duplication is the business of `trim_overlap`, not measured here).
- **Natural data is mostly continuous.** These two French readers pause more than these two English
  ones: 8 and 11 cuts in 28 and 24 minutes against 40 and 47 in 21 and 27 minutes. In en1 only 10
  of 50 utterances are first utterances. A cut needs 30 s with no pause of 1 s or more.
- **Structure.** 0 violations in all natural runs and trial runs at every gain: each kept
  continuation starts exactly 1984 ms before the end of the part before it, ends after it, and the
  previous part is at most 30016 ms.
- **Level.** The voiced RMS of these mastered reads is a median of 0.067 (first) and 0.047
  (continuation) at x1, 13 dB above the 0.015 floor for first utterances. The medians halve with
  the gain as expected. The floor starts to bite at x0.5 (2 of 97 continuations; 0 of 255 first),
  is at the median level at x0.25 (161 of 340 first utterances dropped `quiet`) and drops everything
  at x0.125 and below.
- **A dropped part before the cut starts a new chain.** When the part before the cut was dropped `quiet`, the
  next part was always judged a new start (`continues=False`): 2 of 2 at x0.5, 81 of 81 at x0.25,
  97 of 97 at x0.125 and at x0.0625. 2 of those 81 at x0.25 were then kept (they carry the 2 s of
  the dropped part). Split verdicts on a cut pair (one part kept, the other dropped): 3 at x1
  (all `short` with 0 fresh), 6 at x0.5, 6 at x0.25, 0 below.
- **Silero's fresh-window count depends on the gain.** For trials whose cut is identical to the x1
  one, the count of fresh windows differs from x1 in 69 of 261 (x0.5), 97 of 225 (x0.25), 107 of 180
  (x0.125) and 102 of 135 (x0.0625), by -7 to +7 windows, and is slightly larger at the lower
  gains in total (1826 -> 1903, 1579 -> 1674, 1310 -> 1443, 1003 -> 1194 windows).
- **Fresh versus whole level.** The RMS of the fresh windows alone is below the whole voiced RMS
  (median 0.0369 against 0.0553 at x1): the seeded overlap is louder than the tail. 45 of the 295
  kept continuations at x1 (84 of 233 at x0.5) have fresh windows alone below the floor yet are
  kept, because the gate judges the whole voiced set, overlap included. The reverse also occurs:
  6 of 13 `quiet` drops at x0.5 and 9 of 32 at x0.25 had fresh windows alone at or above the floor.

## Open

1. **The quiet gate of a continuation counts the 2 s overlap.** `_utterance` computes the voiced
   RMS over every voiced window, seeded ones included, although the `short` gate already counts
   fresh windows only for a continuation. At x0.5 and x0.25 15 trial continuations (6 + 9) were
   dropped `quiet` with fresh speech of their own at or above 0.015, so a tail of up to 20 windows
   (640 ms) after a kept cut was lost, and the reverse (kept with fresh windows alone below the
   floor) occurred 45 times at x1 and 84 times at x0.5. At x1 on the natural data neither happens
   (0 of 103 kept, 0 of 0 `quiet`; table in the cut-pair section). Proposed default, not
   implemented: for a continuation, judge `quiet` on the fresh voiced windows alone, like `short`.
   Not tested here beyond these counts. That gate would also drop the 8 X = 0 keeps, whose fresh
   windows are filler alone (0.0005 to 0.0029): the keep-on-noise case of the X = 0 bullet.
2. **No measurement at headset levels.** The floor, the vad thresholds and the 400 ms rule are
   untouched; whether real Discord and game-voice speech sits at x1, x0.5 or x0.25 of these
   read recordings is unknown (see Limits). Default: measure the voiced RMS of real captured
   segments (the numbers the Debug page already shows) before touching the floor.

## Limits

- Read and mastered speech (audiobook readers, studio or quiet room, 64 kb/s MP3), not headset
  microphones, Discord compression or game audio; the gain sweep scales these recordings and
  cannot imitate a different microphone chain, noise floor or compression. fr3 has a small DC
  offset: 0.0086 is the mean of the whole recording, and the offset is not constant (window means
  from 0.0002 at p5 to 0.027 at p95, speech RMS about 0.05); the other kept recordings have none.
- No Whisper: transcripts, hallucination on quiet or noise tails and `trim_overlap` (#137) are not
  checked. No headset, loopback, microphone or running app.
- Silero state is reset per run and per trial, so a trial's cut is not the natural cut (10 of 37
  on the same sample); trials are built on the fresh detector's own cut, 5 of 37 of which have an
  unvoiced last window (the selection rule applied to the natural cut). Only one detector build
  (one onnxruntime session, 1 thread) and one noise draw per trial were run.
- Short tails are synthetic: the real audio is cut at the cut + X and the filler is Gaussian noise
  at the recording's 5th-percentile window RMS (0.0005 to 0.0028), which differs from the room
  noise that follows real speech; at X = 0 8 of 37 continuations were kept on filler alone.
- Natural data contains no continuation with 1 to 12 fresh windows, so the rule change is
  evidenced by the trials only. 3 natural continuations had 0 fresh windows.
- Small n: 4 recordings, 106 natural continuations, 37 trial cuts (333 tails per gain; the 9 tails
  of one cut are not independent). p5 and p95 are nearest rank; for n under 20 (fr2, fr3, the x0.25 continuations) the p95
  is the maximum. At x0.125 and x0.0625 no continuation exists, so those rows have no level data.
- Replaced sources: fr1 (`tour_du_monde_en_80_jours_librivox`, `tour_monde_01_verne_64kb.mp3`, sha256
  `7f232f77ab56fc9f1f96decfb098979af73a50a85b6e3a3771d04f5f14e9c400`, 723.5 s) had 2 cuts, below
  the 3 required; en2 (`adventures_holmes`, `adventureholmes_03_doyle_64kb.mp3`, sha256
  `d7f324f8bf7a5991bedbf282dc49a6709555855dea3792e1b1271f7f36da2de4`, 2719.4 s) has a constant DC
  offset of 0.094 that makes its window RMS never fall below 0.093. Their first natural-run numbers
  were not used. A first trial design that truncated at the natural cut (6 of 37 trials aligned at
  x1) was replaced by the own-cut design above before the numbers here were produced.
