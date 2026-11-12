"""R2slots (QD-R2-C3): grounded slot normaliser in the existing plan call, merged with a rules module.

The model's plan JSON carries ``when`` = {span copied from the question, closed-enum slots day /
part / clock / special, n} (plan_prompt.py). Its output is recorded once per question by
fill_cache.py into model_cache_<set>.jsonl and read back here, so scoring is reproducible.

Harness modes: ``model_time_text is None`` is the rules-only fallback (the rules module unchanged,
no model loaded). Any other value means "the model is loaded": its text is ignored and the cached
real model output for the question is merged with the rules:

1. ground: the span must be a substring of the normalised question, else the model output is
   unusable and the rules decide alone;
2. veto: a span that overlaps a vetoed region (stoplist topic such as "train de nuit", a quoted or
   title-case time word, a capitalised-determiner noun phrase) counts as "no time";
3. model says no time: no rule anchor -> no window; a weak rule anchor (bare weekday / minuit /
   midi / weekend, after a determiner, noun-attached part word) -> no window (veto); a strong one
   -> the rule window (R2_VETO=all vetoes every rule anchor instead);
4. model has a time: rule anchors overlapping its span are fused with it. Slot conflicts: a
   strictly longer model span takes the model's slots (the day is still filled from the rules),
   an equal span takes the model's on conflict, a shorter one the rules'. With no overlapping
   rule anchor, the model's span becomes an anchor of its own ("rules find nothing, model gives
   a grounded span" included);
5. choose: R2_DISJOINT=tiers runs the rules module's own _choose over all anchors (tier, clause,
   then the model span as tiebreak); R2_DISJOINT=model takes the model's anchor outright;
6. build: always the rules module's _window (past preference, clip to now: 0 future windows).

Rules module: env RULES_MODULE (default ../C3fix/candidate.py). Its sha256 must match the entry
for its file name in a frozen.sha256 next to it. It must be C3fix-shaped: _norm, STOPLIST, _lex,
_groups, _choose, _window, _in_title, _is_title, _noun_attached, resolve_question.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import plan_prompt_iter1 as pp  # noqa: E402

DISJOINT = os.environ.get("R2_DISJOINT", "tiers")  # tiers | model
VETO = os.environ.get("R2_VETO", "weak")  # weak | all


# --- rules module ---------------------------------------------------------------------------
def _load_rules() -> ModuleType:
    path = Path(os.environ.get("RULES_MODULE", HERE.parent / "C3fix" / "candidate.py")).resolve()
    frozen = path.parent / "frozen.sha256"
    want = None
    if frozen.exists():
        for line in frozen.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == path.name:
                want = parts[0]
    have = hashlib.sha256(path.read_bytes()).hexdigest()
    if want is None or have != want:
        msg = f"rules module {path} sha256 {have} does not match its frozen.sha256 ({want})"
        raise RuntimeError(msg)
    spec = importlib.util.spec_from_file_location(f"r2rules_{path.parent.name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load_rules()


# --- cached model outputs -------------------------------------------------------------------
def _load_cache() -> dict[str, str]:
    cache: dict[str, str] = {}
    for f in sorted(HERE.glob("model_cache_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["prompt_sha"] == pp.PROMPT_SHA and not rec.get("error"):
                cache[rec["question"]] = rec["raw"]
    return cache


CACHE = _load_cache()

# --- model slots -> rules slots -------------------------------------------------------------
_WD = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_SPECIAL = {
    "recent": lambda n: ("recent",), "last_hours": lambda n: ("last", "h", n),
    "last_minutes": lambda n: ("last", "m", n), "last_days": lambda n: ("last", "d", n),
    "hours_ago": lambda n: ("ago", "h", n), "minutes_ago": lambda n: ("ago", "m", n),
    "this_week": lambda n: ("week", "this"), "last_week": lambda n: ("week", "last"),
    "this_weekend": lambda n: ("weekend", "this"), "last_weekend": lambda n: ("weekend", "last"),
}  # fmt: skip
SLOTS = ("day", "part", "clock", "special")


def _parse_when(raw: str) -> dict | None:
    try:
        data = json.loads(raw)
        w = data["when"]
        ok = (
            isinstance(w["span"], str) and w["day"] in pp.DAYS and w["part"] in pp.PARTS
            and w["special"] in pp.SPECIALS and isinstance(w["clock"], str)
            and isinstance(w["n"], int)
        )  # fmt: skip
    except (ValueError, KeyError, TypeError):
        return None
    return w if ok else None


def _model_slots(w: dict, span_text: str) -> dict:
    n = max(1, w["n"])
    if w["special"] != "none":
        return {"day": None, "part": None, "clock": None, "special": _SPECIAL[w["special"]](n)}
    day = w["day"]
    d: object = None
    if day in ("today", "yesterday", "daybefore"):
        d = day
    elif day in _WD:
        d = ("wd", _WD.index(day))
    elif day == "days_ago":
        d = ("ago", n)
    part = w["part"]
    p = None if part == "none" else part
    if part == "night":
        en = re.search(r"\bnight\b", span_text) and "late" not in span_text
        p = "night_en" if en else "night_fr"
        if d in (None, "today"):
            d = "cette_nuit"
    c = None
    m = re.fullmatch(r"(\d\d):(\d\d)", w["clock"])
    if m:
        c = int(m[1]) * 60 + int(m[2])
    return {"day": d, "part": p, "clock": c, "special": None}


def _empty(s: dict) -> bool:
    return all(s[k] is None for k in SLOTS)


def _combine(win: dict, lose: dict, fill: tuple[str, ...]) -> dict:
    """``win``'s slots, completed by ``lose``'s ``fill`` slots; a special excludes the others."""
    if win["special"] is not None:
        return {"day": None, "part": None, "clock": None, "special": win["special"]}
    if _empty(win):
        return dict(lose)
    out = {**win, "special": None}
    for k in fill:
        if out[k] is None and k != "special":
            out[k] = lose[k]
    return out


# --- vetoes and weak anchors ----------------------------------------------------------------
def _vetoed(q: str, t: str, ts: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in R.STOPLIST.finditer(t)]
    for k in R._lex(ts):  # noqa: SLF001
        noun = R._noun_attached(ts, k.start, k.gov)  # noqa: SLF001
        if R._in_title(q, ts, k.start, noun) or R._is_title(q, ts, k):  # noqa: SLF001
            spans.append((k.start, k.end))
    return spans


_DET = {"the", "a", "an", "le", "la", "les", "un", "une", "des", "aux", "au", "du"}
_GOV = {
    "ce", "cet", "cette", "this", "that", "last", "on", "le", "tonight", "early", "late", "tot",
    "dans", "pendant", "durant", "during", "in", "over", "en", "at", "around", "about", "vers",
    "a", "autour", "aux", "sur", "by", "after", "past", "apres", "depuis",
}  # fmt: skip


def _weak(g: dict, t: str) -> bool:
    """A rule anchor the model may veto: bare or determiner-led time word, noun-attached part."""
    words = re.findall(r"[a-z']+", t[g["start"] : g["end"]])
    first = words[0] if words else ""
    before = re.findall(r"[a-z']+", t[: g["start"]])
    if before and before[-1] in _DET and first not in _GOV:
        return True
    sp = g["special"]
    if sp is not None:
        if sp[0] == "weekend":
            return first not in _GOV and first not in ("the", "du") and not re.search(
                r"dernier|passe|precedent", t[g["start"] : g["end"]])  # fmt: skip
        return False
    day = g["day"]
    if day in ("today", "yesterday", "daybefore", "cette_nuit") or (
        isinstance(day, tuple) and day[0] == "ago"
    ):
        return False
    if isinstance(day, tuple):  # weekday
        fused = g["part"] is not None or g["clock"] is not None
        return not (fused or first in _GOV or re.search(r"dernier|passe", t[g["start"]:g["end"]]))
    return first not in _GOV


# --- merge ----------------------------------------------------------------------------------
def _ground(span: str, t: str) -> tuple[int, int] | None:
    s = R._norm(span).strip(" ,?!.;:")  # noqa: SLF001
    if not s:
        return None
    i = t.find(s)
    return None if i < 0 else (i, i + len(s))


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def merge(question: str, now: datetime, raw: str | None):  # noqa: ANN201, C901, PLR0911
    rules_only = R.resolve_question(question, now, None)
    w = None if raw is None else _parse_when(raw)
    if w is None:
        return rules_only, "unusable"
    t = R._norm(question)  # noqa: SLF001
    ts = R.STOPLIST.sub(lambda m: " " * len(m[0]), t)
    groups = R._groups(question, ts, R._lex(ts))  # noqa: SLF001
    span = None
    if w["span"].strip():
        span = _ground(w["span"], t)
        if span is None:
            return rules_only, "ungrounded"
        if any(_overlap(span, v) for v in _vetoed(question, t, ts)):
            span = None  # a topic / title span is no time
    m = _model_slots(w, t[span[0] : span[1]]) if span else None
    if span is not None and _empty(m):
        return rules_only, "no_slots"
    if span is None:
        if not groups:
            return None, "both_none"
        r = R._choose(question, ts, groups, None)  # noqa: SLF001
        if VETO == "all" or _weak(r, t):
            return None, "veto"
        return R._window(r, now), "rules_strong"  # noqa: SLF001
    over = [g for g in groups if _overlap(span, (g["start"], g["end"]))]
    rest = [g for g in groups if g not in over]
    if over:
        rs = {k: next((g[k] for g in over if g[k] is not None), None) for k in SLOTS}
        a, b = min(g["start"] for g in over), max(g["end"] for g in over)
        lm, lr = span[1] - span[0], b - a
        if lm > lr:
            slots, how = _combine(m, rs, ("day",)), "model_longer"
        elif lm == lr:
            slots, how = _combine(m, rs, SLOTS), "equal"
        else:
            slots, how = _combine(rs, m, SLOTS), "rules_longer"
        g0 = over[0]
        anchor = {**g0, **slots, "start": min(a, span[0]), "end": max(b, span[1]),
                  "ntok": sum(g["ntok"] for g in over)}  # fmt: skip
    else:
        how = "model_only" if not groups else "model_new"
        anchor = {**m, "start": span[0], "end": span[1], "ntok": 1, "title": False,
                  "ungoverned": False, "govs": [None]}  # fmt: skip
    if DISJOINT == "model" or not rest:
        chosen = anchor
    else:
        pool = sorted([*rest, anchor], key=lambda g: g["start"])
        chosen = R._choose(question, ts, pool, span)  # noqa: SLF001
        if chosen["start"] != anchor["start"]:
            how += "+tier_other"
    return R._window(chosen, now), how  # noqa: SLF001


def resolve_question(question: str, now: datetime, model_time_text: str | None):  # noqa: ANN201
    if model_time_text is None:  # rules-only fallback: no model loaded
        return R.resolve_question(question, now, None)
    return merge(question, now, CACHE.get(question))[0]
