"""Run every fixture clip through the production capture + Whisper path and cache everything.

Run:  .venv\\Scripts\\python.exe -I run_production.py [--limit N] [--check N]
Reads data/fixture/clips.jsonl; writes data/cache/{segments.jsonl, clips.jsonl, audio/, vad/}.
Resumable: segments already in segments.jsonl are skipped. ``--check N`` re-runs N segments
through WhisperTranscriber.transcribe itself and compares texts (fidelity check).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import numpy as np  # noqa: E402

import prod  # noqa: E402
from build_fixture import load_clip  # noqa: E402

FIX = HERE / "data" / "fixture"
CACHE = HERE / "data" / "cache"
TAIL_MIN_MS = 2_000  # a speech segment running this far into the noise tail is "speech_tail"
OVERLAP_MIN_MS = 100


def role_of(clip: dict, start: int, end: int) -> tuple[str, float]:
    region = clip["speech_ms"]
    if clip["kind"] == "nonspeech" or region is None:
        return "nonspeech", 0.0
    overlap = max(0, min(end, region[1]) - max(start, region[0]))
    if overlap < OVERLAP_MIN_MS:
        return "nonspeech", 0.0
    coverage = overlap / max(region[1] - region[0], 1)
    if clip["kind"] == "concat" and end - region[1] >= TAIL_MIN_MS:
        return "speech_tail", coverage
    return "speech", coverage


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--check", type=int, default=0)
    args = ap.parse_args()
    clips = [json.loads(l) for l in (FIX / "clips.jsonl").open(encoding="utf-8")]
    if args.limit:
        clips = clips[:: max(1, len(clips) // args.limit)][: args.limit]
    for sub in ("audio", "vad"):
        (CACHE / sub).mkdir(parents=True, exist_ok=True)
    seg_path, clip_path = CACHE / "segments.jsonl", CACHE / "clips.jsonl"
    done = set()
    if clip_path.exists():
        done = {json.loads(l)["clip_id"] for l in clip_path.open(encoding="utf-8")}
    capture = prod.Capture()
    whisper = prod.Whisper()
    t_start = time.perf_counter()
    n_seg = 0
    with seg_path.open("a", encoding="utf-8") as fseg, clip_path.open("a", encoding="utf-8") as fclip:
        for k, clip in enumerate(clips):
            if clip["id"] in done:
                continue
            samples = load_clip(FIX / "clips" / f"{clip['id']}.npy")
            probs, utterances, drops = capture.run(samples)
            np.save(CACHE / "vad" / f"{clip['id']}.npy", probs)
            seg_ids = []
            for i, utt in enumerate(utterances):
                seg_id = f"{clip['id']}#{i}"
                stored = prod.stored_samples(utt.samples)
                np.save(CACHE / "audio" / f"{seg_id}.npy", stored)
                result = whisper.production(prod.normalize_loudness(stored), clip["prompt"])
                raw = result.pop("_raw")
                t0 = time.perf_counter()
                words = whisper.align_words(prod.normalize_loudness(stored), result["parts"], result["language"])
                align_ms = (time.perf_counter() - t0) * 1000
                role, coverage = role_of(clip, utt.start_ms, utt.end_ms)
                rec = {
                    "seg_id": seg_id, "clip_id": clip["id"], "index": i,
                    "start_ms": utt.start_ms, "end_ms": utt.end_ms, "vad_speech_ms": utt.speech_ms,
                    "continues": utt.continues, "role": role, "speech_coverage": round(coverage, 3),
                    "kind": clip["kind"], "subset": clip["subset"], "lang_true": clip["lang"],
                    "opus": clip["opus"], "orig_id": clip["orig_id"], "split": clip["split"],
                    "group": clip["group"], "ref_text": clip["ref_text"], "names": clip["names"],
                    "names_in_vocab": clip["names_in_vocab"], "vocab": clip["vocab"],
                    "previous": clip["previous"], "prompt": clip["prompt"],
                    **result, "words": words, "align_ms": align_ms,
                    "today": prod.today_status(result["text"], result["confidence"]),
                    "n_raw_parts": len(raw),
                }
                fseg.write(json.dumps(rec, ensure_ascii=False) + "\n")
                seg_ids.append(seg_id)
                n_seg += 1
            fclip.write(json.dumps({"clip_id": clip["id"], "segments": seg_ids, "drops": drops,
                                    "split": clip["split"], "kind": clip["kind"],
                                    "subset": clip["subset"], "opus": clip["opus"]}) + "\n")
            fseg.flush()
            fclip.flush()
            if k % 50 == 0:
                print(f"{k}/{len(clips)} clips, {n_seg} segments, {time.perf_counter() - t_start:.0f} s",
                      flush=True)
    print(f"done: {n_seg} new segments in {time.perf_counter() - t_start:.0f} s", flush=True)
    if args.check:
        check(whisper, args.check)


def check(whisper: prod.Whisper, n: int) -> None:
    """Compare cached texts with WhisperTranscriber.transcribe on the same input."""
    segs = [json.loads(l) for l in (CACHE / "segments.jsonl").open(encoding="utf-8")]
    rng = np.random.default_rng(0)
    same = 0
    picks = rng.choice(len(segs), size=min(n, len(segs)), replace=False)
    for j in picks:
        s = segs[j]
        stored = np.load(CACHE / "audio" / f"{s['seg_id']}.npy")
        t = whisper.transcriber.transcribe(prod.normalize_loudness(stored), s["prompt"])
        text = t.text if t else ""
        conf = t.confidence if t else None
        ok = text == s["text"] and (conf == s["confidence"] or abs((conf or 0) - (s["confidence"] or 0)) < 1e-6)
        same += ok
        if not ok:
            print(f"  MISMATCH {s['seg_id']}")
    print(f"fidelity: {same}/{len(picks)} identical to WhisperTranscriber.transcribe")


if __name__ == "__main__":
    main()
