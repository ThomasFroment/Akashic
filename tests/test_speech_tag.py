"""What a stretch of speech was: you, voice chat, game or media (speech_tag), and where it shows."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from memory_data import HOUR, MINUTE, NOW, add_game, add_media, add_segment, add_speaker

from akashic.capture.ports import VOICE_APPS
from akashic.pipeline import episodes
from akashic.pipeline.episodes import build_episodes
from akashic.search import transcripts
from akashic.search.answer import build_sources
from akashic.search.hybrid import LineHit, Results, episode_lines, search_memory
from akashic.search.planner import Plan
from akashic.search.transcripts import speech_tag
from akashic.storage.sessions import games_between

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.search.transcripts import SpeechTag

GAME = [("game.exe", 1_000, 5_000)]


# --- the rules ------------------------------------------------------------------------------


def test_the_mic_is_you_even_with_a_voice_app_or_no_source() -> None:
    assert speech_tag("mic", ("discord.exe",), [], 0, 10) == "you"
    assert speech_tag("mic", (), [], 0, 10) == "you"


def test_a_voice_app_wins_over_a_game_or_browser_source() -> None:
    assert speech_tag("system", ("discord.exe",), [], 0, 10) == "voice chat"
    assert speech_tag("system", ("game.exe", "discord.exe"), GAME, 2_000, 3_000) == "voice chat"
    assert speech_tag("system", ("chrome.exe", "discord.exe"), [], 0, 10) == "voice chat"
    assert all(speech_tag("system", (app,), [], 0, 10) == "voice chat" for app in VOICE_APPS)


def test_a_game_process_inside_its_session_is_game() -> None:
    assert speech_tag("system", ("game.exe",), GAME, 2_000, 3_000) == "game"
    assert speech_tag("system", ("game.exe",), GAME, 500, 1_500) == "game"  # overlaps the start
    assert speech_tag("system", ("game.exe",), GAME, 4_500, 6_000) == "game"  # overlaps the end


@pytest.mark.parametrize("span", [(0, 1_000), (5_000, 6_000)])
def test_the_game_process_outside_its_session_is_media(span: tuple[int, int]) -> None:
    assert speech_tag("system", ("game.exe",), GAME, *span) == "media"


def test_other_sources_are_media_and_none_is_empty() -> None:
    assert speech_tag("system", ("chrome.exe",), GAME, 2_000, 3_000) == "media"
    assert speech_tag("system", ("zen.exe",), [], 0, 10) == "media"
    assert speech_tag("system", (), [], 0, 10) == ""
    assert speech_tag("system", (), GAME, 2_000, 3_000) == ""


def test_names_are_compared_exactly() -> None:
    assert speech_tag("system", ("Discord.exe",), [], 0, 10) == "media"
    assert speech_tag("system", ("Game.exe",), GAME, 2_000, 3_000) == "media"
    assert speech_tag("system", ("game",), GAME, 2_000, 3_000) == "media"


# --- game sessions ----------------------------------------------------------------------------


def test_games_between_returns_overlapping_sessions_open_or_closed(conn: Connection) -> None:
    add_game(conn, 1_000, 2_000, process="a.exe")
    add_game(conn, 3_000, 4_000, process="b.exe", closed=False)
    add_game(conn, 9_000, 10_000, process="far.exe")
    add_game(conn, 100, 1_000, process="touching.exe")  # ends where the range starts
    assert sorted(games_between(conn, 1_000, 5_000)) == [
        ("a.exe", 1_000, 2_000),
        ("b.exe", 3_000, 4_000),
    ]
    assert games_between(conn, 2_000, 3_000) == []


# --- transcripts ----------------------------------------------------------------------------


def _four_kinds(conn: Connection) -> dict[str, int]:
    base = NOW - 5 * HOUR
    add_game(conn, base + 2 * HOUR, base + 3 * HOUR)
    return {
        "you": add_segment(conn, base, text="salut moi", stream="mic", sources=()),
        "voice chat": add_segment(conn, base + MINUTE, text="salut appel"),
        "game": add_segment(
            conn, base + 2 * HOUR + MINUTE, text="salut jeu", sources=("game.exe",)
        ),
        "media": add_segment(conn, base + 4 * HOUR, text="salut video", sources=("zen.exe",)),
        "": add_segment(conn, base + 5 * MINUTE, text="salut vide", sources=()),
    }


def test_search_and_browse_fill_the_tag_of_every_row(conn: Connection) -> None:
    ids = _four_kinds(conn)
    expected = {i: tag for tag, i in ids.items()}
    found = transcripts.search(conn, "salut")
    assert {u.id: u.tag for u in found} == expected
    browsed = transcripts.browse(conn)
    assert {u.id: u.tag for u in browsed} == expected
    assert set(expected.values()) == {"you", "voice chat", "game", "media", ""}


# --- memory search lines, episode lines, sources ----------------------------------------------


def test_memory_search_and_episode_lines_carry_the_tag(conn: Connection) -> None:
    ids = _four_kinds(conn)
    expected = {i: tag for tag, i in ids.items()}
    found = search_memory(conn, Plan(keywords=("salut",)), None, "m", limit=10)
    assert {line.segment_id: line.tag for line in found.lines} == expected
    episode = conn.execute(
        "INSERT INTO episodes (kind, ref, start_ms, end_ms, title, body)"
        " VALUES ('conversation', 'c', 0, ?, 'Call', '')",
        (NOW,),
    ).lastrowid
    for i in ids.values():
        conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (episode, i))
    assert episode is not None
    lines = episode_lines(conn, episode, 10)
    assert {line.segment_id: line.tag for line in lines} == expected


def _line(tag: SpeechTag, speaker: str | None, stream: str = "system") -> LineHit:
    return LineHit(1, stream, NOW, NOW + 3_000, None, speaker, "hello", 7, 0.1, tag=tag)


@pytest.mark.parametrize(
    ("tag", "speaker", "stream", "label"),
    [
        ("voice chat", None, "system", "someone heard said (voice chat)"),
        ("voice chat", "Minlo", "system", "Minlo said (voice chat)"),
        ("game", None, "system", "someone heard said (game)"),
        ("media", None, "system", "someone heard said (media)"),
        ("you", "You", "mic", "You said"),
        ("", None, "system", "someone heard said"),
        ("", "Minlo", "system", "Minlo said"),
    ],
)
def test_line_source_labels_carry_the_tag(
    tag: SpeechTag, speaker: str | None, stream: str, label: str
) -> None:
    (source,) = build_sources(Results(lines=[_line(tag, speaker, stream)]))
    assert source.label == label


def test_a_mic_line_without_a_speaker_name_has_no_suffix() -> None:
    line = replace(_line("you", None, "mic"))
    assert build_sources(Results(lines=[line]))[0].label == "You said"


# --- conversations ----------------------------------------------------------------------------


def test_a_mic_segment_with_realistic_sources_still_makes_a_conversation(
    conn: Connection,
) -> None:
    start = NOW - 3 * HOUR
    add_segment(
        conn, start, text="alors on lance", stream="mic", sources=("discord.exe", "chrome.exe")
    )
    add_segment(
        conn, start + 10_000, text="ok", stream="mic", sources=("discord.exe", "chrome.exe")
    )
    assert build_episodes(conn, NOW)["conversations"] == 1


def test_a_mic_segment_without_a_voice_source_makes_no_conversation(conn: Connection) -> None:
    add_segment(conn, NOW - 3 * HOUR, text="je parle seul", stream="mic", sources=())
    assert build_episodes(conn, NOW)["conversations"] == 0


def test_media_sound_makes_no_conversation(conn: Connection) -> None:
    add_segment(conn, NOW - 3 * HOUR, text="une video", sources=("zen.exe",))
    assert build_episodes(conn, NOW)["conversations"] == 0


def test_in_game_voice_makes_a_conversation(conn: Connection) -> None:
    start = NOW - 3 * HOUR
    add_game(conn, start, start + HOUR)
    add_segment(conn, start + MINUTE, text="vocal en jeu", sources=("game.exe",))
    assert build_episodes(conn, NOW)["conversations"] == 1


def test_the_episode_module_shares_the_voice_app_set() -> None:
    assert episodes.VOICE_APPS is VOICE_APPS


# --- one afternoon, end to end ------------------------------------------------------------------


def test_one_afternoon_keeps_calls_games_media_and_you_apart(conn: Connection) -> None:
    base = NOW - 6 * HOUR
    minlo = add_speaker(conn, "Minlo")
    you = add_speaker(conn, None, is_self=True)
    add_segment(conn, base, text="salut on joue", speaker=minlo)
    add_segment(
        conn, base + 6_000, text="salut ouais", stream="mic", sources=("discord.exe", "chrome.exe"),
        speaker=you,
    )  # fmt: skip
    add_segment(conn, base + 20_000, text="salut c'est quoi ce truc", sources=("discord.exe",))
    add_media(conn, "Une video", base + HOUR, base + HOUR + 20 * MINUTE)
    add_segment(conn, base + HOUR + MINUTE, text="salut la video", sources=("zen.exe",))
    add_game(conn, base + 2 * HOUR, base + 3 * HOUR)
    add_segment(conn, base + 2 * HOUR + MINUTE, text="salut en jeu", sources=("game.exe",))
    add_segment(conn, base + 4 * HOUR, text="salut seul", stream="mic", sources=())
    build_episodes(conn, NOW)

    said = build_sources(
        search_memory(conn, Plan(people=("Minlo",), keywords=("salut",)), None, "m")
    )
    labels = [s.label for s in said if s.kind == "line"]
    assert labels
    assert set(labels) <= {"Minlo said (voice chat)", "someone heard said (voice chat)"}

    videos = build_sources(
        search_memory(conn, Plan(kinds=("video",), keywords=("salut",)), None, "m")
    )
    assert not any("(voice chat)" in s.label for s in videos)

    tags = {u.tag for u in transcripts.search(conn, "salut")}
    assert tags == {"you", "voice chat", "game", "media"}
