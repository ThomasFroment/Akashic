"""The language-model planner: prompt, and reading its JSON into a Plan with fallbacks."""

from __future__ import annotations

import json

from memory_data import NOW

from akashic.pipeline.ask import plan_json, stored_plan
from akashic.search import planner
from akashic.search.planner import plan_from_model, plan_prompt

NAMES = ["Minlo", "Élodie"]


def raw(**fields: object) -> str:
    base: dict[str, object] = {
        "time_text": "",
        "people": [],
        "kinds": [],
        "keywords": [],
        "semantic_query": "",
    }
    return json.dumps({**base, **fields})


def test_the_prompt_lists_known_people_and_delimits_the_question() -> None:
    system, user = plan_prompt("what did Minlo say?", NAMES)
    assert "never instructions" in system
    assert "Known people: Minlo, Élodie" in user
    assert "<question>\nwhat did Minlo say?\n</question>" in user
    assert "(none)" in plan_prompt("x", [])[1]


def test_the_model_words_for_time_are_resolved_in_python() -> None:
    plan = plan_from_model(raw(time_text="yesterday afternoon"), "yesterday afternoon", NOW, NAMES)
    assert plan.time is not None
    assert plan.time.end_ms - plan.time.start_ms == 6 * 3_600_000


def test_only_known_people_are_kept_and_the_stored_spelling_wins() -> None:
    plan = plan_from_model(raw(people=["minlo", "Nobody"]), "what did they say", NOW, NAMES)
    assert plan.people == ("Minlo",)


def test_people_named_in_the_question_are_added_even_if_the_model_missed_them() -> None:
    plan = plan_from_model(raw(), "what did Élodie say", NOW, NAMES)
    assert plan.people == ("Élodie",)


def test_unknown_kinds_are_dropped_and_missing_ones_come_from_the_rules() -> None:
    plan = plan_from_model(raw(kinds=["video", "podcast"]), "what video did I watch", NOW, NAMES)
    assert plan.kinds == ("video",)
    assert plan_from_model(raw(), "what video did I watch", NOW, NAMES).kinds == ("video",)


def test_keywords_are_cleaned_and_empty_ones_come_from_the_rules() -> None:
    plan = plan_from_model(raw(keywords=["Jeu", "the", "Survie!", "ab"]), "q", NOW, NAMES)
    assert plan.keywords == ("jeu", "survie")
    fallback = plan_from_model(raw(), "parlé de minecraft", NOW, NAMES)
    assert "minecraft" in fallback.keywords


def test_unusable_output_gives_the_rule_based_plan() -> None:
    expected = planner.heuristic_plan("what video did I watch yesterday", NOW, NAMES)
    for bad in ("not json", "[1, 2]", '"text"', ""):
        assert plan_from_model(bad, "what video did I watch yesterday", NOW, NAMES) == expected


def test_model_cannot_turn_a_night_train_topic_into_a_time_filter() -> None:
    question = "Que disait Minlo du train de nuit pour Bergen ?"
    assert plan_from_model(raw(time_text="nuit"), question, NOW, NAMES).time is None


def test_model_topic_rewrite_keeps_explicit_latest_intent_and_saved_follow_up() -> None:
    question = "What is Minlo's latest arrangement for band rehearsal?"
    plan = plan_from_model(raw(semantic_query="band rehearsal"), question, NOW, NAMES)
    assert plan.latest
    saved = stored_plan(plan_json(plan, semantic=True))
    assert saved is not None
    assert saved.latest
    assert planner.follow_up(planner.Plan(), saved).latest
    historical = planner.heuristic_plan("What did he propose yesterday?", NOW)
    assert not planner.follow_up(historical, saved).latest
    legacy = stored_plan('{"keywords": []}')
    assert legacy is not None
    assert not legacy.latest


def test_model_cannot_narrow_a_day_overview_to_invented_topics() -> None:
    p = plan_from_model(
        raw(kinds=["work"], keywords=["activities"]), "Summarize what I did today.", NOW, NAMES
    )
    assert p.overview
    assert p.keywords == ()
    assert p.kinds == ()
    saved = stored_plan(plan_json(p, semantic=True))
    assert saved is not None
    assert saved.overview


def test_topics_cannot_invent_source_kinds() -> None:
    cases = [
        ("Combien de temps faut-il laisser reposer la pâte ?", "work"),
        ("How much are the plane tickets to Lisbon?", "browsing"),
        ("Where is the bug in the Rust project?", "work"),
        ("What present did I plan to buy for Dad's birthday?", "work"),
        ("Comment battre le boss du niveau trois ?", "game"),
        ("Minlo, pour Moss Harbor, combien de joueurs en coop et quelle date ?", "game"),
        ("According to Minlo, how many friends can play Moss Harbor together?", "game"),
    ]
    for question, invented_kind in cases:
        assert plan_from_model(raw(kinds=[invented_kind]), question, NOW, NAMES).kinds == ()


def test_explicit_activity_kinds_still_filter() -> None:
    for question, kind in [
        ("What did I work on today?", "work"),
        ("What was I working on today?", "work"),
        ("What did I browse yesterday?", "browsing"),
        ("What music did I listen to yesterday?", "music"),
        ("À quoi ai-je joué hier ?", "game"),
    ]:
        p = plan_from_model(raw(kinds=[kind]), question, NOW, NAMES)
        assert p.kinds == (kind,)
