"""P2: line store with run documents + aligned-reading consensus (C1 narrowed + C3 decompose + C4 + C5).

Deferred worker (nothing on the capture write path; frame rows are written as today and compacted
later). Kinds map onto the app as:
  'content' -> the run's FTS document (full weight);
  'chrome'  -> one app-day chrome FTS document, bm25 x0.2, kept in Ask evidence as one block;
  'noise'   -> a separate noise_fts table holding the down-ranked tokens with their line id: queried
               only after the main tables (its hits rank after every main hit, and reach Ask evidence
               only when nothing else matches), so a noise-kind token stays findable at its moment.

  1. Window runs (C1 narrowed). Per hwnd and local day, a frame stays in the current run when its
     context (digit-masked title, url) is already in the run or >= RUN_OVERLAP of its lines were in
     the previous frame; otherwise it rejoins an earlier run of the day holding RUN_OVERLAP of its
     lines, else opens a new run. One FTS document per run (title/url columns = the run's distinct
     titles/urls). Each frame row keeps an ordered list of (line id, digit slots) instead of its
     text: the frame is rebuilt from the line store (M3.1 on rebuilt frames, m31_rebuilt.py).
  2. Line tracks (C3, decompose variant, OCR only). Consecutive frames of a run are aligned on line
     ids (difflib anchors over exact keys known in the run); in each gap, unknown new OCR lines pair
     by order with vanished OCR lines of the previous frame when d6_ok: digits-only change in <= 1
     slot (clock, counter), or digits unchanged and digit-masked edit distance within D6 (exact <= 2
     chars, 1 edit <= 6, 2 above). UIA lines
     never pair (no OCR noise: a differing UIA reading is a real change). An unpaired line whose
     exact key is known in the run rejoins that track; else a new track.
  3. Canonical = mode reading (digit-masked group first), finalised at run close. Digits are slots
     stored exactly per frame (C2), so rebuilt frames keep their own numbers. Other readings add
     only their tokens not yet in the run document: kept ('content', verbatim segments) unless the
     garbage rule flags them ('noise').
  4. Chrome (C4 narrowed): per (process, day), a canonical key present in >= max(3, 0.5 N) of the
     process's runs AND >= max(3, 0.5 N) of its (title, url) contexts is chrome: stored once per
     app-day with every visibility interval (sightings kept per hwnd), kind 'chrome'.
  5. Garbage (C5 down-rank) on OCR tokens: a token missing the day's UIA lexicon (HMAC-keyed set in
     the app) that is a 1-2 letter non-function word, fails FR-adapted rmgarbage (N6), or is one
     OCR confusion (l/i, e/c, rn/m, ...) away from a UIA word. The UIA trigram LM exists but is
     gated off (LM_MIN_TYPES): on these runs it flagged real words and harness nonces (M3.3 loss).
     Edge garbage is cut off verbatim; interior garbage is cut when the joined line stays within
     10 % of its length (MODE 'join'), else stays inside the content line.
"""

from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from shared import DIGITS_RE, KeptLine, StoredText, fold_token, lev, mask_digits

WRITE_PATH = False

# --- tunables ---------------------------------------------------------------------------------------
D6_LONG = 2  # edits allowed above 6 chars (D6). 1 had fewer synthetic false merges; 2 has fewer misread tokens on day (2766 vs 2968) at 9 vs 7 dropped lines
D6_RATE = 0.0  # optional length-scaled bound: max(D6_LONG, ceil(rate * n)); 0 = plain D6
MAX_SLOT_CHANGES = 1
DIGIT_GUARD = True  # an OCR-flip pairing must keep every digit
PAIR_LOOKAHEAD = 3  # in a gap, how many vanished lines ahead an unknown line may pair with
PAIR_UIA = False  # UIA text has no OCR noise: a differing UIA reading is a real change, never merged
CHROME_MIN_CTX = 3
CHROME_FRAC_CTX = 0.5
CHROME_NEED_CTX = True  # chrome also needs the spread over distinct (title, url) contexts
RUN_OVERLAP = 0.5  # share of a frame's lines that keeps it in (or rejoins) a run across a context change
MAX_TITLES = 8  # distinct titles / urls written into a run document's title / url columns
N6_MAXLEN = 40
N6_ON = True
USE_LM = True
LM_PCT = 0.02  # share of the UIA training types below the LM threshold
LM_MIN_TYPES = 20000  # below this many UIA types in the day, no LM (N6 and short-token rule only)
MISREAD = True  # OCR-confusion neighbours of known words are garbage
MISREAD_MINLEN = 4
OCR_KNOWN_TRACKS = 0  # >0: an OCR token seen in this many tracks of the day is also a misread target
OCR_CONSENSUS = True  # day-level token consensus: one OCR confusion from a token read in >= 3x as many
# tracks of the day (and >= 3) -> misread (directional, so a real word is not flagged by its misread)
# (off: frequent OCR misreads then turn real words into "misreads", e.g. autre <- autrc)
SHORT_GARBAGE = True  # 1-2 letter OCR tokens missing the UIA lexicon are garbage

EMIT_VARIANTS = True  # analysis switch only
MODE = "join"  # interior garbage of a content line: 'join' | 'edge' (kept inside) | 'split' (segments)


def _env_overrides() -> None:
    """Tuning only: P2_<NAME>=<python literal> overrides a tunable above."""
    import ast
    import os

    g = globals()
    for k, v in os.environ.items():
        if k.startswith("P2_") and k[3:] in g:
            g[k[3:]] = ast.literal_eval(v)


_env_overrides()

TOKEN_RE = re.compile(r"[^\W_]+")
VOWELS = set("aeiouy")
CONS_RUN = re.compile(r"[b-df-hj-np-tv-xz]{6,}")
VOW_RUN = re.compile(r"[aeiouy]{5,}")
REPEAT = re.compile(r"([^\W\d_])\1{3,}")
URLISH = re.compile(r"(?:[a-z]+://|www\.|[\w-]+\.(?:com|org|net|fr|io|exe|dll|py|rs|js|ts|json|txt|md|pdf|png|jpg|html?)\b|[\\/])", re.I)
IDENT = re.compile(r"\d+|\D+")


def norm(line: str) -> str:
    return " ".join(line.split())


def n6_fail(w: str) -> bool:
    """FR-adapted rmgarbage on one whitespace token (rules L, A, R, V, P); URLs, paths and
    identifiers with digits are exempt."""
    if not w:
        return False
    if URLISH.search(w):
        return False
    core = w.strip(".,;:!?\"'()[]{}<>«»“”‘’…-–—")
    if not core:
        return False
    if any(c.isdigit() for c in core) and len(IDENT.findall(core)) <= 4:
        return False  # identifier / number with unit (ident)
    if len(core) > N6_MAXLEN:
        return True  # L
    alnum = sum(c.isalnum() for c in core)
    if len(core) >= 3 and alnum * 2 < len(core):
        return True  # A
    if REPEAT.search(core):
        return True  # R
    letters = fold_token("".join(c for c in core if c.isalpha()))
    if len(letters) >= 3:
        if not any(c in VOWELS for c in letters):
            return True  # V: no vowel
        if CONS_RUN.search(letters) or VOW_RUN.search(letters):
            return True  # V (BnL: consonant / vowel runs)
    inner = core[1:-1]
    punct = {c for c in inner if not c.isalnum() and c not in "'’-."}
    if len(punct) >= 2:
        return True  # P
    if len(core) >= 3 and core[0].islower() and core[-1].islower() and any(c.isupper() for c in core[1:-1]) \
            and not re.fullmatch(r"[a-z]+(?:[A-Z][a-z]+)+", core):
        return True  # mixed case inside a lower word, not camelCase
    return False


class UiaLex:
    """The day's UIA token lexicon (HMAC-keyed set in the app) and a char-trigram LM trained on its
    letter-only types; threshold = the LM_PCT percentile of the training types' own scores."""

    def __init__(self, types: set[str], ocr_known: set[str] | None = None) -> None:
        self.words = types
        self.known = types | (ocr_known or set())  # misread targets
        self.ocr_freq: Counter = Counter()  # folded OCR token -> distinct tracks of the day
        train = [w for w in types if w.isalpha() and len(w) >= 3]
        self.ok = len(train) >= LM_MIN_TYPES
        self.tri: Counter = Counter()
        self.bi: Counter = Counter()
        self.uni: Counter = Counter()
        for w in train:
            s = f"^^{w}$"
            for i in range(2, len(s)):
                self.tri[s[i - 2:i + 1]] += 1
                self.bi[s[i - 1:i + 1]] += 1
                self.uni[s[i]] += 1
        self.ctx2: Counter = Counter()
        self.ctx1: Counter = Counter()
        for k, v in self.tri.items():
            self.ctx2[k[:2]] += v
        for k, v in self.bi.items():
            self.ctx1[k[0]] += v
        self.n = sum(self.uni.values())
        self.v = len(self.uni) + 1
        self.cache: dict[str, bool] = {}
        self.theta = -math.inf
        if self.ok:
            sc = sorted(self.score(w) for w in train)
            self.theta = sc[int(LM_PCT * len(sc))]

    def score(self, w: str) -> float:
        s = f"^^{w}$"
        lp = 0.0
        for i in range(2, len(s)):
            a, b, c = s[i - 2:i], s[i - 1], s[i]
            p3 = self.tri[a + c] / self.ctx2[a] if self.ctx2[a] else 0.0
            p2 = self.bi[b + c] / self.ctx1[b] if self.ctx1[b] else 0.0
            p1 = (self.uni[c] + 1) / (self.n + self.v)
            lp += math.log(0.6 * p3 + 0.3 * p2 + 0.1 * p1)
        return lp / (len(s) - 2)

    def low(self, f: str) -> bool:
        """A letter-only folded token scores below the day's threshold."""
        if not self.ok or not f.isalpha() or len(f) < 3:
            return False
        r = self.cache.get(f)
        if r is None:
            r = self.cache[f] = self.score(f) < self.theta
        return r


FUNCTION_WORDS = set(
    "a y i o l d j n s t c m qu de la le un en et il on ou au du ce se ne je tu me te sa ma ta ni si va vu eu pu lu "
    "ca ça là où ai as es an at be by do go he if in is it my no of oh ok or so to up us we am ll re ve nd rd th "
    "mr dr vs tv pc id ui ia os".split()
)
CONFUSE = {"l": "i", "i": "l", "o": "a", "a": "o", "e": "c", "c": "e", "u": "v", "v": "u", "n": "m", "m": "n", "t": "f",
           "f": "t", "h": "b", "b": "h", "g": "q", "q": "g", "y": "v"}
CONFUSE2 = {"rn": "m", "m": "rn", "cl": "d", "d": "cl", "vv": "w", "w": "vv", "ii": "u", "li": "h"}


def confusion_neighbours(f: str):
    for i, ch in enumerate(f):
        alt = CONFUSE.get(ch)
        if alt:
            yield f[:i] + alt + f[i + 1:]
        if ch in "il":
            yield f[:i] + f[i + 1:]
        for a, b in CONFUSE2.items():
            if f.startswith(a, i):
                yield f[:i] + b + f[i + len(a):]


def consensus_misread(f: str, freq: Counter) -> bool:
    """Token-level consensus over the day's OCR tracks: f is one confusion from a reading that more
    tracks agree on (>= 3 and >= 3x f's own count)."""
    if len(f) < 3 or not f.isalpha():
        return False
    n = freq.get(f, 0)
    need = max(3, 3 * n)
    return any(freq.get(w, 0) >= need for w in confusion_neighbours(f))


def misread_of(f: str, words: set[str]) -> bool:
    """f is one OCR confusion away from a known word: one confusable substitution (l/i, o/a, e/c,
    rn/m, ...) or one stray i/l. A misread never matches the word the user saw, so down-ranking it
    costs no findability."""
    if len(f) < MISREAD_MINLEN or not f.isalpha():
        return False
    for i, ch in enumerate(f):
        alt = CONFUSE.get(ch)
        if alt and f[:i] + alt + f[i + 1:] in words:
            return True
        if ch in "il" and f[:i] + f[i + 1:] in words:
            return True
        for a, b in CONFUSE2.items():
            if f.startswith(a, i) and f[:i] + b + f[i + len(a):] in words:
                return True
    return False


def garbage_reason(t: str, lex: UiaLex, w: str | None = None) -> str | None:
    """Why one FTS token of an OCR reading (w = its whitespace token) is garbage, or None: it misses
    the day's UIA lexicon and is a 1-2 letter token, fails N6, or scores low under the UIA LM."""
    f = fold_token(t)
    if f in lex.words or any(ch.isdigit() for ch in f):
        return None
    if len(f) <= 2:
        if not SHORT_GARBAGE or f in FUNCTION_WORDS or (w is not None and ("'" in w or "’" in w)):
            return None  # FR/EN short words and contraction parts (l', d', 's, 'll ...)
        return "short"
    if N6_ON and n6_fail(w if w is not None else t):
        return "n6"
    if MISREAD and misread_of(f, lex.known):
        return "misread"
    if OCR_CONSENSUS and lex.ocr_freq and consensus_misread(f, lex.ocr_freq):
        return "consensus"
    return "lm" if USE_LM and lex.low(f) else None


def token_garbage(t: str, lex: UiaLex, w: str | None = None) -> bool:
    return garbage_reason(t, lex, w) is not None


def d6_ok(a: str, b: str) -> bool:
    """Same line, re-read: either only digits changed, in at most MAX_SLOT_CHANGES slots (a clock,
    a counter; two rows "Ticket #1020 ... 45.20" / "#1021 ... 47.10" differ in every slot and stay
    apart), or the digits are unchanged and the digit-masked edit distance is within D6."""
    ma, mb = mask_digits(a), mask_digits(b)
    da, db = DIGITS_RE.findall(a), DIGITS_RE.findall(b)
    if ma == mb:
        return sum(x != y for x, y in zip(da, db, strict=False)) <= MAX_SLOT_CHANGES
    if DIGIT_GUARD and da != db:
        return False
    n = max(len(ma), len(mb))
    if n <= 2:
        return False
    bound = 1 if n <= 6 else max(D6_LONG, int(-(-D6_RATE * n // 1)))
    if abs(len(ma) - len(mb)) > bound:
        return False
    return lev(ma, mb) <= bound


def day_of(ts_ms: int) -> int:
    return int(time.strftime("%Y%m%d", time.localtime(ts_ms / 1000)))


def context_key(o) -> tuple:
    return (mask_digits(o.title or ""), o.url or "")


# --- tracking -------------------------------------------------------------------------------------


class Track:
    __slots__ = ("tid", "readings", "sight", "ctxs", "src", "frames", "intervals", "_last_obs")

    def __init__(self, tid: int, src: str) -> None:
        self.tid = tid
        self.readings: dict[str, list] = {}  # reading -> [count, first_fid, first_ts, last_ts]
        self.sight: dict[str, list] = {}  # reading -> [(k, fid, ts)] (only to pick the link frame)
        self.ctxs: set = set()  # contexts (masked title, url) the line was seen in
        self.src = src
        self.frames = 0
        self.intervals: list[list[int]] = []  # [first_ts, last_ts] per visibility interval
        self._last_obs = -2

    def see(self, reading: str, o, k: int, ck) -> None:
        self.ctxs.add(ck)
        self.sight.setdefault(reading, []).append((k, o.frame_id, o.ts_ms))
        r = self.readings.get(reading)
        if r is None:
            self.readings[reading] = [1, o.frame_id, o.ts_ms, o.ts_ms]
        else:
            r[0] += 1
            r[3] = o.ts_ms
        self.frames += 1
        if k == self._last_obs + 1 and self.intervals:
            self.intervals[-1][1] = o.ts_ms
        elif k != self._last_obs:
            self.intervals.append([o.ts_ms, o.ts_ms])
        self._last_obs = k

    def link(self, reading: str) -> int:
        """Link frame of a reading: its first sighting (measured on the day run: the first sighting
        is truth-visible for 87.2 % of lines, the middle of the longest stay 86.4 %)."""
        return self.sight[reading][0][1]

    def canonical(self) -> str:
        groups: dict[str, int] = Counter()
        first: dict[str, int] = {}
        for r, (c, _, ts, _) in self.readings.items():
            m = mask_digits(r)
            groups[m] += c
            first[m] = min(first.get(m, ts), ts)
        g = max(groups, key=lambda m: (groups[m], -first[m]))
        best = max((r for r in self.readings if mask_digits(r) == g), key=lambda r: (self.readings[r][0], -self.readings[r][2]))
        return best

    @property
    def first_ts(self) -> int:
        return min(v[2] for v in self.readings.values())

    @property
    def last_ts(self) -> int:
        return max(v[3] for v in self.readings.values())


class Run:
    __slots__ = ("day", "contexts", "titles", "urls", "tracks", "key_to_track", "obs", "frame_ids", "prev")

    def __init__(self, day: int) -> None:
        self.day = day
        self.contexts: set = set()
        self.titles: dict[str, str] = {}  # masked title -> first title seen
        self.urls: dict[str, None] = {}
        self.tracks: list[Track] = []
        self.key_to_track: dict[str, int] = {}
        self.obs: list = []
        self.frame_ids: list[list[tuple[int, str]]] = []  # per obs: (tid, reading) in order
        self.prev: list[tuple[int, str]] = []


STATS = Counter()


def track_frame(run: Run, o, keys: list[str], k: int, ck=None) -> None:
    """k: index of o in its window's history (interval continuity)."""
    run.obs.append(o)
    sk = "u" if o.source == "uia" else "o"  # UIA lines never join OCR tracks (exact text, no consensus)
    cur_tid = [run.key_to_track.get((sk, x)) for x in keys]
    prev = run.prev
    if prev and (PAIR_UIA or o.source != "uia") and any(t is None for t in cur_tid):
        prev_seq = [t for t, _ in prev]
        cur_seq = [t if t is not None else -(j + 1) for j, t in enumerate(cur_tid)]
        cur_set = set(cur_seq)
        sm = SequenceMatcher(None, prev_seq, cur_seq, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace":
                continue
            cands = [i for i in range(i1, i2) if prev_seq[i] not in cur_set and run.tracks[prev_seq[i]].src != "uia"]
            p = 0
            for j in range(j1, j2):
                if cur_tid[j] is not None:
                    continue
                for q in range(p, min(len(cands), p + PAIR_LOOKAHEAD)):
                    i = cands[q]
                    if d6_ok(prev[i][1], keys[j]):
                        cur_tid[j] = prev_seq[i]
                        STATS["paired"] += 1
                        if mask_digits(prev[i][1]) == mask_digits(keys[j]):
                            STATS["paired_digits"] += 1
                        p = q + 1
                        break
                    STATS["pair_reject"] += 1
    out = []
    for j, x in enumerate(keys):
        t = cur_tid[j]
        if t is None:
            t = run.key_to_track.get((sk, x))
        if t is None:
            t = len(run.tracks)
            run.tracks.append(Track(t, o.source))
        run.key_to_track.setdefault((sk, x), t)
        run.tracks[t].see(x, o, k, ck)
        out.append((t, x))
    run.frame_ids.append(out)
    run.prev = out


def frame_keys(o) -> list[str]:
    return [k for k in (norm(x) for x in o.text.splitlines()) if k]


def assign_runs(hist) -> list[Run]:
    """Window runs of one hwnd: a frame stays in the current run when its context is already in the
    run or when at least RUN_OVERLAP of its lines were in the previous frame; otherwise it rejoins
    an earlier run of the day that already holds RUN_OVERLAP of its lines, else opens a new run."""
    runs: list[Run] = []
    cur: Run | None = None
    prev_keys: set[str] = set()
    for g, o in enumerate(hist):
        day = day_of(o.ts_ms)
        ck = context_key(o)
        keys = frame_keys(o)
        ks = set(keys)
        need = RUN_OVERLAP * max(1, len(ks))
        r = None
        if cur is not None and cur.day == day and (ck in cur.contexts or len(ks & prev_keys) >= need):
            r = cur
        else:
            for old in reversed(runs):
                hit = sum(("o", k) in old.key_to_track or ("u", k) in old.key_to_track for k in ks) >= need if ks else ck in old.contexts
                if old.day == day and hit:
                    r = old
                    STATS["run_rejoin"] += 1
                    break
        if r is None:
            r = Run(day)
            runs.append(r)
        r.contexts.add(ck)
        r.titles.setdefault(ck[0], o.title or "")
        if o.url:
            r.urls.setdefault(o.url, None)
        track_frame(r, o, keys, g, ck)  # alignment continues from the run's own last frame
        cur, prev_keys = r, ks
    return runs


# --- deferred pass ----------------------------------------------------------------------------------


def prepare(windows):
    STATS.clear()
    types: dict[int, set[str]] = defaultdict(set)  # day -> folded UIA tokens (HMAC-keyed in the app)
    for hist, _ in windows:
        for o in hist:
            if o.source == "uia":
                types[day_of(o.ts_ms)].update(fold_token(t) for t in TOKEN_RE.findall(o.text))
    ocr_tracks: dict[int, Counter] = defaultdict(Counter)
    runs_by_hwnd: dict[int, list[Run]] = {}
    for hist, ctx in windows:
        runs_by_hwnd[ctx.hwnd] = assign_runs(hist)
    for runs in runs_by_hwnd.values():
        for r in runs:
            for tr in r.tracks:
                if tr.src != "uia":
                    ocr_tracks[r.day].update({fold_token(t) for rd in tr.readings for t in TOKEN_RE.findall(rd)})
    lexicon = {d: UiaLex(types.get(d, set()), {f for f, n in ocr_tracks[d].items() if n >= OCR_KNOWN_TRACKS}
                         if OCR_KNOWN_TRACKS else set())
               for d in set(types) | set(ocr_tracks)}
    for d, lx in lexicon.items():
        lx.ocr_freq = ocr_tracks.get(d, Counter())
    # chrome by context spread per (process, day): spread = distinct window runs holding the key
    proc_of = {ctx.hwnd: (ctx.process or "").lower() for _, ctx in windows}
    n_runs: Counter = Counter()
    key_runs: dict[tuple, set] = defaultdict(set)
    key_ctxs: dict[tuple, set] = defaultdict(set)
    n_ctxs: dict[tuple, set] = defaultdict(set)
    canon: dict[tuple[int, int, int], str] = {}
    for hwnd, runs in runs_by_hwnd.items():
        for ri, r in enumerate(runs):
            pd = (proc_of[hwnd], r.day)
            n_runs[pd] += 1
            for tr in r.tracks:
                c = canon[hwnd, ri, tr.tid] = tr.canonical()
                key_runs[pd, mask_digits(c)].add((hwnd, ri))
                key_ctxs[pd, mask_digits(c)].update(tr.ctxs)
                n_ctxs[pd].update(tr.ctxs)
    chrome_keys = set()
    for (pd, mk), rs in key_runs.items():
        nc = len(key_ctxs[pd, mk])
        if len(rs) >= max(CHROME_MIN_CTX, CHROME_FRAC_CTX * n_runs[pd]) and (
                not CHROME_NEED_CTX or nc >= max(CHROME_MIN_CTX, CHROME_FRAC_CTX * len(n_ctxs[pd]))):
            chrome_keys.add((pd, mk))
    out: dict[int, StoredText] = {h: StoredText([], {}) for h in runs_by_hwnd}
    chrome_entries: dict[tuple, dict] = {}  # (pd, masked) -> best canonical, intervals, readings
    rebuild: dict[int, list[tuple[int, list[str]]]] = {}  # hwnd -> [(ts, lines)] (exact frame rebuild)
    for hwnd, runs in runs_by_hwnd.items():
        st = out[hwnd]
        rb = rebuild[hwnd] = []
        for ri, r in enumerate(runs):
            doc = ("run", hwnd, ri)
            st.doc_meta[doc] = (" | ".join(list(r.titles.values())[:MAX_TITLES]) or None, " ".join(list(r.urls)[:MAX_TITLES]) or None)
            lex = lexicon.get(r.day) or UiaLex(set())
            pd = (proc_of[hwnd], r.day)
            doc_toks: set[str] = set()
            doc_lines: dict[str, list] = {}  # one stored line per text per run (UIA and OCR tracks may share it)
            canon_r = {}
            for tr in r.tracks:
                c = canon[hwnd, ri, tr.tid]
                canon_r[tr.tid] = c
                mk = mask_digits(c)
                if READ_LOG is not None:
                    READ_LOG.extend((hwnd, rd, c, (pd, mk) in chrome_keys, len(tr.readings)) for rd in tr.readings)
                if (pd, mk) in chrome_keys:
                    e = chrome_entries.get((pd, mk))
                    if e is None:
                        e = chrome_entries[pd, mk] = {"frames": 0, "intervals": [], "readings": Counter(), "first": {}}
                    e["intervals"] += [(hwnd, a, b) for a, b in tr.intervals]
                    for rd, (cnt, fid, f_ts, l_ts) in tr.readings.items():
                        e["readings"][rd] += cnt
                        if rd not in e["first"] or f_ts < e["first"][rd][1]:
                            e["first"][rd] = (hwnd, f_ts, tr.link(rd), l_ts, tr.src)
                    continue
                emit_track(st, doc, tr, c, lex, doc_toks, doc_lines)
            for o, ids in zip(r.obs, r.frame_ids, strict=True):
                rb.append((o.ts_ms, [fill_slots(canon_r[t], rd) for t, rd in ids]))
    # chrome documents: one per (process, day)
    for (pd, mk), e in chrome_entries.items():
        doc = ("chrome",) + pd
        rd_best = max(e["readings"], key=lambda x: (e["readings"][x], -e["first"][x][1]))
        hwnd, f_ts, fid, _, src = e["first"][rd_best]
        first_ts = min(a for _, a, _ in e["intervals"])
        last_ts = max(b for _, _, b in e["intervals"])
        lex = lexicon.get(pd[1]) or UiaLex(set())
        st = out[hwnd]
        st.doc_meta.setdefault(doc, (None, None))
        cd = chrome_doc_toks.setdefault(doc, set())
        emit_line(st, doc, rd_best, fid, first_ts, last_ts, src, lex, cd, kind="chrome")
        for rd, (h2, f2, fid2, l2, src2) in sorted(e["first"].items(), key=lambda kv: kv[1][1]):
            if rd == rd_best:
                continue
            emit_variant(out[h2], doc, rd, fid2, f2, l2, src2, lex, cd, kind="chrome")
        STATS["chrome_intervals"] += len(e["intervals"])
    STATS["chrome_keys"] = len(chrome_entries)
    STATS["tracks"] = sum(len(r.tracks) for rs in runs_by_hwnd.values() for r in rs)
    STATS["runs"] = sum(len(rs) for rs in runs_by_hwnd.values())
    STATS["intervals"] = sum(len(t.intervals) for rs in runs_by_hwnd.values() for r in rs for t in r.tracks)
    chrome_doc_toks.clear()
    return {"out": out, "rebuild": rebuild, "stats": dict(STATS), "lexicon": lexicon, "ocr_tracks": ocr_tracks}


chrome_doc_toks: dict = {}


def fill_slots(canonical: str, reading: str) -> str:
    """Frame rebuild: the canonical template with this frame's own digit slots."""
    if reading == canonical:
        return canonical
    slots = DIGITS_RE.findall(reading)
    if len(slots) != len(DIGITS_RE.findall(canonical)):
        return canonical
    it = iter(slots)
    return DIGITS_RE.sub(lambda _: next(it), canonical)


READ_LOG: list | None = None  # analysis hook: (hwnd, reading, canonical, chrome, n_readings)
LINK_LOG: list | None = None  # analysis hook: (hwnd, canonical, link fid, sightings)
FLAG_LOG: list | None = None  # analysis hook: (token, 'line'|'variant', flagged) when a list
ROLE_LOG: dict | None = None  # analysis hook: id(KeptLine) -> "variant"


def _spans(reading: str, take) -> tuple[list[tuple[int, int]], list[tuple[int, int, str]]]:
    """Walk the FTS tokens of `reading`; take(tok, ws_token) -> 'keep' | 'junk' | None (skip).
    Returns the kept segments (runs of consecutive kept tokens, as reading spans) and the junk
    tokens with their spans."""
    segs, junk = [], []
    start = end = None
    for m in re.finditer(r"\S+", reading):
        w = m.group()
        for t in TOKEN_RE.finditer(w):
            what = take(t.group(), w)
            if what == "keep":
                if start is None:
                    start = m.start() + t.start()
                end = m.start() + t.end()
                continue
            if start is not None:
                segs.append((start, end))
                start = None
            if what == "junk":
                junk.append((m.start() + t.start(), m.start() + t.end(), t.group()))
    if start is not None:
        segs.append((start, end))
    return segs, junk


def _segments(reading: str, take) -> tuple[list[str], list[str]]:
    segs, junk = _spans(reading, take)
    return [reading[a:b] for a, b in segs], [t for _, _, t in junk]


def emit_line(st, doc, text, fid, first_ts, last_ts, src, lex, doc_toks, kind="content") -> list:
    """A canonical line, with its garbage tokens moved to one 'noise' line. Edge garbage is cut off
    (the rest stays verbatim); interior garbage is cut when the joined line stays within the link
    tolerance (<= 10 % of its length removed, JOIN), else the line is kept whole (EDGE_ONLY) or
    split into its verbatim segments."""
    if src == "uia" or not TOKEN_RE.search(text):
        segs, junk = [text], []
    else:
        def take(t, w):
            g = garbage_reason(t, lex, w)
            if FLAG_LOG is not None:
                FLAG_LOG.append((t, "line", g))
            return "junk" if g else "keep"

        spans, jspans = _spans(text, take)
        if not jspans:
            segs, junk = [text], []
        elif not spans:
            segs, junk = [], [t for _, _, t in jspans]
        else:
            a, b = spans[0][0], spans[-1][1]
            inner_junk = [j for j in jspans if a <= j[0] < b]
            edge_junk = [t for x, _, t in jspans if not a <= x < b]
            joined = " ".join(text[x:y] for x, y in spans)
            if len(spans) == 1:
                segs, junk = [text[a:b]], edge_junk
            elif MODE == "join" and (b - a) - len(joined) <= max(1, 0.1 * len(joined)):
                segs, junk = [joined], [t for _, _, t in jspans]
            elif MODE in ("join", "edge"):
                segs, junk = [text[a:b]], edge_junk  # interior garbage stays in the content line
                STATS["inner_junk_kept"] += len(inner_junk)
            else:
                segs, junk = [text[x:y] for x, y in spans], [t for _, _, t in jspans]
    n0 = len(st.lines)
    for sg in segs:
        st.lines.append(KeptLine(sg, fid, first_ts, last_ts, doc, kind))
        doc_toks.update(fold_token(t) for t in TOKEN_RE.findall(sg))
    if junk:
        st.lines.append(KeptLine(" ".join(junk), fid, first_ts, last_ts, doc, "noise"))
        STATS["noise_lines" if not segs else "noise_split"] += 1
    return st.lines[n0:]


def emit_variant(st, doc, reading, fid, first_ts, last_ts, src, lex, doc_toks, kind="content") -> None:
    """A minority reading: only its tokens not yet in the document; digit tokens and N6-passing
    tokens are kept (verbatim segments), garbage tokens go to one 'noise' line."""
    if not EMIT_VARIANTS:
        return
    def take(t, w):
        f = fold_token(t)
        if f in doc_toks:
            return None
        doc_toks.add(f)
        g = garbage_reason(t, lex, w) if src != "uia" else None
        if FLAG_LOG is not None:
            FLAG_LOG.append((t, "variant", g))
        return "junk" if g else "keep"

    segs, junk = _segments(reading, take)
    for sg in segs:
        st.lines.append(KeptLine(sg, fid, first_ts, last_ts, doc, kind))
        if ROLE_LOG is not None:
            ROLE_LOG[id(st.lines[-1])] = "variant"
        STATS["variant_lines"] += 1
    if junk:
        st.lines.append(KeptLine(" ".join(junk), fid, first_ts, last_ts, doc, "noise"))
        STATS["variant_noise_lines"] += 1


def emit_track(st, doc, tr: Track, c: str, lex, doc_toks, doc_lines=None) -> None:
    if LINK_LOG is not None:
        LINK_LOG.append((doc[1], c, tr.link(c), tr.sight[c]))
    if doc_lines is not None and c in doc_lines:
        for kl in doc_lines[c]:  # same text already stored in this run: widen its span
            kl.first_ts, kl.last_ts = min(kl.first_ts, tr.first_ts), max(kl.last_ts, tr.last_ts)
    else:
        kls = emit_line(st, doc, c, tr.link(c), tr.first_ts, tr.last_ts, tr.src, lex, doc_toks)
        if doc_lines is not None:
            doc_lines[c] = kls
    for rd, (cnt2, fid2, f2, l2) in sorted(tr.readings.items(), key=lambda kv: kv[1][2]):
        if rd != c:
            emit_variant(st, doc, rd, tr.link(rd), f2, l2, tr.src, lex, doc_toks)


def process(history, ctx):
    sh = ctx.shared
    return sh["out"].get(ctx.hwnd, StoredText([], {}))
