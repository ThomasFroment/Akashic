# Data-collection spikes (2026-11-17)

Throwaway harnesses behind [the 2026-11-17 data-collection audit](../../docs/audits/2026-11-17-data-collection.md)
(spikes S1-S9). Frozen prototypes, outside the lint bar like the rest of `eval/`. They import
nothing from `src/` and write no captured data: live-audio scripts keep samples in memory and
print numbers only.

## Environment

A separate venv (not the app's): Python 3.14, then
`pip install comtypes numpy pillow onnxruntime winrt-runtime winrt-Windows.Media.Ocr
winrt-Windows.Globalization winrt-Windows.Graphics.Imaging winrt-Windows.Security.Cryptography
winrt-Windows.Foundation winrt-Windows.Foundation.Collections winrt-Windows.Storage.Streams
rapidocr av ctranslate2 tokenizers huggingface_hub` and `pip install --no-deps faster-whisper`
(for the Silero v6 file and the Whisper runtime). Every script lowers its own priority to below
normal and runs on the CPU.

## Scripts

| Folder | Script | Spike | Needs |
| :- | :- | :- | :- |
| `ploop/` | `spike.py format\|mute\|sep\|endpoints\|late\|exit` | S1 process-loopback mechanics | `tone.py` children render inaudible tones (private non-persistent session, muted or at volume 0.002; they refuse to play otherwise) |
| `ploop/` | `direct.py`, `cpu.py` | S1 post-volume check, CPU of 1-6 clients | as above |
| `ploop/` | `passive.py` | S2 real audible apps (names and levels only) | something playing |
| `ploop/` | `streams.py SECONDS` | S3 per-app vs mixed speech and interference | apps playing (e.g. a game and a call) |
| `ploop/` | `callstate.py` | S8 capture sessions and ConsentStore (read-only) | |
| `ocr/` | `ocr_spike.py OUT.json`, `timing.py`, `dense.py`, `lossless.py`, `novelty.py`, `novelty2.py` | S4, S6, S7 Windows OCR (`novelty2.py` is the per-line scored run the audit quotes) | fr-FR and en-US OCR languages; the others import `ocr_spike.py` |
| `ocr/` | `rapid.py`, `rapid_full.py` | S4 PP-OCR via RapidOCR (downloads its ONNX models from modelscope.cn on first use) | |
| `tag/` | `tag_spike.py DATA_DIR` | S5 Silero vs CED-mini | `DATA_DIR/model.onnx` (mispeech/ced-mini @ 26c3ebc) and `DATA_DIR/musdb7.zip` (zenodo record 3270814, MUSDB18-7-STEMS.zip); streams FLEURS dev and OpenGameArt CC0 effects |
| `cs/` | `cs_spike.py DATA_DIR N` | S9 French-English switching in one segment | downloads Systran/faster-whisper-large-v3 @ edaa852 (3 GB) and streams FLEURS dev |

Data licences: FLEURS CC-BY-4.0; MUSDB18 research use; OpenGameArt CC0. Nothing downloaded is
committed.
