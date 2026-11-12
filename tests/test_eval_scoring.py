"""The Ask evaluation's pure scorers (``scripts/eval_scoring.py``), with no model and no database.

Covers the false passes the 2026-11-10-2 audit confirmed in the old substring scorers (#114-#116)
and the checks that replace them. ``scripts`` is not a package; it is put on ``sys.path`` and
imported by name, so the module is typed as ``Any`` here.
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from akashic.search.answer import no_evidence

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
scoring: Any = importlib.import_module("eval_scoring")
Q: Any = scoring.Q

# The scorers the audit refuted, exactly as scripts/eval_ask.py had them in the pre-audit code.
OLD_ABSTAIN = re.compile(
    r"(did not|didn't|do not|don't|does not|doesn't|no mention|nothing|not find|not found|"
    r"n'ai pas|n'ai rien|pas de|aucun|rien|no record|couldn't|could not|isn't|not in)",
    re.IGNORECASE,
)


def old_fact(answer: str, alternative: str) -> bool:
    return scoring.plain(alternative) in scoring.plain(answer)


# --- facts (#114) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "alternative"),
    [
        ("Minlo talked about the game coop [4].", "4"),
        ("The bug is in the parser [3].", "3"),
        ("Le train part ce soir [7].", "7"),
        ("He will take the cat to the vet tomorrow [1].", "ill"),
    ],
)
def test_the_old_substring_scorer_passed_these_and_the_new_one_does_not(
    answer: str, alternative: str
) -> None:
    assert old_fact(answer, alternative)  # the false pass the audit confirmed
    assert not scoring.has_phrase(answer, alternative)


def test_citation_markers_never_count_as_facts() -> None:
    assert not scoring.has_phrase("Il y a quatre joueurs [4].", "4")
    assert not scoring.has_phrase("See [1, 4] and [2;7].", "4")
    assert not scoring.has_phrase("See [1, 4] and [2;7].", "7")
    assert scoring.strip_citations("a [1][2] b [3, 4] c [ 5 ]").split() == ["a", "b", "c"]


def test_facts_match_whole_words_and_phrases_across_accents_and_quotes() -> None:
    assert scoring.has_phrase("The cat is ill [1].", "ill")
    assert not scoring.has_phrase("Will you come? He is still here.", "ill")
    assert scoring.has_phrase("Jusqu\u2019à 4 joueurs [2]", "jusqu'a 4")
    assert scoring.has_phrase("Ça sort le 12   Décembre.", "12 decembre")
    assert not scoring.has_phrase("Ça sort le 12 décembre.", "12 d")  # no more prefixes
    assert scoring.has_phrase("Coût : 80 € par billet", "80 €")
    assert scoring.has_phrase("I was editing ask.py.", "ask.py")
    assert not scoring.has_phrase("anything", "")
    assert scoring.fact_groups(
        "Sourdough [1] and storms [2].", (("bread", "sourdough"), ("storm",))
    ) == (True, False)


def test_plus_is_rejected_when_it_also_occurs_in_another_episode() -> None:
    # "plus" is a whole word in most French answers, so word boundaries cannot save it.
    assert old_fact("Je n'en sais pas plus sur ce sujet [2].", "plus")
    assert scoring.has_phrase("Je n'en sais pas plus sur ce sujet [2].", "plus")
    texts = {
        "storm": "La lumière va plus vite que le son.",
        "balloon": "L'air chaud, plus léger, fait monter le ballon.",
    }
    old = Q(
        "Pourquoi voit-on l'éclair ?",
        (("lumière", "light"), ("vite", "fast", "plus")),
        cite=("storm",),
    )
    assert "alternative 'plus' also occurs in 'balloon'" in scoring.question_problems(old, texts)
    new = Q(
        "Pourquoi voit-on l'éclair ?",
        (("plus vite que le son", "light travels faster"),),
        cite=("storm",),
    )
    assert scoring.question_problems(new, texts) == []


@pytest.mark.parametrize(
    ("alternative", "problem"),
    [("4", "bare number"), ("12", "bare number"), ("500", "bare number"), ("ml", "too short")],
)
def test_bare_numbers_and_short_alternatives_are_rejected(alternative: str, problem: str) -> None:
    assert problem in scoring.alternative_problem(alternative)


@pytest.mark.parametrize("alternative", ["500 g", "4 joueurs", "23:40", "1 h", "ill", "€80"])
def test_numbers_with_a_unit_and_three_letter_words_are_accepted(alternative: str) -> None:
    assert scoring.alternative_problem(alternative) is None


def test_question_problems_catch_fixture_mistakes() -> None:
    texts = {"call": "Minlo: the beta opens Friday", "other": "Friday rehearsal"}
    bad = Q("When?", (("friday",), ("monday",)), cite=("call", "gone"), split="test", lang="de")
    problems = scoring.question_problems(bad, texts)
    assert "split 'test'" in problems
    assert "lang 'de'" in problems
    assert "unknown or future episode 'gone'" in problems
    assert "alternative 'friday' also occurs in 'other'" in problems
    assert "no alternative of ('monday',) occurs in the gold episodes" in problems
    assert scoring.question_problems(Q("Who?", abstain=True), texts) == [
        "an abstain question needs plausible distractors in forbid"
    ]
    assert "expects nothing" in scoring.question_problems(Q("Hm?"), texts)
    clash = Q("When?", (("beta",),), cite=("call",), forbid=("opens",))
    assert scoring.question_problems(clash, texts) == [
        "forbidden 'opens' occurs in the gold episodes"
    ]
    assert scoring.question_problems(Q("Who?", (("minlo",),), cite=("call",)), {"call": "x"}) == [
        "no alternative of ('minlo',) occurs in the gold episodes"
    ]
    assert (
        scoring.question_problems(
            Q("Who?", (("minlo",),), cite=("call",)), {"call": "x"}, {"call": "Minlo"}
        )
        == []
    )


# --- abstention (#115) ---------------------------------------------------------------------

FABRICATED = "vous avez regardé Inception sur Netflix ; il n'y a pas de détails"


def test_a_fabricated_answer_is_no_longer_an_abstention() -> None:
    assert OLD_ABSTAIN.search(FABRICATED)  # scored as a correct abstention before
    assert not scoring.is_refusal(FABRICATED)
    netflix = Q(
        "Quel film ai-je regardé sur Netflix hier soir ?", abstain=True, forbid=("hollow depths",)
    )
    verdict = scoring.judge(netflix, FABRICATED, ())
    assert verdict.abstained is False
    assert not verdict.grounded
    assert not verdict.ok


@pytest.mark.parametrize(
    "answer",
    [
        no_evidence("Quel film ai-je regardé hier ?"),
        no_evidence("What film did I watch yesterday?"),
        "I couldn't find anything about Minlo's holidays in Japan.",
        "Sorry, there is no record of a Netflix film last night.",
        "Based on your memory, I found no mention of Japan [1].",
        "Je ne trouve aucune trace d'un film Netflix hier soir.",
        "Il n'y a aucun film dans tes souvenirs d'hier soir. Tu as joué à Hollow Depths.",
        "D'après ta mémoire, je n'ai rien sur ce sujet.",
        "Aucune mention du Japon dans tes conversations avec Minlo.",
        "Your memory doesn't contain anything about that.",
    ],
)
def test_refusals_are_recognised_by_their_first_sentence(answer: str) -> None:
    assert scoring.is_refusal(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "Minlo talked about Tokyo. Nothing else was mentioned.",
        "You watched Inception. I couldn't find the end time.",
        "Le film n'est pas clair, mais il y a pas de doute : Inception.",
        "Hier soir, tu as joué à Hollow Depths ; rien sur Netflix.",
    ],
)
def test_a_refusal_word_later_in_the_answer_is_not_a_refusal(answer: str) -> None:
    assert OLD_ABSTAIN.search(answer)
    assert not scoring.is_refusal(answer)


def test_an_abstention_must_not_cite_an_episode_inside_the_window() -> None:
    q = Q("Quel film ai-je regardé sur Netflix hier soir ?", abstain=True, forbid=("inception",))
    answer = "Je n'ai trouvé aucun film Netflix hier soir. Tu as joué à un jeu [1]."
    assert scoring.judge(q, answer, ("game",), in_window=("game", "game_chat")).abstained is False
    assert scoring.judge(q, answer, ("minlo_call",), in_window=("game",)).abstained is True
    assert scoring.judge(q, answer, (), in_window=("game",)).ok
    assert not scoring.judge(q, "Je n'ai rien trouvé, sauf Inception.", ()).ok  # a distractor


# --- citations (#116) ----------------------------------------------------------------------

VIDEOS = ("bread_video", "storm_video", "rust_video", "balloon_video", "origami_video")


def test_an_answer_citing_every_video_no_longer_scores_like_the_right_one() -> None:
    q = Q(
        "What video did I watch yesterday afternoon?",
        (("sourdough",), ("storm",)),
        cite_all=VIDEOS[:2],
    )
    answer = "You watched sourdough [1], storm [2], rust [3], balloon [4] and origami [5] videos."
    old_cites = set(VIDEOS[:2]) <= set(VIDEOS)  # the recall-only rule
    assert old_cites
    verdict = scoring.judge(q, answer, VIDEOS)
    assert verdict.facts
    assert verdict.citations.recall == 1.0
    assert verdict.citations.precision == pytest.approx(2 / 5)
    assert verdict.citations.false == ("balloon_video", "origami_video", "rust_video")
    assert not verdict.citations.ok
    assert not verdict.ok
    right = scoring.judge(q, "Sourdough [1] and a storm video [2].", VIDEOS[:2])
    assert right.ok
    assert right.citations.precision == 1.0


def test_citation_scores() -> None:
    score = scoring.citation_score
    assert score(["a"], ["a", "b"]) == scoring.Citations(0.5, 1.0, (), ok=True)
    assert score(["a"], ["a", "b"], need_all=True).ok is False
    assert score(["a", "game"], ["a"], ["game"]).ok
    assert score([], ["a"]) == scoring.Citations(0.0, None, (), ok=False)
    assert score(["x"], []) == scoring.Citations(None, 0.0, ("x",), ok=False)
    assert score([], []) == scoring.Citations(None, None, (), ok=True)


def test_a_correct_answer_with_a_wrong_plan_fails_acceptance() -> None:
    q = Q("Who did I talk to?", (("minlo",),), cite=("call",))
    verdict = scoring.judge(q, "Minlo [1].", ("call",), plan_ok=False)
    assert not verdict.ok
    assert not verdict.ok_with_plan
    row = scoring.verdict_json(verdict)
    assert row["plan_ok"] is False
    assert row["ok"] is False
    assert row["cite_precision"] == 1.0


# --- hedging and counts (#125, #177) -------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "Apparemment, le patch arrive jeudi [1].",
        "It sounds like the party is at Odile's [2].",
        "Le patch arriverait jeudi, si j'ai bien compris [1].",
        "Minlo thinks it's Thursday, but the recording is unclear [1].",
        "Quelqu'un (une voix non identifiée) a dit que la bêta ouvre vendredi [3].",
        "Je crois que c'est chez Odile [1].",
    ],
)
def test_hedged_answers(answer: str) -> None:
    assert scoring.is_hedged(answer)


@pytest.mark.parametrize(
    "answer", ["Le patch arrive jeudi [1].", "Minlo said the beta opens Friday [3]."]
)
def test_confident_answers_are_not_hedged(answer: str) -> None:
    assert not scoring.is_hedged(answer)


def test_a_hedge_question_fails_a_confident_answer() -> None:
    q = Q("Quand sort le patch ?", (("jeudi",),), cite=("chat",), hedge=True)
    assert not scoring.judge(q, "Le patch sort jeudi [1].", ("chat",)).ok
    # D9: a hedge is diagnostic, never proof that unclear text supports a factual answer.
    assert not scoring.judge(q, "Le patch sortirait jeudi, apparemment [1].", ("chat",)).ok


@pytest.mark.parametrize(
    ("answer", "counts"),
    [
        ("Tu as regardé huit vidéos [1][2].", {8}),
        ("You watched 8 YouTube videos.", {8}),
        ("You watched 5 of the 8 videos.", {8}),
        ("Huit vidéos, dont une des vidéos sur l'origami.", {8}),
        ("You watched five videos: ...", {5}),
        ("You watched a video about bread.", set[int]()),
    ],
)
def test_stated_video_counts(answer: str, counts: set[int]) -> None:
    assert scoring.stated_counts(answer) == counts


def test_a_count_question_needs_exactly_the_count() -> None:
    q = Q("How many videos?", count=8)
    assert scoring.judge(q, "Eight videos.", ()).count_ok
    assert not scoring.judge(q, "Five videos [1][2][3][4][5].", ()).ok


# --- freezing and comparing (#118, #126) ---------------------------------------------------


def test_the_definitions_digest_is_stable_and_sees_every_field() -> None:
    a = (Q("one", (("x",),), split="heldout"), Q("two", abstain=True, forbid=("y",)))
    assert scoring.definitions_sha256(a) == scoring.definitions_sha256(tuple(a))
    changed = (a[0], Q("two", abstain=True, forbid=("z",)))
    assert scoring.definitions_sha256(a) != scoring.definitions_sha256(changed)
    assert len(scoring.definitions_sha256(a)) == 64


def row(qid: str, *, ok: bool) -> dict[str, object]:
    return {"id": qid, "ok": ok, "question": f"question {qid}"}


def test_compare_prints_per_question_changes() -> None:
    base = {
        "heldout_sha256": "a",
        "models": {
            "m": [row("1", ok=True), row("1", ok=True), row("2", ok=False), row("3", ok=True)]
        },
    }
    new = {
        "heldout_sha256": "a",
        "models": {
            "m": [row("1", ok=True), row("1", ok=False), row("2", ok=False), row("h1", ok=True)],
            "other": [],
        },
    }
    lines = scoring.compare(base, new)
    assert lines[0] == "m: 3/4 -> 2/4 passed runs"
    assert "     1 2/2 -> 1/2  question 1" in lines
    assert not any(line.strip().startswith("2 ") for line in lines)  # unchanged
    assert "    h1 added  question h1" in lines
    assert "     3 removed  question 3" in lines
    assert "other: not in the baseline" in lines
    assert scoring.compare({"heldout_sha256": "b"}, new)[0].startswith("warning: the held-out")
    assert scoring.compare({}, {"models": "broken"}) == []
    assert scoring.compare(base, {"heldout_sha256": "a", "models": {"m": ["junk"]}}) == [
        "m: 3/4 -> 0/0 passed runs",
        "     1 removed  question 1",
        "     2 removed  question 2",
        "     3 removed  question 3",
    ]
