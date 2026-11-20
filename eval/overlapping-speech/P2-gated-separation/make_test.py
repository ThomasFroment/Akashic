"""Test mixtures for the ONNX check: two corpus talkers (EN+EN, FR+EN, FR+FR), 0 dB, partial overlap,
one with a music bed. Saves model/test_mix.npz (mixtures and their sources; corpus audio only).

    uv run python eval/overlapping-speech/P2-gated-separation/make_test.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402

SR = 16_000


def main() -> None:
    rng = random.Random(7)
    clips = json.loads((core.assets.ASSETS / "manifest.json").read_text(encoding="utf-8"))["clips"]
    speech = [c for c in clips if c["kind"] == "speech" and "parts" not in c and 4 <= c["dur_s"] <= 13]
    out = {}
    for k, (la, lb, bed) in enumerate((("en", "en", None), ("fr", "en", None), ("fr", "fr", "music"))):
        a = rng.choice([c for c in speech if c["lang"] == la])
        b = rng.choice([c for c in speech if c["lang"] == lb and c["speaker"] != a["speaker"]])
        xa = core.bank(a["bank"])[a["off"]:a["off"] + a["n"]].astype(np.float32)
        xb = core.bank(b["bank"])[b["off"]:b["off"] + b["n"]].astype(np.float32)
        n = len(xa) + len(xb) // 2
        sa, sb = np.zeros(n, np.float32), np.zeros(n, np.float32)
        sa[:len(xa)] = xa
        sb[n - len(xb):] = xb * np.float32(np.sqrt(np.mean(xa ** 2)) / np.sqrt(np.mean(xb ** 2)))
        mix = sa + sb
        if bed:
            m = core.bank(bed)
            mix = mix + np.tile(m, n // len(m) + 1)[:n] * np.float32(0.5)
        out[f"mix{k}"], out[f"a{k}"], out[f"b{k}"] = mix, sa, sb
    (HERE / "model").mkdir(exist_ok=True)
    np.savez(HERE / "model" / "test_mix.npz", **out)
    print({k: round(len(v) / SR, 2) for k, v in out.items() if k.startswith("mix")})


if __name__ == "__main__":
    main()
