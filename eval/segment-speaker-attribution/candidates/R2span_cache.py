r"""Offline pass for QA-R2-C1: R2span_core.analyse on every non-drift fixture segment, with the
span bank of every embedded channel and every possible within-window group (singletons and
channel subsets). Only the fit reads this; the candidate recomputes everything at runtime.

    ..\.venv\Scripts\python.exe candidates\R2span_cache.py   -> out\R2span_C1\cache.pkl
"""

from __future__ import annotations

import itertools
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from C2_cache import dec  # noqa: E402
from C2_lib import SR  # noqa: E402
from common import FIXTURE, OUT, SharedModels  # noqa: E402
from R2span_core import analyse, group_emb, group_spans  # noqa: E402
from R2span_lib import FrameEmbedder  # noqa: E402

DEST = OUT / "R2span_C1"


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()
    models = SharedModels()
    fe = FrameEmbedder()
    t0 = time.time()
    segs = {}
    for i, e in enumerate(index["segments"]):
        if e["kind"] == "drift":
            continue
        x = dec(blob, e)
        body = x[e["lead_ms"] * SR // 1000 :]
        f, logits = analyse(models, fe, body, banks=True, keep_logits=True)
        chans = [{"win": c.win, "ch": c.ch, "start": c.start_ms, "end": c.end_ms, "ms": c.speech_ms,
                  "emb": c.emb, "T": c.T, "pieces": c.pieces_abs} for c in f.chans]  # fmt: skip
        eS = {}
        for ci, c in enumerate(f.chans):
            if c.bank is not None:
                b = c.bank
                eS[ci] = (b["e"].astype(np.float16), b["n"], b["kind"], b["t"])
        groups = {}
        by: dict[int, list[int]] = {}
        for ci, c in enumerate(f.chans):
            if c.emb is not None:
                by.setdefault(c.win, []).append(ci)
        for ix in by.values():
            for r in range(1, len(ix) + 1):
                for g in itertools.combinations(ix, r):
                    sp = group_spans(fe.head, f, list(g))
                    groups[g] = {"emb": group_emb(fe.head, f, list(g)),
                                 "cos": None if sp is None else sp[1].astype(np.float32),
                                 "nr": None if sp is None else sp[3]}  # fmt: skip
        segs[e["id"]] = {"chans": chans, "pair_ovl": f.pair_ovl, "eS": eS, "groups": groups,
                         "logits": logits}  # fmt: skip
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(index['segments'])} {time.time() - t0:.0f}s", flush=True)
    with (DEST / "cache.pkl").open("wb") as fh:
        pickle.dump({"segs": segs}, fh)
    print(f"done {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
