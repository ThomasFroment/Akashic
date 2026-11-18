# Public conversational speech datasets (FR, EN, FR-EN mixed) with multi-speaker transcripts
Date: 2026-11-18 · Context: Python 3.14 harness `eval/quality/`, today LibriSpeech + MLS read speech · Goal: measure VAD, diarization, speaker identity and FR/EN/mixed transcription on Discord-like calls · Ruled out: none

## Answer
Free core set: **SUMM-RE dev+test** for French (spontaneous meetings, one track per participant, word timings, manual transcripts, CC BY-SA 4.0, ~28 GB) [1][2], and **AMI** for English (100 h meetings, four individual headset WAVs per meeting, CC BY 4.0) [4][5][6]. Summing the per-speaker tracks yourself gives exact overlap and speaker ground truth on a known mix. **CHiME-6 dev/eval** (CC BY-SA 4.0, 23 GB) adds heavy overlap, but it is far-field dinner-party audio, a stress test rather than a call stand-in [7]. No public real, conversational, speaker-labelled FR-EN code-switching corpus was found. Intra-sentence mixing can only come from small or synthetic sets: ServiceNow `asr_codeswitched` en_fr (298 test rows, no licence stated) [11][12], gated SwitchLingua [13], or the Arabic-centred CAFE [14]. Otherwise generate it yourself (LLM-written dialogue read by local TTS voices or bilingual readers). Paid broadcast corpora (ETAPE, REPERE, ALLIES via ELRA; Fisher, ICSI, DIHARD via LDC) only pay off if broadcast or phone speech is worth the fee [16][17][18][19][20].

## Options
| Dataset | Lang | Style | Speakers / labels | Hours | Licence | Size / access | Fit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SUMM-RE dev+test | FR | spontaneous meetings, some over Zoom | 3-4 per meeting, per-speaker tracks, word timings [1] | ~24 h manual (of ~95 h) [1][2] | CC BY-SA 4.0 [1] | 14.2 + 13.8 GB, HF, no gate stated [1] | Best FR match; mix tracks for overlap |
| AMI | EN | meetings (2/3 scenario) | ~4 per meeting, headset per speaker, NXT word/segment times [4][5] | 100 h [4] | CC BY 4.0 [4] | per meeting (~120 MB headsets), or HF `edinburghcstr/ami` 29 GB [5][6] | Best EN match |
| CHiME-6 | EN | dinner parties, far-field | 4 per session, JSON with speaker and time [7] | >50 h | CC BY-SA 4.0 [7] | dev 11 GB, eval 12 GB [7] | Overlap stress test only |
| VoxConverse | EN (mostly) | YouTube debates and news | RTTM only, no transcripts [8] | ? | CC BY 4.0 annotations [8] | GitHub + WAV zips [8] | Diarization and VAD only |
| Santa Barbara (SBCSAE) | EN | face-to-face conversation | time marks per intonation unit, single mixed channel [9] | ~20 h | CC BY-ND 3.0 US [9] | 6.1 GB OpenSLR [9] | Real spontaneous EN; no modified redistribution |
| NOTSOFAR-1 | EN | office meetings | 4-8 per meeting, 35 speakers [10] | eval ~16 h | CC BY 4.0 [10] | HF token [10] | Far-field meetings |
| ESLO / CFPP2000 | FR | interviews | turn-aligned, coarse overlap [3][15] | CFPP ~42 h | ESLO CC BY-NC-SA; CFPP free with citation [3][15] | ORTOLANG | Natural FR interviews for STT |
| ServiceNow asr_codeswitched | EN+FR mixed | synthetic TTS, IT/HR | single speaker per clip, per-word language tags [11][12] | small | none stated [11] | HF, open | Intra-sentence mixing only |
| SwitchLingua_audio | 12 incl. EN, FR | bilingual readers of LLM scripts | 174 speakers [13] | 80+ h | CC BY-NC 4.0 + signed agreement [13] | 4.8 GB, gated | Real voices; FR-EN share unknown |
| CAFE | Algerian Arabic/FR/EN | podcasts | >100 speakers, 17 min overlap subset [14] | ~37 h (2.3 h clean manual) [14] | CC BY 4.0 [14] | 691 MB Zenodo [14] | Mixing is Arabic-centred |
| ETAPE / REPERE / ALLIES | FR | broadcast, debates | diarization, overlap in ETAPE [16][17][18] | 30 / 60 / 900 h | ELRA, 300-2,500 EUR [16][17][18] | ordered via ELRA | Paid; broadcast style |
| Fisher / ICSI / DIHARD III | EN | phone, meetings, mixed | speaker-timed [19][20] | ~1,960 / 72 / 33 h | LDC non-member fee | ordered via LDC | Paid |

## Findings
### French
- SUMM-RE: ~95 h of spontaneous French meetings, each participant on their own microphone and track. The train split is Whisper-transcribed; dev and test (~12 h each) are manual, with word-level timings [1][2].
- ESLO-MD is CC BY-NC-SA 4.0 [3]. CFPP2000 has ~32 interviews of 1-2 h, free to use with a citation [15].
- ETAPE (ELRA-E0046) annotates and scores overlapping speech, at 300 EUR (members) to 2,000 EUR (non-members) [16].
### English
- AMI: 100 h under CC BY 4.0, four individual headset WAVs per meeting [4][5]. The HF copy has rows with speaker_id, begin_time, end_time and text [6].
- CHiME-6 SLR150: CC BY-SA 4.0; dev 11 GB, eval 12 GB, train 97 GB [7].
- VoxConverse ships RTTM under CC BY 4.0, with no transcripts [8]. Santa Barbara is 6.1 GB under CC BY-ND 3.0 US [9].
- No public gaming-chat or Discord-call corpus was found.
### FR-EN code-switching
- ServiceNow asr_codeswitched: 298 en_fr and 188 en_fr_ca test rows, with a per-word language column. The blog says the audio is ElevenLabs Multilingual v2 TTS [11][12].
- SwitchLingua_audio: 80+ h from 174 speakers, CC BY-NC 4.0, gated by an emailed agreement [13].
- CAFE: ~37 h of Algerian Arabic/French/English, CC BY 4.0; the 2h18m clean subset is manually aligned and the 17 min overlap subset has 23 files [14].

## Caveats
- SUMM-RE's "headset" wording and the 35 Zoom meetings out of 283 come from the paper PDF, which the fact-check could not read (UNREACHABLE). The card says only "individual microphone".
- The ServiceNow set states no licence, and it is small and synthetic. Its TTS origin rests on the blog post, not the dataset card.
- SwitchLingua lists 11 languages besides English, and whether it has FR-EN pairs or conversation structure is unknown (gated).
- Hours, sizes and exact licences for ESLO and CFPP2000 were not checked on ORTOLANG (the certificate failed). TCOF, CEFC/Orféo, Rhapsodie, PFC and CallHome French were not checked at all.
- ICSI conflict: LDC sells it, while an Edinburgh page suggests CC BY 4.0. It is treated as LDC here.
- Mixing per-speaker tracks gives clean ground truth but no real codec or network artefacts. None of the free sets is real VoIP audio.

## Sources
[1] SUMM-RE dataset card — https://huggingface.co/datasets/linagora/SUMM-RE — tier 1 — 2026-11-18
[2] SUMM-RE paper, JEP-TALN 2024 — https://aclanthology.org/2024.jeptalnrecital-taln.35.pdf — tier 1 — 2024
[3] ESLO-MD card — https://huggingface.co/datasets/datasets-CNRS/ESLO-MD — tier 2 — 2026-11-18
[4] AMI corpus overview — https://groups.inf.ed.ac.uk/ami/corpus/ — tier 1 — 2026-11-18
[5] AMI download page — https://groups.inf.ed.ac.uk/ami/download/ — tier 1 — 2026-11-18
[6] AMI on Hugging Face — https://huggingface.co/datasets/edinburghcstr/ami — tier 1 — 2026-11-18
[7] CHiME-6, OpenSLR 150 — https://openslr.org/150 — tier 1 — 2026-11-18
[8] VoxConverse — https://github.com/joonson/voxconverse — tier 1 — 2026-11-18
[9] Santa Barbara corpus, OpenSLR 155 — https://openslr.org/155/ — tier 1 — 2026-11-18
[10] NOTSOFAR-1 — https://github.com/microsoft/NOTSOFAR1-Challenge — tier 1 — 2026-11-18
[11] ServiceNow asr_codeswitched — https://huggingface.co/datasets/ServiceNow-AI/asr_codeswitched — tier 1 — 2026-11-18
[12] ServiceNow code-switching blog — https://huggingface.co/blog/ServiceNow-AI/code-switching — tier 2 — 2026-11-18
[13] SwitchLingua_audio — https://huggingface.co/datasets/Shelton1013/SwitchLingua_audio — tier 1 — 2026-11-18
[14] CAFE, Zenodo — https://zenodo.org/records/16964503 — tier 1 — 2026-11-18
[15] CFPP2000 card — https://huggingface.co/datasets/datasets-CNRS/CFPP — tier 2 — 2026-11-18
[16] ETAPE, ELRA-E0046 — https://catalogue.elra.info/en-us/repository/browse/ELRA-E0046/ — tier 1 — 2026-11-18
[17] REPERE, ELRA-E0044 — https://catalogue.elra.info/en-us/repository/browse/ELRA-E0044/ — tier 1 — 2026-11-18
[18] ALLIES, ELRA-S0486 — https://catalog.elra.info/en-us/repository/browse/ELRA-S0486/ — tier 1 — 2026-11-18
[19] Fisher English Part 1 transcripts — https://catalog.ldc.upenn.edu/LDC2004T19 — tier 1 — 2026-11-18
[20] DIHARD III eval — https://catalog.ldc.upenn.edu/LDC2022S14 — tier 1 — 2026-11-18
