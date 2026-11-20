"""Focused check of the mixed-day cause and of the language-split fix (numbers only). Day run, P3's
capture front end: the residual rows holding a truth 'mixed' call utterance (an English clip, 167 ms,
a French clip: eval/quality/assets.py) and a control sample of rows holding only EN or only FR call
speech. Each row decoded (a) as P1/P3 do (one FR/EN choice for the row) and (b) with
candidate.lang_split. Today's stored rows over the same utterances are scored the same way. WER per
row set = Levenshtein of the utterances' words (those mostly inside the row) against the row text.

    uv run python eval/overlapping-speech/P3-stems-plus-separation/langsplit_check.py [n_control]
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
import errors  # noqa: E402
import score  # noqa: E402

cand = core.load_candidate(HERE / "candidate.py")


def main() -> None:
    n_ctrl = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    run = core.Run(next(r for r in core.RUNS if core.SHORT[r] == "day"))
    models = core.load_models()
    ctx = core.Ctx(run, *models)
    utts = sorted((u for u in run.truth.utterances() if u["stream"] == "system" and u["source"] == "call"), key=lambda u: u["t0"])

    def inside(a, b):
        return [u for u in utts if min(b, u["t1"]) - max(a, u["t0"]) >= 0.5 * (u["t1"] - u["t0"])]

    front = cand.Front(ctx)
    picked = []
    for w0, w1 in cand.capture_windows(run.segments, run):
        streams: dict = {}
        for c0 in range(w0, w1, cand.CHUNK_MS):
            c1 = min(w1, c0 + cand.CHUNK_MS)
            outs = front.chunk(c0, c1, streams)
            if c1 == w1:
                for key in list(streams):
                    outs += [(key, u) for u in streams.pop(key).finish()]
            for key, u in outs:
                if key != cand.RESIDUAL or front.classify(key, u)[0] is None:
                    continue
                us = inside(u.start_ms, u.end_ms)
                if us:
                    langs = {v["lang"] for v in us}
                    picked.append(("mixed" if "mixed" in langs else ("+".join(sorted(langs))), u, us))
    rng = random.Random(0)
    mixed = [p for p in picked if p[0] == "mixed"]
    ctrl = [p for p in picked if p[0] in ("en", "fr")]
    ctrl = rng.sample(ctrl, min(n_ctrl, len(ctrl)))
    acc = {}
    probs = {"mixed": [], "en": [], "fr": []}
    for label, u, us in mixed + ctrl:
        ref = score.fold(" ".join(v["text"] for v in us)).split()
        res_a = ctx.decode(u.samples, None, None)
        hyp_a = score.fold(res_a.text if res_a else "").split()
        parts, info = cand.lang_split(ctx, u.samples)
        hyp_b = []
        for a, b in parts:
            r = ctx.decode(u.samples[a:b], None, None)
            hyp_b += score.fold(r.text if r else "").split()
        # today: stored system rows overlapping the utterances, in start order
        t0, t1 = min(v["t0"] for v in us), max(v["t1"] for v in us)
        tod = sorted((s for s in run.segments if s.stream == "system" and s.start_ms < t1 and s.end_ms > t0), key=lambda s: s.start_ms)
        hyp_t = score.fold(" ".join(run.stored[s.id]["transcript"] for s in tod)).split()
        k = "mixed" if label == "mixed" else "control_" + label
        a = acc.setdefault(k, Counter())
        for name, hyp in (("one_lang", hyp_a), ("lang_split", hyp_b), ("today", hyp_t)):
            ops, ins, dist = errors.align(ref, hyp)
            a[f"{name}_err"] += dist
            a[f"{name}_D"] += ops.count("D")
            a[f"{name}_S"] += ops.count("S")
            a[f"{name}_I"] += ins
        a["ref"] += len(ref)
        a["rows"] += 1
        a["split_rows"] += len(parts) > 1
        a[f"row_lang_{res_a.language if res_a else 'none'}"] += 1
        probs["mixed" if label == "mixed" else label].append(info.get("p_top"))
    out = {k: dict(v) | {f"{n}_wer": v[f"{n}_err"] / v["ref"] for n in ("one_lang", "lang_split", "today")} for k, v in acc.items()}
    out["p_top_quartiles"] = {k: sorted(x for x in v if x is not None)[len(v) // 4::max(1, len(v) // 4)] for k, v in probs.items() if v}
    out["gpu_s"] = ctx.gpu_s
    (HERE / "langsplit-check.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
