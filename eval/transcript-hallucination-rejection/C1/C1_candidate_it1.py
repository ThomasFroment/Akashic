"""C1: two-key gate with edge-part stripping, no extra decode (invent run QB).

Keys are computed per Whisper window (Segments grouped by ``seek``); K_echo only on the first
window (later windows are decoded unprompted). The decode itself is the production one
(LOG_PROB_THRESHOLD -2.0 kept); this module only judges its output.

Text keys
  K_A     a credit phrase nobody says (multi-word, anchored on normalized word boundaries)
  K_B     a sayable outro phrase (>= 3 words) in the window / edge part
  K_weak  a <= 2-word BoH entry equal to the whole part: counts only next to another key
  K_echo  window 1, |W| >= 3, a >= 4-word run of the prompt (not inside one vocabulary entry)
          or >= 60% of W covered by prompt 3-grams (same restriction)
  K_cr    compression ratio > 2.4 and >= 100 chars
Audio keys
  K_ns    window no_speech_prob >= NS
  K_vad   Silero speech ratio over the text span [s+0.2, e-0.2] < VAD (recomputed on the stored
          segment, fresh state: no capture change needed)
  K_rate  chars / Silero speech seconds > RATE

Decision per window
  reject  K_A covers >= 80% of the words, or >= 2 keys (K_weak only counts with another key)
  strip   edge part with K_A, or >= 2 of {K_B, K_vad, K_rate} on that part
  unclear exactly 1 key, or today's low-confidence rule
The BoH list is mined on the mining split (normalized whole-part outputs on non-speech seen >= 3
times) plus the repository's HALLUCINATIONS; thresholds are fitted on the mining split.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import numpy as np

WIN_S = 0.032
EDGE_S = 0.2
MIN_BOH_COUNT = 3
# Anchors that make a phrase a subtitle credit (multi-word, never a bare stem).
CREDIT_ANCHORS = ("subtitles by", "sous titrage", "sous titres realises", "amara org community",
                  "communaute d'amara org", "transcription by", "captions by")

P = {"NS": 0.6, "VAD": 0.25, "RATE": 25.0, "ECHO_RUN": 4, "ECHO_COV": 0.6, "CR": 2.4, "CR_CHARS": 100}
CREDIT: list[str] = []   # K_A phrases
SAYABLE: list[str] = []  # K_B phrases (>= 3 words)
WEAK: set[str] = set()   # <= 2-word BoH entries
_SIL: dict[str, np.ndarray] = {}
_STATS: dict[str, int] = {}
KEY_LOG = Path(os.environ["C1_KEY_LOG"]) if os.environ.get("C1_KEY_LOG") else None


# ---------------------------------------------------------------- phrase lists
def _classify(phrases: dict[str, int], norm) -> None:  # noqa: ANN001
    CREDIT.clear(), SAYABLE.clear(), WEAK.clear()
    for p in sorted(phrases):
        n = len(p.split())
        if any(a in p for a in CREDIT_ANCHORS) and n >= 3:
            CREDIT.append(p)
        elif n >= 3:
            SAYABLE.append(p)
        elif n >= 1:
            WEAK.add(p)


def _phrase_re(phrases: list[str]) -> re.Pattern | None:
    if not phrases:
        return None
    alts = sorted((re.escape(p) for p in phrases), key=len, reverse=True)
    return re.compile(r"(?<![^\s])(?:" + "|".join(alts) + r")(?![^\s])")


_RE: dict[str, re.Pattern | None] = {}


def _coverage(rx: re.Pattern | None, words: str) -> float:
    if rx is None or not words:
        return 0.0
    covered = sum(len(m.group(0).split()) for m in rx.finditer(words))
    return covered / max(1, len(words.split()))


# ---------------------------------------------------------------- prompt echo
def _prompt_grams(cc: dict, norm) -> tuple[set, list[tuple[str, ...]]]:  # noqa: ANN001
    """3-grams and word sequences of the prompt that are not inside a single vocabulary entry."""
    prompt = cc.get("prompt") or ""
    vocab = cc.get("vocab") or []
    chosen, length = [], 0
    for entry in vocab:
        extra = len(entry) + (2 if chosen else 0)
        if length + extra > 800:
            break
        chosen.append(entry)
        length += extra
    head = ", ".join(chosen)
    tail = prompt[len(head):].lstrip(". ") if prompt.startswith(head) else prompt
    seq: list[tuple[str, int]] = []  # (word, entry index) for vocab; -1 for tail
    for i, entry in enumerate(chosen):
        seq.extend((w, i) for w in norm(entry).split())
    vocab_words = seq
    tail_words = norm(tail).split()
    return vocab_words, tail_words


def _echo(text: str, cc: dict, norm) -> bool:  # noqa: ANN001
    words = norm(text).split()
    if len(words) < 3:
        return False
    vocab_words, tail_words = _prompt_grams(cc, norm)
    sources = [[(w, -1) for w in tail_words], vocab_words]
    best_run, covered = 0, np.zeros(len(words), bool)
    for src in sources:
        sw = [w for w, _ in src]
        ent = [e for _, e in src]
        for n in range(3, min(len(words), 24) + 1):
            grams = {}
            for i in range(len(sw) - n + 1):
                # a run inside one vocabulary entry is a name, not an echo
                if ent[i] >= 0 and ent[i] == ent[i + n - 1]:
                    continue
                grams[tuple(sw[i:i + n])] = True
            hit = False
            for j in range(len(words) - n + 1):
                if tuple(words[j:j + n]) in grams:
                    hit = True
                    if n == 3:
                        covered[j:j + 3] = True
            if hit:
                best_run = max(best_run, n)
            else:
                break
    return best_run >= P["ECHO_RUN"] or covered.mean() >= P["ECHO_COV"]


# ---------------------------------------------------------------- audio
def _silero(cc: dict, helpers) -> np.ndarray:  # noqa: ANN001
    if cc["id"] not in _SIL:
        _SIL[cc["id"]] = helpers.silero(helpers.samples(cc))
    return _SIL[cc["id"]]


def _span_ratio(probs: np.ndarray, start: float, end: float, total_s: float) -> float:
    if start >= total_s:
        return 0.0
    s, e = start + EDGE_S, min(end, total_s) - EDGE_S
    if e - s < 0.2:
        s, e = start, min(end, total_s)
    a, b = int(s / WIN_S), max(int(s / WIN_S) + 1, int(np.ceil(e / WIN_S)))
    win = probs[a:b]
    return float((win >= 0.5).mean()) if len(win) else 0.0


def _speech_s(probs: np.ndarray, start: float, end: float) -> float:
    a, b = int(start / WIN_S), int(np.ceil(end / WIN_S))
    return float((probs[a:b] >= 0.5).sum()) * WIN_S


# ---------------------------------------------------------------- keys
def _chars(text: str, norm) -> int:  # noqa: ANN001
    return len(norm(text).replace(" ", ""))


def _part_keys(part: dict, probs: np.ndarray, total_s: float, norm) -> dict:  # noqa: ANN001
    w = norm(part["text"])
    ratio = _span_ratio(probs, part["start"], part["end"], total_s)
    sp = _speech_s(probs, part["start"], part["end"])
    return {
        "A": _coverage(_RE["A"], w),
        "B": _coverage(_RE["B"], w) > 0,
        "weak": w in WEAK,
        "vad": ratio < P["VAD"],
        "rate": _chars(part["text"], norm) / max(sp, WIN_S) > P["RATE"],
    }


def window_keys(parts: list[dict], first: bool, cc: dict, probs: np.ndarray, total_s: float, norm) -> dict:  # noqa: ANN001
    text = " ".join(p["text"].strip() for p in parts)
    w = norm(text)
    start, end = parts[0]["start"], parts[-1]["end"]
    sp = _speech_s(probs, start, end)
    keys = {
        "A": _coverage(_RE["A"], w),
        "B": _coverage(_RE["B"], w) > 0,
        "weak": w in WEAK,
        "echo": first and _echo(text, cc, norm),
        "cr": max(p["compression_ratio"] for p in parts) > P["CR"] and len(text) >= P["CR_CHARS"],
        "ns": parts[0]["no_speech_prob"] >= P["NS"],
        "vad": _span_ratio(probs, start, end, total_s) < P["VAD"],
        "rate": _chars(text, norm) / max(sp, WIN_S) > P["RATE"],
    }
    return keys


def _count(k: dict) -> int:
    n = sum(bool(k[x]) for x in ("B", "echo", "cr", "ns", "vad", "rate")) + (k["A"] > 0)
    if k["weak"] and n >= 1:
        n += 1
    return n


def judge_window(parts: list[dict], first: bool, cc: dict, probs, total_s, norm) -> tuple[str, list[dict], dict]:  # noqa: ANN001
    """-> (status, kept parts, keys)."""
    keys = window_keys(parts, first, cc, probs, total_s, norm)
    n = _count(keys)
    if keys["A"] >= 0.8 or n >= 2:
        return "reject", [], keys
    kept = list(parts)
    stripped = False
    if len(parts) > 1:
        for idx in (-1, 0):
            if not kept:
                break
            pk = _part_keys(kept[idx], probs, total_s, norm)
            if pk["A"] > 0 or sum((pk["B"], pk["vad"], pk["rate"])) >= 2:
                kept.pop(idx)
                stripped = True
        if not kept:
            return "reject", [], keys
        if stripped:
            keys = window_keys(kept, first, cc, probs, total_s, norm)
            keys["stripped"] = True
            n = _count(keys)
            if keys["A"] >= 0.8 or n >= 2:
                return "reject", [], keys
    return ("unclear" if n == 1 else "keep"), kept, keys


# ---------------------------------------------------------------- API
def fit(mining, helpers) -> None:  # noqa: ANN001
    norm = helpers.repo.normalize
    counts: dict[str, int] = {}
    for cc, lab in mining:
        if lab["role"] != "nonspeech":
            continue
        for p in cc["parts"]:
            w = norm(p["text"])
            if w:
                counts[w] = counts.get(w, 0) + 1
    mined = {p: c for p, c in counts.items() if c >= MIN_BOH_COUNT}
    phrases = dict(mined)
    for h in helpers.repo.HALLUCINATIONS:
        phrases.setdefault(h, 0)
    _classify(phrases, norm)
    _RE["A"] = _phrase_re(CREDIT)
    _RE["B"] = _phrase_re(SAYABLE)
    print(f"[C1] BoH mined {len(mined)} (>= {MIN_BOH_COUNT}); K_A {CREDIT}; K_B {SAYABLE}; weak {sorted(WEAK)}")
    _fit_thresholds(mining, helpers, norm)


def _genuine(cc: dict, lab: dict, norm) -> bool:  # noqa: ANN001
    """Mining speech whose raw text is mostly the reference (not itself a hallucination)."""
    from difflib import SequenceMatcher
    ref, raw = norm(lab["ref_text"]).split(), norm(cc["text"]).split()
    if not raw:
        return False
    m = sum(b.size for b in SequenceMatcher(None, ref, raw, autojunk=False).get_matching_blocks())
    return m >= 0.5 * len(raw)


def _fit_thresholds(mining, helpers, norm) -> None:  # noqa: ANN001
    """RATE and VAD: the most aggressive grid value that fires on <= 1% of genuine mining speech."""
    genuine, ns = [], []
    for cc, lab in mining:
        if not cc["text"]:
            continue
        probs = _silero(cc, helpers)
        total = len(probs) * WIN_S
        start, end = cc["parts"][0]["start"], cc["parts"][-1]["end"]
        row = (_chars(cc["text"], norm) / max(_speech_s(probs, start, end), WIN_S),
               _span_ratio(probs, start, end, total), cc["parts"][0]["no_speech_prob"])
        if lab["role"] == "nonspeech":
            ns.append(row)
        elif _genuine(cc, lab, norm):
            genuine.append(row)
    g, h = np.array(genuine), np.array(ns)
    for rate in (18, 20, 22, 25, 28, 30, 35, 40):
        if (g[:, 0] > rate).mean() <= 0.01:
            P["RATE"] = float(rate)
            break
    for vad in (0.35, 0.3, 0.25, 0.2, 0.15, 0.1):
        if (g[:, 1] < vad).mean() <= 0.01:
            P["VAD"] = vad
            break
    for nsv in (0.4, 0.45, 0.5, 0.55, 0.6, 0.7):
        if (g[:, 2] >= nsv).mean() <= 0.01:
            P["NS"] = nsv
            break
    print(f"[C1] fitted on {len(g)} genuine speech / {len(h)} non-speech: {P}; "
          f"fire rates speech rate={(g[:, 0] > P['RATE']).mean():.3f} vad={(g[:, 1] < P['VAD']).mean():.3f} "
          f"ns={(g[:, 2] >= P['NS']).mean():.3f} | nonspeech rate={(h[:, 0] > P['RATE']).mean():.3f} "
          f"vad={(h[:, 1] < P['VAD']).mean():.3f} ns={(h[:, 2] >= P['NS']).mean():.3f}")


def judge(cc: dict, helpers) -> dict:  # noqa: ANN001
    t0 = time.perf_counter()
    repo = helpers.repo
    norm = repo.normalize
    parts = cc["parts"]
    if not cc["text"] or not parts:
        return {"status": "reject", "text": "", "extra_decodes": 0, "extra_ms": 0.0, "keys": []}
    probs = _silero(cc, helpers)
    total_s = len(probs) * WIN_S
    windows: dict[int, list[dict]] = {}
    for p in parts:
        windows.setdefault(p["seek"], []).append(p)
    kept_parts, statuses, fired = [], [], []
    for i, seek in enumerate(sorted(windows)):
        st, kept, keys = judge_window(windows[seek], i == 0, cc, probs, total_s, norm)
        statuses.append(st)
        kept_parts.extend(kept)
        fired.append(sorted(k for k, v in keys.items() if v))
    for f, st in zip(fired, statuses, strict=True):
        tag = st + ":" + "+".join(f)
        _STATS[tag] = _STATS.get(tag, 0) + 1
    if KEY_LOG is not None:
        with KEY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": cc["id"], "windows": statuses, "keys": fired}) + "\n")
    text = " ".join(p["text"].strip() for p in kept_parts).strip()
    ms = (time.perf_counter() - t0) * 1000
    if not text:
        return {"status": "reject", "text": "", "extra_decodes": 0, "extra_ms": ms, "keys": fired}
    status = "unclear" if ("unclear" in statuses or repo.is_unclear(cc["confidence"])) else "keep"
    return {"status": status, "text": text, "extra_decodes": 0, "extra_ms": ms, "keys": fired}
