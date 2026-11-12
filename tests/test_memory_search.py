"""The rule-based planner and hybrid search over episodes, transcript lines and frames."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import numpy as np
import pytest
from memory_data import (
    HOUR,
    MINUTE,
    add_episode,
    add_frame,
    add_segment,
    add_speaker,
)

from akashic.search.answer import build_sources
from akashic.search.hybrid import (
    episode_lines,
    known_names,
    search_memory,
    speaker_ids,
)
from akashic.search.planner import Plan, heuristic_plan
from akashic.search.timeparse import TimeRange
from akashic.search.transcripts import browse
from akashic.storage import db, embeddings
from akashic.storage.speakers import NewTurn, SpeakerStore

if TYPE_CHECKING:
    from sqlite3 import Connection

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 5, 14, 20, tzinfo=TZ)  # Thursday afternoon
NOW_MS = int(NOW.timestamp() * 1000)


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp() * 1000)


def plan(question: str, names: tuple[str, ...] = ()) -> Plan:
    return heuristic_plan(question, NOW_MS, names, TZ)


# --- planner -------------------------------------------------------------------------------


def test_the_video_question_becomes_a_video_filter_in_a_time_window() -> None:
    p = plan("What video did I watch yesterday afternoon?")
    assert p.time == TimeRange(at(4, 12), at(4, 18), "What video did I watch yesterday afternoon?")
    assert p.kinds == ("video",)
    assert p.keywords == ()
    assert p.people == ()


def test_the_conversation_question_names_the_person_the_kind_and_the_topic() -> None:
    p = plan(
        "What did Minlo say during our conversation this morning, something about a game?",
        ("Minlo", "Alex"),
    )
    assert p.people == ("Minlo",)
    assert p.kinds == ("conversation",)
    assert p.keywords == ("game",)
    assert p.time is not None
    assert (p.time.start_ms, p.time.end_ms) == (at(5, 6), at(5, 12))


def test_french_questions_work_the_same_way() -> None:
    p = plan("De quoi parlait Zoé hier soir à propos d'un jeu ?", ("zoe",))
    assert p.people == ("zoe",)  # accents and case do not matter
    assert p.kinds == ("conversation",)
    assert "jeu" in p.keywords
    assert p.time is not None
    assert (p.time.start_ms, p.time.end_ms) == (at(4, 18), at(4, 23))
    v = plan("Quelle vidéo ai-je regardée avant-hier ?")
    assert v.kinds == ("video",)


def test_names_are_matched_without_accents_or_case(conn: Connection) -> None:
    zoe = add_speaker(conn, "Zoé")
    add_speaker(conn, None)
    assert known_names(conn) == ["Zoé"]
    assert speaker_ids(conn, ["ZOE"]) == {zoe}
    assert speaker_ids(conn, ["nobody"]) == set()


# --- episodes ------------------------------------------------------------------------------


def test_filters_alone_answer_a_question_without_words(conn: Connection) -> None:
    early = add_episode(conn, "video", at(4, 13), at(4, 14), "Cooking show")
    late = add_episode(conn, "video", at(4, 16), at(4, 17), "Space documentary")
    add_episode(conn, "video", at(3, 13), at(3, 14), "A day too early")
    add_episode(conn, "game", at(4, 14), at(4, 15), "Not a video")
    found = search_memory(conn, plan("What video did I watch yesterday afternoon?"), None, "m")
    assert [e.id for e in found.episodes] == [early, late]  # timeline order
    assert not found.semantic
    assert found.lines == []
    assert found.frames == []


def test_a_kind_that_matches_nothing_returns_no_unrelated_evidence(conn: Connection) -> None:
    add_episode(conn, "browsing", at(4, 13), at(4, 14), "Some page")
    found = search_memory(conn, plan("What video did I watch yesterday afternoon?"), None, "m")
    assert found.episodes == []


def test_words_rank_episodes_ignoring_accents(conn: Connection) -> None:
    a = add_episode(conn, "video", at(4, 13), at(4, 14), "Recette", "une recette de crêpes")
    b = add_episode(conn, "video", at(4, 15), at(4, 16), "Stratégie", "la stratégie du jeu")
    found = search_memory(conn, plan("la strategie hier"), None, "m")
    assert [e.id for e in found.episodes] == [b, a]  # b matches; a follows by time
    assert found.episodes[0].score > found.episodes[1].score == 0.0


def test_meaning_ranks_episodes_even_without_shared_words(conn: Connection) -> None:
    near = add_episode(conn, "video", at(4, 13), at(4, 14), "Sans rapport")
    far = add_episode(conn, "video", at(4, 15), at(4, 16), "Autre chose")
    embeddings.put(conn, "episode", near, "m", np.array([1, 0], dtype=np.float32), at(4, 13), 0)
    embeddings.put(conn, "episode", far, "m", np.array([0, 1], dtype=np.float32), at(4, 15), 0)
    qvec = np.array([0.9, 0.1], dtype=np.float32)
    found = search_memory(conn, plan("hier"), qvec, "m")
    assert [e.id for e in found.episodes] == [near, far]
    assert found.semantic


def test_words_and_meaning_agreeing_beat_either_alone(conn: Connection) -> None:
    both = add_episode(conn, "video", at(4, 13), at(4, 14), "Alpha", "volcan éruption")
    words_only = add_episode(conn, "video", at(4, 14), at(4, 15), "Beta", "volcan")
    meaning_only = add_episode(conn, "video", at(4, 15), at(4, 16), "Gamma", "autre")
    for episode, vector in ((both, [1, 0]), (words_only, [0, 1]), (meaning_only, [0.8, 0.2])):
        embeddings.put(conn, "episode", episode, "m", np.array(vector, dtype=np.float32), 0, 0)
    found = search_memory(conn, plan("volcan hier"), np.array([1, 0], dtype=np.float32), "m")
    assert found.episodes[0].id == both


def test_people_narrow_conversations_and_unknown_names_return_no_evidence(conn: Connection) -> None:
    minlo = add_speaker(conn, "Minlo")
    alex = add_speaker(conn, "Alex")
    me = add_speaker(conn, None, is_self=True)
    stranger = add_speaker(conn, None)
    with_minlo = add_episode(
        conn, "conversation", at(5, 7), at(5, 8), "Call", people=(minlo, me, stranger)
    )
    add_episode(conn, "conversation", at(5, 9), at(5, 10), "Other call", people=(alex, me))
    question = "What did Minlo say this morning?"
    found = search_memory(conn, plan(question, ("Minlo",)), None, "m")
    assert [e.id for e in found.episodes] == [with_minlo]
    assert found.episodes[0].people == ("Minlo", "You")
    assert found.episodes[0].unnamed == 1
    # Nobody has that name: preserve the requested constraint.
    nobody = search_memory(conn, plan("What did Bob say this morning?", ("Bob",)), None, "m")
    assert nobody.episodes == []


# --- what was said -------------------------------------------------------------------------


def test_transcript_lines_by_a_person_are_found_by_words_and_meaning(conn: Connection) -> None:
    minlo = add_speaker(conn, "Minlo")
    alex = add_speaker(conn, "Alex")
    start = at(5, 7)
    about_game = add_segment(conn, start, text="there is a new game coming out", speaker=minlo)
    add_segment(conn, start + MINUTE, text="the new game is great", speaker=alex)  # not Minlo
    weather = add_segment(conn, start + 2 * MINUTE, text="the weather is nice today", speaker=minlo)
    add_segment(conn, start + 3 * MINUTE, text="a new game outside the window", speaker=minlo)
    conn.execute(
        "UPDATE audio_segments SET start_ms = ?, end_ms = ? WHERE id = 4", (at(6, 7), at(6, 8))
    )
    embeddings.put(conn, "segment", weather, "m", np.array([0, 1], dtype=np.float32), start, 0)
    embeddings.put(conn, "segment", about_game, "m", np.array([1, 0], dtype=np.float32), start, 0)

    p = plan("What did Minlo say this morning, something about a game?", ("Minlo",))
    by_words = search_memory(conn, p, None, "m")
    assert [(line.segment_id, line.speaker) for line in by_words.lines] == [(about_game, "Minlo")]
    with_meaning = search_memory(conn, p, np.array([1, 0.1], dtype=np.float32), "m")
    assert [line.segment_id for line in with_meaning.lines] == [about_game, weather]
    assert all(line.speaker == "Minlo" for line in with_meaning.lines)


def test_lines_know_their_episode_and_whether_the_speaker_is_you(conn: Connection) -> None:
    me = add_speaker(conn, None, is_self=True)
    segment = add_segment(conn, at(5, 7), text="un mot précis", stream="mic", speaker=me)
    episode = add_episode(conn, "conversation", at(5, 7), at(5, 8), "Call")
    conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (episode, segment))
    (line,) = search_memory(conn, plan("précis ce matin"), None, "m").lines
    assert (line.speaker, line.episode_id, line.stream) == ("You", episode, "mic")
    assert [x.segment_id for x in episode_lines(conn, episode, 10)] == [segment]
    assert episode_lines(conn, 999, 10) == []


def test_a_line_comes_with_the_exchange_around_it(conn: Connection) -> None:
    me = add_speaker(conn, None, is_self=True)
    minlo = add_speaker(conn, "Minlo")
    t = at(5, 9)
    far = add_segment(conn, t - 2 * MINUTE, text="much earlier")  # outside the 45 s window
    before = [add_segment(conn, t - (40 - i * 10) * 1000, text=f"avant {i}") for i in range(4)]
    match = add_segment(conn, t, text="le jeu sort demain", speaker=minlo)
    mine = add_segment(conn, t + 5_000, text="trop bien", stream="mic", speaker=me)
    after = [add_segment(conn, t + (10 + i * 5) * 1000, text=f"après {i}") for i in range(4)]
    conn.execute("UPDATE audio_segments SET confidence = 0.3 WHERE id = ?", (mine,))
    (line,) = search_memory(conn, plan("jeu ce matin"), None, "m").lines
    ids = [c.segment_id for c in line.context]
    assert ids == [*before[1:], match, mine, *after[:2]]  # three before, three after
    assert far not in ids
    assert [c.who for c in line.context][3:5] == ["Minlo", "You"]
    assert [c.is_match for c in line.context].count(True) == 1
    assert [c.unclear for c in line.context][4] is True
    assert line.confidence is None


def test_an_episode_lists_what_was_said_in_time_order_and_skips_empty_text(
    conn: Connection,
) -> None:
    episode = add_episode(conn, "video", 0, HOUR, "V")
    later = add_segment(conn, 2 * MINUTE, text="second", sources=("zen.exe",))
    earlier = add_segment(conn, MINUTE, text="first", sources=("zen.exe",))
    blank = add_segment(conn, 3 * MINUTE, text="", sources=("zen.exe",))
    for segment in (later, earlier, blank):
        conn.execute("INSERT INTO episode_items VALUES (?, 'video_segment', ?)", (episode, segment))
    assert [x.text for x in episode_lines(conn, episode, 10)] == ["first", "second"]
    assert [x.text for x in episode_lines(conn, episode, 1)] == ["first"]


def test_screen_text_matching_the_words_is_returned_too(conn: Connection) -> None:
    inside = add_frame(conn, at(5, 9), "Elden Ring boss guide", "Guide")
    add_frame(conn, at(5, 20), "Elden Ring boss guide")  # outside the morning
    found = search_memory(conn, plan("elden boss ce matin"), None, "m")
    assert [f.id for f in found.frames] == [inside]


@pytest.mark.parametrize("mode", ["words", "vectors", "hybrid"])
@pytest.mark.parametrize("topic", ["game", "jeu", "game jeu"])
def test_speaker_filter_precedes_retrieval_depth(conn: Connection, mode: str, topic: str) -> None:
    """A busy history must not crowd the requested person's evidence out of top K."""
    minlo = add_speaker(conn, "Minlo")
    alex = add_speaker(conn, "Alex")
    start = at(4, 13)
    query = np.array([1, 0], dtype=np.float32)
    for i in range(220):
        other = add_segment(conn, start + i * 5000, text=topic, speaker=alex)
        embeddings.put(conn, "segment", other, "m", query, start, 0)
    target = add_segment(
        conn, start + HOUR, text=f"{topic} with a longer description", speaker=minlo
    )
    embeddings.put(conn, "segment", target, "m", np.array([0.8, 0.6], dtype=np.float32), start, 0)
    p = Plan(
        time=TimeRange(start, start + 2 * HOUR, "synthetic window"),
        people=("Minlo",),
        keywords=tuple(topic.split()) if mode != "vectors" else (),
    )
    found = search_memory(conn, p, query if mode != "words" else None, "m")
    assert [line.segment_id for line in found.lines] == [target]
    assert found.lines[0].start_ms == start + HOUR


def test_episode_kind_filter_precedes_keyword_depth(conn: Connection) -> None:
    start = at(4, 13)
    for i in range(220):
        add_episode(conn, "work", start + i, start + MINUTE, "game")
    add_episode(conn, "video", start, start + MINUTE, "An unrelated older video")
    target = add_episode(conn, "video", start + HOUR, start + 2 * HOUR, "game walkthrough")
    found = search_memory(conn, Plan(kinds=("video",), keywords=("game",)), None, "m", limit=1)
    assert [episode.id for episode in found.episodes] == [target]


@pytest.mark.parametrize("mode", ["words", "vectors", "hybrid"])
@pytest.mark.parametrize("text", ["the game releases Friday", "le jeu sort vendredi"])
def test_mixed_speakers_are_retrievable_without_false_quotations(
    conn: Connection, mode: str, text: str
) -> None:
    start = at(4, 13)
    segment = add_segment(conn, start, text=text, length_ms=10_000)
    store = SpeakerStore(conn)
    store.record_turns(
        segment,
        [
            NewTurn(0, 3000, 3000, np.array([1, 0], dtype=np.float32)),
            NewTurn(3000, 10000, 7000, np.array([0, 1], dtype=np.float32)),
        ],
        0,
    )
    first, second = [s.id for s in reversed(store.all_speakers())]
    store.rename(first, "Minlo")
    store.rename(second, "Alex")
    query = np.array([1, 0], dtype=np.float32)
    embeddings.put(conn, "segment", segment, "m", query, start, 0)
    for person in ("Minlo", "Alex"):
        found = search_memory(
            conn,
            Plan(people=(person,), keywords=tuple(text.split()) if mode != "vectors" else ()),
            query if mode != "words" else None,
            "m",
        )
        assert [line.segment_id for line in found.lines] == [segment]
        assert found.lines[0].speaker is None
        assert found.lines[0].start_ms == start
        assert found.lines[0].context[0].who == "someone"
        assert build_sources(found)[0].label == "someone heard said"
    assert browse(conn)[0].speaker is None
    assert all(t.transcript is None for t in store.turns_of(first))


@pytest.mark.parametrize("mode", ["words", "vectors", "hybrid"])
@pytest.mark.parametrize("topic", ["survival", "survie"])
def test_old_relevant_episodes_survive_dense_history(
    conn: Connection, mode: str, topic: str
) -> None:
    start = at(4, 13)
    minlo = add_speaker(conn, "Minlo")
    query = np.array([1, 0], dtype=np.float32)
    with db.transaction(conn):
        target = add_episode(conn, "conversation", start, start + MINUTE, topic, people=(minlo,))
        for i in range(2100):
            add_episode(
                conn,
                "conversation",
                start + (i + 1) * 1000,
                start + (i + 1) * 1000 + MINUTE,
                "unrelated",
                people=(minlo,),
            )
    embeddings.put(conn, "episode", target, "m", query, start, 0)
    found = search_memory(
        conn,
        Plan(
            time=TimeRange(start, start + HOUR, "dense window"),
            kinds=("conversation",),
            people=("Minlo",),
            keywords=(topic,) if mode != "vectors" else (),
        ),
        query if mode != "words" else None,
        "m",
        limit=1,
    )
    assert [episode.id for episode in found.episodes] == [target]


@pytest.mark.parametrize("mode", ["words", "hybrid"])
@pytest.mark.parametrize(
    ("keyword", "sentence"),
    [
        ("game", "The game releases Friday."),
        ("strategie", "La stratégie sera annoncée vendredi."),
        ("release", "Le release du jeu est vendredi."),
    ],
)
def test_late_matching_text_reaches_answer_sources(
    conn: Connection, mode: str, keyword: str, sentence: str
) -> None:
    start = at(4, 13)
    text = "Introduction sans rapport. " * 40 + sentence + " Suite sans rapport." * 40
    minlo = add_speaker(conn, "Minlo")
    segment = add_segment(conn, start, text=text, speaker=minlo)
    episode = add_episode(
        conn, "conversation", start, start + MINUTE, "Call", text, people=(minlo,)
    )
    conn.execute("UPDATE audio_segments SET confidence = 0.3 WHERE id = ?", (segment,))
    query = np.array([1, 0], dtype=np.float32)
    for kind, owner in (("episode", episode), ("segment", segment)):
        embeddings.put(conn, kind, owner, "m", query, start, 0)
    found = search_memory(
        conn,
        Plan(people=("Minlo",), keywords=(keyword,)),
        query if mode == "hybrid" else None,
        "m",
    )
    assert found.lines[0].segment_id == segment
    assert found.episodes[0].id == episode
    sources = build_sources(found)
    assert {s.kind for s in sources} == {"episode", "line"}
    for source in sources:
        assert sentence in source.text
        assert source.start_ms == start
    assert "(unclear)" in sources[1].text
    assert all(len(text) <= 400 for text in (found.lines[0].text, found.episodes[0].snippet))


@pytest.mark.parametrize("keywords", [(), ("missing",), ("Call",), ('" OR *',)])
def test_nonliteral_hits_keep_prefix_excerpts(conn: Connection, keywords: tuple[str, ...]) -> None:
    """Meaning-only and title-only hits have no known body position to centre on."""
    start = at(4, 13)
    text = "An unrelated introduction. " * 40
    segment = add_segment(conn, start, text=text)
    episode = add_episode(conn, "conversation", start, start + MINUTE, "Call", text)
    query = np.array([1, 0], dtype=np.float32)
    for kind, owner in (("episode", episode), ("segment", segment)):
        embeddings.put(conn, kind, owner, "m", query, start, 0)
    found = search_memory(conn, Plan(keywords=keywords), query, "m")
    assert found.lines[0].text == text[:400]
    assert found.episodes[0].snippet == text[:400]
    assert found.lines[0].context[0].text == found.lines[0].text


def test_nearby_matches_do_not_deduplicate_away_late_matching_words(conn: Connection) -> None:
    start = at(4, 13)
    sentences = ("The game releases Friday.", "La stratégie arrive lundi.")
    for i, sentence in enumerate(sentences):
        add_segment(conn, start + i * 5000, text="Introduction. " * 80 + sentence)
    found = search_memory(conn, Plan(keywords=("game", "strategie")), None, "m")
    assert len(found.lines) == 2
    sources = build_sources(found)
    assert all(any(sentence in source.text for source in sources) for sentence in sentences)


@pytest.mark.parametrize("mode", ["keywords", "vectors"])
def test_absent_person_does_not_relax_constraints(conn: Connection, mode: str) -> None:
    add_speaker(conn, "Sacha")
    other = add_speaker(conn, "Élodie")
    add_episode(conn, "conversation", 1000, 2000, "mods", people=(other,))
    segment = add_segment(conn, 1000, text="mods", speaker=other)
    add_frame(conn, 1000, "mods")
    vector = np.array([1, 0], dtype=np.float32)
    embeddings.put(conn, "segment", segment, "m", vector, 1000, 0)
    found = search_memory(
        conn,
        Plan(people=("Sacha",), keywords=("mods",)),
        vector if mode == "vectors" else None,
        "m",
    )
    assert not found.episodes
    assert not found.lines
    assert not found.frames


def test_app_filters_apply_before_ranking(conn: Connection) -> None:
    good = add_segment(conn, 1000, text="mods", sources=("Discord.exe",))
    add_segment(conn, 2000, text="mods", sources=("chrome.exe",))
    add_frame(conn, 1000, "mods")
    p = plan("mods on Discord")
    assert p.apps == ("discord.exe",)
    found = search_memory(conn, p, None, "m")
    assert [line.segment_id for line in found.lines] == [good]
    assert found.frames == []


def test_app_filter_precedes_semantic_limits(conn: Connection) -> None:
    vector = np.array([1, 0], dtype=np.float32)
    for i in range(105):
        segment = add_segment(conn, 1000 + i, text="mods", sources=("chrome.exe",))
        embeddings.put(conn, "segment", segment, "m", vector, 1000 + i, 0)
    target = add_segment(conn, 3000, text="mods", sources=("Discord.exe",))
    embeddings.put(conn, "segment", target, "m", vector, 3000, 0)
    found = search_memory(conn, Plan(apps=("discord.exe",)), vector, "m", limit=1)
    assert [line.segment_id for line in found.lines] == [target]


@pytest.mark.parametrize(
    ("keywords", "older", "correction"),
    [
        (
            ("rehearsal", "band"),
            "The band rehearsal moved downstairs because of the echo.",
            "Correction pour la répétition : samedi à dix-neuf heures au studio bleu.",
        ),
        (
            ("repetition", "groupe"),
            "La répétition du groupe sera au studio rouge.",
            "Correction: band practice is Saturday at 19:00 in the blue studio.",
        ),
    ],
)
def test_latest_keeps_bilingual_revision_despite_repeated_older_keywords(
    conn: Connection, keywords: tuple[str, ...], older: str, correction: str
) -> None:
    person = add_speaker(conn, "Minlo")
    vector = np.array([1, 0], dtype=np.float32)
    for i in range(12):
        start = 1000 + i * HOUR
        episode = add_episode(
            conn, "conversation", start, start + MINUTE, "Call", older, people=(person,)
        )
        segment = add_segment(conn, start, text=older, speaker=person)
        conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (episode, segment))
        score = (1, 0) if i == 0 else ((0.99, 0.14) if i == 1 else (0.8, 0.6))
        for kind, owner in (("episode", episode), ("segment", segment)):
            embeddings.put(conn, kind, owner, "m", np.array(score, dtype=np.float32), start, 0)
    start = 20 * HOUR
    target = add_episode(
        conn, "conversation", start, start + MINUTE, "Call", correction, people=(person,)
    )
    target_line = add_segment(conn, start, text=correction, speaker=person)
    conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (target, target_line))
    for kind, owner in (("episode", target), ("segment", target_line)):
        embeddings.put(conn, kind, owner, "m", np.array([0.98, 0.2], dtype=np.float32), start, 0)
    # The newest item is unrelated: chronology must not push it into the source budget.
    other = add_episode(
        conn,
        "conversation",
        start + HOUR,
        start + 2 * HOUR,
        "Call",
        "The cat ate its dinner.",
        people=(person,),
    )
    embeddings.put(conn, "episode", other, "m", np.array([0, 1], dtype=np.float32), start, 0)
    ordinary = search_memory(conn, Plan(people=("Minlo",), keywords=keywords), vector, "m")
    assert all(correction not in source.text for source in build_sources(ordinary))
    revised = search_memory(
        conn, Plan(people=("Minlo",), keywords=keywords, latest=True), vector, "m"
    )
    assert target in [e.id for e in revised.episodes[:4]]
    assert target_line in [line.segment_id for line in revised.lines[:4]]
    assert any(correction in source.text for source in build_sources(revised))
    assert other not in [e.id for e in revised.episodes[:5]]


@pytest.mark.parametrize(
    "question",
    [
        "What is Minlo's latest arrangement for band rehearsal?",
        "Quelle est la dernière proposition de Minlo pour la répétition ?",
        "What is the most recent plan?",
    ],
)
def test_explicit_revision_questions_keep_latest_intent(question: str) -> None:
    assert plan(question).latest
    assert plan(question).time is None


def test_historical_questions_do_not_request_latest_evidence() -> None:
    assert not plan("Quelle était la proposition pour la répétition il y a dix jours ?").latest


@pytest.mark.parametrize("question", ["Summarize what I did today.", "Résume ma journée d'hier."])
def test_day_summary_is_not_a_topic_keyword_search(question: str) -> None:
    p = plan(question)
    assert p.overview
    assert p.time is not None
    assert p.keywords == ()


def test_day_summary_covers_period_endpoints_and_retains_scope(conn: Connection) -> None:
    minlo = add_speaker(conn, "Minlo")
    other = add_speaker(conn, "Alex")
    ids = [
        add_episode(
            conn,
            "conversation",
            1000 + i * MINUTE,
            1000 + (i + 1) * MINUTE,
            f"Activity {i}",
            "Lisbonne and the Rust parser" if i == 0 else "A distinct topic",
            people=(minlo,),
        )
        for i in range(13)
    ]
    add_episode(conn, "conversation", 2000, 3000, "Wrong person", people=(other,))
    add_episode(conn, "work", 2000, 3000, "Wrong kind", people=(minlo,))
    add_episode(conn, "conversation", 30 * MINUTE, 31 * MINUTE, "Outside time", people=(minlo,))
    p = Plan(
        time=TimeRange(1000, 20 * MINUTE, "day"),
        people=("Minlo",),
        kinds=("conversation",),
        overview=True,
    )
    found = search_memory(conn, p, np.array([1, 0], dtype=np.float32), "m")
    assert found.overview_total == 13
    assert len(found.episodes) == 10
    assert found.episodes[0].id == ids[0]
    assert found.episodes[-1].id == ids[-1]
    assert all(e.id in ids for e in found.episodes)
    sources = build_sources(found)
    assert len(sources) == 10
    assert any("Lisbonne" in s.text for s in sources)


def test_topic_summary_keeps_relevance_ranking(conn: Connection) -> None:
    add_episode(conn, "conversation", 1000, 2000, "Other", "Nothing related")
    target = add_episode(conn, "conversation", 3000, 4000, "Bergen", "The night train")
    found = search_memory(
        conn, Plan(overview=True, keywords=("Bergen",), time=TimeRange(0, 5000, "day")), None, "m"
    )
    assert found.episodes[0].id == target
    assert found.overview_total is None
