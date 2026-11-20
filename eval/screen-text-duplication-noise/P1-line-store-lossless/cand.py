"""P1: lossless line store with run intervals (C1 narrowed) + digit template / edit residual (C2)
+ chrome by context spread (C4 narrowed) + garbage down-rank (C5, down-rank only).

What the deferred worker would store (replayed here in memory):
  lines      one row per distinct exact line per (process, day): HMAC(K_line, process|day|line) key,
             text stored as a template reference + exact residual (C2), so the frame text rebuilds
             byte for byte.
  frame_windows rows stay, with an ordered line-id list instead of text (rebuild = join).
  sightings  per (line, window run): on/off intervals (first/last ts of each streak of frames).
  FTS        one external-content document per window run (hwnd, same title+url, no gap > RUN_GAP):
             column text = its content lines; column junk (lowest bm25 weight) = its garbage lines;
             chrome lines of an app-day form one document per (process, day), bm25 down-weighted,
             kept in Ask evidence as one block, every visibility interval kept.
Harness mapping: content -> kind 'content' in the run doc; chrome -> kind 'chrome' in the app-day doc
(emitted once, by the window that saw it first); garbage -> kind 'noise' (stored, not in the harness
index; findability through the junk column is measured by run_p1.py with a second index).

Nothing here prints stored text. Debug state (for run_p1.py) holds counts and in-memory text only.
"""

from __future__ import annotations

import datetime as _dt
import difflib
import math
import re
from collections import Counter, defaultdict

from shared import DIGITS_RE, TOKEN_RE, KeptLine, StoredText, fold_token, lev, tokens

WRITE_PATH = False  # nothing on the capture write path: the worker reads frame_windows.text later

# --- tunables (set on the synthetic set first) -----------------------------------------------------
P = {
    "run_gap_ms": 300_000,  # a streak (interval) closes after 5 min without a stored frame of the hwnd
    "chrome_min_ctx": 3,  # C4: key in >= max(3, 0.5 N_ctx) distinct (title, url) contexts
    "chrome_ctx_frac": 0.5,
    "chrome_run_frac": 0.8,  # single-context apps: key in >= 0.8 of the app-day's runs ...
    "chrome_min_runs": 3,  # ... with >= 3 runs
    "chrome_max_len": 200,  # longer lines are never chrome (menus/tabs/status bars are short)
    "bad_need_unstable": True,  # C5 guard for a garbage-shaped token inside a kept line: line read
    "weak_need_unstable": True,  # once or token only in a minority reading (same for short misses)
    "split_tokens": True,  # such tokens go to the junk column, the line is indexed in exact pieces
    "minority_unk": False,  # also down-rank word-shaped misses that only a minority reading has
    "minority_line": False,  # a minority reading whose extra tokens all miss the lexicon -> junk
    "line_frac": 0.34,  # a line whose garbage-shaped token chars reach this share goes whole to junk ...
    "line_need_unstable": False,  # ... stable or not (static garbage is stable), if the evidence is
    # strong: a garbage-shaped token, or >= 3 tokens (a lone short miss like 'ok' never is)
    "garb_short": 2,  # tokens of <= 2 letters missing the UIA lexicon are weak garbage evidence
    "use_lm": True,  # char-trigram LM of the day's UIA token types as a second shape test
    "lm_pct": 0.02,  # LM threshold: this percentile of the UIA token types' own scores
    "lm_min_types": 2000,  # below this many UIA token types the LM is not trusted (off)
    "near_frac": 0.1,  # C2 edit pairing bound: ceil(0.1 len), at least 1 (len >= 8)
    "link": "mid",  # link frame: "mid" of the longest streak, or "first" sighting
}

SLOT = "\x1f"  # digit slot marker in templates (never in captured text: control char)
VOWELS = set("aeiouy")


def day_of(ts_ms: int) -> int:
    return _dt.datetime.fromtimestamp(ts_ms / 1000).date().toordinal()


def runs_of(history: list) -> list[list]:
    """Window runs: consecutive stored texts of one hwnd with the same title and url, no gap > run_gap."""
    out, cur = [], []
    for o in history:
        if cur and ((o.title, o.url) != (cur[-1].title, cur[-1].url) or o.ts_ms - cur[-1].ts_ms > P["run_gap_ms"]):
            out.append(cur)
            cur = []
        cur.append(o)
    if cur:
        out.append(cur)
    return out


def ckey(line: str) -> str:
    """Chrome/template key: whitespace-collapsed, casefolded, digits masked."""
    return DIGITS_RE.sub("#", " ".join(line.split()).casefold())


# --- C2: lossless digit template + exact edit residual ---------------------------------------------


def digit_template(line: str) -> tuple[str, tuple[str, ...]]:
    return DIGITS_RE.sub(SLOT, line), tuple(DIGITS_RE.findall(line))


def fill(template: str, slots: tuple[str, ...]) -> str:
    parts = template.split(SLOT)
    out = [parts[0]]
    for s, p in zip(slots, parts[1:], strict=True):
        out += [s, p]
    return "".join(out)


def residual(base: str, var: str) -> list[tuple[int, int, str]]:
    """Exact edit script turning base into var: (i1, i2, replacement) on base."""
    sm = difflib.SequenceMatcher(None, base, var, autojunk=False)
    return [(i1, i2, var[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def apply_residual(base: str, ops: list[tuple[int, int, str]]) -> str:
    out, pos = [], 0
    for i1, i2, rep in ops:
        out += [base[pos:i1], rep]
        pos = i2
    out.append(base[pos:])
    return "".join(out)


class LineStore:
    """Per (process, day) physical line table: templates, variants as slots or edit residuals.
    Tracks stored chars to compare with today's per-frame text."""

    def __init__(self) -> None:
        self.ids: dict[str, int] = {}  # exact line -> line id
        self.rows: list[tuple] = []  # ('T', template, slots) | ('E', base_id, ops)
        self.tmpl: dict[str, int] = {}  # digit template -> first line id with it
        self.tok_post: dict[str, list[int]] = defaultdict(list)  # pairing candidates (token -> ids)
        self.stored_chars = 0
        self.n_digit = self.n_edit = self.n_new = 0

    def text(self, lid: int) -> str:
        r = self.rows[lid]
        if r[0] == "T":
            return fill(r[1], r[2])
        return apply_residual(self.text(r[1]), r[2])

    def add(self, line: str) -> tuple[int, int | None]:
        """Line id, and the id of the line it was paired with (None for a new template)."""
        lid = self.ids.get(line)
        if lid is not None:
            return lid, None
        lid = len(self.rows)
        self.ids[line] = lid
        t, slots = digit_template(line)
        base = self.tmpl.get(t)
        if base is not None:  # same template: store only the digit slots
            self.rows.append(("T", t, slots))
            self.stored_chars += sum(len(s) for s in slots) + 2
            self.n_digit += 1
            return lid, None  # digit variants are real changes, never a minority reading
        paired = self._near(line) if len(line) >= 8 else None
        if paired is not None:
            base_text = self.text(paired)
            ops = residual(base_text, line)
            self.rows.append(("E", paired, ops))
            self.stored_chars += sum(len(r) + 4 for _, _, r in ops) + 2
            self.n_edit += 1
            self.tmpl.setdefault(t, lid)
            return lid, paired
        self.rows.append(("T", t, slots))
        self.tmpl[t] = lid
        self.stored_chars += len(line)
        self.n_new += 1
        for tok in set(line.split()):
            if len(tok) >= 4:
                post = self.tok_post[tok]
                if len(post) < 64:
                    post.append(lid)
        return lid, None

    def _near(self, line: str) -> int | None:
        bound = max(1, -(-int(P["near_frac"] * len(line) * 10) // 10))
        votes: Counter = Counter()
        for tok in set(line.split()):
            if len(tok) >= 4:
                for i in self.tok_post.get(tok, ())[-32:]:
                    votes[i] += 1
        for i, _ in votes.most_common(4):
            r = self.rows[i]
            cand = fill(r[1], r[2]) if r[0] == "T" else self.text(i)
            if abs(len(cand) - len(line)) <= bound and lev(cand, line) <= bound:
                return i
        return None


# --- C5: garbage shape (FR-adapted rmgarbage + char-trigram LM of the user's own UIA text) --------


def shape_bad(tok: str) -> bool:
    """rmgarbage rules on one FTS token (a letters/digits run), FR-adapted (BnL: 4 vowels / 6
    consonants in a row; y and folded accented letters count as vowels). The length rule is the
    original 40 (21 would hit long names and identifiers)."""
    f = fold_token(tok)
    if f.isdigit():
        return False
    if len(f) > 40 and not re.fullmatch(r"[0-9a-f]+", f):
        return True  # L
    if re.search(r"(.)\1\1\1", f):
        return True  # R
    letters = "".join(c for c in f if c.isalpha())
    if len(letters) >= 3:
        if not any(c in VOWELS for c in letters):
            return True  # V: no vowel at all
        if re.search(r"[^aeiouy]{6,}", letters) or re.search(r"[aeiouy]{5,}", letters):
            return True  # V: 6 consonants / 5 vowels in a row
    if 2 <= len(tok) <= 3 and re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok) and not re.fullmatch(r"[A-Z][a-z]+", tok):
        return True  # case flip in a short token ('jI', 'lIl')
    if len(tok) >= 2 and set(tok) <= set("Il1ij") and tok.casefold() != "il":
        return True  # vertical strokes only ('II', 'lIl')
    return False


class TriLM:
    """Char-trigram model of the folded UIA token types of the day (no word list, no dependency)."""

    def __init__(self, words: set[str]) -> None:
        train = [w for w in words if w.isalpha() and len(w) >= 3]
        self.n: Counter = Counter()
        self.ctx: Counter = Counter()
        alpha = set()
        for w in train:
            s = f"^^{w}$"
            alpha.update(s)
            for i in range(len(s) - 2):
                self.n[s[i:i + 3]] += 1
                self.ctx[s[i:i + 2]] += 1
        self.v = len(alpha) + 1
        sc = sorted(self.score(w) for w in train)
        self.thr = sc[int(P["lm_pct"] * len(sc))] if len(sc) >= P["lm_min_types"] else -1e9  # too little UIA text: off

    def score(self, w: str) -> float:
        s = f"^^{w}$"
        return sum(math.log((self.n[s[i:i + 3]] + 1) / (self.ctx[s[i:i + 2]] + self.v)) for i in range(len(s) - 2)) / (len(s) - 2)


def token_class(tok: str, lex: set[str], lm: TriLM | None) -> str:
    """ok (UIA lexicon hit, number, identifier) | weak (short miss) | bad (garbage-shaped miss) | unk."""
    f = fold_token(tok)
    if f in lex or f.isdigit() or (any(c.isdigit() for c in f) and any(c.isalpha() for c in f) and len(f) >= 3):
        return "ok"
    if len(f) <= P["garb_short"]:
        return "weak"
    if shape_bad(tok):
        return "bad"
    if lm is not None and f.isalpha() and lm.score(f) < lm.thr:
        return "bad"
    return "unk"


def junk_spans(line: str, lex: set[str], lm: TriLM | None, line_unstable: bool, residual: set[str] | None):
    """Spans of the tokens C5 down-ranks in one OCR line, and whether the line has any token.
    residual: folded tokens of this reading that its more frequent paired reading lacks (C2)."""
    spans, n = [], 0
    for m in TOKEN_RE.finditer(line):
        n += 1
        t = m.group()
        c = token_class(t, lex, lm)
        if c == "ok":
            continue
        unstable = line_unstable or (residual is not None and fold_token(t) in residual)
        if c == "bad" and (unstable or not P["bad_need_unstable"]):
            spans.append(m.span())
        elif c == "weak" and (unstable or not P["weak_need_unstable"]):
            spans.append(m.span())
        elif c == "unk" and P["minority_unk"] and residual is not None and fold_token(t) in residual:
            spans.append(m.span())
    return spans, n


def split_junk(line: str, spans: list[tuple[int, int]]) -> tuple[list[str], str]:
    """(the indexed pieces between junk tokens, the junk tokens). Pieces are exact substrings of the
    stored line, so each still matches its frame text; the stored line itself is unchanged."""
    keep, junk, pos = [], [], 0
    for a, b in spans:
        keep.append(line[pos:a])
        junk.append(line[a:b])
        pos = b
    keep.append(line[pos:])
    return [k.strip() for k in keep if tokens(k)], " ".join(junk)


# --- deferred global pass ---------------------------------------------------------------------------


class Shared:
    def __init__(self) -> None:
        self.chrome: set[tuple] = set()  # (process, day, ckey)
        self.chrome_first: dict[tuple, tuple] = {}  # (process, day, exact line) -> (ts, hwnd, frame)
        self.chrome_last: dict[tuple, int] = {}
        self.lex: set[str] = set()  # folded tokens of UIA text (HMAC-keyed in the app)
        self.lm: TriLM | None = None
        self.stores: dict[tuple, LineStore] = {}
        self.debug: dict = {"faithful": {}, "kinds": {}, "evidence": {}, "raw": {}}


def prepare(windows):
    sh = Shared()
    ctxs: dict[tuple, set] = defaultdict(set)
    key_ctx: dict[tuple, set] = defaultdict(set)
    key_runs: Counter = Counter()
    n_runs: Counter = Counter()
    for hist, ctx in windows:
        for run in runs_of(hist):
            pd = (ctx.process, day_of(run[0].ts_ms))
            n_runs[pd] += 1
            in_run = set()
            for o in run:
                c = (o.title, o.url)
                ctxs[pd].add(c)
                if o.source == "uia":
                    sh.lex.update(fold_token(t) for t in tokens(o.text))
                for ln in set(o.text.split("\n")):
                    if ln.strip() and len(ln) <= P["chrome_max_len"]:
                        k = ckey(ln)
                        key_ctx[pd, k].add(c)
                        in_run.add(k)
            for k in in_run:
                key_runs[pd, k] += 1
    for (pd, k), cs in key_ctx.items():
        n = len(ctxs[pd])
        if n >= P["chrome_min_ctx"]:
            if len(cs) >= max(P["chrome_min_ctx"], P["chrome_ctx_frac"] * n):
                sh.chrome.add((*pd, k))
        elif n_runs[pd] >= P["chrome_min_runs"] and key_runs[pd, k] >= P["chrome_run_frac"] * n_runs[pd]:
            sh.chrome.add((*pd, k))
    # first sighting of each exact chrome line per app-day: its owner emits it once
    for hist, ctx in windows:
        for o in hist:
            d = day_of(o.ts_ms)
            for ln in o.text.split("\n"):
                if ln.strip() and (ctx.process, d, ckey(ln)) in sh.chrome:
                    k = (ctx.process, d, ln)
                    first = sh.chrome_first.get(k)
                    if first is None or o.ts_ms < first[0]:
                        sh.chrome_first[k] = (o.ts_ms, ctx.hwnd, o.frame_id)
                    sh.chrome_last[k] = max(sh.chrome_last.get(k, 0), o.ts_ms)
    sh.lm = TriLM(sh.lex) if P["use_lm"] else None
    global LAST
    LAST = sh
    return sh


LAST: Shared | None = None  # the last prepare() result, read by run_p1.py for its extra metrics


def doc_key(o) -> tuple:
    """FTS document of a stored text: the window's page (url) or, without a url, its title, on that
    day. Coming back to the same page continues its document; a page that retitles itself keeps its
    document and adds the new title to the document's title column."""
    return ("u", o.url, day_of(o.ts_ms)) if o.url else ("t", o.title, day_of(o.ts_ms))


def process(history, ctx):
    sh: Shared = ctx.shared
    lines: list[KeptLine] = []
    meta: dict = {}
    faithful: list[KeptLine] = []  # one per (line, interval): the rebuilt frames, for M3.1
    kinds: Counter = Counter()
    first: dict[tuple, tuple] = {}  # (doc, line) -> (fid, ts) of its first sighting
    run_frames: dict[tuple, list] = {}  # frames (fid, ts) of the open streak
    best: dict[tuple, list] = {}  # frames of the longest streak so far
    last: dict[tuple, int] = {}
    support: Counter = Counter()
    paired: dict[tuple, bool] = {}
    streak: dict[tuple, list] = {}  # open interval per (doc, line): [frame, first_ts, last_ts]
    prev: set[tuple] = set()
    exact: set[tuple] = set()  # read through UIA at least once: exact text, never garbage
    base_of: dict[tuple, int] = {}
    prev_ts = None

    def close(k):
        s = streak.pop(k)
        faithful.append(KeptLine(k[1], s[0], s[1], s[2], k[0]))
        fr = run_frames.pop(k)
        if len(fr) > len(best.get(k, ())):
            best[k] = fr

    for o in history:
        dk = doc_key(o)
        doc = ("doc", ctx.hwnd, *dk)
        if doc not in meta:
            meta[doc] = [[], o.url]
        if o.title and o.title not in meta[doc][0]:
            meta[doc][0].append(o.title)
        pd = (ctx.process, dk[2])
        store = sh.stores.get(pd)
        if store is None:
            store = sh.stores[pd] = LineStore()
        if prev_ts is not None and o.ts_ms - prev_ts > P["run_gap_ms"]:
            for k in prev:  # hidden or not captured for a while: every interval closes
                close(k)
            prev = set()
        cur = set()
        for ln in o.text.split("\n"):
            if ln.strip():
                cur.add((doc, ln))
                if o.source == "uia":
                    exact.add((doc, ln))
        for k in cur:
            support[k] += 1
            last[k] = o.ts_ms
            if k not in first:
                first[k] = (o.frame_id, o.ts_ms)
                _, base = store.add(k[1])
                paired[k] = base is not None
                if base is not None:
                    base_of[k] = base
            if k in prev:
                streak[k][2] = o.ts_ms
                run_frames[k].append((o.frame_id, o.ts_ms))
            else:
                streak[k] = [o.frame_id, o.ts_ms, o.ts_ms]
                run_frames[k] = [(o.frame_id, o.ts_ms)]
        for k in prev - cur:
            close(k)
        prev, prev_ts = cur, o.ts_ms
    for k in prev:
        close(k)
    ev: dict = {}
    raws: list[str] = []  # the stored line each KeptLine stands for (pieces and junk included)

    def emit(raw: str, kl: KeptLine) -> None:
        lines.append(kl)
        raws.append(raw)

    for (doc, ln), (fid, ts) in first.items():
        pd = (ctx.process, doc[4])
        frames = best[doc, ln]
        is_chrome = len(ln) <= P["chrome_max_len"] and (*pd, ckey(ln)) in sh.chrome
        ev[doc, ln] = "chrome" if is_chrome else []
        if is_chrome:
            own = sh.chrome_first.get((*pd, ln))
            if own is None or own[1] != ctx.hwnd or own[2] != fid:
                kinds["chrome_ref"] += 1
                continue  # stored and indexed once per app-day by the window that saw it first
            out_doc, lfid, lts, lend, main = ("chrome", *pd), own[2], own[0], sh.chrome_last[(*pd, ln)], "chrome"
        else:
            # link: first sighting, or the middle frame of its longest streak (may fake which frame
            # represents the line)
            lfid, _ = (fid, ts) if P["link"] == "first" else frames[len(frames) // 2]
            out_doc, lts, lend, main = doc, ts, last[doc, ln], "content"  # span: first..last sighting
        spans = []
        if (doc, ln) not in exact:
            residual = None
            b = base_of.get((doc, ln))
            if b is not None:
                bl = sh.stores[pd].text(b)
                # minority reading: a more frequent reading of the same place exists in this document,
                # with the same digits (a digit change is a real change, never a misreading)
                if (doc, bl) in support and support[doc, bl] > support[doc, ln] and DIGITS_RE.findall(bl) == DIGITS_RE.findall(ln):
                    residual = {fold_token(t) for t in tokens(ln)} - {fold_token(t) for t in tokens(bl)}
            if residual and P["minority_line"] and all(token_class(t, sh.lex, sh.lm) != "ok" for t in tokens(ln) if fold_token(t) in residual):
                kinds["noise_minority"] += 1  # the majority reading holds this line in the index
                emit(ln, KeptLine(ln, lfid, lts, lend, out_doc, "noise"))
                continue
            spans, ntok = junk_spans(ln, sh.lex, sh.lm, support[doc, ln] == 1, residual)
            if ntok and P["line_frac"] < 1.0 and (support[doc, ln] == 1 or not P["line_need_unstable"]):
                toks = tokens(ln)
                cls = [token_class(t, sh.lex, sh.lm) for t in toks]
                miss = sum(len(t) for t, c in zip(toks, cls, strict=True) if c in ("weak", "bad"))
                strong = "bad" in cls or len(toks) >= 3  # one short miss alone ('ok', 'go') is no evidence
                if strong and miss >= P["line_frac"] * sum(len(t) for t in toks):
                    spans = [(0, 0)] * ntok  # garbage-shaped line: the whole line goes to the junk column
            if spans and len(spans) == ntok:
                kinds["noise_line"] += 1
                emit(ln, KeptLine(ln, lfid, lts, lend, out_doc, "noise"))
                continue
            if not P["split_tokens"]:
                spans = []
        if spans:
            keep, junk = split_junk(ln, spans)
            kinds["junk_tokens"] += len(spans)
            kinds[main + "_stripped"] += 1
            for piece in keep:
                emit(ln, KeptLine(piece, lfid, lts, lend, out_doc, main))
            if not is_chrome:
                ev[doc, ln] = keep
            emit(ln, KeptLine(junk, lfid, lts, lend, out_doc, "noise"))
        else:
            kinds[main] += 1
            emit(ln, KeptLine(ln, lfid, lts, lend, out_doc, main))
            if not is_chrome:
                ev[doc, ln] = [ln]
    sh.debug["faithful"][ctx.hwnd] = faithful
    sh.debug["evidence"][ctx.hwnd] = ev  # (doc, line) -> indexed texts ([] = junk) or 'chrome'
    sh.debug["raw"][ctx.hwnd] = raws
    sh.debug["kinds"][ctx.hwnd] = kinds
    meta = {d: (" | ".join(m[0]) or None, m[1]) for d, m in meta.items()}
    for kl in lines:
        if kl.doc[0] == "chrome":
            meta[kl.doc] = (None, None)
    return StoredText(lines, meta)
