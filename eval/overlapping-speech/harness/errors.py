"""Word-level error attribution shared by the diagnosis and the run report: the scorer's M9.1 groups
rebuilt with each reference word's estimated time, talker, source and alignment outcome.

Only numbers leave this module (counts and ratios); texts stay in memory.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import numpy as np

import score

OVERLAP_PAD_MS = 100  # another talker's voiced span within this of a word counts as concurrent
BOUNDARY_MS = 500  # a word this close to a segment edge inside the group is a "cut" word


def align(ref: list[str], hyp: list[str]):
    """Levenshtein with backtrace: per ref word 'C', 'S' or 'D', plus the insertion count."""
    n, m = len(ref), len(hyp)
    d = np.zeros((n + 1, m + 1), dtype=np.int32)
    d[:, 0] = np.arange(n + 1)
    d[0, :] = np.arange(m + 1)
    for i in range(1, n + 1):
        r = ref[i - 1]
        row, prev = d[i], d[i - 1]
        if not m:
            continue
        sub = prev[:-1] + np.array([r != h for h in hyp], dtype=np.int32)
        dele = prev[1:] + 1
        best = np.concatenate([[i], np.minimum(sub, dele)]) - np.arange(m + 1)
        row[:] = np.minimum.accumulate(best) + np.arange(m + 1)  # insertions run left to right
    ops, i, j, ins = [], n, m, 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i, j] == d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]):
            ops.append("C" if ref[i - 1] == hyp[j - 1] else "S")
            i, j = i - 1, j - 1
        elif i > 0 and d[i, j] == d[i - 1, j] + 1:
            ops.append("D")
            i -= 1
        else:
            ins += 1
            j -= 1
    return ops[::-1], ins, int(d[n, m])


def semi_global(ref: list[str], hyp: list[str]) -> int:
    """Edit distance of ref against its best-matching contiguous stretch of hyp (free ends in hyp)."""
    if not ref:
        return 0
    prev = np.zeros(len(hyp) + 1, dtype=np.int32)
    for i, r in enumerate(ref, 1):
        sub = prev[:-1] + np.array([r != h for h in hyp], dtype=np.int32)
        m = len(hyp)
        best = np.concatenate([[i], np.minimum(sub, prev[1:] + 1)]) - np.arange(m + 1)
        prev = (np.minimum.accumulate(best) + np.arange(m + 1)).astype(np.int32)
    return int(prev.min())


def word_times(u: dict, n: int) -> list[float]:
    """Estimated time of each of n words: spread evenly over the utterance's voiced time."""
    spans = u["spans"] or [(u["t0"], u["t1"])]
    total = sum(b - a for a, b in spans) or 1
    out = []
    for k in range(n):
        pos = (k + 0.5) / n * total
        for a, b in spans:
            if pos <= b - a:
                out.append(a + pos)
                break
            pos -= b - a
        else:
            out.append(spans[-1][1])
    return out


def groups(utts: list[dict], segs: list[tuple], stream: str):
    """score.m8_m9's grouping, verbatim in effect: items sorted by start, chained while they overlap.
    segs: (id, stream, start_ms, end_ms, speech_ms, text, ...)."""
    items = [("u", u["t0"], u["t1"], u) for u in utts if u["stream"] == stream]
    items += [("s", a, b, (text or ""), sid) for sid, st, a, b, _, text, *_ in segs if st == stream]
    items.sort(key=lambda x: x[1])
    out, cur, cur_end = [], [], -1
    for it in items:
        if cur and it[1] >= cur_end:
            out.append(cur)
            cur = []
        cur.append(it)
        cur_end = max(cur_end, it[2]) if len(cur) > 1 else it[2]
    if cur:
        out.append(cur)
    return out


def inside(t: float, spans) -> bool:
    return any(a <= t < b for a, b in spans)


def analyse(utts: list[dict], segs: list[tuple], levels: dict[int, float] | None = None, beds: dict[str, list] | None = None,
            causes: list | None = None) -> list[dict]:
    """One record per scored group (utterances present): counts only.

    levels: id(utterance dict) -> talker level (rendered RMS) to name the quieter talker of a pair.
    beds: name -> merged audible spans (game, music, outerwilds) from the truth OS log.
    causes: [(label, merged spans)] tried in order to explain a word no segment covers."""
    levels = levels or {}
    beds = beds or {}
    causes = causes or []
    mic_spans = score.merge([s for u in utts if u["stream"] == "mic" for s in u["spans"]])
    out = []
    for stream in ("mic", "system"):
        seg_spans = score.merge([(a, b) for _, st, a, b, *_ in segs if st == stream])
        for g in groups(utts, segs, stream):
            us = [x[3] for x in g if x[0] == "u"]
            if not us:
                continue
            ss = sorted((x for x in g if x[0] == "s"), key=lambda x: x[1])
            hyp = score.fold(" ".join(x[3] for x in ss)).split()
            words, owner = [], []
            for k, u in enumerate(us):
                w = score.fold(u["text"]).split()
                words += w
                owner += [k] * len(w)
            langs = {u["lang"] for u in us if u.get("kind") != "tts"}
            lang = "tts" if not langs else (langs.pop() if len(langs) == 1 else "multi")
            overlapped = any(us[i]["t1"] > us[j]["t0"] for i in range(len(us)) for j in range(i + 1, len(us)))
            ops, ins, dist = align(words, hyp)
            # per utterance word times; concurrency per word
            times = []
            for k, u in enumerate(us):
                n = owner.count(k)
                times += word_times(u, n)
            edges = [e for x in ss for e in (x[1], x[2])]
            g0, g1 = min(x[1] for x in g), max(x[2] for x in g)
            inner_edges = [e for e in edges if g0 < e < g1]
            region = []
            pair_kind = []
            uncovered_why = []
            for idx, t in enumerate(times):
                k = owner[idx]
                others = [j for j, o in enumerate(us) if j != k and any(a - OVERLAP_PAD_MS <= t < b + OVERLAP_PAD_MS for a, b in o["spans"])]
                if not inside(t, seg_spans):
                    why = "other_endpoint" if us[k]["source"] == "elsewhere" else next((lab for lab, sp in causes if inside(t, sp)), "dropped_or_vad")
                    region.append("uncovered")
                    pair_kind.append(None)
                    uncovered_why.append(why)
                    continue
                if others:
                    j = others[0]
                    mine, theirs = levels.get(id(us[k]), 0.0), levels.get(id(us[j]), 0.0)
                    region.append("overlap_quieter" if mine < theirs else "overlap_louder")
                    pair_kind.append(kind_of(us[k], us[j]))
                    continue
                pair_kind.append(None)
                region.append("boundary" if any(abs(t - e) <= BOUNDARY_MS for e in inner_edges) else "clean")
            pairs = Counter()
            for i in range(len(us)):
                for j in range(i + 1, len(us)):
                    if us[i]["t1"] > us[j]["t0"]:
                        pairs[kind_of(us[i], us[j])] += 1
            sources = Counter(u["source"] for u in us)
            span = (min(u["t0"] for u in us), max(u["t1"] for u in us))
            voiced = score.merge([s for u in us for s in u["spans"]])
            vtot = sum(b - a for a, b in voiced) or 1
            bed = sorted(k for k, sp in beds.items() if score.overlap_ms(voiced, sp) > 0.2 * vtot)
            mic_over = stream == "system" and score.overlap_ms(voiced, mic_spans) > 0
            uw = iter(uncovered_why)
            unc = Counter(f"{o}:{next(uw)}" for o, r in zip(ops, region, strict=True) if r == "uncovered")
            micc = [stream == "system" and any(a - OVERLAP_PAD_MS <= t < b + OVERLAP_PAD_MS for a, b in mic_spans) for t in times]
            local = sum(semi_global(score.fold(u["text"]).split(), hyp) for u in us)
            out.append({
                "stream": stream, "lang": lang, "overlap": overlapped, "ref": len(words), "hyp": len(hyp), "dist": dist,
                "S": ops.count("S"), "D": ops.count("D"), "I": ins, "local_errors": local,
                "ops_by_region": dict(Counter(f"{o}:{r}" for o, r in zip(ops, region, strict=True) if o != "C")),
                "words_by_region": dict(Counter(region)),
                "err_by_pair": dict(Counter(pk for o, pk in zip(ops, pair_kind, strict=True) if o != "C" and pk)),
                "pairs": dict(pairs), "sources": dict(sources), "bed": bed, "mic_over": mic_over,
                "uncovered_ops": {k: v for k, v in unc.items() if not k.startswith("C:")},
                "mic_concurrent": {"words": sum(micc), "errors": sum(1 for o, m in zip(ops, micc, strict=True) if m and o != "C"),
                                   "other_words": len(micc) - sum(micc), "other_errors": sum(1 for o, m in zip(ops, micc, strict=True) if not m and o != "C")},
                "n_utts": len(us), "n_segs": len(ss), "span_ms": span[1] - span[0],
            })
    return out


def kind_of(a: dict, b: dict) -> str:
    sa, sb = sorted((src_name(a), src_name(b)))
    return f"{sa}+{sb}"


def src_name(u: dict) -> str:
    if u.get("proc"):
        return u["proc"]
    s = u["source"]
    return {"call": "call", "video": "video", "elsewhere": "elsewhere", "own": "mic"}.get(s, s)


def totals(recs: list[dict], pred) -> dict:
    sel = [r for r in recs if pred(r)]
    ref = sum(r["ref"] for r in sel)
    dist = sum(r["dist"] for r in sel)
    return {"groups": len(sel), "ref_words": ref, "errors": dist, "wer": dist / ref if ref else None,
            "S": sum(r["S"] for r in sel), "D": sum(r["D"] for r in sel), "I": sum(r["I"] for r in sel),
            "local_errors": sum(r["local_errors"] for r in sel)}


def merge_counts(recs: list[dict], key: str) -> dict:
    c = defaultdict(int)
    for r in recs:
        for k, v in r[key].items():
            c[k] += v
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))
