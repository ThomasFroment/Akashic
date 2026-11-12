"""C1: standalone plan + typed-cue gate + per-field carry table (rules only).

The new question is planned alone (the harness passes ``standalone_plan`` =
``heuristic_plan(question)``). A gate classifies cue words of the new question into typed
cues, and a per-field carry table decides which still-empty fields take the earlier plan's
value. ``planner.follow_up`` is untouched; this is a separate function.

Cues (on ``normalize(question)``):
    P person anaphor, T thing deictic, W time link, F insertion/ellipsis, X exclusion,
    C continuation (copies the whole earlier plan).

The vector-text decision ("prev\\nq" only when keywords carry, else the standalone
semantic_query) is returned as the plan's ``semantic_query``, and the last gate decision is
kept in ``LAST`` for inspection.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from akashic.search.planner import Plan
from akashic.search.timeparse import normalize

LAST: dict[str, Any] = {}

_WORD = re.compile(r"[a-z0-9]+")


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


# Phrases removed before the person-anaphor test ("il y a" is not "he").
_IMPERSONAL = _rx(r"\bil\s+(?:n\s+)?y\s+(?:a|avait|aura)\b|\bil\s+(?:faut|fallait|fait|faisait)\b")

# P: person anaphors.
_P = _rx(
    r"\b(?:he|she|him|his|her|hers|they|them|their|il|elle|ils|elles|lui|leur|eux|son|sa|ses)\b"
)

# T: thing deictics.
_TIME_UNIT = (
    r"(?:morning|afternoon|evening|night|week|weekend|month|year|time|moment|day|"
    r"matin|soir|soiree|nuit|apres|midi|semaine|mois|annee|jour|journee|moment|temps|"
    r"week|weekend|derniers?|dernieres?|"
    r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
)
_NOT_DET_AFTER_THAT = r"(?:i|you|we|he|she|they|it|the|a|an|my|your|j|je|is|was|s)"
_T = _rx(
    r"\b(?:it|ca|cela|celui|celle|celles|ceux)\b"
    r"|\bthat\s*[?.!]*\s*$"  # clause-final
    r"|\bthat\s+(?:was|is|s)\b"  # before was/is
    rf"|\bthat\s+(?!{_NOT_DET_AFTER_THAT}\b)[a-z]+"  # determiner, not relative "that I"
    rf"|(?<!\best\s)\b(?:this|ce|cet|cette|ces)\s+(?!{_TIME_UNIT}\b|que\b|qui\b|qu\b)[a-z]+"
    r"|\b(?:m|t|s|l|j|n|il|elle|on|ils|elles|je|tu|nous|vous|lui)\s+en\b"  # clitic en
    r"|\b(?:j|tu|on|nous|vous|ils|elles|il|elle)\s+(?:n\s+)?y\s+(?!a\b|avait\b|aura\b)"
    r"|\b(?:la\s+)?dessus\b"
    r"|\b(?:about|of|on|after|before|with|like)\s+that\b"  # iter1: pronoun "that" after a preposition
    r"|^\s*(?:is|was|did)\s+that\b"  # iter1: "Is that out already?"
)

# W: time links.
_W = _rx(
    r"\bthen\b|\b(?:after|before)\s+(?:that|this|it)\b|\bafterwards?\b|\blater\b"
    r"|\bthat\s+day\b|\bsame\s+(?:day|time)\b|\b(?:right|just|straight)\s+(?:after|before)\b"
    r"|\bensuite\b|\bpuis\b|\bapres\b(?!\s+midi)|\bavant\b(?!\s+hier)"
    r"|\bce\s+jour\s+la\b|\ba\s+ce\s+moment\b|\bpendant\s+ce\s+temps\b|\bmeme\s+(?:jour|temps)\b"
)

# F: insertion / ellipsis.
_LEAD = _rx(
    r"^\s*(?:and|et|also|aussi|but|mais|so|donc|or|ou|what\s+about|how\s+about|et\s+pour|sinon)\b"
)
_CLAUSE = _rx(
    r"\b(?:i|you|we|he|she|they|je|j|tu|il|elle|on|nous|vous|ils|elles)\s+[a-z]+"
    r"|\b(?:did|do|does|have|has|was|were|is|are)\s+(?:i|you|we|he|she|they)\b"
)
_WH_VERB = _rx(
    r"\b(?:what|which|who|whom|when|where|how|why)\b.*\b(?:did|do|does|was|were|is|are|have|"
    r"has|had|will|can)\b"
    r"|\b(?:qu|que|quoi|quel|quelle|quels|quelles|qui|quand|ou|comment|combien|pourquoi)\b.*"
    r"\b(?:ai|as|a|avons|avez|ont|est|etait|suis|fait)\b"
    r"|\b(?:ai|as|a|avons|avez|ont|est|etait)\b.*\b(?:quoi|qui|quel|quelle|quels|quelles|ou)\b"
)
_CONTINUE = _rx(
    r"^\s*(?:(?:and|et)\s+)?(?:tell\s+me\s+more|more|go\s+on|keep\s+going|continue|why|"
    r"and\s+then|then|dis\s+m\s+en\s+plus|dis\s+moi\s+en\s+plus|en\s+dire\s+plus|plus|"
    r"pourquoi|et\s+apres|apres|et\s+puis|la\s+suite|vas\s+y|vas\s+y\s+continue)"
    r"\s*[?.!]*\s*$"
)

# X: exclusion.
_X = _rx(r"\b(?:else|other|another|autre|autres|encore)\b")

_CUE_WORDS = frozenset(
    """
    he she him his her hers they them their il elle ils elles lui leur eux son sa ses
    it ca cela celui celle celles ceux that this ce cet cette ces en dessus
    then after before afterwards afterward later day same time right just straight
    ensuite puis apres avant jour moment temps meme pendant
    and et also aussi but mais donc what about how pour sinon
    more tell go continue why pourquoi plus suite dis
    else other another autre autres encore rien anything
    lundi mardi mercredi jeudi vendredi samedi dimanche
    monday tuesday wednesday thursday friday saturday sunday
    janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre
    january february march april may june july august september october november december
    """.split()
)
_FUNCTION = frozenset(
    """
    what which who whom when where how why did do does was were is are have has had will can
    the a an i you we me my to of on in at about with for from
    qu que quoi quel quelle quels quelles qui quand ou comment combien est a ai as avons avez
    ont etait suis fait j je tu on nous vous m t s l n d le la les un une de du des sur au aux
    """.split()
)


def _cues(q: str, s: Plan) -> dict[str, bool]:
    unimp = _IMPERSONAL.sub(" ", q)
    p = bool(_P.search(unimp)) and not s.people
    w = bool(_W.search(q))
    # iter2: the "that"/"ce" inside a time link ("after that", "ce jour-là") is part of W,
    # not a thing deictic.
    t = bool(_T.search(_W.sub(" ", q)))
    x = bool(_X.search(q))
    c = bool(_CONTINUE.search(q))
    lead = bool(_LEAD.search(q))
    clause = bool(_CLAUSE.search(unimp))
    # iter1: a self-contained clause (subject + verb, or wh-word + verb) gates both
    # length-based ellipsis tests, not only the "<= 2 content tokens" one.
    own_clause = clause or bool(_WH_VERB.search(q))
    kw = [k for k in s.keywords if k not in _CUE_WORDS]
    zero_anchor = (
        s.time is None and not s.people and not s.kinds and not s.apps and len(kw) <= 1
    ) and not own_clause
    content = [
        w_ for w_ in _WORD.findall(q) if w_ not in _CUE_WORDS and w_ not in _FUNCTION
    ]
    short = len(content) <= 2 and s.time is None and not own_clause
    f = (lead and not clause) or zero_anchor or short or c
    # iter1: L = a leading and/et in front of a full clause ("And what music did I listen to?"):
    # a link to the earlier moment, like W.
    link = lead and clause
    return {"P": p, "T": t, "W": w, "L": link, "F": f, "X": x, "C": c}


# iter1: own topic slot. A new topic is a content keyword introduced by a topic preposition
# ("about the DLC", "sur le DLC", "du nouveau boss"), or a capitalised non-initial word
# (a proper noun or acronym) that is a keyword. A deictic after the preposition ("about that",
# "de cette vidéo") refers back and is not a new topic.
_TOPIC_PREP = frozenset("about of on regarding sur de du des concernant".split())
_SKIP = frozenset("the a an my your his her le la les l un une mon ma mes son sa ses".split())
_DEICTIC = frozenset("that this it ca cela ce cet cette ces celui celle".split())


def _own_topic(question: str, q: str, kw: list[str]) -> bool:
    if not kw:
        return False
    kws = set(kw)
    words = _WORD.findall(q)
    for i, w in enumerate(words):
        if w not in _TOPIC_PREP:
            continue
        j = i + 1
        while j < len(words) and words[j] in _SKIP:
            j += 1
        if j < len(words) and words[j] not in _DEICTIC and words[j] in kws:
            return True
    raw = re.findall(r"[^\W\d_][\w']*", question)
    return any(
        tok[:1].isupper() and normalize(tok).split(" ")[0] in kws for tok in raw[1:]
    )


def plan_thread(question, earlier_question, earlier_plan, standalone_plan, names, now):  # noqa: ANN001, ANN201, ARG001, PLR0913
    q = normalize(question).strip()
    s, e = standalone_plan, earlier_plan
    cue = _cues(q, s)
    P, T, W, L, F, X, C = (cue[k] for k in "PTWLFXC")  # noqa: N806
    joined = f"{earlier_question.strip()}\n{question.strip()}"

    if C:  # pure continuation: the whole earlier plan
        out = replace(e, semantic_query=joined)
        LAST.clear()
        LAST.update(cue=cue, carry=dict.fromkeys(("time", "kinds", "people", "apps", "keywords"), True),
                    vector=joined)  # fmt: skip
        return out

    complete = s.time is not None and bool(s.kinds or s.people) and not any(cue.values())
    kw = [k for k in s.keywords if k not in _CUE_WORDS]
    empty = {
        "time": s.time is None,
        "kinds": not s.kinds,
        "people": not s.people,
        "apps": not s.apps,
        # iter1: keywords are "empty" unless the question states its own topic slot.
        "keywords": not _own_topic(question, q, kw),
    }
    rule = {
        # iter1: time links (W) and a leading and/et before a clause (L) carry the time only.
        "time": P or T or W or L or F or X,
        "kinds": P or T or F or X,
        "people": P or F or X,
        "apps": T or F or X,
        # iter1: a person anaphor with no topic of its own keeps the earlier topic
        # ("What did I answer him?").
        "keywords": (T or F or P) and not X,
    }
    carry = {k: (not complete) and empty[k] and rule[k] for k in rule}
    # Vetoes.
    if s.kinds and "conversation" not in s.kinds:
        carry["people"] = False
    if s.kinds and e.kinds and not set(s.kinds) & set(e.kinds) and not (P or T):
        carry["people"] = False
        carry["keywords"] = False

    vector = joined if carry["keywords"] else s.semantic_query
    out = replace(
        s,
        time=e.time if carry["time"] else s.time,
        kinds=e.kinds if carry["kinds"] else s.kinds,
        people=e.people if carry["people"] else s.people,
        apps=e.apps if carry["apps"] else s.apps,
        keywords=e.keywords if carry["keywords"] else s.keywords,
        latest=(e.latest if carry["time"] else s.latest),
        semantic_query=vector,
    )
    LAST.clear()
    LAST.update(cue=cue, complete=complete, carry=carry, vector=vector)
    return out
