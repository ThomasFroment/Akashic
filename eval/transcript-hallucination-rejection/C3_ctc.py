"""C3: CTC monotonic-alignment key (new local model).

A wav2vec2 CTC model (jonatasgrosman xlsr-53 French / English, Apache-2.0) is forced to align
Whisper's text to the audio with a numpy Viterbi over the 2N+1 blank-interleaved CTC states.

    G = mean over frames the best path spends on a scored character of
        [max_k log p_t(k) - log p_t(char)]

Real speech lines the characters up with the model's own peaks (G ~ 0); text over noise must
sit on frames the model calls blank (G large). Digits and characters outside the head's alphabet
are a wildcard state (matches any frame, not scored); words of the user's vocabulary are aligned
but not scored. Both heads are run and the lower G is kept (code-switching, misdetected language).

Decision (thresholds fitted on the mining split only):
    today's reject (empty / whole-string known hallucination)       -> reject
    edge sentence with G_sent > g* and a text key, rest G <= g*      -> strip that sentence
    G > g* and a second key (p_ns >= NS_KEY, credit phrase, echo)   -> reject
    G > g*                                                          -> unclear
    otherwise today's keep / unclear (confidence)
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODELS = HERE / "data" / "models_extra"
HEADS = {"fr": MODELS / "xlsr53-french", "en": MODELS / "xlsr53-english"}
STAR = -1  # wildcard token id
NS_KEY = 0.6
ECHO_N = 4
# Credit / outro phrases, matched as whole words anchored at the start or end of the text
# (normalised). The repo's whole-string list plus a few multi-word stems.
EXTRA_CREDITS = (
    "sous titres realises par", "sous titrage", "amara org", "closed captioning",
    "subtitles by", "merci d'avoir regarde", "thank you for watching", "thanks for watching",
    "abonnez vous", "please subscribe", "like and subscribe",
)

STATE: dict = {"g": None, "models": {}, "credits": None, "fit": {}}


# ---------------------------------------------------------------- model
def _load(lang: str):  # noqa: ANN202
    if lang not in STATE["models"]:
        import torch
        from transformers import Wav2Vec2ForCTC

        model = Wav2Vec2ForCTC.from_pretrained(str(HEADS[lang]), local_files_only=True,
                                               dtype=torch.float16).to("cuda").eval()
        vocab = json.loads((HEADS[lang] / "vocab.json").read_text(encoding="utf-8"))
        STATE["models"][lang] = (model, vocab)
    return STATE["models"][lang]


def log_probs(samples: np.ndarray, lang: str) -> np.ndarray:
    """(T, V) log-softmax of the CTC head, 20 ms frames."""
    import torch

    model, _vocab = _load(lang)
    x = samples.astype(np.float32)
    x = (x - x.mean()) / np.sqrt(x.var() + 1e-7)
    with torch.inference_mode():
        t = torch.from_numpy(x)[None].to("cuda", torch.float16)
        logits = model(t).logits[0].float()
        return torch.log_softmax(logits, dim=-1).cpu().numpy()


# ---------------------------------------------------------------- text -> tokens
def _fold(ch: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))


def tokenize(text: str, vocab: dict, protected: set[str]) -> tuple[list[int], list[int], list[str], list[bool]]:
    """Token ids (STAR for digits/OOV runs), the word index of each token (-1 for '|'), the
    words, and whether each word is scored (False for vocabulary words)."""
    text = unicodedata.normalize("NFC", text.casefold().replace("’", "'"))
    words = re.findall(r"[^\s]+", text)
    toks: list[int] = []
    owner: list[int] = []
    out_words: list[str] = []
    scored: list[bool] = []
    sep = vocab["|"]
    for w in words:
        w = re.sub(r"^[^\w]+|[^\w]+$", "", w)
        if not w:
            continue
        wi = len(out_words)
        out_words.append(w)
        scored.append(_fold(w) not in protected)
        if toks:
            toks.append(sep)
            owner.append(-1)
        for ch in w:
            tid = vocab.get(ch)
            if tid is None and not ch.isdigit():
                tid = vocab.get(_fold(ch)) if len(_fold(ch)) == 1 else None
            if tid is None:
                tid = STAR
            if tid == STAR and toks and toks[-1] == STAR:
                continue
            toks.append(tid)
            owner.append(wi)
    return toks, owner, out_words, scored


# ---------------------------------------------------------------- Viterbi
def viterbi(lp: np.ndarray, toks: list[int], blank: int = 0) -> np.ndarray | None:
    """Best CTC path; returns the state index per frame (even = blank, odd = token k//2) or
    None when the text cannot fit the frames (N + repeats > T)."""
    T, _V = lp.shape
    N = len(toks)
    S = 2 * N + 1
    tok = np.full(S, blank, np.int64)
    tok[1::2] = toks
    is_star = tok == STAR
    star_em = lp.max(axis=1)  # wildcard: any class (incl. blank), never scored
    # transition s-2 -> s allowed for tokens that differ from the token two states back
    skip = np.zeros(S, bool)
    skip[3::2] = np.array([toks[i] != toks[i - 1] or toks[i] == STAR for i in range(1, N)], bool)
    need = N + int(np.sum(~skip[3::2] & ~is_star[3::2])) if N > 1 else N
    if need > T:
        return None
    em = lp[:, np.where(is_star, 0, tok)]
    em[:, is_star] = star_em[:, None]
    NEG = -1e30
    score = np.full(S, NEG)
    score[0] = em[0, 0]
    if S > 1:
        score[1] = em[0, 1]
    back = np.zeros((T, S), np.int8)
    for t in range(1, T):
        stay = score
        step = np.concatenate(([NEG], score[:-1]))
        jump = np.concatenate(([NEG, NEG], score[:-2]))
        jump = np.where(skip, jump, NEG)
        best = np.maximum(stay, np.maximum(step, jump))
        arg = np.where(best == stay, 0, np.where(best == step, 1, 2)).astype(np.int8)
        score = best + em[t]
        back[t] = arg
    end = S - 1 if S == 1 or score[S - 1] >= score[S - 2] else S - 2
    if score[end] <= NEG / 2:
        return None
    path = np.empty(T, np.int64)
    s = end
    for t in range(T - 1, -1, -1):
        path[t] = s
        s -= int(back[t, s])
    return path


def g_scores(lp: np.ndarray, toks: list[int], owner: list[int], scored: list[bool]) -> dict:
    """Segment G and per-word sums (gap sum, frame count) along the best path."""
    nwords = len(scored)
    if not toks:
        return {"G": None, "word_gap": np.zeros(nwords), "word_n": np.zeros(nwords), "feasible": True}
    path = viterbi(lp, toks)
    if path is None:
        return {"G": float("inf"), "word_gap": np.full(nwords, np.inf), "word_n": np.ones(nwords),
                "feasible": False}
    word_gap = np.zeros(nwords)
    word_n = np.zeros(nwords)
    best = lp.max(axis=1)
    odd = path % 2 == 1
    for t in np.nonzero(odd)[0]:
        k = path[t] // 2
        tid = toks[k]
        wi = owner[k]
        if tid == STAR or wi < 0 or not scored[wi]:
            continue
        word_gap[wi] += best[t] - lp[t, tid]
        word_n[wi] += 1
    n = word_n.sum()
    return {"G": float(word_gap.sum() / n) if n else None, "word_gap": word_gap, "word_n": word_n,
            "feasible": True}


# ---------------------------------------------------------------- keys
def _norm_words(text: str, repo) -> list[str]:  # noqa: ANN001
    return repo.normalize(text or "").split()


def credits(repo) -> list[list[str]]:  # noqa: ANN001
    if STATE["credits"] is None:
        phrases = {p for p in repo.HALLUCINATIONS if len(p.split()) >= 2} | set(EXTRA_CREDITS)
        STATE["credits"] = [p.split() for p in sorted(phrases)]
    return STATE["credits"]


def credit_key(words: list[str], repo) -> bool:  # noqa: ANN001
    """A multi-word credit phrase at the start or the end of the text."""
    for p in credits(repo):
        n = len(p)
        if len(words) >= n and (words[:n] == p or words[-n:] == p):
            return True
    return False


def echo_key(words: list[str], prompt_words: list[str]) -> bool:
    """The text repeats an ECHO_N-word run of the prompt (vocabulary list + previous tail);
    shorter texts (2-3 words) must appear whole."""
    if len(words) < 2 or not prompt_words:
        return False
    n = min(ECHO_N, len(words))
    grams = {tuple(prompt_words[i : i + n]) for i in range(len(prompt_words) - n + 1)}
    return any(tuple(words[i : i + n]) in grams for i in range(len(words) - n + 1))


def ns_key(cc: dict) -> bool:
    return any(p["no_speech_prob"] >= NS_KEY for p in cc.get("parts") or [])


# ---------------------------------------------------------------- scoring one segment
def protected_words(cc: dict, repo) -> set[str]:  # noqa: ANN001
    out: set[str] = set()
    for entry in cc.get("vocab") or []:
        out.update(repo.normalize(entry).split())
    return out


def analyse(cc: dict, helpers) -> dict:  # noqa: ANN001
    """G under both heads (lower kept) plus per-sentence G for the edge sentences."""
    repo = helpers.repo
    text = cc["text"] or ""
    samples = helpers.samples(cc)
    prot = protected_words(cc, repo)
    best = None
    heard = 0
    for lang in ("fr", "en"):
        _model, vocab = _load(lang)
        lp = log_probs(samples, lang)
        # speech presence: frames whose best class is a letter (not blank, '|' or specials)
        silent = [vocab[k] for k in ("<pad>", "<s>", "</s>", "<unk>", "|") if k in vocab]
        heard = max(heard, int(np.sum(~np.isin(lp.argmax(axis=1), silent))))
        toks, owner, words, scored = tokenize(text, vocab, prot)
        res = g_scores(lp, toks, owner, scored)
        res.update(lang=lang, words=words, T=lp.shape[0])
        if best is None or _key(res["G"]) < _key(best["G"]):
            best = res
    best["heard"] = heard
    return best


def _key(g: float | None) -> float:
    return -1.0 if g is None else g


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p for p in parts if p]


def sentence_g(res: dict, text: str) -> list[tuple[str, float | None]]:
    """Split the aligned words into sentences and return (sentence, G) pairs."""
    sents = sentences(text)
    out = []
    wi = 0
    for s in sents:
        n = len([w for w in re.findall(r"[^\s]+", s) if re.sub(r"^[^\w]+|[^\w]+$", "", w)])
        gap = res["word_gap"][wi : wi + n].sum()
        cnt = res["word_n"][wi : wi + n].sum()
        out.append((s, float(gap / cnt) if cnt else None))
        wi += n
    return out


def today(cc: dict, repo) -> str:  # noqa: ANN001
    text = cc["text"]
    if not text or repo.is_hallucination(text):
        return "reject"
    return "unclear" if repo.is_unclear(cc["confidence"]) else "keep"


def features(cc: dict, res: dict, helpers) -> dict:  # noqa: ANN001
    """Everything the decision needs that does not depend on the thresholds."""
    repo = helpers.repo
    text = cc["text"]
    prompt_words = _norm_words(cc.get("prompt") or "", repo)

    def text_key(ws: list[str]) -> bool:
        return credit_key(ws, repo) or echo_key(ws, prompt_words)

    sg = sentence_g(res, text)
    edges = []
    if len(sg) >= 2:
        for idx in (len(sg) - 1, 0):
            s, gs = sg[idx]
            if gs is None or not text_key(_norm_words(s, repo)):
                continue
            rest = [x for i, x in enumerate(sg) if i != idx]
            gap = [g for _, g in rest if g is not None]
            if gap:
                edges.append((gs, max(gap), " ".join(x for x, _ in rest)))
    words = _norm_words(text, repo)
    return {"base": today(cc, repo), "G": res["G"], "edges": edges, "ns": ns_key(cc),
            "credit": credit_key(words, repo), "echo": echo_key(words, prompt_words),
            "heard": res["heard"], "text": text}


def decide(f: dict, g_star: float, h_star: int) -> dict:
    """h_star: the CTC heads hear at most this many letter frames -> no speech present. A
    rejection needs it; when the CTC hears speech, a would-be rejection is only "unclear"
    (Whisper replaced a real short reply by a hallucination: the utterance is kept)."""
    quiet = f["heard"] <= h_star
    if f["base"] == "reject":  # today: whole-string known hallucination (empty text never gets here)
        if quiet:
            return {"status": "reject", "text": "", "why": "today"}
        return {"status": "unclear", "text": f["text"], "why": "today-heard"}
    for gs, rest_max, kept in f["edges"]:  # leading / trailing sentence judged on its own
        if gs > g_star and rest_max <= g_star:
            return {"status": f["base"], "text": kept, "why": "strip"}
    G = f["G"]
    if G is not None and G > g_star:
        # a prompt echo is not a reject key: on real speech Whisper sometimes replaces a short
        # reply by the previous tail (mining: 6 short replies, G = inf)
        if (f["ns"] or f["credit"]) and quiet:
            return {"status": "reject", "text": "", "why": "G+key"}
        return {"status": "unclear", "text": f["text"], "why": "G"}
    if G is None and f["echo"]:  # nothing scorable (vocabulary / digits only) but echoes the prompt
        return {"status": "unclear", "text": f["text"], "why": "echo"}
    return {"status": f["base"], "text": f["text"], "why": "base"}


# ---------------------------------------------------------------- harness API
def fit(mining: list[tuple[dict, dict]], helpers) -> None:  # noqa: ANN001
    """g*: the largest mining recall subject to real speech newly flagged unclear (today keep)
    <= UNCLEAR_CAP of real speech and no new speech rejection / strip.
    h*: then the largest letter-frame count that keeps real-speech rejections of non-empty
    Whisper text (today's whole-string rule included) <= REJECT_CAP of real speech."""
    import pickle

    cache_file = HERE / "C3" / f"gcache_{SCORE_TAG}.pkl"
    cache = pickle.loads(cache_file.read_bytes()) if cache_file.exists() else {}
    rows = []
    for cc, lab in mining:
        if not cc["text"]:
            continue
        if cc["id"] not in cache:
            cache[cc["id"]] = analyse(cc, helpers)
        res = cache[cc["id"]]
        rows.append((cc, lab, res, features(cc, res, helpers)))
    cache_file.write_bytes(pickle.dumps(cache))
    speech = [f for cc, lab, r, f in rows if lab["role"] != "nonspeech"]
    halluc = [f for cc, lab, r, f in rows if lab["role"] == "nonspeech"]
    n_speech = sum(1 for _cc, lab in mining if lab["role"] != "nonspeech")
    n_halluc = len(halluc)
    grid = sorted({round(f["G"], 2) for f in speech + halluc if f["G"] is not None and np.isfinite(f["G"])})
    table = []
    best = None
    h_none = -1  # g* is fitted with every rejection off the table (h* = -1: nothing is quiet)
    for g in grid:
        ds = [decide(f, g, 10**9) for f in speech]
        new_rej = sum(1 for d, f in zip(ds, speech) if (d["status"] == "reject" and f["base"] != "reject")
                      or d["why"] == "strip")
        unc = sum(1 for d, f in zip(ds, speech) if d["status"] == "unclear" and f["base"] == "keep")
        rec = sum(1 for f in halluc if decide(f, g, h_none)["status"] != "keep") / n_halluc
        table.append(("g", g, new_rej, unc, round(rec, 4)))
        if new_rej == 0 and unc <= UNCLEAR_CAP * n_speech and (best is None or rec > best[4] or
                                                                (rec == best[4] and g > best[1])):
            best = table[-1]
    g_star = best[1]
    h_best = None
    for h in range(-1, 60):
        rej = sum(1 for f in speech if decide(f, g_star, h)["status"] == "reject")
        rrec = sum(1 for f in halluc if decide(f, g_star, h)["status"] == "reject") / n_halluc
        rec = sum(1 for f in halluc if decide(f, g_star, h)["status"] != "keep") / n_halluc
        table.append(("h", h, rej, round(rrec, 4), round(rec, 4)))
        if rej <= REJECT_CAP * n_speech:
            h_best = (h, rej, rrec, rec)
    STATE["g"], STATE["h"] = g_star, h_best[0]
    STATE["fit"].update(g_star=g_star, h_star=h_best[0], mining_speech_rejects_nonempty=h_best[1],
                        mining_reject_recall_nonempty=round(h_best[2], 4), mining_recall_nonempty=round(h_best[3], 4),
                        mining_new_unclear=best[3], n_speech=n_speech, n_halluc_nonempty=n_halluc,
                        unclear_cap=UNCLEAR_CAP, reject_cap=REJECT_CAP)
    dump = [{"role": lab["role"], "subset": lab["subset"], "opus": lab["opus"], "lang": lab["lang_true"],
             "G": r["G"], "heard": r["heard"], "head": r["lang"],
             "ns": max((p["no_speech_prob"] for p in cc["parts"]), default=None),
             "credit": f["credit"], "echo": f["echo"], "nskey": f["ns"], "base": f["base"],
             "text": cc["text"][:80]} for cc, lab, r, f in rows]
    (HERE / "C3" / f"fit_{RUN}.json").write_text(
        json.dumps({"fit": STATE["fit"], "sweep": table, "rows": dump}, ensure_ascii=False, indent=0),
        encoding="utf-8")
    print("fit", STATE["fit"])


UNCLEAR_CAP = 0.05
REJECT_CAP = 0.0025
RUN = "it2"
SCORE_TAG = "v2"  # bump when the scoring (model, tokenisation, Viterbi) changes


def judge(cc: dict, helpers) -> dict:  # noqa: ANN001
    t0 = time.perf_counter()
    if not cc["text"]:
        return {"status": "reject", "text": "", "extra_decodes": 0, "extra_ms": 0.0}
    res = analyse(cc, helpers)
    v = decide(features(cc, res, helpers), STATE["g"], STATE["h"])
    return {"status": v["status"], "text": v["text"], "extra_decodes": 0,
            "extra_ms": (time.perf_counter() - t0) * 1000}
