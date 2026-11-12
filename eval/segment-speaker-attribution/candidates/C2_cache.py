"""Offline pass for C2: segment + embed every fixture segment and enrolment clip once with the
C2 turn construction, plus duration crops of single-speaker turns for calibration trials.

    ..\\.venv\\Scripts\\python.exe candidates\\C2_cache.py     -> out\\C2_calib\\cache.pkl

Only the calibration (C2_fit.py) and the dev simulator read this; the candidate recomputes
everything at runtime.
"""

from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import json  # noqa: E402

from C2_lib import EDGES, PER_FRAME, SR, decode, runs, segment_turns, window_starts  # noqa: E402
from common import FIXTURE, OUT, SharedModels, app_opus  # noqa: E402
from akashic.storage.speakers import MIN_UPDATE_MS  # noqa: E402

DEST = OUT / "C2_calib"


def dec(blob: bytes, e: dict) -> np.ndarray:
    x = app_opus.decode_opus(blob[e["offset"] : e["offset"] + e["size"]])
    n = e["length_ms"] * SR // 1000
    out = np.zeros(n, np.float32)
    out[: min(n, len(x))] = x[:n]
    return out


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()
    models = SharedModels()
    rng = np.random.default_rng(7)
    t0 = time.time()

    enrol = {}
    for spk, clips in index["enrol"].items():
        embs = []
        for c in clips:
            x = dec(blob, c)
            for t in models.turns(x):
                if t.speech_ms >= MIN_UPDATE_MS:
                    embs.append((t.speech_ms, models.embed(t.samples)))
        enrol[spk] = embs
    print(f"enrol done {time.time() - t0:.0f}s", flush=True)

    segs = {}
    crops = []  # (seg_id, speaker, speech_ms, emb)
    for i, e in enumerate(index["segments"]):
        x = dec(blob, e)
        lead = e["lead_ms"] * SR // 1000
        body = x[lead:]
        turns, _ovl, logits = segment_turns(models, body, keep_logits=True)
        rows = []
        for t in turns:
            emb = None if t.samples is None else models.embed(t.samples)
            rows.append((t.win, t.ch, t.start_ms + e["lead_ms"], t.end_ms + e["lead_ms"],
                         t.speech_ms, emb))  # fmt: skip
        segs[e["id"]] = {"turns": rows, "n_win": len(logits)}
        if e["kind"] in ("mixed", "drift") or e["single"] is None:
            continue
        # crops of the dominant channel of the first window: one per short bucket
        starts = window_starts(len(body))
        st = starts[0]
        win = np.zeros(160_000, np.float32)
        chunk = body[st : st + 160_000]
        win[: len(chunk)] = chunk
        act = decode(logits[0])
        real = min(len(act), int(len(chunk) / PER_FRAME))
        alone = act.sum(axis=1) == 1
        counts = [(act[:real, c] & alone[:real]).sum() for c in range(3)]
        ch = int(np.argmax(counts))
        where = np.flatnonzero(act[:real, ch] & alone[:real])
        for b in range(4):
            d = rng.uniform(EDGES[b], EDGES[b + 1])
            nf = int(round(d * SR / PER_FRAME))
            if nf > len(where) or nf < 2:
                continue
            k = int(rng.integers(0, len(where) - nf + 1))
            sel = where[k : k + nf]
            samp = np.concatenate([win[int(a * PER_FRAME) : int((b2 + 1) * PER_FRAME)]
                                   for a, b2 in runs(sel)])  # fmt: skip
            crops.append((e["id"], e["single"], int(nf * PER_FRAME * 1000 / SR),
                          models.embed(samp)))  # fmt: skip
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(index['segments'])} {time.time() - t0:.0f}s", flush=True)
    with (DEST / "cache.pkl").open("wb") as f:
        pickle.dump({"enrol": enrol, "segs": segs, "crops": crops}, f)
    print(f"done {time.time() - t0:.0f}s, {len(crops)} crops")


if __name__ == "__main__":
    main()
