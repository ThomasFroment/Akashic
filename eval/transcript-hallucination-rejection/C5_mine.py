"""Compute C5 features on the MINING split only (labels kept for fitting); --limit for a spike."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness  # noqa: E402
import C5_feat  # noqa: E402

OUT = HERE / "C5"


def label_of(cc: dict, lab: dict) -> dict:
    return {"role": lab["role"], "subset": lab["subset"], "lang": lab["lang_true"], "opus": lab["opus"],
            "halluc": lab["role"] == "nonspeech" and bool(harness.words(cc["text"])),
            "ref_text": lab["ref_text"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="mining_feats.json")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    data = harness.load_cache()
    helpers = harness.Helpers({cc["id"]: lab["seg_id"] for cc, lab in data})
    rows = []
    mining = [(cc, lab) for cc, lab in data if lab["split"] == "mining"]
    if args.limit:
        import random
        random.Random(1).shuffle(mining)
        mining = mining[: args.limit]
    for i, (cc, lab) in enumerate(mining):
        f = C5_feat.features(cc, helpers) if cc["parts"] and cc["text"] else {"today": cc["today"], "empty": True}
        f.update(id=cc["id"], label=label_of(cc, lab), text=cc["text"])
        rows.append(f)
        if args.limit or i % 100 == 0:
            print(i, lab["role"], lab["subset"], cc["today"], {k: round(f[k], 3) for k in
                  ("S_P", "S_0", "lpA", "lpB", "lpC", "lpD", "cover", "enc_ms", "align_ms", "decode_ms") if k in f},
                  flush=True)
    (OUT / args.out).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
