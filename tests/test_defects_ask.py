"""Known defects of Ask: planning, retrieval, sources and the saved answer (audit Phase 4).

Each ``known_defect`` test asserts the behaviour the audit asks for and fails today for the
reason the finding gives; plain tests are the baselines those findings rely on. The language
models are scripted fakes; rows are plain SQL from ``memory_data``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from defects import known_defect
from memory_data import HOUR, MINUTE, NOW, add_media, add_segment, add_speaker

from akashic.pipeline.ask import AskHandler, ask_job
from akashic.pipeline.episodes import build_episodes
from akashic.pipeline.jobs import Job
from akashic.pipeline.llm import LlamaError
from akashic.search import answer
from akashic.search.hybrid import episode_lines, search_memory
from akashic.search.planner import PLAN_SYSTEM, Plan, heuristic_plan, plan_from_model
from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from sqlite3 import Connection

    import numpy as np
    from numpy.typing import NDArray

LONG_AGO = NOW - 6 * HOUR


def model_plan(**fields: object) -> str:
    plan: dict[str, object] = {
        "time_text": "",
        "people": [],
        "kinds": [],
        "keywords": [],
        "semantic_query": "",
    }
    plan.update(fields)
    return json.dumps(plan)


# --- #50: a quoted name from the model -------------------------------------------------------


@known_defect(50, "membership tests the stripped name but looks up the unstripped one", KeyError)
def test_a_quoted_name_from_the_model_is_kept() -> None:
    raw = model_plan(people=["'Minlo'"], kinds=["conversation"])
    plan = plan_from_model(raw, "what did Minlo say?", NOW, ["Minlo"])
    assert plan.people == ("Minlo",)


def test_a_plain_name_from_the_model_is_kept() -> None:
    """Baseline for #50."""
    plan = plan_from_model(model_plan(people=["minlo"]), "what did Minlo say?", NOW, ["Minlo"])
    assert plan.people == ("Minlo",)


# --- #53, #54: retrieval filters --------------------------------------------------------------


@known_defect(53, "episode_lines applies the limit before dropping empty or expired lines")
def test_a_video_whose_first_lines_expired_still_returns_its_later_lines(
    conn: Connection,
) -> None:
    add_media(conn, "Dragons", LONG_AGO, LONG_AGO + 10 * MINUTE)
    first = add_segment(conn, LONG_AGO + MINUTE, text="intro", sources=("zen.exe",))
    second = add_segment(conn, LONG_AGO + 2 * MINUTE, text="générique", sources=("zen.exe",))
    kept = add_segment(conn, LONG_AGO + 3 * MINUTE, text="les dragons volent", sources=("zen.exe",))
    build_episodes(conn, NOW)
    (episode_id,) = (int(r[0]) for r in conn.execute("SELECT id FROM episodes"))
    conn.execute(
        "UPDATE audio_segments SET transcript = NULL WHERE id IN (?, ?)", (first, second)
    )  # their transcripts expired; the links stay
    assert [line.segment_id for line in episode_lines(conn, episode_id, limit=2)] == [kept]


def _work_episode(conn: Connection, app: str) -> None:
    conn.execute(
        "INSERT INTO episodes (kind, ref, start_ms, end_ms, title, app, body)"
        " VALUES ('work', ?, ?, ?, 'Budget 2027.xlsx', ?, 'Budget 2027.xlsx')",
        (f"work:{app}", LONG_AGO, LONG_AGO + HOUR, app),
    )


@known_defect(54, "replace(x, '.exe', '') is case-sensitive, so EXCEL.EXE never matches")
def test_an_upper_case_executable_matches_the_app_filter(conn: Connection) -> None:
    _work_episode(conn, "EXCEL.EXE")
    plan = heuristic_plan("What did I do in excel.exe?", NOW)
    assert plan.apps == ("excel.exe",)
    assert [e.app for e in search_memory(conn, plan, None, "m").episodes] == ["EXCEL.EXE"]


def test_a_lower_case_executable_matches_the_app_filter(conn: Connection) -> None:
    """Baseline for #54."""
    _work_episode(conn, "excel.exe")
    plan = heuristic_plan("What did I do in excel.exe?", NOW)
    assert [e.app for e in search_memory(conn, plan, None, "m").episodes] == ["excel.exe"]


# --- #176, #182: line evidence ---------------------------------------------------------------


def _minlo_call(conn: Connection, text: str) -> tuple[int, int]:
    minlo = add_speaker(conn, "Minlo")
    segment = add_segment(conn, LONG_AGO, text=text, speaker=minlo)
    build_episodes(conn, NOW)
    return minlo, segment


@known_defect(176, "lines come only from FTS or vector hits, with no fallback to eligible lines")
def test_a_person_filtered_question_gets_the_persons_lines_without_a_word_match(
    conn: Connection,
) -> None:
    _, segment = _minlo_call(conn, "on fera une partie de Valorant ce soir")
    plan = heuristic_plan("What did Minlo say about the game?", NOW, ["Minlo"])
    assert plan.people == ("Minlo",)
    found = search_memory(conn, plan, None, "m")  # passages not embedded yet
    assert [e.kind for e in found.episodes] == ["conversation"]  # the episode is found ...
    assert segment in [line.segment_id for line in found.lines]  # ... and so is the line


def test_a_word_match_finds_the_persons_line(conn: Connection) -> None:
    """Baseline for #176: when the words match, the line is there."""
    _, segment = _minlo_call(conn, "the new game comes out next month")
    plan = heuristic_plan("What did Minlo say about the game?", NOW, ["Minlo"])
    assert [line.segment_id for line in search_memory(conn, plan, None, "m").lines] == [segment]


@known_defect(182, "keywords have no prefixes, so 'tournoi' misses 'tournois'")
def test_an_inflected_keyword_still_finds_the_line(conn: Connection) -> None:
    segment = add_segment(conn, LONG_AGO, text="on a fait trois tournois ce week-end")
    plan = heuristic_plan("Where did I hear about the tournoi?", NOW)
    assert "tournoi" in plan.keywords
    assert segment in [line.segment_id for line in search_memory(conn, plan, None, "m").lines]


@known_defect(182, "the planner asks for keywords in the question's language only")
def test_the_planner_asks_the_model_for_keywords_in_both_languages() -> None:
    (keywords,) = (line for line in PLAN_SYSTEM.splitlines() if line.startswith("- keywords:"))
    instruction = keywords + PLAN_SYSTEM.split(keywords, 1)[1].split("\n- ", 1)[0]
    assert "French" in instruction
    assert "English" in instruction


# --- #183, #184: planner words and cues ------------------------------------------------------


@known_defect(183, "stopwords miss notre, truc, rien, weekdays, months and time units")
def test_filler_and_time_words_are_not_keywords() -> None:
    plan = heuristic_plan(
        "Pourquoi notre truc ne marchait pas lundi, rien depuis 20 minutes, en novembre ?", NOW
    )
    noise = {"notre", "truc", "rien", "lundi", "minutes", "novembre"}
    assert noise.isdisjoint(plan.keywords)


@known_defect(183, "model keywords that are cues or names match every title")
def test_model_keywords_that_are_cues_or_names_are_dropped() -> None:
    raw = model_plan(keywords=["conversation", "video", "Minlo", "jeu"], people=["Minlo"])
    plan = plan_from_model(raw, "Dans quelle conversation Minlo a parlé du jeu ?", NOW, ["Minlo"])
    assert plan.keywords == ("jeu",)


@pytest.mark.parametrize(
    ("question", "names", "kind", "wanted"),
    [
        pytest.param(
            "What did Minlo say regarding the tournament?",
            ["Minlo"],
            "video",
            False,
            marks=known_defect(184, "'regarding' matches the video cue regard*"),
        ),
        pytest.param(
            "Dans quelle partie du doc j'ai mis le lien ?",
            [],
            "game",
            False,
            marks=known_defect(184, "'partie du doc' is read as a game"),
        ),
        pytest.param(
            "Qu'est-ce que j'ai maté hier soir ?",
            [],
            "video",
            True,
            marks=known_defect(184, "'maté' (watched) has no video cue"),
        ),
        ("Qu'est-ce que j'ai regardé hier soir ?", [], "video", True),  # baseline
        ("What did Minlo say about the game?", ["Minlo"], "conversation", True),  # baseline
    ],
)
def test_kind_cues(question: str, names: list[str], kind: str, *, wanted: bool) -> None:
    assert (kind in heuristic_plan(question, NOW, names).kinds) is wanted


@known_defect(184, "a speaker named Max is a strict filter the model cannot veto")
def test_a_lower_case_word_that_is_also_a_name_can_be_vetoed_by_the_model() -> None:
    question = "J'ai mis les graphismes en max settings dans quel jeu ?"
    plan = plan_from_model(model_plan(people=[], keywords=["graphismes"]), question, NOW, ["Max"])
    assert plan.people == ()


# --- #185: citation times --------------------------------------------------------------------


def _local(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%H:%M")


@known_defect(185, "an episode source gives only its start time")
def test_an_episode_source_says_when_it_ended() -> None:
    start, end = LONG_AGO, LONG_AGO + 2 * HOUR + 40 * MINUTE
    source = answer.Source(1, "episode", 7, start, end, "conversation: Discord", "on lance ?")
    _, user = answer.answer_prompt("What did Minlo say?", Plan(), [source], NOW)
    assert _local(start) in user
    assert _local(end) in user


# --- the ask job: #177, #186 -----------------------------------------------------------------


class FakeChat:
    model = "chat-model"

    def __init__(self, reply: str, plan: str | None = None) -> None:
        self.reply = reply
        self.plan = plan
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], max_tokens: int = 0
    ) -> str:
        del system, user, schema, max_tokens
        if self.plan is None:
            raise LlamaError  # the rule-based plan
        return self.plan

    def stream(self, system: str, user: str, max_tokens: int = 0) -> Iterator[str]:
        del system, max_tokens
        self.prompts.append(user)
        yield self.reply

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


class NoEmbedder:
    model = "emb"

    def available(self) -> bool:
        return False

    def embed_query(self, text: str) -> NDArray[np.float32]:
        raise AssertionError(text)


def run_ask(conn: Connection, chat: FakeChat, question: str) -> dict[str, object]:
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO questions (asked_ms, text) VALUES (?, ?)", (NOW, question)
        )
    assert cursor.lastrowid is not None
    new = ask_job(cursor.lastrowid)
    handler = AskHandler(conn, chat, NoEmbedder(), lambda: True, lambda: False, lambda: NOW)
    handler.run(Job(1, new.kind, new.key, new.payload, new.data_ms, 1))
    row = conn.execute(
        "SELECT state, answer, error FROM questions WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return dict(zip(("state", "answer", "error"), row, strict=True))


def _yesterday_at(hour: int, minute: int = 0) -> int:
    today = datetime.fromtimestamp(NOW / 1000).astimezone()
    moment = (today - timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int(moment.timestamp() * 1000)


EIGHT_VIDEOS = [f"Vidéo numéro {i}" for i in range(8)]


def _eight_videos_yesterday_afternoon(conn: Connection) -> None:
    for i, title in enumerate(EIGHT_VIDEOS):
        start = _yesterday_at(13, 0) + i * 30 * MINUTE
        add_media(conn, title, start, start + 20 * MINUTE)
    build_episodes(conn, NOW)


@known_defect(177, "five arbitrary videos of eight are given as if complete, with no 'N of M'")
def test_a_list_question_sees_every_video_or_is_told_how_many_matched(conn: Connection) -> None:
    _eight_videos_yesterday_afternoon(conn)
    chat = FakeChat("Tu as regardé plusieurs vidéos [1].")
    run_ask(conn, chat, "What video did I watch yesterday afternoon?")
    (user,) = chat.prompts
    shown = [t for t in EIGHT_VIDEOS if t in user]
    assert len(shown) == len(EIGHT_VIDEOS) or re.search(rf"\b{len(shown)} of 8\b", user)


def test_a_list_question_finds_the_videos_of_the_window(conn: Connection) -> None:
    """Baseline for #177: retrieval itself finds all eight; only the answer step cuts."""
    _eight_videos_yesterday_afternoon(conn)
    plan = heuristic_plan("What video did I watch yesterday afternoon?", NOW)
    assert plan.kinds == ("video",)
    assert plan.keywords == ()
    titles = {e.title for e in search_memory(conn, plan, None, "m", limit=10).episodes}
    assert titles == set(EIGHT_VIDEOS)


@known_defect(186, "an answer with no valid citation is saved as an ordinary grounded answer")
def test_an_uncited_answer_is_not_saved_as_grounded(conn: Connection) -> None:
    add_media(conn, "Dragons", LONG_AGO, LONG_AGO + 10 * MINUTE)
    build_episodes(conn, NOW)
    uncited = "Tu as regardé une vidéo sur les dragons, puis tu as"  # no [n], cut off
    done = run_ask(conn, FakeChat(uncited), "Qu'est-ce que j'ai regardé ?")
    assert done["state"] == "done"
    assert done["answer"] != uncited or done["error"] is not None


def test_a_cited_answer_is_saved_as_written(conn: Connection) -> None:
    """Baseline for #186."""
    add_media(conn, "Dragons", LONG_AGO, LONG_AGO + 10 * MINUTE)
    build_episodes(conn, NOW)
    done = run_ask(conn, FakeChat("Une vidéo sur les dragons [1]."), "Qu'est-ce que j'ai regardé ?")
    assert (done["state"], done["answer"], done["error"]) == (
        "done",
        "Une vidéo sur les dragons [1].",
        None,
    )
