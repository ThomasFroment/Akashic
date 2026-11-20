"""Row geometry behind the mixed-day regression (numbers only, no Whisper): replays P3's capture front
end (P1's, unchanged) over a run and compares its residual/stem utterance spans with today's stored
system segments, around the truth call utterances. For each call utterance: rows touching it, and
whether the row that holds most of it also holds call speech in another language (a row Whisper must
decode in one language). Writes geometry-<run>.json.

    uv run python eval/overlapping-speech/P3-stems-plus-separation/geometry.py [std|day] [--cap MS]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
import score  # noqa: E402

cand = core.load_candidate(HERE / "candidate.py")


class NoModels:
    pass


def front_rows(run) -> list[tuple[str, int, int, int]]:
    ctx = core.Ctx(run, None, None)
    front = cand.Front(ctx)
    rows = []
    for w0, w1 in cand.capture_windows(run.segments, run):
        streams: dict = {}
        for c0 in range(w0, w1, cand.CHUNK_MS):
            c1 = min(w1, c0 + cand.CHUNK_MS)
            outs = front.chunk(c0, c1, streams)
            if c1 == w1:
                for key in list(streams):
                    outs += [(key, u) for u in streams.pop(key).finish()]
            for key, u in outs:
                src, kind = front.classify(key, u)
                if src is not None:
                    rows.append((kind, u.start_ms, u.end_ms, u.speech_ms))
    return rows


def lang_of(u) -> str:
    return u["lang"]


def analyse(run, rows) -> dict:
    utts = [u for u in run.truth.utterances() if u["stream"] == "system" and u["source"] == "call"]
    utts.sort(key=lambda u: u["t0"])
    out = {}
    for name, rr in rows.items():
        per = Counter()
        mixed_rows = Counter()
        lens = [b - a for _, a, b, _ in rr]
        call_rows = []
        for u in utts:
            touching = [(a, b) for _, a, b, _ in rr if a < u["t1"] and b > u["t0"]]
            per[min(len(touching), 3)] += 1
            if not touching:
                continue
            a, b = max(touching, key=lambda r: min(r[1], u["t1"]) - max(r[0], u["t0"]))
            others = [v for v in utts if v is not u and v["t0"] < b and v["t1"] > a and min(b, v["t1"]) - max(a, v["t0"]) > 500]
            langs = {u["lang"]} | {v["lang"] for v in others}
            key = (u["lang"], "multi_utt" if others else "one_utt", "+".join(sorted(langs)))
            mixed_rows[key] += 1
            call_rows.append((u["lang"], b - a, len(others)))
        ml = [x for x in call_rows if x[0] == "mixed"]
        out[name] = {"rows": len(rr), "row_len_s_median": sorted(lens)[len(lens) // 2] / 1000 if lens else None,
                     "row_len_s_p90": sorted(lens)[int(len(lens) * 0.9)] / 1000 if lens else None,
                     "rows_touching_call_utt": {str(k): v for k, v in sorted(per.items())},
                     "main_row_of_call_utt": {"|".join(k): v for k, v in sorted(mixed_rows.items())},
                     "mixed_utts": len(ml), "mixed_main_row_len_s_mean": sum(x[1] for x in ml) / len(ml) / 1000 if ml else None,
                     "mixed_main_row_holds_other_utts": sum(1 for x in ml if x[2] > 0),
                     "mixed_main_row_over_20s": sum(1 for x in ml if x[1] > 20_000)}
    # how today's segments were cut inside the long mixed utterances: the edges' cause
    mixed = [u for u in utts if u["lang"] == "mixed"]
    tod = [(a, b) for _, a, b, _ in rows["today"]]
    res = [(a, b) for k, a, b, _ in rows["p3_front"] if k in ("residual", "fallback")]
    def edges_inside(spans, u):
        return [e for a, b in spans for e in (a, b) if u["t0"] + 300 < e < u["t1"] - 300]
    def gap_at(u, e):  # is the edge inside a truth voiced gap of the utterance (a pause) ?
        return any(x1 <= e <= y0 for (_, x1), (y0, _) in zip(u["spans"], u["spans"][1:], strict=False))
    cnt = Counter()
    for u in mixed:
        for name, sp in (("today", tod), ("residual", res)):
            es = edges_inside(sp, u)
            cnt[f"{name}_edges"] += len(es)
            cnt[f"{name}_edges_in_pause"] += sum(gap_at(u, e) for e in es)
            cnt[f"{name}_utts_cut"] += bool(es)
    out["mixed_utt_cuts"] = dict(cnt)
    out["mixed_utt_len_s"] = sorted(round((u["t1"] - u["t0"]) / 1000, 1) for u in mixed)
    out["mixed_utt_pause_max_s"] = sorted(round(max((y0 - x1 for (_, x1), (y0, _) in zip(u["spans"], u["spans"][1:], strict=False)), default=0) / 1000, 2) for u in mixed)
    return out


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "day"
    run = core.Run(next(r for r in core.RUNS if core.SHORT[r] == name))
    rows = {"today": [("today", s.start_ms, s.end_ms, s.speech_ms) for s in run.segments if s.stream == "system"], "p3_front": front_rows(run)}
    rows["residual_only"] = [r for r in rows["p3_front"] if r[0] in ("residual", "fallback")]
    res = analyse(run, rows)
    (HERE / f"geometry-{name}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k not in ("mixed_utt_len_s", "mixed_utt_pause_max_s")}, indent=1))
    print("mixed utt len s", res["mixed_utt_len_s"][:5], "...", res["mixed_utt_len_s"][-5:])
    print("mixed utt longest pause s", res["mixed_utt_pause_max_s"][:5], "...", res["mixed_utt_pause_max_s"][-5:])


if __name__ == "__main__":
    main()
