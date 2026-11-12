# Fixture: ASR hallucination rejection (invent run QB)

Dev-only evaluation fixture and harness. Nothing here is app code; no Akashic data directory,
database or captured audio is read. All audio is public.

## Commands (from `eval\`, with the environment of `eval\requirements.txt`)

```powershell
.venv\Scripts\python.exe -I transcript-hallucination-rejection\fetch_data.py        # downloads -> data\raw, data\models
.venv\Scripts\python.exe -I transcript-hallucination-rejection\build_fixture.py     # -> data\fixture (clips.jsonl + int16 .npy)
.venv\Scripts\python.exe -I transcript-hallucination-rejection\run_production.py --check 40   # -> data\cache (resumable)
.venv\Scripts\python.exe -I transcript-hallucination-rejection\harness.py transcript-hallucination-rejection\ref_today.py --name ref
```

Harness output: `<name>\metrics.txt` (one line), `<name>\details.json` (metrics + one row per
segment with raw/stored text and flags), `<name>\side.png` (fixed layout and scales).

Dev venv additions (pinned to uv.lock): `faster-whisper==1.2.1`, `ctranslate2==4.8.5`,
`nvidia-cublas-cu12==12.9.1.4` (pulled `tokenizers 0.23.3`, `huggingface_hub 2.4.0`, `tqdm`).
Already present: `av 19.0.2`, `onnxruntime 1.31.1` (CPU; Silero runs on CPU in production too),
`numpy 2.5.4`, `matplotlib`. torch/transformers/onnxruntime-gpu were not needed.

## Data (all account-free, no licence gate)

| Source | URL | Licence | Used | On disk |
|---|---|---|---|---|
| Whisper large-v3 (Systran CT2, commit `edaa852`, same sha256 as `pipeline/models.py`) | huggingface.co/Systran/faster-whisper-large-v3 | MIT | primary + helper decodes | 3.09 GB |
| FLEURS `fr_fr` + `en_us` dev | huggingface.co/datasets/google/fleurs `data/{lang}/audio/dev.tar.gz` + `dev.tsv` | CC-BY-4.0 | names, normal, concat speech; previous-tail texts | 300 MB |
| Multilingual Spoken Words (MSWC), opus shards fr test 0,2,3,4 + dev 2; en test 1,3,4,6,7,8,11 + dev 11 | huggingface.co/datasets/MLCommons/ml_spoken_words `data/opus/{lang}/{split}/audio/{n}.tar.gz` | CC-BY-4.0 | short replies (1 s word clips) | 14 MB kept (~5 GB streamed, not stored) |
| Google Speech Commands v0.02 test (`yes`, `no`) | download.tensorflow.org/data/speech_commands_test_set_v0.02.tar.gz | CC-BY-4.0 | English "no"/"yes" (MSWC drops words < 3 letters) | 4.5 MB kept (113 MB streamed) |
| ESC-50 (all 2000 clips) | github.com/karolpiczak/ESC-50 (codeload master zip) | CC BY-NC 3.0 (personal, non-commercial) | non-speech: environment, human non-speech, domestic, urban | 646 MB |
| FSD50K eval subset (720 clips, no human-voice label; groups music 260 / game-like 220 / desk 120 / other 120) | huggingface.co/datasets/Fhrozen/FSD50k `clips/eval/*.wav` (mirror of Zenodo 4060432) | per clip: CC0 333, CC-BY 3.0 266, CC-BY-NC 3.0 90, Sampling+ 31 | non-speech: music, game-like, desk | 567 MB |
| FSD50K dev, VAD-prefiltered: 9000 non-voice clips streamed, the 252 that open a production segment at their fixture level kept as 16 kHz int16 | same mirror, `clips/dev/*.wav` | per clip (see `kept.json`) | non-speech that actually reaches Whisper | 105 MB |
| DEMAND 16 kHz, ch01 of DKITCHEN, NFIELD (mining) and DWASHING, STRAFFIC, TCAR (test) | zenodo.org/records/1227121 | CC-BY-4.0 | stationary noise, 15 s pieces | 523 MB |
| OpenGameArt CC0 packs: 512 retro SFX, 100 CC0 SFX, 80 creature SFX, 50 retro synth SFX, 25 bang SFX, 100 wood/metal SFX (Juhani Junkala / rubberduck), 5 Action Chiptunes, Chiptune Adventures | opengameart.org (URLs in `fetch_data.py`) | CC0 | game SFX sequences (3-12 s) and chiptune music (20 s pieces) | 85 MB |

Not used: Common Voice (licence gate), MUSAN (openslr mirror at ~1.3 MB/s, music-first tar),
Kenney packs (HTTP 403 to scripts).

Disk: raw 2.2 GB + model 2.9 GiB + fixture 1.6 GB (int16) + cache 0.75 GB = about 7.5 GB.

## Fixture (`build_fixture.py`)

Levels: every clip is scaled to a seeded log-uniform "active" level (RMS of the louder half of
its 32 ms windows): speech 0.03-0.15, non-speech 0.02-0.15. Dataset recording levels are not
playback levels (FLEURS sits mostly below the 0.015 energy floor: 55% of it was dropped before
this). Every clip is 16 kHz mono, padded with 0.5 s of digital silence before and 1.5 s after (as a
loopback stream hears silence), saved as int16 PCM. Each clip gets a production prompt:
`build_prompt(vocabulary, previous)` from the repo, with a per-clip vocabulary of 3-30 entries
drawn from a plausible personal list (friends, games, tools; invented strings) plus proper nouns
harvested from FLEURS, and the previous tail = an unrelated FLEURS transcript (same language
70%, else the other one). Names clips put 1-3 of their own names into the vocabulary 80% of the
time (`names_in_vocab`); other clips never include their own names.

- `short`: MSWC words (fr: oui, non, merci, bon, bien, alors, allez, voilà, vrai, bah, bonjour,
  pardon, parfait, salut, super; en: yes, yeah, yep, okay, nope, thanks, sure, sorry, right,
  hello, hey, nice, please, really, wow, fine, great) capped at 25 per word, plus Speech Commands
  yes/no. MSWC clips are words cut from read sentences, so they can hold fragments of the
  neighbouring words. French "ouais", "ok", "nan" do not exist in MSWC.
- `names`: FLEURS utterances with at least one non-initial capitalised word run.
- `normal`: other FLEURS utterances (<= 18 s).
- `concat_fleurs` / `concat_short`: a speech clip followed by 3-10 s of a non-speech clip of the
  same split that opens a production segment, cut from just before its first VAD-active window.
  Half are plain (0-0.3 s gap, tail at 0.3-1.0x the speech level), half are beds (tail starts
  1-3 s before the speech ends, 6-12 dB under it). Plain concatenations of arbitrary noise almost
  never stayed in the speech segment (1 s hangover), so this keeps the tail where Whisper hears it.
- `nonspeech`: ESC-50, FSD50K, DEMAND, OpenGameArt (<= 25 s, >= 1 s).
- Discord condition: every short/names/normal clip also exists as `<id>_opus`, the padded clip
  sent through 48 kHz Opus at 32 kbps and back (PyAV/libopus), same prompt.
- Split: `mining` (40%) vs `test` (60%) by sha1 of the source group (FLEURS sentence id, Common
  Voice source sentence, Speech Commands speaker, ESC-50 source file, FSD50K clip, DEMAND
  environment (fixed), OpenGameArt file; SFX sequences only combine files of one split).

## Production path (`prod.py`, `run_production.py`)

Uses the repo's own classes (imported from `src/`, no stubs needed):
`capture.audio.Segmenter` fed like `StreamRecorder._consume` (audio.py:473-497; zero windows get
probability 0.0, line 487), `capture.vad.SileroVad` (v6 ONNX, CPU), settings from
`capture/main.py:46-57` on `config.Audio()` defaults (vad 0.5/0.35, energy floor 0.015,
min speech 400 ms, hangover 1 s, max 30 s); storage round trip through
`storage.opus.encode_opus/decode_opus` (24 kbps); `transcribe.normalize_loudness`;
`WhisperTranscriber._load` (CUDA float16, local files) and the exact options of whisper.py:104-116
(beam 5, no conditioning, no_speech 0.6, log_prob -2.0, compression 2.4, suppress_blank, no VAD
filter), language = likelier of fr/en from `detect_language`. Today's outcome: empty text or
`is_hallucination` -> reject (transcribe.py:209-211), else unclear when
`search.hybrid.is_unclear(confidence)`, else keep. `--check` re-runs random segments through
`WhisperTranscriber.transcribe` itself and compares text and confidence.

Cache: `data/cache/segments.jsonl` (one row per stored segment: boundaries, VAD speech ms,
prompt/vocab/previous, language + fr/en/top-5 probabilities, every faster-whisper Segment with
seek/start/end/text/tokens/avg_logprob/no_speech_prob/compression_ratio/temperature, word timings
from a post-hoc alignment that does not change the decode, text, confidence, decode_ms, today),
`data/cache/audio/<seg>.npy` (stored samples, float32), `data/cache/vad/<clip>.npy` (Silero
probability per 32 ms window), `data/cache/clips.jsonl` (segments and gate drops per clip).

## Labels and metrics (`harness.py`)

Segment roles: `nonspeech` (no 100 ms overlap with the clip's speech region), `speech`,
`speech_tail` (a concat segment ending >= 2 s after the speech). The candidate sees no label;
segment ids are anonymised.

- Hallucination units: a `nonspeech` segment whose raw Whisper text is non-empty, and a
  `speech_tail` segment whose raw text has words after the end of the reference (difflib on
  `normalize`d words) with at least one aligned word starting >= 0.2 s after the speech ends.
- Recall = not stored as clear: reject or unclear (tails: also when the stored text has no
  trailing extra words). Reject-only recall = not stored at all (reject, or tail stripped).
- False rejection (real speech, `speech` + `speech_tail`): rejected, or the stored text matches
  fewer reference words than the raw Whisper text did (a correctly heard word was stripped).
  Reported per subset with Wilson 95% upper bounds; `frr_among_today_nonempty` excludes
  segments Whisper already returned empty or the whole-string filter already dropped.
- Unclear rate on real speech; extra decodes per suspect (segments with >= 1 helper decode;
  counted by the harness and max'ed with the candidate's report); extra time = total judge wall
  time / total primary (detect + decode) time.

Candidate API: `judge(clip_cache, helpers)`; optional `fit(mining_pairs, helpers)`.
`helpers`: `samples(cc)`, `whisper_input(cc)`, `silero(samples)`, `model`, `ct2`, `whisper`,
`decode(samples, prompt, language=None, **overrides)`, `production(samples, prompt)`,
`detect_language`, `align_words`, `encode`, `repo` (normalize, HALLUCINATIONS,
is_hallucination, build_prompt, confidence, is_unclear and the constants), `mining`.

## Limits

- Read speech (FLEURS, Common Voice words), not Discord conversation; MSWC words carry
  neighbour fragments; no real game or Discord captures; Opus 32 kbps is the only channel effect.
- Laughter, coughing, crying etc. (ESC-50) count as non-speech: any text there is a hallucination.
- FSD50K labels are weak: a few clips may contain unlabelled voice.
- Silero window probabilities are not stored by the app today; a candidate using them needs a
  capture change (VAD changes are a non-goal). Candidates can recompute them on the stored
  segment with `helpers.silero`.
- Tail detection relies on Whisper's word timings; tails that merge into the last real word
  are not counted.

## Reference result (today, test split; `ref\`)

`ref recall=0.727 (n=253) reject_recall=0.344 ns_recall=0.726 tail_recall=1.000 (n=1) FRR=0.0086 (ub95=0.0158, n=1157) short=0.0156/0.0336 names=0.0000/0.0190 opus=0.0038/0.0138 fr=0.0118/0.0274 en=0.0068/0.0158 unclear=0.168`

- Fidelity: 40/40 random segments identical (text and confidence) to `WhisperTranscriber.transcribe`.
- Segments that reached Whisper (all / test): speech 1926 / 1125, speech_tail 45 / 32,
  nonspeech 432 / 261 (every one produced text). Subsets (test): short 187 + 198 opus, names 99 +
  99 opus, normal 229 + 229 opus, concat 146 (93 fleurs, 53 short), FSD50K-dev 172, ESC-50 37,
  FSD50K eval 11, OpenGameArt 11, DEMAND 0. Speech by language (test): fr 423, en 734.
- All 10 false rejections today are segments Whisper itself returned empty or the whole-string
  filter matched; FRR among segments today keeps non-empty is 0/1147. Unclear: short replies 45%,
  names/normal 0%.
- Non-speech recall by source (test, not-clear): FSD50K-dev 114/166, ESC-50 29/35, FSD50K 8/10,
  OpenGameArt 11/11, concat tail-only segments 21/30.
- Trailing hallucination inside a real-speech segment is structurally rare here: only 1 tail
  unit in test (n=1), and 27 plain speech segments show raw words after the reference end (mostly
  MSWC neighbour fragments). The tail metric is not statistically usable; whole-segment
  hallucination is what this fixture measures.
