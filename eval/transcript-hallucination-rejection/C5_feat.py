"""C5 features: factorial audio x prompt likelihood contrast (teacher-forced via CT2 Whisper.align).

Cells, per decoded 30 s window (parts grouped by Segment.seek), scoring Whisper's own text tokens:
    A = (enc(X), P)   B = (enc(X0), P)   C = (enc(X), none)   D = (enc(X0), none)
X0 = 30 s of zero audio (encoder output cached once). P = <|startofprev|> + last 223 prompt tokens,
only for the first window (faster-whisper resets the prompt after window 1 when
condition_on_previous_text=False). Start sequence: [P] + SOT + lang + transcribe + notimestamps.
Timestamp tokens are stripped (ids >= EOT); fp16 probabilities are floored before the log.
"""

from __future__ import annotations

import re
import time

import numpy as np

FLOOR = 1e-6
_STATE: dict = {}
PUNCT = re.compile(r"^[\W_]*$")


def _setup(helpers):  # noqa: ANN001, ANN202
    if "enc0" in _STATE:
        return _STATE
    from faster_whisper.audio import pad_or_trim

    model = helpers.model
    fe = model.feature_extractor
    zero = np.zeros(fe.n_samples, np.float32)
    feats0 = pad_or_trim(fe(zero)[:, : fe.nb_max_frames])
    _STATE["enc0"] = model.encode(feats0)
    _STATE["toks"] = {}
    return _STATE


def _tokenizer(helpers, lang: str):  # noqa: ANN001, ANN202
    from faster_whisper.tokenizer import Tokenizer

    toks = _STATE["toks"]
    if lang not in toks:
        m = helpers.model
        toks[lang] = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language=lang)
    return toks[lang]


def _probs(ct2, enc, start: list[int], text: list[int], frames: int) -> np.ndarray:
    res = ct2.align(enc, start, [text], frames)[0]
    p = np.asarray(res.text_token_probs, np.float64)[: len(text)]
    p = np.nan_to_num(p, nan=FLOOR)
    return np.log(np.clip(p, FLOOR, 1.0))


def ngram_cover(y_words: list[str], p_words: list[str], n: int = 3) -> float:
    """Fraction of y words inside an n-gram also present in P."""
    if len(y_words) < n or len(p_words) < n:
        return 0.0
    grams = {tuple(p_words[i : i + n]) for i in range(len(p_words) - n + 1)}
    hit = np.zeros(len(y_words), bool)
    for i in range(len(y_words) - n + 1):
        if tuple(y_words[i : i + n]) in grams:
            hit[i : i + n] = True
    return float(hit.mean())


def ghost_hit(norm_text: str, halls) -> bool:  # noqa: ANN001
    """A known multi-word hallucination phrase occurs as a substring (not only the whole text)."""
    return any(len(h.split()) >= 2 and h in norm_text for h in halls)


def decode_stats(cc: dict, normalize) -> dict:  # noqa: ANN001
    """Ablation control: only what the primary decode already returns (no extra model work)."""
    parts = cc["parts"]
    return {"n_words": len(normalize(cc["text"] or "").split()), "duration": cc["duration_s"],
            "p_ns": max((p["no_speech_prob"] for p in parts), default=1.0),
            "avg_lp": float(np.mean([p["avg_logprob"] for p in parts])) if parts else -5.0,
            "cr": max((p["compression_ratio"] for p in parts), default=0.0), "ghost": False}


def features(cc: dict, helpers) -> dict:  # noqa: ANN001
    """Every per-segment feature; timings split into encode vs align so encoder reuse can be costed."""
    st = _setup(helpers)
    repo = helpers.repo
    model, ct2 = helpers.model, helpers.ct2
    from faster_whisper.audio import pad_or_trim

    t0 = time.perf_counter()
    samples = helpers.whisper_input(cc)
    fe = model.feature_extractor
    feats = fe(samples)
    content = feats.shape[-1] - 1
    tok = _tokenizer(helpers, cc["language"])
    eot = tok.eot
    prompt_tokens = tok.encode(" " + cc["prompt"].strip())[-223:] if cc.get("prompt") else []
    sot = list(tok.sot_sequence) + [tok.no_timestamps]
    by_seek: dict[int, list[dict]] = {}
    for p in cc["parts"]:
        by_seek.setdefault(p["seek"], []).append(p)
    enc_ms = align_ms = 0.0
    wins = []
    for wi, (seek, group) in enumerate(sorted(by_seek.items())):
        text = [t for p in group for t in p["tokens"] if t < eot]
        if not text:
            continue
        size = max(1, min(fe.nb_max_frames, content - seek))
        t1 = time.perf_counter()
        enc = model.encode(pad_or_trim(feats[:, seek : seek + size]))
        t2 = time.perf_counter()
        enc_ms += (t2 - t1) * 1000
        c = _probs(ct2, enc, sot, text, size)
        d = _probs(ct2, st["enc0"], sot, text, size)
        prompted = wi == 0 and seek == 0 and bool(prompt_tokens)
        if prompted:
            ps = [tok.sot_prev] + prompt_tokens + sot
            a = _probs(ct2, enc, ps, text, size)
            b = _probs(ct2, st["enc0"], ps, text, size)
        else:
            a, b = c, d
        align_ms += (time.perf_counter() - t2) * 1000
        # word tokens: drop punctuation-only tokens
        mask = np.array([not PUNCT.match(tok.decode([t]).strip()) for t in text])
        if not mask.any():
            mask[:] = True
        wins.append({"n": int(mask.sum()), "a": a[mask], "b": b[mask], "c": c[mask], "d": d[mask],
                     "prompted": prompted, "seek": seek})
    total_ms = (time.perf_counter() - t0) * 1000
    norm = repo.normalize(cc["text"] or "")
    y_words = norm.split()
    p_words = repo.normalize(cc.get("prompt") or "").split()
    cover = ngram_cover(y_words, p_words)
    parts = cc["parts"]
    vad = np.asarray(cc.get("vad_probs") or [0.0])
    f = {
        "n_words": len(y_words), "n_win": len(wins), "duration": cc["duration_s"],
        "p_ns": max((p["no_speech_prob"] for p in parts), default=1.0),
        "p_ns_first": parts[0]["no_speech_prob"] if parts else 1.0,
        "avg_lp": float(np.mean([p["avg_logprob"] for p in parts])) if parts else -5.0,
        "cr": max((p["compression_ratio"] for p in parts), default=0.0),
        "conf": cc.get("confidence"), "today": cc["today"],
        "silero_ratio": float((vad >= 0.5).mean()),
        "ghost": ghost_hit(norm, repo.HALLUCINATIONS), "cover": cover,
        "enc_ms": enc_ms, "align_ms": align_ms, "total_ms": total_ms, "decode_ms": cc["decode_ms"],
    }
    if wins:
        a = np.concatenate([w["a"] for w in wins]); b = np.concatenate([w["b"] for w in wins])
        c = np.concatenate([w["c"] for w in wins]); d = np.concatenate([w["d"] for w in wins])
        f.update(
            S_P=float(np.mean(a - b)), S_0=float(np.mean(c - d)),
            S_P_min_win=float(min(np.mean(w["a"] - w["b"]) for w in wins)),
            S_0_min_win=float(min(np.mean(w["c"] - w["d"]) for w in wins)),
            lpA=float(np.mean(a)), lpB=float(np.mean(b)), lpC=float(np.mean(c)), lpD=float(np.mean(d)),
            minA=float(np.min(a)), n_tok=int(len(a)),
        )
    return f


def derive(f: dict) -> dict:
    """Derived fusion inputs (iteration 2 adds the prompt lifts A-C and B-D)."""
    import math

    f = dict(f)
    f["log_words"] = math.log1p(f.get("n_words", 0))
    f["ghost"] = float(f.get("ghost", 0))
    if "lpA" in f:
        f["lift_X"] = f["lpA"] - f["lpC"]
        f["lift_0"] = f["lpB"] - f["lpD"]
    return f


def score_S(f: dict) -> float:
    """The candidate's S: max(S_P, S_0 when y copies >= one 3-gram from P)."""
    if "S_P" not in f:
        return float("-inf")
    s = f["S_P"]
    if f["cover"] > 0:
        s = max(s, f["S_0"])
    return s
