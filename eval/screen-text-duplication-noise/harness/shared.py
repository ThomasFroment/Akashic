"""Shared code of the duplicated/noisy screen text harness.

PRIVACY: stores are read in memory only. Nothing here prints, logs or writes stored text; callers
report counts, ratios and keyed hashes. Strings the harness itself rendered (truth/*.jsonl) may be
printed.

Candidate contract (a module file; the harness puts this folder on sys.path, so `from shared import
KeptLine, StoredText` works; see baseline/cand.py):
    process(history: list[Obs], ctx: Ctx) -> StoredText            required
    prepare(windows: list[tuple[list[Obs], Ctx]]) -> object          optional, deferred global pass;
                                                                     its result is ctx.shared
    step(state, obs: Obs, ctx: Ctx) -> state                         optional, the per-frame write
                                                                     path (timed per frame)
    WRITE_PATH: bool = False   True: process() itself runs on the write path (timed per frame)
process() gets one window's (hwnd's) stored texts in time order and returns the lines it keeps. Each
KeptLine names its FTS row (doc key; rows may span windows, e.g. chrome once per app), the frame it
links to, the span of moments it stands for, and its kind: 'content' and 'chrome' rows are indexed,
'noise' lines are stored but not indexed. The harness rebuilds an in-memory FTS5 (unicode61
remove_diacritics 2, columns text/title/url, title/url from doc_meta or the link frame).
"""

from __future__ import annotations

import bisect
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(r"C:\Users\thoma\Documents\Akashic")
QUALITY = REPO / "eval" / "quality"
HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
for p in (str(QUALITY), str(REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

RUNS = {
    "smoke": "20261118-smoke-s11-target-2241",
    "standard": "20261119-standard-s21-target-1818",
    "day": "20261119-day-s31-target-0547",
}
SHORT = {"smoke": "s", "standard": "st", "day": "d"}

APP_TYPES = {
    "chrome.exe": "browser", "msedge.exe": "browser", "zen.exe": "browser", "firefox.exe": "browser",
    "discord.exe": "chat", "steamwebhelper.exe": "launcher", "steam.exe": "launcher",
    "windowsterminal.exe": "terminal", "code.exe": "editor", "notepad.exe": "editor",
    "explorer.exe": "files", "calculatorapp.exe": "calculator", "outerwilds.exe": "game",
    "python.exe": "harness_win",
}


def app_type(process: str | None) -> str:
    return APP_TYPES.get((process or "").lower(), "other")


# --- text primitives ------------------------------------------------------------------------------

TOKEN_RE = re.compile(r"[^\W_]+")  # FTS5 unicode61: runs of letters/digits; '_' and punctuation split
DIGITS_RE = re.compile(r"\d+")


def tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text)


def fold_token(t: str) -> str:
    """unicode61 remove_diacritics 2 + casefold, for one token."""
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn").casefold()


def fts_tokens(text: str) -> list[str]:
    return [fold_token(t) for t in TOKEN_RE.findall(text)]


def fold(text: str, accents: bool = True) -> str:
    """Same as eval/quality/score.py fold (NFC, casefold, punctuation -> space, collapsed spaces)."""
    text = unicodedata.normalize("NFC", text).casefold()
    if not accents:
        text = "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")
    text = "".join(c if not unicodedata.category(c).startswith("P") else " " for c in text)
    return " ".join(text.split())


def mask_digits(s: str) -> str:
    return DIGITS_RE.sub("#", s)


def text_lines(text: str) -> list[str]:
    """Non-empty stripped lines, as M3.2 counts them."""
    return [x.strip() for x in text.splitlines() if x.strip()]


# --- bit-parallel Levenshtein (Myers 1999 / Hyyro 2001), Python big ints --------------------------


def _peq(p: str) -> dict[str, int]:
    d: dict[str, int] = {}
    for i, c in enumerate(p):
        d[c] = d.get(c, 0) | (1 << i)
    return d


def lev(a: str, b: str) -> int:
    """Edit distance (unit costs)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    m = len(b)
    if m == 0:
        return len(a)
    peq = _peq(b)
    full = (1 << m) - 1
    top = 1 << (m - 1)
    pv, mv, score = full, 0, m
    for c in a:
        eq = peq.get(c, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | (~(xh | pv) & full)
        mh = pv & xh
        if ph & top:
            score += 1
        elif mh & top:
            score -= 1
        ph = ((ph << 1) | 1) & full
        mh = (mh << 1) & full
        pv = mh | (~(xv | ph) & full)
        mv = ph & xv
    return score


def lev_search(pattern: str, text: str) -> tuple[int, int]:
    """Smallest edit distance between `pattern` and any substring of `text`, and the (exclusive)
    end of that substring in `text`."""
    m = len(pattern)
    if m == 0:
        return 0, 0
    peq = _peq(pattern)
    full = (1 << m) - 1
    top = 1 << (m - 1)
    pv, mv, score = full, 0, m
    best, best_end = m, 0
    for j, c in enumerate(text):
        eq = peq.get(c, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | (~(xh | pv) & full)
        mh = pv & xh
        if ph & top:
            score += 1
        elif mh & top:
            score -= 1
        ph = (ph << 1) & full
        mh = (mh << 1) & full
        pv = mh | (~(xv | ph) & full)
        mv = ph & xv
        if score < best:
            best, best_end = score, j + 1
    return best, best_end


def best_line_cer(ref_folded: str, hyp_folded: list[str], cache: dict | None = None) -> float:
    """score.best_line_cer on already folded strings, with a length lower bound to skip work."""
    if not ref_folded:
        return 0.0
    n = len(ref_folded)
    best = 1.0 if not hyp_folded else math.inf
    for h in sorted(hyp_folded, key=lambda h: abs(len(h) - n)):
        if abs(len(h) - n) / n >= best:
            break
        key = (ref_folded, h)
        if cache is not None and key in cache:
            d = cache[key]
        else:
            d = lev(ref_folded, h)
            if cache is not None:
                cache[key] = d
        best = min(best, d / n)
        if best == 0:
            break
    return min(best, math.inf)


# --- candidate API ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Obs:
    """One stored window text: a frame_windows row (role app, text not NULL)."""

    ts_ms: int
    frame_id: int
    window_id: int
    source: str  # 'uia' | 'ocr' | 'ocr_deferred'
    text: str
    title: str | None
    url: str | None
    monitor: str  # 'primary' | 'left' | 'right' | 'm<id>'
    focused: bool
    boxes: list | None = None  # line boxes: not stored by Akashic today, always None on replays


@dataclass(slots=True)
class Ctx:
    run: str
    hwnd: int
    process: str | None
    app: str  # app type: browser, chat, launcher, terminal, editor, files, game, ...
    title: str | None  # of the first observation
    url: str | None
    monitor: str
    shared: object = None  # prepare()'s result, plus anything process() keeps across windows


@dataclass(slots=True)
class KeptLine:
    text: str
    frame_id: int  # the frame this line links to
    first_ts: int  # span the line stands for (its moments); the link frame's ts lies in it
    last_ts: int
    doc: Hashable  # FTS document key; lines with one key form one row, joined by '\n' in order
    kind: str = "content"  # 'content' and 'chrome' are indexed; 'noise' is stored, not indexed


@dataclass(slots=True)
class StoredText:
    lines: list[KeptLine]
    doc_meta: dict = field(default_factory=dict)  # doc -> (title, url); default: the link frame's


# --- lexicon and token classes ---------------------------------------------------------------------

TECH = set(
    "www http https com org net fr io html htm exe dll py rs js ts json txt md pdf png jpg jpeg gif webp mp4 mkv "
    "url ui ux app apps id ok wifi usb cpu gpu ram ssd hdd fps hp xp ps pc os vs api sdk ai faq rss dns ip tcp "
    "utf csv xml css sql cmd cd dir ls git src lib bin dev min max etc vs inc ltd tv hd uhd hdr dlc npc pvp pve "
    "gg lol ptdr mdr wtf btw imo dm pm am".split()
)
SINGLE_WORDS = set("a i y l d s n j c m t o".split()) - {"o"}
SYLLABLES = ("zor", "vex", "kal", "mun", "tir", "bol", "qua", "dren", "fyl", "gro", "hesk", "nim", "pax", "wul", "jek", "rho",
             "sab", "tov", "uln", "yem", "cid", "lop", "bav", "nur", "dax", "fel", "gim", "hok", "reb", "sil", "tay", "vun")
NONCE_RE = re.compile(f"(?:{'|'.join(SYLLABLES)}){{8}}")
CAMEL_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _load_site_module():
    """eval/quality/site.py under a private name (the stdlib already owns 'site')."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("quality_site", QUALITY / "site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Lexicon:
    """Token classes for the noise metric: harness | word | ident | unknown (+ wordlike/garbled)."""

    def __init__(self, harness_strings: Iterable[str] = ()) -> None:
        words_file = HERE / "lexicon" / "words.txt"
        self.words: set[str] = set()
        self.fleurs: set[str] = set()
        for line in words_file.read_text(encoding="utf-8").splitlines():
            w, _, src = line.partition("\t")
            self.words.add(w)
            if "fleurs" in src:
                self.fleurs.add(w)
        self.words |= TECH | SINGLE_WORDS
        self.harness: set[str] = set()
        for s in [*CORPUS, *harness_strings]:
            self.harness.update(fts_tokens(s))
        self.harness = {t for t in self.harness if not t.isdigit()}
        self._cache: dict[str, str] = {}
        self._bigram()

    def _bigram(self) -> None:
        train = [w for w in self.fleurs | {t for s in CORPUS for t in fts_tokens(s)} if w.isalpha() and len(w) >= 2]
        cnt: Counter = Counter()
        ctx: Counter = Counter()
        alphabet: set[str] = set()
        for w in train:
            s = f"^{w}$"
            alphabet.update(s)
            for a, b in zip(s, s[1:], strict=False):
                cnt[a, b] += 1
                ctx[a] += 1
        self._cnt, self._ctx, self._v = cnt, ctx, len(alphabet) + 1
        scores = sorted(self.bigram_score(w) for w in train if len(w) >= 3)
        self.bigram_threshold = scores[int(0.02 * len(scores))]

    def bigram_score(self, w: str) -> float:
        s = f"^{w}$"
        return sum(math.log((self._cnt[a, b] + 1) / (self._ctx[a] + self._v)) for a, b in zip(s, s[1:], strict=False)) / (len(s) - 1)

    def classify(self, tok: str) -> str:
        c = self._cache.get(tok)
        if c is not None:
            return c
        f = fold_token(tok)
        if f in self.harness or NONCE_RE.fullmatch(f):
            c = "harness"
        elif f in self.words:
            c = "word"
        elif f.isdigit():
            c = "ident"
        elif any(ch.isdigit() for ch in f) and len(f) <= 16 and len(re.findall(r"\d+|\D+", f)) <= 4:
            c = "ident"
        elif re.fullmatch(r"[0-9a-f]{7,}", f) and any(ch.isdigit() for ch in f):
            c = "ident"
        else:
            parts = CAMEL_RE.findall(tok)
            if len(parts) >= 2 and "".join(parts) == tok and all(fold_token(p) in self.words for p in parts):
                c = "ident"
            else:
                c = "unknown"
        self._cache[tok] = c
        return c

    def garbled(self, tok: str) -> bool:
        """An unknown token that does not even look like a FR/EN word (proper nouns usually do)."""
        f = fold_token(tok)
        if len(f) < 3 or not f.isalpha() or not re.search(r"[aeiouy]", f):
            return True
        return self.bigram_score(f) < self.bigram_threshold

    def unknown_chars(self, line: str) -> tuple[int, int]:
        """(chars in unknown tokens, chars in all tokens) of a line."""
        u = t = 0
        for tok in tokens(line):
            t += len(tok)
            if self.classify(tok) == "unknown":
                u += len(tok)
        return u, t


CORPUS: list[str] = []


def init_corpus() -> None:
    if not CORPUS:
        sentences = json.loads((QUALITY / "assets" / "sentences.json").read_text(encoding="utf-8"))
        for v in sentences.values():
            CORPUS.extend(v)
        qs = _load_site_module()
        for v in qs.TITLES.values():
            CORPUS.extend(v)
        CORPUS.extend([qs.CODE, *qs.SERIES, "Related", "Chapter", "Reply by user", "Ask anything about your day.",
                       "profile-a profile-b profile-c", "Play", "Picture in picture", "Description:", "Comment:", "Channel",
                       "walkthrough", "quality harness text", "quality-folder", "rust-project", "main.rs",
                       'fn main() { println!("quality harness"); }', "quality termfeed", "#general"])


# --- store and truth -------------------------------------------------------------------------------


@dataclass(slots=True)
class Window:
    hwnd: int
    ctx: Ctx
    history: list[Obs]
    wid: str | None  # harness window id (truth), None if unknown


@dataclass(slots=True)
class Store:
    run_id: str
    conn: object
    windows: list[Window]  # in order of first observation
    order: list[tuple[int, Obs]]  # (hwnd, obs) for every text row, by (ts, window id)
    span_ms: int
    fts_store_bytes: int  # frame_windows_fts_* tables in the real store
    text_store_bytes: int
    score_order: list[int]  # window ids in eval/quality/score.py m3's row order (its M3.1 sample)


def load_store(run_id: str, truth: RunTruth | None = None) -> Store:
    import score

    _, _, conn, _ = score.open_store(run_id)
    names = score.monitor_names(conn)
    rows = conn.execute(
        "SELECT w.id, w.frame_id, f.ts_ms, f.monitor_id, w.hwnd, w.process, w.title, w.url, w.focused, w.text, w.text_source"
        " FROM frame_windows w JOIN frames f ON f.id = w.frame_id"
        " WHERE w.role = 'app' AND w.text IS NOT NULL ORDER BY f.ts_ms, w.id"
    ).fetchall()
    by_hwnd: dict[int, Window] = {}
    order: list[tuple[int, Obs]] = []
    for wid_, fid, ts, mon, hwnd, proc, title, url, focused, text, src in rows:
        o = Obs(ts, fid, wid_, src, text, title, url, names.get(mon, f"m{mon}"), bool(focused))
        order.append((hwnd, o))
        w = by_hwnd.get(hwnd)
        if w is None:
            w = Window(hwnd, Ctx(run_id, hwnd, proc, app_type(proc), title, url, o.monitor),
                       [], truth.hwnd_to_wid.get(hwnd) if truth else None)
            by_hwnd[hwnd] = w
        w.history.append(o)
    span = conn.execute("SELECT max(ts_ms) - min(ts_ms) FROM frames").fetchone()[0] or 1
    fts_bytes = sum(conn.execute(f"SELECT coalesce(sum(length({c})), 0) FROM {t}").fetchone()[0]
                    for t, c in (("frame_windows_fts_data", "block"), ("frame_windows_fts_idx", "term"), ("frame_windows_fts_docsize", "sz")))
    text_bytes = sum(len(o.text.encode("utf-8")) for _, o in order)
    score_order = [r[0] for r in conn.execute(  # the exact statement of score.m3, for the same sample
        "SELECT w.id, w.hwnd, f.ts_ms, w.text, w.text_source FROM frame_windows w JOIN frames f ON f.id = w.frame_id"
        " WHERE w.role = 'app' AND w.text IS NOT NULL ORDER BY f.ts_ms")]
    return Store(run_id, conn, list(by_hwnd.values()), order, span, fts_bytes, text_bytes, score_order)


@dataclass(frozen=True, slots=True)
class TLine:
    tid: str
    kind: str  # 'content' | 'hchrome' (harness page header/aside/title)
    text: str
    folded: str


class RunTruth:
    def __init__(self, run_id: str) -> None:
        import score

        init_corpus()
        self.run_id = run_id
        self.tr = tr = score.Truth(QUALITY / "results" / run_id)
        self.hwnd_to_wid: dict[int, str] = dict(tr.hwnd_to_window)
        self.lines: dict[str, list[TLine]] = defaultdict(list)
        self.line_by_id: dict[str, TLine] = {}
        strings: list[str] = []
        seen_chrome: set[tuple[str, str]] = set()

        def add(wid: str, tid: str, kind: str, text: str) -> None:
            tl = TLine(tid, kind, text, fold(text))
            if not tl.folded:
                return
            self.lines[wid].append(tl)
            self.line_by_id.setdefault(tid, tl)
            strings.append(text)

        for e in tr.pages:
            w = e.get("w", "")
            if "#private" in w:
                continue
            if e.get("type") == "lines":
                for ln in e["lines"]:
                    add(w, ln["id"], "content", ln["text"])
                for block in e.get("chrome", []):
                    for part in block.split("\n"):
                        if part.strip() and (w, part) not in seen_chrome:
                            seen_chrome.add((w, part))
                            add(w, f"chrome:{w}:{len(seen_chrome)}", "hchrome", part)
                if e.get("title") and (w, e["title"]) not in seen_chrome:
                    seen_chrome.add((w, e["title"]))
                    add(w, f"title:{w}:{len(seen_chrome)}", "hchrome", e["title"])
            elif e.get("type") == "title" and (w, e["title"]) not in seen_chrome:
                seen_chrome.add((w, e["title"]))
                add(w, f"title:{w}:{len(seen_chrome)}", "hchrome", e["title"])
            elif e.get("type") == "video_built":
                strings.append(e.get("title", ""))
        term_wid = next((w["id"] for w in tr.scn["windows"] if w["app"] == "terminal"), None)
        from common import read_jsonl  # eval/quality/common.py

        term_file = QUALITY / "results" / run_id / "truth" / "terminal.jsonl"
        for e in read_jsonl(term_file) if term_file.is_file() else []:
            if e.get("type") == "line" and term_wid:
                add(term_wid, f"term:{e['id']}", "content", e["text"])
        qs = _load_site_module()
        seed = tr.scn["seed"]
        for w in tr.scn["windows"]:
            n = qs.nonce(seed, w["id"])
            strings += [f"quality-{n}-rust-project", f"quality-{n}.txt", n]
        self.strings = strings
        # M3.3: the dwelled lines and their nonce (score.m3)
        visible = score.visible_reports(tr)
        self.visible = visible
        self.report_times = {wid: [t for t, _ in reps] for wid, reps in visible.items()}
        shown_long = [i for i in score.dwelled_lines(visible, score.shown_spans(tr), tr.t1) if i in self.line_by_id]
        self.shown_long = sorted(shown_long)
        # one query per dwelled line id, as score.m3 counts them (cycled 'more' lines share a nonce)
        self.nonces = [self.line_by_id[i].text.split()[-1] for i in self.shown_long]
        self.shown_spans = score.shown_spans(tr)
        self.focus_params = {s["id"]: s["params"] for s in tr.scn["steps"] if s["kind"] == "focus"}
        self.span_ms = tr.t1 - tr.t0

    def visible_at(self, wid: str | None, ts: int) -> list[str]:
        """Line ids the page reported on screen at `ts` (last report at or before ts)."""
        if wid is None:
            return []
        times = self.report_times.get(wid, [])
        i = bisect.bisect_right(times, ts) - 1
        return self.visible[wid][i][1] if i >= 0 else []

    def visible_near(self, wid: str | None, ts: int, tol_ms: int = 1500) -> set[str]:
        """Ids visible at ts or at a report within tol_ms either side (reports lag paint by ~250 ms)."""
        if wid is None:
            return set()
        times = self.report_times.get(wid, [])
        out = set(self.visible_at(wid, ts))
        lo, hi = bisect.bisect_left(times, ts - tol_ms), bisect.bisect_right(times, ts + tol_ms)
        for i in range(max(0, lo - 1), hi):
            out.update(self.visible[wid][i][1])
        return out


# --- truth labelling of stored lines ----------------------------------------------------------------


class Labeler:
    """Which harness-rendered line (if any) a stored line is a copy or fragment of.

    A stored line S (folded) belongs to truth line T of its window when S matches a substring of T
    within 25 % edits of len(S) (wrapped/partial OCR lines), or T matches a substring of S within
    25 % of len(T) (S joins T with neighbours). Lines shorter than 6 folded chars only match a whole
    truth string. Returns (tid, kind, start, end) with [start, end) the covered span of T."""

    def __init__(self, truth: RunTruth) -> None:
        self.truth = truth
        self._idx: dict[str, tuple[list[TLine], dict[str, list[int]], dict[str, int]]] = {}
        self._cache: dict[tuple[str, str], tuple | None] = {}

    def _index(self, wid: str):
        got = self._idx.get(wid)
        if got is None:
            tl = self.truth.lines.get(wid, [])
            inv: dict[str, list[int]] = defaultdict(list)
            whole: dict[str, int] = {}
            for i, t in enumerate(tl):
                whole.setdefault(t.folded, i)
                for tok in set(t.folded.split()):
                    if len(tok) >= 4:
                        inv[tok].append(i)
            got = (tl, inv, whole)
            self._idx[wid] = got
        return got

    def label(self, wid: str | None, line: str) -> tuple | None:
        if wid is None:
            return None
        s = fold(line)
        key = (wid, s)
        if key in self._cache:
            return self._cache[key]
        res = self._label(wid, s)
        self._cache[key] = res
        return res

    def _label(self, wid: str, s: str) -> tuple | None:
        tl, inv, whole = self._index(wid)
        if not s or not tl:
            return None
        if s in whole:
            t = tl[whole[s]]
            return (t.tid, t.kind, 0, len(t.folded))
        if len(s) < 6:
            return None
        votes: Counter = Counter()
        for tok in set(s.split()):
            post = inv.get(tok)
            if post and len(post) <= 400:
                for i in post:
                    votes[i] += 1
        best = None
        for i, _ in votes.most_common(6):
            t = tl[i]
            if len(s) <= len(t.folded):
                d, end = lev_search(s, t.folded)
                if d <= max(1, 0.25 * len(s)):
                    r = (d / len(s), t.tid, t.kind, max(0, end - len(s)), end)
                    best = r if best is None or r < best else best
            else:
                d, _ = lev_search(t.folded, s)
                if d <= max(1, 0.25 * len(t.folded)):
                    r = (d / len(t.folded), t.tid, t.kind, 0, len(t.folded))
                    best = r if best is None or r < best else best
        return None if best is None else best[1:]


# --- near-duplicate line index (diagnosis class b) -------------------------------------------------


class NearIndex:
    """Lines already seen in one window: exact, digit-masked, or within a few edits."""

    def __init__(self) -> None:
        self.exact: set[str] = set()
        self.masked: set[str] = set()
        self.lines: list[str] = []
        self.tok: dict[str, list[int]] = defaultdict(list)
        self.ends: dict[str, list[int]] = defaultdict(list)

    @staticmethod
    def _norm(line: str) -> str:
        return " ".join(line.casefold().split())

    def has_exact(self, line: str) -> bool:
        return line in self.exact

    def near(self, line: str) -> bool:
        n = self._norm(line)
        if mask_digits(n) in self.masked:
            return True
        if len(n) < 8:
            return False
        bound = max(2, math.ceil(0.1 * len(n)))
        votes: Counter = Counter()
        for t in set(n.split()):
            if len(t) >= 3:
                post = self.tok.get(t)
                if post and len(post) <= 300:
                    for i in post[-200:]:
                        votes[i] += 1
        for k in (n[:6], n[-6:]):
            for i in self.ends.get(k, [])[-50:]:
                votes[i] += 1
        for i, _ in votes.most_common(8):
            o = self.lines[i]
            if abs(len(o) - len(n)) <= bound and lev(o, n) <= bound:
                return True
        return False

    def add(self, line: str) -> None:
        if line in self.exact:
            return
        self.exact.add(line)
        n = self._norm(line)
        self.masked.add(mask_digits(n))
        i = len(self.lines)
        self.lines.append(n)
        for t in set(n.split()):
            if len(t) >= 3:
                self.tok[t].append(i)
        if len(n) >= 8:
            self.ends[n[:6]].append(i)
            self.ends[n[-6:]].append(i)


# --- FTS ---------------------------------------------------------------------------------------------


def new_fts(contentless: bool = False) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    opts = ", content=''" if contentless else ""
    c.execute(f"CREATE VIRTUAL TABLE t USING fts5(text, title, url{opts}, tokenize='unicode61 remove_diacritics 2')")
    return c


def fts_bytes(c: sqlite3.Connection) -> int:
    """Index bytes of table t (segments, term index, doc sizes), after flushing pending terms."""
    c.commit()
    c.execute("INSERT INTO t (t) VALUES ('optimize')")
    c.commit()
    return sum(c.execute(f"SELECT coalesce(sum(length({col})), 0) FROM {t}").fetchone()[0]
               for t, col in (("t_data", "block"), ("t_idx", "term"), ("t_docsize", "sz")))


def keyed_hash(text: str, key: bytes = b"ocr-dup-noise-harness") -> str:
    import hashlib
    import hmac

    return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()[:12]
