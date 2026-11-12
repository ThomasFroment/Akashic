"""From capture rows to a cited answer (#124), with fake models only.

Capture-shaped rows (media sessions, transcribed audio segments with speaker turns) go through
``build_episodes``, the embed jobs (a deterministic hashing embedder) and the ``ask`` job (a
scripted chat model that returns a plan and an answer citing [1]). Each question has distractors
of another day, another person and another kind; the saved sources must all come from the right
episode. Nothing here inserts an episode directly: the episodes are the ones production builds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict, cast

import pytest
from ask_fakes import FakeChat, FlushClock, ask
from memory_data import HOUR, MINUTE, NOW, add_media, add_segment, add_speaker
from oracles import HashEmbedder

from akashic.pipeline.ask import AskHandler
from akashic.pipeline.embed import EMBED, EmbedHandler, enqueue_missing
from akashic.pipeline.episodes import build_episodes
from akashic.pipeline.jobs import JobQueue
from akashic.search.answer import MAX_EPISODES
from akashic.search.timeparse import resolve

if TYPE_CHECKING:
    from sqlite3 import Connection

AFTERNOON: Final = resolve("yesterday afternoon", NOW)
MORNING: Final = resolve("this morning", NOW)
DAY: Final = 24 * HOUR
VIDEO: Final = "Speedrun Hollow Knight expliqué"


def _speech(  # noqa: PLR0913 - one stretch of transcribed speech
    conn: Connection,
    start: int,
    lines: tuple[str, ...],
    *,
    sources: tuple[str, ...],
    speaker: int | None = None,
    stream: str = "system",
) -> list[int]:
    """Consecutive 8 s utterances 12 s apart."""
    return [
        add_segment(
            conn, start + n * 12_000, text=text, sources=sources, speaker=speaker,
            stream=stream, length_ms=8_000,
        )
        for n, text in enumerate(lines)
    ]  # fmt: skip


def _video(
    conn: Connection, title: str, start: int, lines: tuple[str, ...], app: str = "Zen"
) -> None:
    add_media(conn, title, start, start + 20 * MINUTE, app=app, artist="Une chaîne")
    _speech(conn, start + MINUTE, lines, sources=(app.lower() + ".exe",))


def _call(conn: Connection, start: int, person: int, you: int, lines: tuple[str, ...]) -> None:
    _speech(conn, start, lines, sources=("discord.exe",), speaker=person)
    _speech(conn, start + 6_000, ("ah ouais", "carrément"), sources=(), speaker=you, stream="mic")


def _embed_everything(conn: Connection) -> None:
    queue = JobQueue(conn)
    embedder = HashEmbedder()
    handler = EmbedHandler(conn, embedder, lambda: True, lambda: NOW)
    while enqueue_missing(conn, queue, embedder.model, NOW):
        job = queue.claim([EMBED], NOW)
        assert job is not None
        handler.run(job)
        queue.complete(job.id, NOW)


@pytest.fixture
def history(conn: Connection) -> dict[str, int]:
    """One day of invented capture rows; returns the ids of the episodes that must be found."""
    assert AFTERNOON is not None
    assert MORNING is not None
    minlo = add_speaker(conn, "Minlo")
    alex = add_speaker(conn, "Alex")
    you = add_speaker(conn, None, is_self=True)
    # Yesterday afternoon: the video, and music at the same time (another kind).
    _video(
        conn,
        VIDEO,
        AFTERNOON.start_ms + 2 * HOUR,
        ("on regarde la route la plus rapide", "the fastest route skips the boss"),
    )
    add_media(
        conn,
        "One More Time",
        AFTERNOON.start_ms + 3 * HOUR,
        AFTERNOON.start_ms + 3 * HOUR + 5 * MINUTE,
        app="Spotify",
        artist="Daft Punk",
    )
    # Other days and times: videos that must not answer "yesterday afternoon".
    _video(conn, "Recette de crêpes", AFTERNOON.start_ms + 2 * HOUR - DAY, ("la pâte repose",))
    _video(conn, "Journal du matin", AFTERNOON.start_ms - 4 * HOUR, ("les nouvelles du jour",))
    add_media(conn, "Bande-annonce", AFTERNOON.start_ms + HOUR, AFTERNOON.start_ms + HOUR + 30_000)
    # This morning: Minlo talks about a game; Alex too, later; a video about games.
    _call(
        conn,
        MORNING.start_ms + 2 * HOUR,
        minlo,
        you,
        ("j'ai acheté le nouveau jeu hier soir", "the game is really good, on joue ce soir ?"),
    )
    _call(
        conn,
        MORNING.start_ms + 4 * HOUR,
        alex,
        you,
        ("tu as vu ce jeu ?", "the game is on sale this week"),
    )
    _video(
        conn,
        "Top 10 games of the year",
        MORNING.start_ms + 3 * HOUR,
        ("number one is a great game",),
    )
    # Yesterday: Minlo already talked about a game.
    _call(conn, MORNING.start_ms + 2 * HOUR - DAY, minlo, you,
          ("je télécharge le jeu", "the game download is slow"))  # fmt: skip

    built = build_episodes(conn, NOW)
    assert built == {"media": 5, "games": 0, "conversations": 3, "app_runs": 0}
    _embed_everything(conn)
    video = conn.execute("SELECT id FROM episodes WHERE title = ?", (VIDEO,)).fetchone()[0]
    call = conn.execute(
        "SELECT e.id FROM episodes e JOIN episode_people p ON p.episode_id = e.id"
        " WHERE e.kind = 'conversation' AND p.speaker_id = ? AND e.start_ms >= ?",
        (minlo, MORNING.start_ms),
    ).fetchone()[0]
    return {"video": int(video), "call": int(call)}


class Saved(TypedDict):
    kind: str
    id: int
    segment_ids: list[int]


@dataclass(frozen=True, slots=True)
class Answered:
    state: str
    answer: str
    kinds: list[str]  # the kinds of the plan the job searched with
    sources: list[Saved]
    prompt: str  # what the answering model was shown


def _answer(conn: Connection, question: str, plan: dict[str, object]) -> Answered:
    chat = FakeChat(pieces=("Here it is [1].",), plan=json.dumps(plan))
    handler = AskHandler(
        conn, chat, HashEmbedder(), lambda: True, lambda: True, lambda: NOW, FlushClock()
    )
    handler.run(ask(conn, question))
    row = conn.execute(
        "SELECT state, answer, sources, plan FROM questions ORDER BY id DESC"
    ).fetchone()
    used = cast("dict[str, list[str]]", json.loads(row[3]))
    sources = cast("list[Saved]", json.loads(row[2]))
    return Answered(row[0], row[1], used["kinds"], sources, chat.prompts[0][1])


def _evidence_of(conn: Connection, episode: int) -> set[int]:
    return {
        int(r[0])
        for r in conn.execute(
            "SELECT item_id FROM episode_items WHERE episode_id = ?"
            " AND item_kind IN ('segment', 'video_segment')",
            (episode,),
        )
    }


def _check_sources(conn: Connection, sources: list[Saved], episode: int) -> None:
    """Source 1 is the episode; every other source is speech of that same episode."""
    assert sources[0]["kind"] == "episode"
    assert sources[0]["id"] == episode
    evidence = _evidence_of(conn, episode)
    for source in sources[1:]:
        assert source["kind"] == "line", source
        assert source["id"] in evidence, source
        assert set(source["segment_ids"]) <= evidence, source


def test_what_video_did_i_watch_yesterday_afternoon(
    conn: Connection, history: dict[str, int]
) -> None:
    result = _answer(
        conn,
        "What video did I watch yesterday afternoon?",
        {"time_text": "yesterday afternoon", "people": [], "kinds": ["video"], "keywords": [],
         "semantic_query": "a video watched"},
    )  # fmt: skip
    assert result.state == "done"
    assert result.answer == "Here it is [1]."
    assert result.kinds == ["video"]
    _check_sources(conn, result.sources, history["video"])
    assert VIDEO in result.prompt
    for other in ("One More Time", "Recette de crêpes", "Journal du matin", "Top 10 games"):
        assert other not in result.prompt


def test_what_did_minlo_say_this_morning_about_a_game(
    conn: Connection, history: dict[str, int]
) -> None:
    result = _answer(
        conn,
        "What did Minlo say this morning about a game?",
        {"time_text": "this morning", "people": ["Minlo"], "kinds": ["conversation"],
         "keywords": ["game", "jeu"], "semantic_query": "a game"},
    )  # fmt: skip
    assert result.state == "done"
    assert result.answer == "Here it is [1]."
    assert result.kinds == ["conversation"]
    _check_sources(conn, result.sources, history["call"])
    assert any(s["kind"] == "line" for s in result.sources)
    assert "nouveau jeu" in result.prompt
    for other in ("ce jeu ?", "on sale", "télécharge", "number one"):
        assert other not in result.prompt


SERIES_EPISODES: Final = ("Arcane S1E1", "Arcane S1E2", "The Bear S2E3")


@pytest.fixture
def series_history(conn: Connection) -> dict[str, int]:
    """Series episodes on three different days, each day with music and a call around it;
    returns the episode id of every series episode by title."""
    minlo = add_speaker(conn, "Minlo")
    you = add_speaker(conn, None, is_self=True)
    for days_ago, title in enumerate(SERIES_EPISODES, start=1):
        evening = NOW - days_ago * DAY
        _video(conn, title, evening, (f"dialogue de {title}",), app="Netflix")
        add_media(
            conn, f"Chanson {days_ago}", evening + HOUR, evening + HOUR + 5 * MINUTE,
            app="Spotify", artist="Daft Punk",
        )  # fmt: skip
        _call(conn, evening + 2 * HOUR, minlo, you, (f"appel du jour {days_ago}", "à demain"))
    build_episodes(conn, NOW)
    _embed_everything(conn)
    rows = conn.execute(
        "SELECT title, id FROM episodes WHERE kind = 'video' AND app = 'Netflix'"
    ).fetchall()
    return {title: int(episode_id) for title, episode_id in rows}


def test_give_me_a_list_of_all_the_series_i_watched(
    conn: Connection, series_history: dict[str, int]
) -> None:
    assert set(series_history) == set(SERIES_EPISODES)
    assert len(series_history) <= MAX_EPISODES
    result = _answer(
        conn,
        "Give me a list of all the series I watched",
        {"time_text": "", "people": [], "kinds": ["video"], "keywords": [], "semantic_query": ""},
    )
    assert result.state == "done"
    episode_sources = {s["id"] for s in result.sources if s["kind"] == "episode"}
    assert episode_sources == set(series_history.values())
    for title in SERIES_EPISODES:
        assert title in result.prompt
    for distractor in ("Chanson", "Daft Punk", "appel du jour", "Minlo"):
        assert distractor not in result.prompt
