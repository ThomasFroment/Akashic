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
    for lang in ("fr", "en"):
        _model, vocab = _load(lang)
        lp = log_probs(samples, lang)
        toks, owner, words, scored = tokenize(text, vocab, prot)
        res = g_scores(lp, toks, owner, scored)
        res.update(lang=lang, words=words, T=lp.shape[0])
        if best is None or _key(res["G"]) < _key(best["G"]):
            best = res
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
    """Everything the decision needs that does not depend on g*."""
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
            "credit": credit_key(words, repo), "echo": echo_key(words, prompt_words), "text": text}


def decide(f: dict, g_star: float) -> dict:
    if f["base"] == "reject":
        return {"status": "reject", "text": "", "why": "today"}
    for gs, rest_max, kept in f["edges"]:  # leading / trailing sentence judged on its own
        if gs > g_star and rest_max <= g_star:
            return {"status": f["base"], "text": kept, "why": "strip"}
    G = f["G"]
    if G is not None and G > g_star:
        # a prompt echo is not a reject key: on real speech Whisper sometimes replaces a short
        # reply by the previous tail (mining: 6 short replies, G = inf); flag those unclear
        if f["ns"] or f["credit"]:
            return {"status": "reject", "text": "", "why": "G+key"}
        return {"status": "unclear", "text": f["text"], "why": "G"}
    if G is None and f["echo"]:  # nothing scorable (vocabulary / digits only) but echoes the prompt
        return {"status": "unclear", "text": f["text"], "why": "echo"}
    return {"status": f["base"], "text": f["text"], "why": "base"}


# ---------------------------------------------------------------- harness API
def fit(mining: list[tuple[dict, dict]], helpers) -> None:  # noqa: ANN001
    """g*: the largest recall on mining hallucinations subject to (a) new rejections or strips of
    real speech <= 0.5% of real speech and (b) real speech newly flagged unclear (today keep)
    <= UNCLEAR_CAP of real speech."""
    import pickle

    import torch

    cache_file = HERE / "C3" / f"gcache_{SCORE_TAG}.pkl"
    cache = pickle.loads(cache_file.read_bytes()) if cache_file.exists() else {}
    rows = []
    for cc, lab in mining:
        if not cc["text"] or today(cc, helpers.repo) == "reject":
            continue
        if cc["id"] not in cache:
            cache[cc["id"]] = analyse(cc, helpers)
        res = cache[cc["id"]]
        rows.append((cc, lab, res, features(cc, res, helpers)))
    cache_file.write_bytes(pickle.dumps(cache))
    STATE["fit"]["vram_peak_mb"] = torch.cuda.max_memory_allocated() / 2**20 if torch.cuda.is_initialized() else None
    speech = [(lab, f) for cc, lab, r, f in rows if lab["role"] != "nonspeech"]
    halluc = [f for cc, lab, r, f in rows if lab["role"] == "nonspeech"]
    n_speech_all = sum(1 for _cc, lab in mining if lab["role"] != "nonspeech")
    n_halluc_all = sum(1 for cc, lab in mining if lab["role"] == "nonspeech" and cc["text"])
    grid = sorted({round(f["G"], 2) for _l, f in speech + [(None, h) for h in halluc]
                   if f["G"] is not None and np.isfinite(f["G"])})
    best = None
    table = []
    for g in grid:
        ds = [decide(f, g) for _l, f in speech]
        rej = sum(1 for d in ds if d["status"] == "reject" and d["why"] != "today" or d["why"] == "strip")
        unc = sum(1 for d, (_l, f) in zip(ds, speech) if d["status"] == "unclear" and f["base"] == "keep")
        unclear_all = sum(1 for d in ds if d["status"] == "unclear")
        caught = sum(1 for f in halluc if decide(f, g)["status"] != "keep")
        caught += n_halluc_all - len(halluc)  # today's rejects
        rec = caught / n_halluc_all
        table.append((g, rej, unc, unclear_all, round(rec, 4)))
        if rej <= 0.005 * n_speech_all and unc <= UNCLEAR_CAP * n_speech_all:
            if best is None or rec > best[4] or (rec == best[4] and g > best[0]):
                best = table[-1]
    STATE["g"] = best[0]
    STATE["fit"].update(g_star=best[0], mining_new_rejects_or_strips=best[1], mining_speech_G_over=best[2],
                        mining_speech_unclear=best[3], mining_recall=best[4], n_speech=n_speech_all,
                        n_halluc=n_halluc_all, unclear_cap=UNCLEAR_CAP)
    dump = [{"role": lab["role"], "subset": lab["subset"], "opus": lab["opus"], "lang": lab["lang_true"],
             "G": r["G"], "head": r["lang"], "ns": max((p["no_speech_prob"] for p in cc["parts"]), default=None),
             "credit": f["credit"], "echo": f["echo"], "nskey": f["ns"], "edges": [e[:2] for e in f["edges"]], "base": f["base"],
             "text": cc["text"][:80]} for cc, lab, r, f in rows]
    (HERE / "C3" / f"fit_{RUN}.json").write_text(
        json.dumps({"fit": STATE["fit"], "sweep": table, "rows": dump}, ensure_ascii=False, indent=0),
        encoding="utf-8")
    print("fit", STATE["fit"])


UNCLEAR_CAP = 0.05
RUN = "it1"
SCORE_TAG = "v1"  # bump when the scoring (model, tokenisation, Viterbi) changes


def judge(cc: dict, helpers) -> dict:  # noqa: ANN001
    t0 = time.perf_counter()
    if not cc["text"] or today(cc, helpers.repo) == "reject":
        return {"status": "reject", "text": "", "extra_decodes": 0, "extra_ms": 0.0}
    res = analyse(cc, helpers)
    v = decide(features(cc, res, helpers), STATE["g"])
    return {"status": v["status"], "text": v["text"], "extra_decodes": 0,
            "extra_ms": (time.perf_counter() - t0) * 1000}
