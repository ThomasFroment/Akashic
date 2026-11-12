"""C3: anchor lexer with clause attachment, on top of a logical-day phrase table (C1 table).

Deterministic, standard library only. Pipeline:

1. normalize (accents stripped, lower case, same length as the input so spans map back);
2. lex every time anchor (day word, part of day, clock time, or a complete special window such
   as "tout à l'heure", "il y a 2 heures", "ce week-end"), longest match first;
3. drop topic anchors (stoplist; a time word capitalized in mid-sentence is a title);
4. fuse anchors separated only by filler words into one day ∩ part ∩ clock anchor;
5. mark subordinate anchors (inside a clause opened by that/qui/que/dont/quand/when..., or
   attached to a noun by de/d'/du/from/of);
6. choose by the hand-ordered score (main clause, number of lexical tokens, -position); the
   model's time_text only breaks ties among lexed anchors;
7. merge missing slots only from other main-clause anchors that conflict on no slot;
8. build the window on the logical day L = date(now - 5 h), past preference (step back one
   occurrence if the start is not in the past), in-progress ends clipped to now.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

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
    r"(?:(?P<gov>en fin de|en debut de|fin de|debut de|au cours de la|au cours de l'|dans la|dans l'"
    r"|pendant la|pendant l'|durant la|durant l'|early this|late this|in the|during the|tot ce"
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
}  # fmt: skip
_PW = "|".join(sorted(PART_WORDS, key=len, reverse=True)).replace(" ", r"\s")
RE_PART = re.compile(r"\b" + GOV_PART + r"(?P<w>" + _PW + r")\b")

RE_DAY = [
    (re.compile(r"\b(?:the )?day before yesterday\b|\bavant[- ]hier\b"), "daybefore"),
    (re.compile(r"\b(?:earlier today|plus tot aujourd'hui|aujourd'hui|aujourd hui|today)\b"),
     "today"),
    (re.compile(r"\b(?:hier|yesterday|yday)\b"), "yesterday"),
]  # fmt: skip
RE_WD = re.compile(
    r"\b(?:(?:on|last|this past|le|ce)\s+)?(?P<wd>" + _WD + r")(?:\s+(?:dernier|passe))?\b"
)
RE_LASTNIGHT = re.compile(
    r"\b(?P<late>late )?last night\b|\bla nuit (?:derniere|passee)\b|\bhier dans la nuit\b"
)
RE_AFTER_MIDNIGHT = re.compile(r"\b(?:apres|after|past) (?:minuit|midnight)\b")

GOV_CLK = r"(?:(?:vers|a|at|around|about|autour de|aux alentours de|sur les|by)\s+)?"
RE_CLK_FR = re.compile(r"\b" + GOV_CLK + r"(?P<h>\d{1,2})\s?h(?:(?P<m>\d{2})\b|\b)")
RE_CLK_FR2 = re.compile(r"\b(?:vers|a|autour de|aux alentours de|sur les)\s+(?P<h>\d{1,2}) heures?\b")
RE_CLK_EN = re.compile(
    r"\b" + GOV_CLK + r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s?(?P<ap>am|pm|a\.m\.|p\.m\.)"
    r"|\b(?:at|around|about|by)\s+(?P<h2>\d{1,2}):(?P<m2>\d{2})\b"
    r"|\b" + GOV_CLK + r"(?P<h3>\d{1,2}) o'clock\b"
)
RE_NOON = re.compile(
    r"\b(?:(?:at|around|about|by)\s+)?(?:noon|midday)\b"
    r"|\b(?:vers|autour de|aux alentours de|sur les) midi\b"
)
RE_MIDNIGHT = re.compile(
    r"\b(?:(?:at|around|about|vers|a|autour de|aux alentours de|sur les)\s+)?(?:minuit|midnight)\b"
)

RE_RECENT = re.compile(
    r"\btout[- ]a[- ]l'?heure\b|\ba (?:little )?while ago\b|\bil y a (?:un )?(?:petit )?moment\b"
    r"|\bil y a peu\b|\bjust now\b|\ba l'instant\b|\b(?:un peu )?plus tot\b|\bearlier\b(?! than)"
)
_UNIT = r"(?P<u>hours?|heures?|h|minutes?|mins?|min|days?|jours?)"
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
    r"\bil y a\s+(?P<n>" + _N[1:-1] + r")\s*" + _UNIT + r"\b"
    r"|\b(?P<n2>" + _N[1:-1] + r")\s+(?P<u2>hours?|minutes?|mins?|days?)\s+ago\b"
)
RE_HALF = re.compile(r"\bil y a une demi-heure\b|\bhalf an hour ago\b")
RE_WEEK = re.compile(
    r"\b(?P<this>cette semaine|this week)\b(?![- ]?end)"
    r"|\b(?P<last>la semaine (?:derniere|passee)|last week)\b(?![- ]?end)"
    r"|\b(?P<past>(?:the |this )?past week)\b(?![- ]?end)"
)
RE_WEEKEND = re.compile(
    r"\b(?:(?P<g>ce|this|le|the|over the|during the|pendant le|durant le|au cours du|du|last)\s+)?"
    r"week[- ]?ends?\b(?P<post>\s+(?:dernier|passe|precedent))?"
)

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
    return "d" if u[0] in "dj" else ("m" if u.startswith("min") else "h")


def _lex(t: str) -> list[Tok]:  # noqa: C901, PLR0912
    toks: list[Tok] = []
    add = toks.append
    for rx, kind in RE_DAY:
        for m in rx.finditer(t):
            add(Tok(m.start(), m.end(), day=kind))
    for m in RE_WD.finditer(t):
        name = m["wd"]
        k = (WD_FR.index(name) if name in WD_FR else WD_EN.index(name))
        add(Tok(m.start(), m.end(), day=("wd", k)))
    for m in RE_LASTNIGHT.finditer(t):
        part = "night_en" if "last night" in m[0] and not m["late"] else "night_fr"
        add(Tok(m.start(), m.end(), day="yesterday", part=part))
    for m in RE_AFTER_MIDNIGHT.finditer(t):
        add(Tok(m.start(), m.end(), part="after_midnight"))
    for m in RE_PART.finditer(t):
        w = re.sub(r"\s+", " ", m["w"])
        part = PART_WORDS[w]
        gov = (m["gov"] or "").strip() or None
        day = None
        if w == "tonight" or gov in ("ce", "cet", "cette", "this", "that", "early this",
                                     "late this", "tot ce", "tot cette"):  # fmt: skip
            day = "cette_nuit" if (part == "night_fr" and gov == "cette") else "today"
        if gov in ("early", "tot ce", "tot cette", "early this") and part == "morning":
            part = "early_morning"
        if gov in ("late", "late this", "en fin de", "fin de") and part == "evening":
            pass  # still the evening; the table's evening already runs past midnight
        governed = gov is not None or w == "tonight"
        add(Tok(m.start(), m.end(), day=day, part=part, gov=gov, governed=governed))
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
        last = m["g"] == "last" or m["post"] is not None
        add(Tok(m.start(), m.end(), special=("weekend", "last" if last else "this")))
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
    """A time word written with a capital in mid-sentence is part of a name or title."""
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


def _groups(orig: str, t: str, toks: list[Tok]) -> list[dict]:
    groups: list[dict] = []
    for k in toks:
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
            if comma < 0 or interrogative:
                continue
            regions.append((p, comma))
            continue
        if before.endswith(",") and interrogative:
            continue
        regions.append((p, comma if comma >= 0 else len(t)))
    return regions


def _choose(orig: str, t: str, groups: list[dict], model_span: tuple[int, int] | None) -> dict:
    regions = _sub_regions(orig, t, groups)
    for g in groups:
        sub = any(a <= g["start"] < b for a, b in regions)
        sub = sub or RE_NOMINAL.search(t[max(0, g["start"] - 6) : g["start"]]) is not None
        sub = sub or g["govs"][0] in ("de", "d'", "du", "de la")
        g["main"] = not sub
    main = [g for g in groups if g["main"]]
    pool = main or groups

    def score(g: dict) -> tuple:
        overlap = model_span is not None and g["start"] < model_span[1] and model_span[0] < g["end"]
        return (g["ntok"], overlap, -g["start"])

    best = max(pool, key=score)
    if best["special"] is None and best["main"]:
        chosen = dict(best)
        for g in main:
            if g is best or g["special"] is not None or _conflict(chosen, g):
                continue
            for s in ("day", "part", "clock"):
                if chosen[s] is None and g[s] is not None:
                    chosen[s] = g[s]
        return chosen
    return best


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
    if day is None or day == "today":
        d, step = L, True
    elif day == "yesterday":
        d = L - timedelta(days=1)
    elif day == "daybefore":
        d = L - timedelta(days=2)
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
    toks = _lex(t)
    if not toks:
        return None
    groups = _groups(question, t, toks)
    if not groups:
        return None
    model_span = None
    if model_time_text:
        mt = _norm(model_time_text).strip()
        i = t.find(mt) if mt else -1
        if i >= 0:
            model_span = (i, i + len(mt))
    return _window(_choose(question, t, groups, model_span), now)
