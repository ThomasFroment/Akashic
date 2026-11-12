"""R2rules (QD-R2-C1 + QD-R2-C2 on top of C3fix), rules only; model_time_text is ignored.

Additions over C3fix (frozen sha 6380a8b0...):
- C1 closed slot grammar: static, offline-verified variant table (head-word variants in
  PART_WORDS / RE_DAY / RE_WEEKEND*, phrase variants in RE_NIGHTS / RE_RECENT / RE_AGO; each
  checked by verify_variants.py in 20 carrier questions x 5 nows against its canonical form), a
  modifier slot for "tard/late" ("hier soir tard" = FR night), "the night before last",
  "Xh du matin/soir" setting the 12-hour half, extra part governors ("around lunchtime"),
  "earlier this week".
- runtime abbreviation folding: apocope of part-of-day heads only (never weekdays: "le jeu" is
  not jeudi), bare after a deictic governor (ce/this/hier/weekday) and apostrophe-terminated
  after an article ("le mat'"); "WE" initials after a governor (capitals after an EN governor);
  day skeletons (ajd, auj, yday, ystd) as static entries.
- governed "WE" / "Week-end" are exempt from C3fix's mid-sentence capital rule unless the
  governor itself is capitalized ("SZA's The Weekend" stays a title).
- C2 gazetteer veto: a RAM-only set of 2-4-grams of captured titles (captured_titles.py stands in
  for the capture tables) that hold a time word plus a content word (or two separate anchors:
  "last week tonight"); a lexed anchor overlapping such an n-gram of the question is dropped
  before fusion. Loader: gazetteer(), rebuilt when its key changes (here: env CTW_GAZ =
  off|public|set and the scored set; in production: PRAGMA data_version / own-write counter, per
  process, checked per question).
- C2 structural fallback (no gazetteer needed): determiner + deictic ("the tonight show"); bare
  midnight/noon word + lower-case content word ("minuit chretiens"); article-governed part word +
  lower-case content word ("la nuit americaine"), exempt when governed by a deictic or followed
  by a capitalized name ("Ce soir Minlo m'a parle ..."); EN weekday after "episode of" or a watch
  verb with no wh-object.
- "dans la nuit" without a day word follows the "cette nuit" rule.
- a sentence-initial "That" is a determiner, not a relative opener.

C3fix's description follows.

C3fix: anchor lexer with clause attachment, on top of a logical-day phrase table (C1 table).

Deterministic, standard library only. Pipeline:

1. normalize (accents stripped, lower case, same length as the input so spans map back);
2. lex every time anchor (day word, part of day, clock time, or a complete special window such
   as "tout à l'heure", "il y a 2 heures", "ce week-end"), longest match first;
3. drop topic anchors: stoplist; a time word capitalized in mid-sentence; a time word inside
   quotes; a noun-attached time word inside a noun phrase opened by a determiner capitalized in
   mid-sentence ("sur Les 12 coups de midi");
4. fuse anchors separated only by filler words into one day ∩ part ∩ clock anchor;
5. tier each anchor: 0 adverbial, 1 attached to a noun (de/d'/du/from/of), 2 inside a clause
   opened by that/qui/que/dont/quand/when...;
6. pick the comma clause: the last (asking) clause if it holds a tier 0/1 anchor, else the
   first clause holding one (the dislocated head: "Le stream de ce soir, ..., c'était quoi ?");
   within it, lowest tier, then the model's time_text span, then leftmost;
7. merge missing slots from tier-0 anchors of the same clause, or of a clause that is only the
   time phrase ("Samedi, j'ai regardé quoi vers 22h ?"), that conflict on no slot;
8. build the window on the logical day L = date(now - 5 h), past preference (step back one
   occurrence if the start is not in the past), in-progress ends clipped to now.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

DAY_START = 5 * 60  # minutes; the logical day runs 05:00 to 05:00
HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)

# Part-of-day table, minutes of the logical day D (>= 1440 is on D+1).
PARTS = {
    "day": (5 * 60, 29 * 60),
    "early_morning": (3 * 60, 9 * 60),
    "morning": (6 * 60, 12 * 60),
    "noon": (11 * 60 + 30, 14 * 60),
    "afternoon": (12 * 60, 18 * 60),
    "evening": (18 * 60, 26 * 60),
    "night_fr": (22 * 60, 30 * 60),
    "night_en": (18 * 60, 30 * 60),
    "after_midnight": (24 * 60, 30 * 60),
}

# --- normalization ------------------------------------------------------------------------
_SRC = "àáâãäåçèéêëìíîïñòóôõöùúûüýÿÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝœŒæÆ’‘`´"
_DST = "aaaaaaceeeeiiiinooooouuuuyyAAAAAACEEEEIIIINOOOOOUUUUYoOaA''''"
_TABLE = str.maketrans(_SRC, _DST)


def _norm(text: str) -> str:
    return text.translate(_TABLE).lower()


# --- lexicon --------------------------------------------------------------------------------
NUM = {
    "un": 1, "une": 1, "one": 1, "a": 1, "an": 1, "deux": 2, "two": 2, "trois": 3, "three": 3,
    "quatre": 4, "four": 4, "cinq": 5, "five": 5, "six": 6, "sept": 7, "seven": 7, "huit": 8,
    "eight": 8, "neuf": 9, "nine": 9, "dix": 10, "ten": 10, "douze": 12, "twelve": 12,
    "quinze": 15, "fifteen": 15, "vingt": 20, "twenty": 20, "trente": 30, "thirty": 30,
    "quarante": 40, "forty": 40, "cinquante": 50, "fifty": 50,
}  # fmt: skip
_N = r"(\d{1,3}|" + "|".join(sorted(NUM, key=len, reverse=True)) + r")"
WD_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
WD_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WD = "|".join(WD_FR + WD_EN)

# Topic phrases (removed before lexing), extended from timeparse.py's stoplist.
STOPLIST = re.compile(
    r"\b(?:trains? de nuit|night trains?|night shifts?|morning sickness|night mode|mode nuit"
    r"|boites? de nuit|night ?clubs?|good morning|good night|bonne nuit|bonne soiree"
    r"|bonne journee|bon apres-midi|happy hour)\b"
)

GOV_PART = (
    r"(?:(?P<gov>aux alentours du|aux alentours de la|autour du|autour de la|a l'heure du"
    r"|vers le|vers la|vers l'|around|towards|toward|en fin de|en debut de|fin de|debut de|au cours de la|au cours de l'|dans la|dans l'"
    r"|pendant la|pendant l'|pendant le|durant la|durant l'|durant le|early this|late this|in the"
    r"|during the|over the|tot ce|during|over"
    r"|tot cette|de la|cette|cet|ce|this|that|le|la|l'|en|au|du|de|d'|a|at|early|late)\s*)?"
)
PART_WORDS = {
    "matinee": "morning", "matin": "morning", "morning": "morning",
    "apres-midi": "afternoon", "apres midi": "afternoon", "aprems": "afternoon",
    "aprem": "afternoon", "afternoon": "afternoon",
    "midi": "noon", "lunchtime": "noon", "lunch": "noon", "dejeuner": "noon",
    "pause dej": "noon", "dej": "noon",
    "soiree": "evening", "soir": "evening", "evening": "evening", "tonight": "evening",
    "nuit": "night_fr", "night": "night_en",
    # verified head-word variants
    "arvo": "afternoon", "apres-m": "afternoon",
    "lunch time": "noon", "lunch-time": "noon",
    "mornin": "morning", "tonite": "evening", "2nite": "evening", "2night": "evening",
}  # fmt: skip
TONIGHT = ("tonight", "tonite", "2nite", "2night")
_PW = "|".join(sorted(PART_WORDS, key=len, reverse=True)).replace(" ", r"\s")
RE_PART = re.compile(r"\b" + GOV_PART + r"(?P<w>" + _PW + r")\b")

RE_DAY = [
    (re.compile(r"\b(?:the )?day before yesterday\b|\bavant[- ]?hier\b"), "daybefore"),
    (re.compile(r"\b(?:earlier today|so far today|plus tot aujourd'hui|aujourd'hui|aujourd hui"
                r"|aujourdhui|today|ajd|auj|aujd|ojd|2day)\b"), "today"),
    (re.compile(r"\b(?:hier|yesterday|yday|ystd|yest|ystrdy)\b"), "yesterday"),
]  # fmt: skip
RE_WD = re.compile(
    r"\b(?:(?:on|last|this past|le|ce)\s+)?(?P<wd>" + _WD + r")(?:\s+(?:dernier|passe))?\b"
)
# (regex, day, part): complete night/evening phrases, including the "tard/late" modifier slot
RE_NIGHTS = [
    (re.compile(r"\blast (?:night|nite)\b"), "yesterday", "night_en"),
    (re.compile(r"\blate last (?:night|nite)\b|\bla nuit (?:derniere|passee)\b"
                r"|\bhier,? (?:tard )?dans la nuit\b|\btard hier soir\b"
                r"|\bhier soir,? (?:assez |tres )?tard\b"
                r"|\bhier,? tard (?:le soir|dans la soiree|en soiree)\b"
                r"|\blate yesterday (?:evening|night)\b"
                r"|\byesterday,? late (?:in the evening|at night)\b"), "yesterday", "night_fr"),
    (re.compile(r"\b(?:the )?night before last\b"), "daybefore", "night_en"),
    (re.compile(r"\bla nuit d'avant[- ]?hier\b|\bavant[- ]?hier,? (?:tard )?dans la nuit\b"
                r"|\btard avant[- ]?hier soir\b|\bavant[- ]?hier soir,? tard\b"),
     "daybefore", "night_fr"),
    (re.compile(r"\btard ce soir\b|\bce soir,? (?:assez |tres )?tard\b|\blate tonight\b"
                r"|\blate this evening\b"), "today", "night_fr"),
]  # fmt: skip
RE_AFTER_MIDNIGHT = re.compile(
    r"\b(?:apres|after|past|passe|passed) (?:minuit|midnight)\b|\bminuit passe\b"
)

GOV_CLK = (r"(?:(?:vers les|vers|a|at around|at about|at|around|about|autour de"
           r"|aux alentours de|aux environs de|sur le coup de|sur les coups de|sur les|by)\s+)?")
RE_CLK_FR = re.compile(r"\b" + GOV_CLK + r"(?P<h>\d{1,2})\s?h(?:(?P<m>\d{2})\b|\b)")
RE_CLK_FR2 = re.compile(r"\b(?:vers|a|autour de|aux alentours de|sur les)\s+(?P<h>\d{1,2}) heures?\b")
RE_CLK_EN = re.compile(
    r"\b" + GOV_CLK + r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s?(?P<ap>am|pm|a\.m\.|p\.m\.)"
    r"|\b(?:at|around|about|by|vers|a|autour de)\s+(?P<h2>\d{1,2}):(?P<m2>\d{2})\b"
    r"|\b" + GOV_CLK + r"(?P<h3>\d{1,2}) o'clock\b"
)
# "Xh du matin/soir", "3 in the morning", "10 at night": the clock with its 12-hour half
RE_CLK_HALF = re.compile(
    r"\b(?P<h>\d{1,2})\s?h(?P<m>\d{2})?\s+(?P<half>du matin|du mat'?|du soir"
    r"|de l'apres[- ]midi|de l'aprem|de la nuit)(?![a-z])"
    r"|\b(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?(?: o'clock)?\s+(?P<half2>in the morning"
    r"|in the afternoon|in the evening|at night)\b"
)
RE_NOON = re.compile(
    r"\b(?:(?:at|around|about|by)\s+)?(?:noon|midday)\b"
    r"|\b(?:vers|autour de|aux alentours de|sur les) midi\b"
)
RE_MIDNIGHT = re.compile(
    r"\b(?:(?:at|around|about|vers|a|autour de|aux alentours de|sur les)\s+)?(?:minuit|midnight)\b"
)

RE_RECENT = re.compile(
    r"\b(?:tout|toute|tt)[- ]a[- ]l'?heure\b|\ba (?:little )?(?:bit )?while ago\b"
    r"|\ba (?:little )?bit ago\b|\b(?:il y a|y a|y'a|ya) (?:un )?(?:petit )?moment\b"
    r"|\bil y a peu\b|\bjust now\b|\ba l'instant\b"
    r"|\b(?:un peu )?plus tot\b(?! (?:ce|cet|cette|aujourd|dans|hier|today|this|que))"
    r"|\bearlier\b(?! (?:than|this|today|tonight|in the))"
)
_UNIT = r"(?P<u>hours?|heures?|hrs?|h|minutes?|mins?|min|mn|days?|jours?)"
RE_LAST_EN = re.compile(
    r"\b(?:(?:in|over|during|within|for)\s+)?(?:the\s+)?(?:last|past)\s+(?:(?P<n>"
    + _N[1:-1] + r")\s+)?" + _UNIT + r"\b"
)
RE_LAST_FR = re.compile(
    r"\b(?:(?:dans|pendant|durant|sur)\s+)?(?:les|ces|la|cette)\s+(?:(?P<n>" + _N[1:-1]
    + r")\s+)?dernier(?:e|s|es)?\s+" + _UNIT + r"\b"
    r"|\bdepuis\s+(?P<n2>" + _N[1:-1] + r")\s+(?P<u2>heures?|minutes?|jours?)\b"
)
RE_AGO = re.compile(
    r"\b(?:il y a|y a|y'a|ya)\s+(?P<n>" + _N[1:-1] + r")\s*" + _UNIT + r"\b"
    r"|\b(?P<n2>" + _N[1:-1] + r")\s+(?P<u2>hours?|hrs?|minutes?|mins?|days?)\s+ago\b"
)
RE_HALF = re.compile(r"\b(?:il y a|y a|y'a) une demi[- ]heure\b|\b(?:half an|a half) hour ago\b")
RE_WEEK = re.compile(
    r"\b(?P<this>cette semaine|this week)\b(?![- ]?end)"
    r"|\b(?P<last>la semaine (?:derniere|passee)|last week)\b(?![- ]?end)"
    r"|\b(?P<past>(?:the |this )?past week)\b(?![- ]?end)"
)
RE_WEEKEND = re.compile(
    r"\b(?:(?P<g>ce|this past|the past|this last|this|le|the|over the|during the|pendant le"
    r"|durant le|au cours du|du|last)\s+)?"
    r"(?:week[- ]?ends?|wkend)\b(?P<post>\s+(?:dernier|passe|precedent))?"
)
# initials / abbreviations: governed only; lower-case "we" only after a French governor
RE_WEEKEND_ABBR = re.compile(
    r"\b(?P<g>ce|le|du|pendant le|durant le|au cours du|this past|this|over the|last)\s+"
    r"(?P<w>we|w-e|wknd)\b(?P<post>\s+(?:dernier|passe|precedent))?"
)
WEEKEND_LAST = ("last", "this past", "the past", "this last")
FR_GOV_WE = ("ce", "le", "du", "pendant le", "durant le", "au cours du")

RE_OPENER = re.compile(
    r"\b(?:that|which|who|when|while|after|before|since|until|qui|que|qu'|dont|ou|quand"
    r"|lorsque|lorsqu'|auquel|auxquels|auxquelles|duquel)(?=[\s,?!.]|$|(?<=')\w)"
)
RE_AUX = re.compile(
    r"(?:did|do|does|was|were|is|are|have|has|had|can|could|would|will|est-ce|est|ai|as|a)$"
)
RE_NOMINAL = re.compile(r"(?:\bde|\bd'|\bdu|\bfrom|\bof)\s*$")
FILLER = re.compile(
    r"^(?:[\s,]|\b(?:a|at|in|the|le|la|l'|en|de|d'|du|vers|around|about|dans|on|during"
    r"|pendant|durant|au|sur)\b)*$"
)
EN_PROPER = set(WD_EN)
FR_WORDS = set(WD_FR) | {"hier", "aujourd'hui", "matin", "soir", "nuit", "midi", "minuit"}


class Tok:
    __slots__ = ("day", "end", "gov", "governed", "ntok", "part", "clock", "special", "start",
                 "title")  # fmt: skip

    def __init__(self, start: int, end: int, **kw: object) -> None:
        self.start, self.end = start, end
        self.day = kw.get("day")
        self.part = kw.get("part")
        self.clock = kw.get("clock")
        self.special = kw.get("special")
        self.gov = kw.get("gov")
        self.governed = kw.get("governed", True)
        self.ntok = 1
        self.title = False


def _num(s: str | None, default: int = 1) -> int:
    if not s:
        return default
    return int(s) if s.isdigit() else NUM.get(s, default)


def _unit(u: str) -> str:
    return "d" if u[0] in "dj" else ("m" if u[0] == "m" else "h")


# --- apocope folding (part-of-day heads only; generated once from the heads, static) ---------
_HEADS = {
    "matin": "morning", "matinee": "morning", "soir": "evening", "soiree": "evening",
    "nuit": "night_fr", "aprem": "afternoon", "dejeuner": "noon", "morning": "morning",
    "afternoon": "afternoon", "evening": "evening", "lunchtime": "noon", "night": "night_en",
}  # fmt: skip
# real words that are also head prefixes (or would cross a weekday apocope): never folded
_APOCOPE_BLOCK = {"after", "even", "soi", "mid", "lun", "apr", "nig", "nigh", "mor", "eve"}


def _apocopes() -> dict[str, str]:
    seen: dict[str, set[str]] = {}
    for head, part in _HEADS.items():
        for n in range(3, len(head)):
            seen.setdefault(head[:n], set()).add(part)
    return {
        p: next(iter(parts))
        for p, parts in seen.items()
        if len(parts) == 1 and p not in _APOCOPE_BLOCK and p not in PART_WORDS
    }


APOCOPE = _apocopes()
_DEICTIC_GOV = ("ce", "cet", "cette", "this", "tot ce", "tot cette", "early this", "late this")
_DAY_GOV = r"hier|yesterday|yday|avant[- ]?hier|aujourd'hui|ajd|today|" + _WD
_WEAK_GOV = (r"dans la|dans l'|pendant la|pendant le|pendant l'|durant la|durant le|de la|de l'"
             r"|in the|during the|le|la|l'|du|au|en")  # fmt: skip
RE_APOCOPE = re.compile(
    r"\b(?P<w>" + "|".join(sorted(APOCOPE, key=len, reverse=True)) + r")(?P<apo>')?(?![a-z0-9'-])"
)
RE_DAY_GOV = re.compile(_DAY_GOV)
RE_APO_GOV = re.compile(
    r"\b(?P<gov>tot ce|tot cette|early this|late this|cet|cette|ce|this|" + _DAY_GOV + r"|"
    + _WEAK_GOV + r")(?:\s+|(?<='))$"
)


def _lex(t: str, orig: str | None = None) -> list[Tok]:  # noqa: C901, PLR0912, PLR0915
    toks: list[Tok] = []
    add = toks.append
    for rx, kind in RE_DAY:
        for m in rx.finditer(t):
            add(Tok(m.start(), m.end(), day=kind))
    for m in RE_WD.finditer(t):
        name = m["wd"]
        k = (WD_FR.index(name) if name in WD_FR else WD_EN.index(name))
        add(Tok(m.start(), m.end(), day=("wd", k)))
    for rx, day, part in RE_NIGHTS:
        for m in rx.finditer(t):
            add(Tok(m.start(), m.end(), day=day, part=part))
    for m in RE_AFTER_MIDNIGHT.finditer(t):
        add(Tok(m.start(), m.end(), part="after_midnight"))
    for m in RE_PART.finditer(t):
        w = re.sub(r"\s+", " ", m["w"])
        part = PART_WORDS[w]
        gov = (m["gov"] or "").strip() or None
        if gov in ("tot ce", "tot cette") and t[: m.start()].endswith("plus "):
            gov = gov[4:]  # "plus tot ce matin" = earlier this morning, not early morning
        day = None
        if w in TONIGHT or gov in ("ce", "cet", "cette", "this", "that", "early this",
                                     "late this", "tot ce", "tot cette"):  # fmt: skip
            day = "cette_nuit" if (part == "night_fr" and gov == "cette") else "today"
        if gov in ("early", "tot ce", "tot cette", "early this") and part == "morning":
            part = "early_morning"
        if gov in ("late", "late this", "en fin de", "fin de") and part == "evening":
            pass  # still the evening; the table's evening already runs past midnight
        governed = gov is not None or w in TONIGHT
        add(Tok(m.start(), m.end(), day=day, part=part, gov=gov, governed=governed))
    for m in RE_APOCOPE.finditer(t):
        part = APOCOPE[m["w"]]
        g = RE_APO_GOV.search(t, max(0, m.start() - 24), m.start())
        if g is None:
            continue
        gov = re.sub(r"\s+", " ", g["gov"])
        if gov in _DEICTIC_GOV:
            day = "cette_nuit" if (part == "night_fr" and gov == "cette") else "today"
            if gov in ("tot ce", "tot cette", "early this") and part == "morning":
                part = "early_morning"
            add(Tok(g.start(), m.end(), day=day, part=part, gov=gov))
        elif RE_DAY_GOV.fullmatch(gov):  # "hier mat'": the day word is lexed on its own
            add(Tok(m.start(), m.end(), part=part, gov=gov))
        elif m["apo"]:  # after an article only when written as an apocope ("le mat'")
            add(Tok(g.start(), m.end(), part=part, gov=gov))
    for m in RE_CLK_HALF.finditer(t):
        if m["h"] is not None:
            h, mm, half = int(m["h"]), int(m["m"] or 0), m["half"]
        else:
            h, mm, half = int(m["h2"]), int(m["m2"] or 0), m["half2"]
        if not 1 <= h <= 12 or mm >= 60:
            continue
        if half.endswith(("soir", "midi", "aprem", "afternoon", "evening")):
            h = h % 12 + 12
        elif half.endswith("night"):
            h = h % 12 + (12 if h >= 6 else 0)
        else:  # matin / mat / nuit / morning
            h = h % 12
        add(Tok(m.start(), m.end(), clock=h * 60 + mm))
    for m in RE_CLK_FR.finditer(t):
        h = int(m["h"])
        if h <= 24:
            add(Tok(m.start(), m.end(), clock=(h % 24) * 60 + int(m["m"] or 0)))
    for m in RE_CLK_FR2.finditer(t):
        h = int(m["h"])
        if h <= 24:
            add(Tok(m.start(), m.end(), clock=(h % 24) * 60))
    for m in RE_CLK_EN.finditer(t):
        if m["h"] is not None:
            h, mm = int(m["h"]), int(m["m"] or 0)
            if not 1 <= h <= 12:
                continue
            h = h % 12 + (12 if m["ap"].startswith("p") else 0)
        elif m["h2"] is not None:
            h, mm = int(m["h2"]), int(m["m2"])
        else:
            h, mm = int(m["h3"]), 0
        if h < 24 and mm < 60:
            add(Tok(m.start(), m.end(), clock=h * 60 + mm))
    for m in RE_NOON.finditer(t):
        add(Tok(m.start(), m.end(), clock=12 * 60))
    for m in RE_MIDNIGHT.finditer(t):
        add(Tok(m.start(), m.end(), clock=0))
    for m in RE_RECENT.finditer(t):
        add(Tok(m.start(), m.end(), special=("recent",)))
    for m in RE_LAST_EN.finditer(t):
        add(Tok(m.start(), m.end(), special=("last", _unit(m["u"]), _num(m["n"]))))
    for m in RE_LAST_FR.finditer(t):
        if m["u"] is not None:
            add(Tok(m.start(), m.end(), special=("last", _unit(m["u"]), _num(m["n"]))))
        else:
            add(Tok(m.start(), m.end(), special=("last", _unit(m["u2"]), _num(m["n2"]))))
    for m in RE_AGO.finditer(t):
        n, u = (m["n"], m["u"]) if m["n"] is not None else (m["n2"], m["u2"])
        unit = _unit(u)
        if unit == "d":
            add(Tok(m.start(), m.end(), day=("ago", _num(n))))
        else:
            add(Tok(m.start(), m.end(), special=("ago", unit, _num(n))))
    for m in RE_HALF.finditer(t):
        add(Tok(m.start(), m.end(), special=("ago", "m", 30)))
    for m in RE_WEEK.finditer(t):
        which = "this" if m["this"] else ("last" if m["last"] else "past")
        add(Tok(m.start(), m.end(), special=("week", which)))
    for m in RE_WEEKEND.finditer(t):
        last = m["g"] in WEEKEND_LAST or m["post"] is not None
        add(Tok(m.start(), m.end(), special=("weekend", "last" if last else "this"), gov=m["g"]))
    for m in RE_WEEKEND_ABBR.finditer(t):
        if m["w"] == "we" and m["g"] not in FR_GOV_WE and (
            orig is None or orig[m.start("w") : m.end("w")] != "WE"
        ):
            continue  # "this we ..." is the English pronoun unless written "WE"
        last = m["g"] in WEEKEND_LAST or m["post"] is not None
        add(Tok(m.start(), m.end(), special=("weekend", "last" if last else "this"), gov=m["g"]))
    # longest match first, then leftmost; drop overlaps
    toks.sort(key=lambda k: (k.start, -(k.end - k.start)))
    out: list[Tok] = []
    best_end = -1
    for k in sorted(toks, key=lambda k: (-(k.end - k.start), k.start)):
        if all(k.end <= o.start or k.start >= o.end for o in out):
            out.append(k)
    del best_end
    out.sort(key=lambda k: k.start)
    return out


def _sentence_start(orig: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and orig[i] in " \t\"'«(":
        i -= 1
    return i < 0 or orig[i] in ".?!\n"


def _is_title(orig: str, norm: str, k: Tok) -> bool:
    """A time word written with a capital in mid-sentence is part of a name or title.

    Exemption (R2): a weekend word governed by a lower-case (or sentence-initial) governor
    ("ce WE", "ce Week-end") is a time; "SZA's The Weekend" (capitalized governor) is not.
    """
    if k.special is not None and k.special[0] == "weekend" and k.gov:
        return orig[k.start].isupper() and not _sentence_start(orig, k.start)
    for m in re.finditer(r"[a-z']+", norm[k.start : k.end]):
        word = m[0]
        p = k.start + m.start()
        if not orig[p].isupper() or _sentence_start(orig, p):
            continue
        if word in EN_PROPER:
            continue
        return True
    return False


def _conflict(a: dict, b: dict) -> bool:
    return any(a[s] is not None and b[s] is not None and a[s] != b[s]
               for s in ("day", "part", "clock"))  # fmt: skip


# --- C2: structural fallback ---------------------------------------------------------------
FUNCTION_WORDS = frozenset(
    """
    a an the of in on at to for from by with and or but as is are was were be been it its
    this that these those my your his her our their me you he she we they i m s t re ve ll d
    do did does have has had not no so if than then there here what which who whom when where
    why how about over under after before into onto out up down off just also very all
    le la les l un une des du de d au aux en et ou mais donc or ni car ce cet cette ces mon ma
    mes ton ta tes son sa ses notre nos votre vos leur leurs je tu il elle on nous vous ils
    elles me te se lui y qui que qu quoi dont quand comment pourquoi combien quel quelle quels
    quelles est sont etait ete ai as avons avez ont pas ne plus tres sur sous dans par pour
    avec sans chez vers entre apres avant pendant depuis
    """.split()
)
TIME_TOKENS = (
    frozenset(
        """
        matin matinee soir soiree nuit midi minuit aprem apresmidi hier aujourdhui ajd auj
        morning afternoon evening night tonight tonite today yesterday yday noon midday midnight
        lunch lunchtime dejeuner dej weekend week semaine
        """.split()
    )
    | frozenset(WD_FR)
    | frozenset(WD_EN)
)
MEDIA_NOUNS = frozenset(
    """
    episode episodes ep saison season video videos vid clip clips extrait extraits chanson song
    son track morceau film movie serie series album cover remix version podcast stream trailer
    scene article emission playlist game jeu chaine channel live
    """.split()
)
# words that may follow a time word without making it a name ("midi pile", "la nuit derniere")
_TIME_TAILS = frozenset(
    """
    pile passe passee tapant tapante environ sharp exactly precise precises juste demi trente
    quart dernier derniere precedent precedente meme tot tard suivant suivante venu entier
    entiere complete
    """.split()
)
_WATCH = frozenset(
    "watch watched watching binge binged rewatch rewatched finish finished start started".split()
)
_ARTICLES = ("le", "la", "l'", "de la", "de l'", "du")
_WH = re.compile(r"\b(?:what|which|quel|quelle|quels|quelles|quoi)\b")
# compounds are one gazetteer word ("aujourd'hui", "week-end", "apres-midi")
_RE_WORD = re.compile(r"aujourd'? ?hui|week[- ]?end|apres[- ]midi|[a-z0-9]+")
_DEICTIC_WORDS = frozenset("ce cet cette this last hier yesterday tonight today".split())


def _gwords(t: str) -> list[tuple[str, int, int]]:
    """Gazetteer words of normalized text with their offsets."""
    return [
        (w if w.isalnum() else re.sub(r"[^a-z0-9]", "", w), m.start(), m.end())
        for m in _RE_WORD.finditer(t)
        for w in (m[0],)
    ]


def _next_word(orig: str, t: str, end: int) -> tuple[str, bool] | None:
    """The word right after `end` (spaces only, no punctuation in between) and its case."""
    m = re.match(r" +([a-z][a-z'-]*)", t[end:])
    if m is None:
        return None
    p = end + m.start(1)
    return m[1], orig[p].isupper()


def _prev_words(t: str, start: int, n: int) -> list[str]:
    return re.findall(r"[a-z]+", t[max(0, start - 40) : start])[-n:]


def _structural_topic(orig: str, t: str, k: Tok) -> bool:
    """C2 structural fallback: is this anchor a word of a name rather than a time?"""
    text = t[k.start : k.end]
    prev = _prev_words(t, k.start, 2)
    # (a) a determiner before a deictic adverb: "the tonight show", "the today show"
    if text in ("today", "yesterday", *TONIGHT) and prev and prev[-1] in ("the", "a", "an"):
        return True
    nxt = _next_word(orig, t, k.end)

    def content(w: tuple[str, bool] | None) -> bool:
        return (
            w is not None and not w[1] and w[0] not in FUNCTION_WORDS and "'" not in w[0]
            and w[0] not in TIME_TOKENS and w[0] not in _TIME_TAILS
        )  # fmt: skip

    # (b) a bare midnight/noon word followed by a lower-case content word: "minuit chretiens"
    if text in ("minuit", "midnight", "noon", "midday") and content(nxt):
        return True
    # (c) an article-governed part word + lower-case content word: "la nuit americaine";
    #     never after a deictic governor ("ce soir Minlo ...") or before a capitalized name
    if k.part is not None and k.day is None and k.gov in _ARTICLES and content(nxt):
        return True
    # (d) an English weekday that is the object: "episode of Wednesday", "did I watch wednesday"
    if text in WD_EN:
        if (
            len(prev) == 2
            and prev[0] in ("episode", "episodes", "ep", "season", "saison")
            and prev[1] in ("of", "de", "d'")
        ):
            return True
        if prev and prev[-1] in _WATCH and _WH.search(t) is None:
            return True
    return False


# --- C2: captured-title gazetteer (RAM only) ------------------------------------------------
_GAZ: dict = {"key": None, "grams": frozenset()}
_HERE = Path(__file__).resolve().parent


def _scored_set() -> str:
    """The question file the harness scores (simulation of "the user captured these titles")."""
    path = os.environ.get("CTW_GAZ_SET")
    if not path and "--phrases" in sys.argv:
        path = sys.argv[sys.argv.index("--phrases") + 1]
    p = Path(path or "phrases.jsonl")
    if not p.is_absolute() and not p.exists():
        p = _HERE.parent / p
    return str(p.resolve())


def _title_grams(title: str) -> set[str]:
    """2-4-grams of a title holding a time word and a content word whose time word has no
    deictic governor inside the n-gram ("ce soir ou jamais" is skipped, "soir ou jamais" kept),
    or holding two separate anchors ("last week tonight")."""
    words = [w for w, _, _ in _gwords(_norm(title))]
    out = set()
    for n in (2, 3, 4):
        for i in range(len(words) - n + 1):
            gram = words[i : i + n]
            if not any(w in TIME_TOKENS for w in gram):
                continue
            s = " ".join(gram)
            if len(_groups(s, s, _lex(s, s))) >= 2:  # two separate anchors: "last week tonight"
                out.add(s)
            elif any(w not in FUNCTION_WORDS and w not in TIME_TOKENS for w in gram) and not any(
                a in _DEICTIC_WORDS and b in TIME_TOKENS for a, b in zip(gram, gram[1:])
            ):
                out.add(s)
    return out


def gazetteer() -> frozenset[str]:
    """RAM-only n-gram set of captured titles; rebuilt when its key changes.

    Here the key is (CTW_GAZ mode, scored set); modes: off, public (default), set (public plus
    the scored set's time_free titles). In production the key is the database's
    `PRAGMA data_version` plus the process's own write counter, read per question in every
    process that resolves questions (API and worker), so capture, retention and deletions take
    effect on the next question; the set is never written to disk.
    """
    mode = os.environ.get("CTW_GAZ", "public")
    key = (mode, os.environ.get("CTW_GAZ_SET"), len(sys.argv)) if mode == "set" else (mode,)
    if _GAZ["key"] != key:
        grams: set[str] = set()
        if mode in ("public", "set"):
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "r2_captured_titles", _HERE / "captured_titles.py"
            )
            ct = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ct)
            for title in set(ct.PUBLIC_TITLES):
                grams |= _title_grams(title)
            if mode == "set":
                grams |= {
                    g
                    for g in ct.set_titles(
                        _scored_set(), lambda q: [w for w, _, _ in _gwords(_norm(q))], TIME_TOKENS,
                        FUNCTION_WORDS, MEDIA_NOUNS,
                    )
                    if g in _title_grams(g)  # same filter as a captured title ("last week": no)
                }  # fmt: skip
        _GAZ["key"], _GAZ["grams"] = key, frozenset(grams)
    return _GAZ["grams"]


def _vetoed_spans(t: str, grams: frozenset[str]) -> list[tuple[int, int]]:
    if not grams:
        return []
    words = _gwords(t)
    spans = []
    seen = set()
    for c, (w, _, _) in enumerate(words):
        if w not in TIME_TOKENS:
            continue
        for n in (2, 3, 4):
            for i in range(max(0, c - n + 1), min(c, len(words) - n) + 1):
                if (i, n) in seen:
                    continue
                seen.add((i, n))
                if " ".join(x[0] for x in words[i : i + n]) in grams:
                    spans.append((words[i][1], words[i + n - 1][2]))
    return spans


def _groups(orig: str, t: str, toks: list[Tok], veto=()) -> list[dict]:  # noqa: ANN001
    groups: list[dict] = []
    for k in toks:
        if _in_title(orig, t, k.start, _noun_attached(t, k.start, k.gov)):
            continue  # dropped alone, so it never fuses with a real anchor next to it
        if any(a < k.end and k.start < b for a, b in veto):
            continue  # inside a captured title (C2 gazetteer), dropped before fusion
        if _structural_topic(orig, t, k):
            continue  # C2 structural fallback
        k.title = _is_title(orig, t, k)
        slots = {"day": k.day, "part": k.part, "clock": k.clock}
        g = groups[-1] if groups else None
        if (
            g is not None
            and k.special is None
            and g["special"] is None
            and FILLER.match(t[g["end"] : k.start])
            and not _conflict(g, slots)
        ):
            for s, v in slots.items():
                if v is not None and g[s] is None:
                    g[s] = v
            g["end"] = k.end
            g["ntok"] += 1
            g["title"] |= k.title
            g["ungoverned"] = g["ungoverned"] and not k.governed
            g["govs"].append(k.gov)
            continue
        groups.append({
            **slots, "special": k.special, "start": k.start, "end": k.end, "ntok": 1,
            "title": k.title, "ungoverned": not k.governed, "govs": [k.gov],
        })  # fmt: skip
    keep = []
    for g in groups:
        if g["title"]:
            continue
        # a bare part word ("mode nuit") is a time only when a governor or a day names it
        if g["ungoverned"] and g["day"] is None and g["clock"] is None and g["special"] is None:
            continue
        keep.append(g)
    return keep


def _sub_regions(orig: str, t: str, groups: list[dict]) -> list[tuple[int, int]]:
    starts = {g["start"] for g in groups}
    regions = []
    for m in RE_OPENER.finditer(t):
        p, word = m.start(), m[0]
        if word == "ou" and orig[p : p + 2] != "où":
            continue
        before = t[:p].rstrip()
        if word in ("que", "qu'", "qui") and re.search(r"est[- ]ce$", before):
            continue
        after = t[m.end() :]
        nxt = m.end() + (len(after) - len(after.lstrip()))
        if nxt in starts:  # "quand hier soir", "before noon": a preposition, not a clause
            continue
        words = re.findall(r"[\w'-]+", after)[:3]
        interrogative = bool(words) and (
            RE_AUX.match(words[0]) is not None
            or (word in ("which", "what") and len(words) > 1 and RE_AUX.match(words[1]))
        )
        head = before == "" or (before == "ce" and word in ("que", "qu'", "qui"))
        head = head or _sentence_start(orig, p - (2 if before == "ce" else 0))
        comma = t.find(",", m.end())
        if head:
            if comma < 0 or interrogative or word == "that":  # "That podcast from ...": determiner
                continue
            regions.append((p, comma))
            continue
        if before.endswith(",") and interrogative:
            continue
        regions.append((p, comma if comma >= 0 else len(t)))
    return regions


def _noun_attached(t: str, start: int, gov: str | None) -> bool:
    """The anchor modifies a noun ("la vidéo de ce matin", "the song from last night")."""
    return (
        RE_NOMINAL.search(t[max(0, start - 6) : start]) is not None
        or gov in ("de", "d'", "du", "de la")
    )


RE_DET = re.compile(r"(?:le|la|les|l'|un|une|des|du|the|a|an)$")


def _in_title(orig: str, t: str, i: int, noun: bool) -> bool:
    """A time word inside a quoted span, or a noun-attached one inside a noun phrase opened by a
    determiner capitalized in mid-sentence ("sur Les 12 coups de midi", "Le Journal de 20h")."""
    before = orig[:i]
    if before.count('"') % 2 == 1 or before.rfind("«") > before.rfind("»"):
        return True
    if not noun:
        return False
    for m in reversed(list(re.finditer(r"[^\s,;:?!.\"«»()]+", t[max(0, i - 48) : i]))):
        p = max(0, i - 48) + m.start()
        if re.search(r"[,;:?!.\"«»()]", t[p + len(m[0]) : i]):
            return False  # punctuation between the word and the anchor: not one noun phrase
        word = m[0]
        if RE_OPENER.fullmatch(word):
            return False
        if RE_DET.match(word) and orig[p].isupper() and not _sentence_start(orig, p):
            return True
        if word.startswith("l'") and orig[p].isupper() and not _sentence_start(orig, p):
            return True
    return False


def _clauses(t: str) -> list[int]:
    """Start offsets of the comma-separated clauses."""
    return [0] + [i + 1 for i, c in enumerate(t) if c in ",;"]


def _clause_of(starts: list[int], pos: int) -> int:
    k = 0
    for i, s in enumerate(starts):
        if s <= pos:
            k = i
    return k


def _choose(orig: str, t: str, groups: list[dict], model_span: tuple[int, int] | None) -> dict:
    """Pick the anchor the question is about.

    Tier per anchor: 0 = adverbial of a clause, 1 = attached to a noun (de/d'/du/from/of),
    2 = inside a relative/temporal clause (that/qui/que/dont/when/quand...).
    Clause: the comma-separated clause holding the question's main anchor is the last one (the
    asking clause, "..., did I watch it last night?") when it holds a tier 0/1 anchor; otherwise
    the dislocated head ("Le stream de ce soir, ..., c'était quoi ?"), i.e. the first clause
    holding one. Within the clause: lowest tier, then the model's span, then leftmost.
    """
    regions = _sub_regions(orig, t, groups)
    starts = _clauses(t)
    for g in groups:
        noun = _noun_attached(t, g["start"], g["govs"][0])
        g["tier"] = 2 if any(a <= g["start"] < b for a, b in regions) else int(noun)
        g["clause"] = _clause_of(starts, g["start"])
    pool = [g for g in groups if g["tier"] < 2] or groups
    last = len(starts) - 1
    in_last = [g for g in pool if g["clause"] == last]
    clause = last if in_last else min(g["clause"] for g in pool)
    pool = [g for g in pool if g["clause"] == clause]

    def key(g: dict) -> tuple:
        overlap = model_span is not None and g["start"] < model_span[1] and model_span[0] < g["end"]
        return (g["tier"], not overlap, g["start"])

    best = min(pool, key=key)
    if best["special"] is not None or best["tier"] != 0:
        return best
    chosen = dict(best)
    for g in groups:
        if g is best or g["special"] is not None or g["tier"] != 0 or _conflict(chosen, g):
            continue
        # merge only within the clause, or from a clause that is nothing but the time phrase
        bare = (
            FILLER.match(t[starts[g["clause"]] : g["start"]])
            and FILLER.match(t[g["end"] : (starts + [len(t) + 1])[g["clause"] + 1] - 1])
        )
        if g["clause"] != clause and not bare:
            continue
        for s in ("day", "part", "clock"):
            if chosen[s] is None and g[s] is not None:
                chosen[s] = g[s]
    return chosen


# --- window building ------------------------------------------------------------------------
def _at(d: date, minutes: int, tz) -> datetime:  # noqa: ANN001
    return datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(minutes=minutes)


def _special(sp: tuple, now: datetime, L: date) -> tuple[datetime, datetime] | None:  # noqa: N803
    tz = now.tzinfo
    kind = sp[0]
    if kind == "recent":
        return now - 3 * HOUR, now
    if kind == "ago":
        _, unit, n = sp
        if unit == "h":
            c = now - n * HOUR
            return c - 30 * MINUTE, min(c + 30 * MINUTE, now)
        c = now - n * MINUTE
        return c - 10 * MINUTE, min(c + 10 * MINUTE, now)
    if kind == "last":
        _, unit, n = sp
        span = {"h": HOUR, "m": MINUTE, "d": timedelta(days=1)}[unit] * n
        return now - span, now
    if kind == "week":
        monday = L - timedelta(days=L.weekday())
        if sp[1] == "this":
            return _at(monday, DAY_START, tz), now
        if sp[1] == "last":
            return _at(monday - timedelta(days=7), DAY_START, tz), _at(monday, DAY_START, tz)
        return now - timedelta(days=7), now
    if kind == "weekend":
        wl = L.weekday()
        if wl >= 5:
            sat = L - timedelta(days=wl - 5)
            if sp[1] == "this":
                return _at(sat - timedelta(days=1), 18 * 60, tz), now
            sat -= timedelta(days=7)
        else:
            sat = L - timedelta(days=(wl - 5) % 7)
        end = min(_at(sat + timedelta(days=2), DAY_START, tz), now)
        return _at(sat - timedelta(days=1), 18 * 60, tz), end
    return None


def _window(g: dict, now: datetime) -> tuple[datetime, datetime] | None:
    tz = now.tzinfo
    L = (now - timedelta(minutes=DAY_START)).date()  # noqa: N806
    if g["special"] is not None:
        return _special(g["special"], now, L)
    day, part, clock = g["day"], g["part"], g["clock"]
    step = False
    if (day is None and part != "night_fr") or day == "today":
        d, step = L, True
    elif day == "yesterday":
        d = L - timedelta(days=1)
    elif day == "daybefore":
        d = L - timedelta(days=2)
    elif day is None and part == "night_fr":  # "dans la nuit": the "cette nuit" rule
        d, step = (L if now.hour * 60 + now.minute < DAY_START else L - timedelta(days=1)), True
    elif day == "cette_nuit":
        d, step = (L if now.hour * 60 + now.minute < DAY_START else L - timedelta(days=1)), True
    elif day[0] == "ago":
        d = L - timedelta(days=day[1])
    else:  # ("wd", k): most recent such calendar day strictly before calendar today
        back = (now.date().weekday() - day[1]) % 7 or 7
        d = now.date() - timedelta(days=back)
    if day == "today" and part is None and clock is None:
        return _at(L, DAY_START, tz), now
    a, b = PARTS[part or "day"]
    if clock is not None:
        h = clock
        if part is not None:
            if not a <= h < b and a <= h + 720 < b and h < 720:
                h += 720
            if not a <= h < b and a <= h + 1440 < b:
                h += 1440
            if a <= h < b:
                a, b = h - 30, h + 30
        else:
            if h < DAY_START:
                h += 1440
            a, b = h - 30, h + 30
    s, e = _at(d, a, tz), _at(d, b, tz)
    if step and s >= now:
        s, e = s - timedelta(days=1), e - timedelta(days=1)
    e = min(e, now)
    if s >= e:
        return None
    return s, e


def resolve_question(question: str, now: datetime, model_time_text: str | None):  # noqa: ANN201
    t = _norm(question)
    t = STOPLIST.sub(lambda m: " " * len(m[0]), t)
    toks = _lex(t, question)
    if not toks:
        return None
    groups = _groups(question, t, toks, _vetoed_spans(t, gazetteer()))
    if not groups:
        return None
    del model_time_text  # rules only
    return _window(_choose(question, t, groups, None), now)
