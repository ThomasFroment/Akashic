"""Diagnosis of today's stores: where the stored screen-text characters go. Numbers only.

    uv run python eval/screen-text-duplication-noise/harness/diagnose.py [smoke standard day]

Classes, in precedence order, for every stored line (non-empty, stripped, as M3.2 counts them):
  a        the row's whole text equals an earlier stored text of the same window (hwnd)
  b_exact  the line was already stored for this window (exact)
  b_near   the line equals an earlier line of this window after digit masking (clock, counter),
           or is within max(2, 10 %) edits of one (few-character / OCR change)
  c        UI chrome: the line (digit-masked if <= 32 chars) occurs in >= 2 contexts (window,
           url-or-title) of its app and in >= 30 % of that app's contexts
  d        OCR garbage: >= 50 % of its token characters are unknown tokens (see noise), or no token
  e        content: the rest (e_truth: of which the line is a harness-rendered content line)
Noise (token level, FTS5 unicode61 tokens): a token that is not harness-rendered text (run truth +
harness corpus + nonce shape), not a word of lexicon/words.txt (FLEURS en/fr + Whisper BPE word
tokens), and not identifier-shaped (numbers, letter-digit mixes, hex ids, CamelCase of words).
Writes ../diagnosis/<run>.json and ../diagnosis/summary.txt.
"""

from __future__ import annotations

import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import shared as S  # noqa: E402

OUT = S.STUDY / "diagnosis"
CLASSES = ("a", "b_exact", "b_near", "c", "d", "e")


def chrome_key(line: str) -> str:
    """Digits are masked only on short lines (clocks, counters, badges); a long line that differs by
    a number (a terminal line counter) stays its own key."""
    n = " ".join(line.casefold().split())
    return S.mask_digits(n) if len(n) <= 32 else n


def chrome_keys(store: S.Store) -> set[tuple[str, str]]:
    ctx_of: dict[str, set] = defaultdict(set)
    key_ctx: dict[tuple[str, str], set] = defaultdict(set)
    for w in store.windows:
        p = (w.ctx.process or "").lower()
        for o in w.history:
            c = (w.hwnd, o.url or o.title)
            ctx_of[p].add(c)
            for ln in S.text_lines(o.text):
                key_ctx[p, chrome_key(ln)].add(c)
    return {k for k, cs in key_ctx.items() if len(cs) >= 2 and len(cs) >= 0.3 * len(ctx_of[k[0]])}


def classify_store(store: S.Store, truth: S.RunTruth, lx: S.Lexicon, lb: S.Labeler):
    chrome = chrome_keys(store)
    chars: dict[str, Counter] = defaultdict(Counter)  # split -> class -> chars
    toks: dict[str, Counter] = defaultdict(Counter)  # class -> tokens / unknown / garbled
    toks_src: dict[str, Counter] = defaultdict(Counter)
    cross: dict[str, Counter] = defaultdict(Counter)  # class -> truth kind -> chars
    hazard = Counter()
    types = Counter()  # line type x duplicate status, and line type x truth kind (chars)
    class_lines: dict[int, dict[str, list[str]]] = {}  # window_id -> class -> lines (in memory only)
    a_prev = 0
    for w in store.windows:
        seen_text: set[int] = set()
        prev_hash = None
        near = S.NearIndex()
        seen_tids: set[str] = set()
        p = (w.ctx.process or "").lower()
        for o in w.history:
            h = hash(o.text)
            whole_dup = h in seen_text
            seen_text.add(h)
            if whole_dup and h == prev_hash:
                a_prev += sum(len(x) for x in S.text_lines(o.text))
            prev_hash = h
            per = defaultdict(list)
            for ln in S.text_lines(o.text):
                if whole_dup:
                    cls = "a"
                elif near.has_exact(ln):
                    cls = "b_exact"
                elif near.near(ln):
                    cls = "b_near"
                elif (p, chrome_key(ln)) in chrome:
                    cls = "c"
                else:
                    u, t = lx.unknown_chars(ln)
                    cls = "d" if t == 0 or u >= 0.5 * t else "e"
                near.add(ln)
                lab = lb.label(w.wid, ln)
                kind = lab[1] if lab else "none"
                if cls == "e" and kind == "content":
                    chars["all"]["e_truth"] += len(ln)
                if lab and kind == "content":
                    new = lab[0] not in seen_tids
                    seen_tids.add(lab[0])
                    if new and cls in ("b_exact", "b_near"):
                        hazard[f"{cls}_new_content_line"] += 1
                        hazard[f"{cls}_new_content_chars"] += len(ln)
                    if cls in ("c", "d"):
                        hazard[f"{cls}_content_chars"] += len(ln)
                for split in ("all", f"src={o.source}", f"app={w.ctx.app}", f"mon={o.monitor}", f"src={o.source}|app={w.ctx.app}"):
                    chars[split][cls] += len(ln)
                cross[cls][kind] += len(ln)
                # orthogonal view: what the line is, whatever its duplicate status
                if (p, chrome_key(ln)) in chrome:
                    typ = "chrome"
                else:
                    u, t = lx.unknown_chars(ln)
                    typ = "garbage" if t == 0 or u >= 0.5 * t else "content"
                dup = "a" if cls == "a" else ("b" if cls.startswith("b") else "first")
                types[f"{typ}|{dup}"] += len(ln)
                types[f"{typ}|truth={kind}"] += len(ln)
                for tok in S.tokens(ln):
                    c = lx.classify(tok)
                    for bucket in (toks[cls], toks_src[o.source], toks["all"]):
                        bucket["tokens"] += 1
                        bucket["token_chars"] += len(tok)
                        if c == "unknown":
                            bucket["unknown"] += 1
                            bucket["unknown_chars"] += len(tok)
                            if lx.garbled(tok):
                                bucket["garbled"] += 1
                        else:
                            bucket[c] += 1
                per[cls].append(ln)
            class_lines[o.window_id] = per
    return chars, toks, toks_src, cross, hazard, class_lines, a_prev, types


def sizes(store: S.Store, class_lines) -> dict:
    out = {}
    for cls in (*CLASSES, "all"):
        c = S.new_fts(contentless=True)
        text_bytes = 0
        for _, o in store.order:
            per = class_lines[o.window_id]
            lines = [x for v in per.values() for x in v] if cls == "all" else per.get(cls, [])
            if lines:
                doc = "\n".join(lines)
                text_bytes += len(doc.encode("utf-8"))
                c.execute("INSERT INTO t (rowid, text) VALUES (?, ?)", (o.window_id, doc))
        out[cls] = {"text_bytes": text_bytes, "fts_bytes": S.fts_bytes(c)}
        c.close()
    # the real store, for reference: today's FTS rows also index title and url
    out["store"] = {"text_bytes": store.text_store_bytes, "fts_bytes": store.fts_store_bytes}
    return out


def crowding(store: S.Store, truth: S.RunTruth) -> dict:
    from akashic.search import keyword

    hwnd_of = {o.window_id: h for h, o in store.order}
    wid_of = {w.hwnd: w.wid for w in store.windows}

    def sighting(hwnd: int, ts: int):
        for i, (a, b) in enumerate(truth.shown_spans.get(wid_of.get(hwnd), [])):
            if a - 2000 <= ts <= b + 2000:
                return i
        return None

    def run(queries: list[str]) -> dict:
        n = found = slots = same_win = same_sight = 0
        for q in queries:
            hits = keyword.search(store.conn, q, limit=5)
            n += 1
            if not hits:
                continue
            found += 1
            slots += len(hits)
            prev = []
            for h in hits:
                hw = hwnd_of.get(h.window_id)
                sg = sighting(hw, h.ts_ms)
                if any(p[0] == hw for p in prev):
                    same_win += 1
                    if sg is not None and any(p == (hw, sg) for p in prev):
                        same_sight += 1
                prev.append((hw, sg))
        return {"queries": n, "found": found, "mean_hits_top5": slots / max(found, 1),
                "dup_slots_same_window": same_win / max(slots, 1), "dup_slots_same_sighting": same_sight / max(slots, 1),
                "mean_distinct_windows_top5": (slots - same_win) / max(found, 1)}

    rng = random.Random(7)
    words = sorted({t for tl in truth.line_by_id.values() if tl.kind == "content" for t in tl.text.split()[:-1]
                    if len(t) >= 6 and t.isalpha()})
    sample = rng.sample(words, min(200, len(words)))
    return {"nonce": run(sorted(set(truth.nonces))), "word": run(sample)}


def share(counter: Counter) -> dict:
    tot = sum(counter[c] for c in CLASSES)
    return {"chars": tot, **{c: round(counter[c] / tot, 4) if tot else None for c in CLASSES},
            "e_truth": round(counter["e_truth"] / tot, 4) if tot and "e_truth" in counter else None}


def main(keys: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    S.init_corpus()
    lines = []
    for k in keys:
        t0 = time.perf_counter()
        truth = S.RunTruth(S.RUNS[k])
        store = S.load_store(S.RUNS[k], truth)
        lx = S.Lexicon(truth.strings)
        lb = S.Labeler(truth)
        chars, toks, toks_src, cross, hazard, class_lines, a_prev, types = classify_store(store, truth, lx, lb)
        sz = sizes(store, class_lines)
        crowd = crowding(store, truth)
        tot = sum(chars["all"][c] for c in CLASSES)
        res = {
            "run": S.RUNS[k], "rows": len(store.order), "windows": len(store.windows), "span_h": store.span_ms / 3.6e6,
            "m32_check": (chars["all"]["a"] + chars["all"]["b_exact"]) / tot,
            "a_identical_to_previous_share": a_prev / tot,
            "classes": {split: share(c) for split, c in sorted(chars.items())},
            "truth_crosstab_chars": {c: dict(cross[c]) for c in CLASSES},
            "hazards": dict(hazard),
            "line_types_chars": dict(types),
            "tokens": {c: dict(v) for c, v in toks.items()},
            "tokens_by_source": {c: dict(v) for c, v in toks_src.items()},
            "noise_share_all": toks["all"]["unknown"] / max(toks["all"]["tokens"], 1),
            "garbled_share_all": toks["all"]["garbled"] / max(toks["all"]["tokens"], 1),
            "sizes": sz, "crowding": crowd, "lexicon_words": len(lx.words), "harness_words": len(lx.harness),
            "runtime_s": time.perf_counter() - t0,
        }
        (OUT / f"{S.RUNS[k]}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        lines.append(summary_line(k, res))
        print(lines[-1], flush=True)
    (OUT / "summary.txt").write_text("\n\n".join(lines) + "\n", encoding="utf-8")


def summary_line(k: str, r: dict) -> str:
    al = r["classes"]["all"]
    fts = r["sizes"]
    full = fts["all"]["fts_bytes"]
    out = [f"[{k}] rows={r['rows']} windows={r['windows']} span_h={r['span_h']:.2f} chars={al['chars']} m32_check={r['m32_check']:.4f}",
           "  all: " + " ".join(f"{c}={al[c]:.3f}" for c in CLASSES) + f" e_truth={al['e_truth']}",
           f"  noise_share={r['noise_share_all']:.4f} garbled_share={r['garbled_share_all']:.4f} a_prev_share={r['a_identical_to_previous_share']:.3f}",
           "  fts_share: " + " ".join(f"{c}={fts[c]['fts_bytes'] / max(sum(fts[x]['fts_bytes'] for x in CLASSES), 1):.3f}" for c in CLASSES)
           + f" full_fts_kb={full / 1024:.0f} store_fts_kb={fts['store']['fts_bytes'] / 1024:.0f}",
           "  text_share: " + " ".join(f"{c}={fts[c]['text_bytes'] / max(fts['all']['text_bytes'], 1):.3f}" for c in CLASSES)
           + f" text_kb={fts['all']['text_bytes'] / 1024:.0f}"]
    for split, v in sorted(r["classes"].items()):
        if split != "all" and "|" not in split:
            out.append(f"  {split}: chars={v['chars']} " + " ".join(f"{c}={v[c]:.3f}" for c in CLASSES))
    ty = r["line_types_chars"]
    tot = al["chars"]
    out.append("  type(all chars): " + " ".join(f"{t}={sum(v for k, v in ty.items() if k.startswith(t + '|') and '|truth=' not in k) / tot:.3f}" for t in ("chrome", "garbage", "content"))
               + " | first-occurrence: " + " ".join(f"{t}={ty.get(t + '|first', 0) / tot:.4f}" for t in ("chrome", "garbage", "content")))
    out.append("  type x truth (share of type chars): " + " ".join(
        f"{t}:{k}={ty.get(f'{t}|truth={k}', 0) / max(sum(v for kk, v in ty.items() if kk.startswith(t + '|truth=')), 1):.3f}"
        for t in ("chrome", "garbage", "content") for k in ("content", "hchrome", "none")))
    out.append("  hazards: " + " ".join(f"{a}={b}" for a, b in sorted(r["hazards"].items())))
    for q, v in r["crowding"].items():
        out.append(f"  crowd[{q}]: " + " ".join(f"{a}={b:.3f}" if isinstance(b, float) else f"{a}={b}" for a, b in v.items()))
    return "\n".join(out)


if __name__ == "__main__":
    main(sys.argv[1:] or ["smoke", "standard", "day"])
