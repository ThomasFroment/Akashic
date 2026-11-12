"""C3 Single joint shift bit (KISS), with the refuter's correction.

One bit s decides the whole question: s = 1 (topic shift) gives the standalone plan unchanged;
s = 0 applies today's field-wise "or" (planner.follow_up) to the cue-stripped standalone plan.

    s = no_cue and (complete or (own.kinds and own.kinds disjoint from earlier.kinds))

complete = own time and (own kinds or own people or an overview request). The short-question
rule of cue class F (<= 2 content tokens, zero anchors) only applies to questions that are not
complete, so it can no longer defeat "What did I watch yesterday?" (refuter correction).
Cue classes P, T, W, F, X are C1's. Keyword stripping (cue words, pronouns, weekday/month
names) only decides whether the keyword field counts as filled; a filled field keeps the
standalone value. Veto: own kinds without conversation => people do not carry.
planner.follow_up itself is not modified.
"""

from __future__ import annotations

import re
from dataclasses import replace

from akashic.search.planner import _STOPWORDS, _WORD, Plan
from akashic.search.timeparse import normalize

_TIME_UNIT = r"(?:matin|soir|soiree|nuit|semaine|mois|annee|apres midi|week|morning|afternoon|evening|year|month)"
_P = re.compile(r"\b(?:he|she|him|his|her|they|them|il|elle|ils|elles|lui|leur|eux|son|sa|ses)\b")
_P_FIXED = re.compile(r"\bil (?:y a|faut|fait)\b")
_T = re.compile(
    r"\b(?:it|ca|cela|celui|celle|ceux|celles|dessus)\b"
    r"|\bthat\b(?!\s+(?:i|you|he|she|we|they|it|the|a|an|my)\b)"
    rf"|\b(?:this|cette|cet|ces)\b(?!\s+{_TIME_UNIT})"
    r"|\b(?:il|elle|ils|elles|on|je|j|tu|nous|vous|m|t)\s+(?:en|y)\b(?!\s+a\b)"
)
_W = re.compile(
    r"\b(?:then|afterwards?|later|ensuite|puis)\b|\b(?:after|before) (?:that|this|it)\b"
    r"|\b(?:right|just|juste) (?:after|before|apres|avant)\b|\b(?:that|same) (?:day|time)\b"
    r"|\bapres\b(?! midi)|\bavant\b(?! hier)|\bce jour la\b|\bmeme (?:jour|moment)\b"
    r"|\b(?:a|en) (?:ce|meme) (?:moment|temps)\b|\bpendant ce temps\b"
)
_F_LEAD = re.compile(
    r"^\W*(?:and|et|also|aussi|but|mais|so|donc|or|ou|sinon|what about|how about|et pour)\b"
)
_CONTINUE = re.compile(
    r"^\W*(?:(?:and )?tell me more|more|go on|keep going|continue|why|pourquoi|et apres|"
    r"and then|dis m en (?:plus|davantage)|raconte m en plus|en dis plus)\W*$"
)
_X = re.compile(r"\b(?:else|other|another|autre|autres|rien d autre)\b")
_STRIP = frozenset(
    """he she him his her they them il elle ils elles lui leur eux son sa ses it ca cela celui
    celle ceux celles dessus then after afterwards later right same day time ensuite puis apres
    avant jour meme moment temps more tell why pourquoi continue plus else other another autre
    autres rien anything monday tuesday wednesday thursday friday saturday sunday lundi mardi
    mercredi jeudi vendredi samedi dimanche january february march april may june july august
    september october november december janvier fevrier mars avril mai juin juillet aout
    septembre octobre novembre decembre""".split()
)


def plan_thread(question, earlier_question, earlier_plan, standalone_plan, names, now):  # noqa: ANN001, ANN201, ARG001, PLR0913
    q, s, e = normalize(question), standalone_plan, earlier_plan
    if _CONTINUE.search(q):
        return e  # a pure continuation request copies the earlier plan
    own_kw = [w for w in s.keywords if w not in _STRIP]
    complete = s.time is not None and bool(s.kinds or s.people or s.overview)
    content = [w for w in _WORD.findall(q) if len(w) >= 3 and w not in _STOPWORDS]  # noqa: PLR2004
    short = not complete and (len(content) <= 2 or not (s.time or s.people or s.kinds or len(own_kw) > 1))  # noqa: PLR2004
    pronoun = not s.people and bool(_P.search(_P_FIXED.sub(" ", q)))
    cue = pronoun or bool(_T.search(q) or _W.search(q) or _F_LEAD.search(q) or _X.search(q)) or short
    shift = not cue and (complete or bool(s.kinds and not set(s.kinds) & set(e.kinds)))
    if shift:
        return s
    kinds = s.kinds or e.kinds
    carry_people = not (s.kinds and "conversation" not in s.kinds)
    return replace(
        s,
        time=s.time or e.time,
        people=s.people or (e.people if carry_people else ()),
        kinds=kinds,
        apps=s.apps or e.apps,
        keywords=s.keywords if own_kw else e.keywords,
        latest=s.latest or (e.latest and s.time is None),
    )
