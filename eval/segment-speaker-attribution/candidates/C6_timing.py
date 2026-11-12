"""C6: per-embedding ms of the three embedders, interleaved round-robin so that background load
(other processes on this PC) hits all models alike. fbank + forward, CPU provider, the app's
session options. Real fixture speech (a solo segment), lengths 0.5-10 s.

Writes out/C6_eer/timing.txt.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from C6_eer import MODELS, embed, session  # noqa: E402  (candidates dir is the script dir)

from common import FIXTURE, OUT, SR  # noqa: E402
from harness import decode  # noqa: E402

LENGTHS = (0.5, 1, 1.5, 2, 3, 4, 6, 8, 10)
REPS = 40


def main() -> None:
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    blob = (FIXTURE / "segments.bin").read_bytes()
    e = next(s for s in index["segments"] if s["kind"] == "solo" and s["length_ms"] > 12_000)
    x = decode(blob, e)
    sess = {k: session(p) for k, p in MODELS.items()}
    ts: dict[str, dict[float, list[float]]] = {k: {s: [] for s in LENGTHS} for k in MODELS}
    for s in LENGTHS:
        clip = x[: int(s * SR)]
        for k in sess:
            for _ in range(3):
                embed(sess[k], clip)
    for _ in range(REPS):
        for s in LENGTHS:
            clip = x[: int(s * SR)]
            for k in sess:
                t = time.perf_counter()
                embed(sess[k], clip)
                ts[k][s].append((time.perf_counter() - t) * 1000)
    lines = ["median ms per embedding (fbank+forward), interleaved, " + f"{REPS} reps",
             "model        " + " ".join(f"{s:>6}s" for s in LENGTHS)]  # fmt: skip
    med = {k: [float(np.median(ts[k][s])) for s in LENGTHS] for k in MODELS}
    for k in MODELS:
        lines.append(f"{k:12} " + " ".join(f"{v:7.1f}" for v in med[k]))
    lines.append("ratio to resnet34_LM")
    for k in MODELS:
        lines.append(
            f"{k:12} " + " ".join(f"{a / b:7.2f}" for a, b in zip(med[k], med["resnet34_LM"]))
        )
    txt = "\n".join(lines)
    (OUT / "C6_eer" / "timing.txt").write_text(txt + "\n", encoding="utf-8")
    print(txt)


if __name__ == "__main__":
    main()
