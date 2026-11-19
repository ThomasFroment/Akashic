# How should a local transcriber choose French or English per speech segment?
Date: 2026-11-19 · Context: Python 3.14, faster-whisper 1.2.1, Whisper large-v3; Akashic `pipeline/whisper.py` (`_detect`), segments up to 30 s with a 2 s continuation overlap, `condition_on_previous_text=False` · Goal: choose the language per segment so French, English and mixed speech transcribe well (audit #139; TODO C3 #139) · Ruled out: cloud speech services

## Answer
Keep the current rule: run Whisper's language detection and pick the likelier of French and
English, which is what faster-whisper's maintainer advises [1][2]. On the smoke runs it picked the
right language on every single-language segment (English 25/25, French 7/7), so the French WER
there (0.27-0.29 against 0.06 for English) comes from an utterance cut across two segments and from
overlapping speech, not from language choice [3][4]. The remaining risk is short segments: with
Whisper choosing among all its languages, large-v2 identified 1 / 2 / 3 s clips at 68 / 90 / 95 %
[5], which overstates the error of a two-language choice and is unmeasured on Akashic's data. If
short segments do flip, carrying the previous language when the French/English margin is small is
the one mitigation with a measured gain elsewhere (about 0.9 points of language-diarization error,
Whisper medium) [6]. faster-whisper's `multilingual=True` cannot restrict candidates, so it does not
fit [2][7].

## Findings
### How detection works
- openai-whisper runs one decoder step at the start-of-transcript position, masks non-language
  tokens and takes the softmax; `transcribe()` detects once on the first (padded) 30 s and reuses
  that language [8].
- faster-whisper 1.2.1 trims audio to `language_detection_segments` × 30 s (default 1), stops at the
  first chunk above `language_detection_threshold` (0.5), else takes a majority vote; the returned
  `all_language_probs` come from the last chunk evaluated [1].
- `multilingual=True` re-detects on every 30 s window and takes the top language unconditionally;
  candidates cannot be restricted in that mode [1][7].
- Issue #1476 reports translated output with `multilingual=True` unless
  `condition_on_previous_text=False` [9]; Akashic already passes `False` [2].
- Akashic's `_detect` keeps only French and English from the detection probabilities and returns
  the likelier [2].
### Measured accuracy and mitigations
- Whisper large-v2 on a VoxLingua107-derived 8-language test set, choosing among its ~96
  languages: 68.0 / 90.3 / 95.4 % at 1 / 2 / 3 s (MLS 61.7 / 93.5 / 98.0, CommonVoice 23.5 / 79.9 /
  93.1) [5].
- Whisper medium language diarization on DISPLACE (Indian languages and English): restricting the
  output from 99 to 12 languages cut the error from 40.14 to 38.12 % with the tolerance rule on;
  the tolerance rule (keep the previous half-overlapping window's language when the top-2 gap is
  under 0.05) alone moved 5 s windows from 39.06 to 38.12 % (1 s: 46.8 → 45.83, 2 s: 42.6 → 40.99) [6].
- Intra-sentence code-switching with stock Whisper is poor and was fixed mainly by fine-tuning
  (Mandarin-English, Whisper small) [10].
### Akashic smoke runs
- Detected vs truth language per segment: run 2220 en→en 12, fr→fr 4, mixed→fr 1; run 2241 en→en 13,
  fr→fr 3, mixed→fr 1; TTS or multi-language segments excluded [3].
- French single-language segments: WER 0.03-0.13, except one utterance cut into two segments
  (6.5 s at 0.38, 4.9 s at 0.74) [3].
- M9.1: French 0.269 / 0.290, English 0.064 / 0.059, overlapping speech 0.753 / 0.753 [4].

## Caveats
- The smoke segments were mostly 5-12 s of read speech from corpora; Akashic's short, spontaneous
  segments were not represented, so short-clip error on its data is unmeasured [3][4].
- The short-clip accuracy figures are large-v2 choosing among all languages, from an arXiv
  preprint; they do not bound a French/English-only choice [5].
- The tolerance-rule gain is on a different task (language diarization), language set and model,
  with "previous" meaning the previous window [6].
- No source measures decoding in both languages and keeping the better score, or a dedicated
  language-ID model before Whisper, for French/English [5][6].
- Reusing the transcription pass's encoder output for detection is possible only through
  faster-whisper's private `generate_segments` (`transcribe.py:918, 1189-1190`) [1].

## Sources
[1] faster-whisper 1.2.1 `transcribe.py` — `.venv\Lib\site-packages\faster_whisper\transcribe.py:918, 1189-1198, 1768-1841` (https://github.com/SYSTRAN/faster-whisper) — tier 1 — 1.2.1
[2] Akashic `_detect` and decode options — `src/akashic/pipeline/whisper.py:117, 130-135`; maintainer advice in https://github.com/SYSTRAN/faster-whisper/issues/1164 — tier 1 / 2 — 2026-11-18
[3] Per-segment language check on the smoke stores — `eval/quality/spikes/segment_language_check.py`, output `eval/quality/spikes/segment_language_check.txt` — tier 1 — target PC, 2026-11-19
[4] Smoke scores — `eval/quality/results/20261118-smoke-s11-target-2220/scores.json`, `-2241/scores.json` (M9.1) — tier 1 — 2026-11-18
[5] Generative linguistic representation for spoken language identification — https://arxiv.org/html/2312.10964 — tier 1 (preprint) — Dec 2023
[6] Vachhani et al., Multi-resolution approach to identification of spoken languages… using Whisper — https://www.isca-archive.org/interspeech_2023/vachhani23_interspeech.pdf — tier 1 — Interspeech 2023
[7] faster-whisper issue #1164, constrain languages when autodetecting — https://github.com/SYSTRAN/faster-whisper/issues/1164 — tier 2 — accessed 2026-11-19
[8] openai-whisper `decoding.py`, `transcribe.py` — https://raw.githubusercontent.com/openai/whisper/main/whisper/decoding.py, https://raw.githubusercontent.com/openai/whisper/main/whisper/transcribe.py — tier 1 — main, 2026-11-19
[9] faster-whisper issue #1476 — https://github.com/SYSTRAN/faster-whisper/issues/1476 — tier 2 — accessed 2026-11-19
[10] Adapting OpenAI's Whisper for speech recognition on code-switch Mandarin-English — https://arxiv.org/abs/2311.17382 — tier 1 (preprint) — 2023
