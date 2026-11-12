"""C1fix: standalone plan + typed-cue gate + per-field carry table (rules only).

Fix round of C1 (lines marked "fix:"): continuation = request verb + amount word, "on" before a
date is not a subject, verb-only activity kinds with a third-person subject are not the
question's kind, T carries people, named-person shift keeps the earlier topic, possessive
noun phrases and amount/time words in the topic test, French clitic l' and bare c'est as T.

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
_TIME_WORD = re.compile(_TIME_UNIT + r"|weekends?|tonight|today|yesterday|hier|aujourd|demain")
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
    # fix: French object clitic l'/les before an auxiliary ("je l'ai regardée", "tu les as vus")
    r"|\b(?:j|je|tu|il|elle|on|nous|vous|ils|elles)\s+(?:ne\s+|n\s+)?(?:l|les)\s+"
    r"(?:ai|as|a|avons|avez|ont|avais|avait|avaient)\b"
    # fix: a clause-final copula "c'est / c'était", alone or with a wh-word ("C'est où ?",
    # "Quand est-ce que c'était ?"): its subject "ce" points back at the earlier thing.
    r"|\bc\s+(?:est|etait)\s*(?:(?:ou|quand|qui|quoi|comment|combien|pourquoi)\s*)?[?.!]*\s*$"
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
_MONTH = (
    r"(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)"
)
_CLAUSE = _rx(
    r"\b(?:i|you|we|he|she|they|je|j|tu|il|elle|nous|vous|ils|elles)\s+[a-z]+"
    # fix: "on" is the French subject pronoun only when it is not the English preposition
    # in front of a date, a time word, a determiner or a number ("and on Tuesday?").
    rf"|\bon\s+(?!{_TIME_UNIT}\b|{_MONTH}\b|the\b|a\b|an\b|my\b|this\b|that\b|last\b|next\b|"
    r"[0-9])[a-z]+"
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
# fix: a continuation is a request verb (imperative/infinitive, addressed to the assistant)
# followed by an amount word, anywhere in the question: "tell me more about it", "can you give
# me more details", "tu peux me donner plus de détails", "dis-m'en plus sur cette vidéo",
# "explique davantage", "raconte la suite". "dit" (past tense, about someone else) is excluded.
_REQ_VERB = (
    r"(?:tell|give|say|explain|elaborate|expand|share|"
    r"dis|dites|dire|donne|donnez|donner|explique|expliquez|expliquer|raconte|racontez|"
    r"raconter|developpe|developpez|developper|precise|precisez|preciser)"
)
_MORE = r"(?:more|further|details?|plus|davantage|detail|suite)"
_CONTINUE_REQ = _rx(rf"\b{_REQ_VERB}\b(?:\s+[a-z]+){{0,4}}?\s+{_MORE}\b")
_AMOUNT = _rx(rf"{_MORE}|details?|precisions?|infos?|informations?|explications?")
_REQUEST_WORD = _rx(rf"{_REQ_VERB}|you|peux|can")
# A bare "elaborate" / "développe" / "précise" is a continuation on its own.
_CONTINUE_BARE = _rx(
    r"^\s*(?:(?:and|et|can|could)\s+)?(?:you\s+|tu\s+peux\s+|peux\s+tu\s+)?"
    r"(?:elaborate|expand|developpe|developper|precise|preciser)\s*(?:on\s+(?:it|that)\s*)?"
    r"[?.!]*\s*$"
)
# Reported speech ("did he tell me more about...", "il m'a dit plus...") is not a request.
_REPORTED = _rx(
    r"\b(?:did|does|has|had|was|will)\s+(?:he|she|they|i|we)\b"
    r"|\b(?:he|she|they|il|elle|ils|elles|on)\s+(?:m\s+|me\s+|t\s+|nous\s+|lui\s+|en\s+)*"
    r"(?:a|ont|avait|avaient|told|said)\b"
)

# fix: kinds named by a user-activity verb (watch/vu/play/listen/browse/work), as opposed to a
# kind noun (video, stream, film, partie). With a third-person subject and no first person, the
# verb describes the other person's action ("Elle l'a vue où ?"), not a kind of the user's
# moment, so it does not count as the question's own kind.
_THIRD_SUBJECT = _rx(
    r"\b(?:he|she|they|il|elle|ils|elles)\s+[a-z]+|\b(?:did|does|do|was|is|has)\s+(?:he|she|they)\b"
)
_FIRST_PERSON = _rx(r"\b(?:i|je|j|me|my|moi)\b")
_KIND_NOUN = {
    "video": _rx(r"\b(?:video\w*|youtube|twitch|film\w*|stream\w*)\b"),
    "game": _rx(r"\bpartie\b"),
    "conversation": _rx(r"\b(?:conversation\w*|calls?|appels?|discord)\b"),
}

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
    c = bool(
        _CONTINUE.search(q)
        or _CONTINUE_BARE.search(q)
        or (_CONTINUE_REQ.search(q) and not _REPORTED.search(q))
    )
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
# fix: locative/pointer nouns of a prepositional locution ("du côté de Sacha", "on the side of",
# "au niveau de", "in terms of") introduce an anchor, not a topic.
_LOCUTION = frozenset("cote side niveau part terms regard".split())
# fix: a possessive determiner + keyword is a noun-phrase object ("his grade", "sa note"):
# the question states its own topic even without a topic preposition.
_POSSESSIVE = frozenset("his her their my your its son sa ses leur leurs mon ma mes".split())


def _own_topic(question: str, q: str, kw: list[str], deictic: bool = False) -> bool:  # noqa: FBT001, FBT002
    if not kw:
        return False
    kws = set(kw)
    words = _WORD.findall(q)
    for i, w in enumerate(words):
        if w in _POSSESSIVE and not deictic and i + 1 < len(words) and words[i + 1] in kws:
            return True
        if w not in _TOPIC_PREP:
            continue
        j = i + 1
        while j < len(words) and words[j] in _SKIP:
            j += 1
        if (
            j < len(words)
            and words[j] not in _DEICTIC
            and words[j] not in _LOCUTION
            and words[j] in kws
        ):
            return True
    raw = re.findall(r"[^\W\d_][\w']*", question)
    return any(
        tok[:1].isupper() and normalize(tok).split(" ")[0] in kws for tok in raw[1:]
    )


def _own_kinds(q: str, kinds: tuple[str, ...]) -> tuple[str, ...]:
    """Drop user-activity kinds named only by a verb whose subject is another person."""
    if not kinds or not _THIRD_SUBJECT.search(q) or _FIRST_PERSON.search(q):
        return kinds
    # conversation verbs (said, dit) do name the moment when someone else is the subject
    out = tuple(k for k in kinds if k == "conversation" or _noun_kind(q, k))
    return out if len(out) != len(kinds) else kinds


def _noun_kind(q: str, kind: str) -> bool:
    rx = _KIND_NOUN.get(kind)
    return rx is not None and rx.search(q) is not None


def plan_thread(question, earlier_question, earlier_plan, standalone_plan, names, now):  # noqa: ANN001, ANN201, ARG001, PLR0913
    q = normalize(question).strip()
    s, e = standalone_plan, earlier_plan
    kinds = _own_kinds(q, s.kinds)
    g = replace(s, kinds=kinds) if kinds != s.kinds else s
    cue = _cues(q, g)
    P, T, W, L, F, X, C = (cue[k] for k in "PTWLFXC")  # noqa: N806
    joined = f"{earlier_question.strip()}\n{question.strip()}"
    # fix: time words ("weekend", "soir") are never a topic, wherever they sit.
    # Amount nouns ("plus de détails", "more info") ask for depth, they are not a topic either.
    kw = [
        k
        for k in s.keywords
        if k not in _CUE_WORDS and not _TIME_WORD.fullmatch(k) and not _AMOUNT.fullmatch(k)
    ]
    if C:  # fix: the request's own words ("plus de détails", "more info") are not a topic
        kw = [k for k in kw if not _REQUEST_WORD.fullmatch(k)]
    own_topic = _own_topic(question, q, kw, T)

    if C:  # continuation: the whole earlier plan (its request verb is not a kind)
        # fix: "tell me more about the DLC" keeps its own topic, the rest continues.
        # fix: a field the request states itself ("tell me more about what Sacha said
        # yesterday") is its own; the request verb's kind is not, only a kind noun is.
        noun_kinds = tuple(k for k in s.kinds if _noun_kind(q, k))
        out = replace(
            e,
            time=s.time or e.time,
            latest=s.latest if s.time else e.latest,
            people=s.people or e.people,
            apps=s.apps or e.apps,
            kinds=noun_kinds or e.kinds,
            semantic_query=joined,
        )
        if own_topic:
            out = replace(out, keywords=s.keywords, semantic_query=s.semantic_query)
        LAST.clear()
        LAST.update(cue=cue, carry=dict.fromkeys(("time", "kinds", "people", "apps"), True)
                    | {"keywords": not own_topic}, vector=out.semantic_query)  # fmt: skip
        return out

    complete = g.time is not None and bool(g.kinds or g.people) and not any(cue.values())
    # fix: a named-person shift ("And Sacha?", "Et Sacha, il a dit quoi ?", "What about
    # Sacha?"): a leading link word and a new named person, with no topic of its own, keeps the
    # earlier topic and kinds even when a resumptive pronoun makes it a full clause.
    # The earlier topic exists only when its keywords are not all time/cue words ("lundi").
    shift = (
        L
        and bool(g.people)
        and set(g.people) != set(e.people)
        and any(k not in _CUE_WORDS for k in e.keywords)
    )
    empty = {
        "time": g.time is None,
        "kinds": not g.kinds,
        "people": not g.people,
        "apps": not g.apps,
        # iter1: keywords are "empty" unless the question states its own topic slot.
        "keywords": not own_topic,
    }
    # fix: a thing deictic ("When is it?", "C'est quand ça ?") points at the earlier moment,
    # which includes its people, when the question names no person of its own.
    rule = {
        # iter1: time links (W) and a leading and/et before a clause (L) carry the time only.
        "time": P or T or W or L or F or X,
        "kinds": P or T or F or X or shift,
        "people": P or T or F or X,
        "apps": T or F or X,
        # iter1: a person anaphor with no topic of its own keeps the earlier topic
        # ("What did I answer him?").
        "keywords": (T or F or P or shift) and not X,
    }
    carry = {k: (not complete) and empty[k] and rule[k] for k in rule}
    # Vetoes.
    if g.kinds and "conversation" not in g.kinds:
        carry["people"] = False
    if g.kinds and e.kinds and not set(g.kinds) & set(e.kinds) and not (P or T):
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
