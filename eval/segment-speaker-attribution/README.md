# Segment speaker attribution: fixture and harness (dev-only)

Scope: "QA Speaker attribution" invent run. Nothing here is app code; the app's modules are
imported read-only from `src/akashic` (see `common.py`). No user data is opened.

## Run

From this folder (PowerShell), with the dev venv `..\.venv` (Python 3.14):

```
..\.venv\Scripts\python.exe prepare_sources.py          # decode corpora -> data\sources (~1.5 min)
..\.venv\Scripts\python.exe make_fixture.py             # deterministic fixture -> data\fixture
..\.venv\Scripts\python.exe harness.py candidates\reference.py      # -> out\reference\
..\.venv\Scripts\python.exe harness.py candidates\<cand>.py         # -> out\<NAME>\ (side.png vs reference)
```

`--limit N` runs only the first N evaluation segments; `--no-drift` skips the drift stream.

Dev-venv packages installed for this (dev-only, not app dependencies): `onnxruntime==1.31.1`
(CPU build; the app ships `onnxruntime-gpu` 1.31.1 but the speaker models run on
`CPUExecutionProvider`, the same kernels), `av==19.0.2` (as `uv.lock`), `pyarrow` (reads the MLS
parquet files). numpy, matplotlib and sqlcipher3 were already there.

## Data (downloads under `data\`)

| What | Source | Licence | Size |
|---|---|---|---|
| LibriSpeech dev-clean + test-clean (EN, 80 speakers, 40 F / 40 M, ~8 min each) | https://www.openslr.org/resources/12/dev-clean.tar.gz, .../test-clean.tar.gz | CC BY 4.0 | 338 + 347 MB |
| MLS French dev + test + 9_hours (FR, 18 + 18 + 30 speakers, read audiobooks) | https://huggingface.co/datasets/facebook/multilingual_librispeech/resolve/main/french/{dev,test,9_hours}-00000-of-00001.parquet (not gated, no login) | CC BY 4.0 | 157 + 158 + 142 MB |
| pyannote segmentation-3.0 ONNX (app pin, sha256 057ee564...ea25 verified) | huggingface.co/onnx-community/pyannote-segmentation-3.0 @733a93b6 | MIT | 6.0 MB |
| WeSpeaker ResNet34-LM ONNX (app pin, sha256 7bb2f06e...c068 verified) | huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM @f0c48c29 | CC BY 4.0 | 26.5 MB |

Derived: `data\sources` (per-speaker int16 banks, at most 900 s per speaker, ~2.8 GB),
`data\fixture` (Ogg/Opus segments + `index.json`). Total on disk about 4 GB.

Not used: Common Voice (Mozilla Data Collective needs an account), full MLS French (16-61 GB),
FLEURS (no speaker ids), MUSAN noise (11 GB; replaced by synthetic pink/brown noise).
The MLS parquet has no gender: it is estimated from median F0 (>= 165 Hz = F); on LibriSpeech
the same estimate agrees with the corpus gender for 78/80 speakers.

## Fixture (`make_fixture.py`, seed 20261110)

- Roles: EN 30 enrolled (15 F / 15 M) + 10 impostors; FR 26 enrolled (13 / 13) + 10 impostors.
  Each speaker's utterances are split: enrolment clips and test material never share an
  utterance; impostors never enrol.
- Enrolment: 3-5 clips of 4-12 s per speaker (the turns a named profile is built from).
- Signal chain per segment: each speaker's track, level-normalised, 50% with pink or brown
  noise at 5-20 dB SNR, through libopus at 48 kHz `application=voip` 24/32/64 kbps (Discord-like,
  per speaker), mixed, then the app's own `storage/opus.encode_opus` (24 kbps) - the bytes the
  speakers job decodes. Codec delay checked: 0 samples.
- Segment shape as `capture/audio.py`: 300 ms preroll, pauses inside a segment < 1 s (corpus
  pauses > 0.7 s shortened to 0.38 s), 200 ms tail, cap 30.016 s (938 x 32 ms), continuation
  starting 1.984 s (62 windows) before the cut.
- Kinds: solo 5-30 s (4 per enrolled speaker); `solo_10k` lengths 10k + 0.5..2 s, k = 1, 2
  (3 per speaker); `solo_10_0` 10.0-10.49 s (1 per speaker); `similar` (8 most similar
  same-gender enrolled pairs by enrolment-centroid cosine: 4 solos per speaker + 2 mixed per
  pair); `impostor` solos (4 per impostor); `mixed` two-speaker: alternating turns, a 0.3-1 s
  backchannel (half in the last 10 s window), a 1-2.5 s reply at the end; 30% of partners are
  impostors; continuation pairs (120): `same` (one speaker across the cut), `switch` (the lead
  is the previous speaker, the next speaker starts 0.1-0.6 s after the cut), `switch_early`
  (next speaker starts 0.3-1 s before the cut, so the lead is mixed).
- Labels: speech activity from 32 ms frame energy on the clean source (-30 dB from the
  utterance's 95th percentile); `labels` = speakers with >= 300 ms after the lead; `single` =
  the only one; `expected` = single if enrolled, else null. Speech spans are stored for mapping
  candidate-created profiles to speakers. The lead length is stored (`lead_ms`); whether a
  transcript trim would succeed is not modelled.
- Drift stream: 500 solo segments (5-20 s) of the most similar enrolled pair, interleaved at
  random (material is reused with different crops, levels and codec draws).

Limits: read audiobook speech, not conversation; overlap only from backchannels and early
switches; no packet loss; noise is synthetic; two FR speakers' genders may be wrong (F0).

## Harness

See the docstring of `harness.py` and the interface in `common.py` (`Segment`, `PrevSegment`,
`Attribution`, `SharedModels`). A candidate replaces windowing / identification / profile
updates; `SharedModels` gives the app's `turns()` and `embed()` and the raw
`segment_logits(window)` with every ONNX forward timed.

Counts: 1008 evaluation segments (602 solo incl. 168 `solo_10k`, 56 `solo_10_0`, 80 `similar`,
90 continuation firsts; 80 impostor; 176 mixed, 4 of which ended with only one speaker >= 300 ms
and are scored as solo; 120 continuation seconds) + 500 drift segments; 722 segments have an
expected author. Most similar enrolled pairs (enrolment-centroid cosine): fr10087/fr11247 0.559
(the drift pair), fr1770/fr9242 0.529, en3729/en6319 0.515. Strongest impostor: 0.65 to an
enrolled voice.

## Reference (today's pipeline), `out\reference\metrics.txt`

```
solo_auth=0.7008 solo10k_auth=0.4643 solo10_0_auth=0.9821 cont2_auth=0.4224 wrong=0.0794
wrong_mixed=0.3239 wrong_impostor=0.1250 p50_ms=99.2 p95_ms=173.1 drift_wrong_first100=0.0000
drift_wrong_last100=0.0000 drift_cos_end_min=0.9566 drift_pair_cos_end=0.5199
```

- Authorship (722 single-enrolled segments) 70.1%; solo 75.2%, `solo_10k` 46.4%, `solo_10_0`
  98.2%, continuation second 42.2% (`same` 85%, `switch` 17%, `switch_early` 24%), first 80%.
  Failures are the documented mechanism: a NULL turn in the tail window (< 1 s, or 1-2 s with
  no match) or the previous speaker's turn in the 1.984 s lead.
- Wrong-author 7.9% overall: solo 0%, mixed 32.4% (backchannel 68%, last-window 36%,
  alternating 8%, reply 7.5%: the short second voice is dropped under 300 ms or merged into the
  main local speaker by segmentation), impostor 12.5% (matched an enrolled profile at >= 0.5),
  `cont_first` 9% (the `switch_early` firsts, which are mixed).
- Drift (fr10087/fr11247, cosine 0.56): no wrong author in the first or last 100; centroids end
  at 0.957/0.957 to their clean enrolment centroids and 0.52 to each other (0.57 at start). On
  this fixture today's pipeline does not show cross-voice drift; the stream is a regression
  check, not a demonstration of the defect.
- Timing (attribute() wall time, decode excluded, CPU provider, default threads, after
  warm-up): p50 99 ms, p95 173 ms, 6.3 ms per audio second; per segment 2.2 segmentation
  forwards (29 ms) and 2.4 embedding forwards (68 ms) on average. Run-to-run noise ~5 ms.
