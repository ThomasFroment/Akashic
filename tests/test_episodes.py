"""Episodes: what becomes one, what does not, and that a pass never builds anything twice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_data import (
    HOUR,
    MINUTE,
    NOW,
    add_game,
    add_job,
    add_media,
    add_segment,
    add_speaker,
    add_visit,
)

from akashic.pipeline.episodes import (
    CLOSE_GAP_MS,
    CONVERSATIONS_SINCE,
    MIC_MARGIN_MS,
    build_episodes,
    horizon_ms,
)
from akashic.storage.state import get_int

if TYPE_CHECKING:
    from sqlite3 import Connection

LONG_AGO = NOW - 6 * HOUR  # everything below happens hours before "now": long closed


def episodes(conn: Connection) -> list[tuple[str, str, str]]:
    return conn.execute("SELECT kind, ref, title FROM episodes ORDER BY start_ms, id").fetchall()


# --- the horizon ---------------------------------------------------------------------------


def test_horizon_stops_at_the_oldest_unfinished_transcription_or_speaker_job(
    conn: Connection,
) -> None:
    add_job(conn, "transcribe", "t:1", NOW - 3 * HOUR)
    add_job(conn, "speakers", "s:1", NOW - 5 * HOUR)
    add_job(conn, "transcribe", "t:0", NOW - 9 * HOUR, state="done")  # finished: ignored
    add_job(conn, "ocr_frame", "o:1", NOW - 8 * HOUR)  # not about audio: ignored
    assert horizon_ms(conn, NOW) == NOW - 5 * HOUR


# --- videos and music ----------------------------------------------------------------------


def test_a_played_media_session_becomes_a_video_with_what_was_said_during_it(
    conn: Connection,
) -> None:
    start = LONG_AGO
    media = add_media(
        conn, "Les secrets du jeu", start, start + 10 * MINUTE, url="https://youtube.com/watch?v=x"
    )
    heard = add_segment(
        conn, start + MINUTE, text="aujourd'hui on parle de stratégie", sources=("zen.exe",)
    )
    add_segment(conn, start + 2 * MINUTE, text="a call in discord", sources=("discord.exe",))
    add_segment(conn, start + 3 * MINUTE, text="my own voice", stream="mic", sources=("zen.exe",))
    add_segment(conn, start + 20 * MINUTE, text="after it ended", sources=("zen.exe",))

    assert build_episodes(conn, NOW)["media"] == 1

    kind, ref, title, app, url, body = conn.execute(
        "SELECT kind, ref, title, app, url, body FROM episodes"
    ).fetchone()
    assert (kind, ref, title, app) == ("video", f"media:{media}", "Les secrets du jeu", "Zen")
    assert url == "https://youtube.com/watch?v=x"
    assert "A Channel" in body
    assert "stratégie" in body
    assert "discord" not in body
    assert "own voice" not in body
    assert "after it ended" not in body
    items = conn.execute(
        "SELECT item_kind, item_id FROM episode_items i JOIN episodes e ON e.id = i.episode_id"
        " WHERE e.kind = 'video' ORDER BY item_kind"
    ).fetchall()
    assert items == [("media_session", media), ("video_segment", heard)]


def test_music_apps_and_hosts_make_music_episodes(conn: Connection) -> None:
    add_media(conn, "Song", LONG_AGO, LONG_AGO + 5 * MINUTE, app="Spotify", artist="Band")
    add_media(
        conn,
        "Mix",
        LONG_AGO + HOUR,
        LONG_AGO + HOUR + 5 * MINUTE,
        url="https://music.youtube.com/watch?v=y",
    )
    build_episodes(conn, NOW)
    assert [k for k, _, _ in episodes(conn)] == ["music", "music"]


def test_short_open_or_recent_media_sessions_wait_or_are_ignored(conn: Connection) -> None:
    add_media(conn, "Too short", LONG_AGO, LONG_AGO + 5 * MINUTE, played_ms=30_000)
    add_media(conn, "Still open", LONG_AGO, LONG_AGO + 5 * MINUTE, closed=False)
    add_media(conn, "Just ended", NOW - 2 * MINUTE, NOW - MINUTE)
    assert build_episodes(conn, NOW)["media"] == 0
    assert episodes(conn) == []


# --- games ---------------------------------------------------------------------------------


def test_a_long_closed_game_session_is_an_episode(conn: Connection) -> None:
    game = add_game(conn, LONG_AGO, LONG_AGO + 90 * MINUTE)
    add_game(conn, LONG_AGO + 3 * HOUR, LONG_AGO + 3 * HOUR + MINUTE)  # a minute: not an episode
    add_game(conn, LONG_AGO + 4 * HOUR, LONG_AGO + 5 * HOUR, closed=False)
    assert build_episodes(conn, NOW)["games"] == 1
    assert episodes(conn) == [("game", f"game:{game}", "The Game")]


# --- conversations -------------------------------------------------------------------------


def test_speech_during_a_voice_app_becomes_a_conversation_with_its_people(
    conn: Connection,
) -> None:
    me = add_speaker(conn, None, is_self=True)
    minlo = add_speaker(conn, "Minlo")
    stranger = add_speaker(conn, None)
    s1 = add_segment(conn, LONG_AGO, text="salut, tu as vu le nouveau jeu ?", speaker=minlo)
    s2 = add_segment(
        conn, LONG_AGO + MINUTE, text="non, raconte", stream="mic", sources=(), speaker=me
    )
    s3 = add_segment(conn, LONG_AGO + 2 * MINUTE, text="il sort le mois prochain", speaker=stranger)
    add_segment(conn, LONG_AGO + HOUR, text="the next call", speaker=minlo)  # a later conversation
    add_segment(
        conn, LONG_AGO + 30 * MINUTE, text="talking to myself", stream="mic", sources=()
    )  # mic alone, nobody on the call: not part of anything

    assert build_episodes(conn, NOW)["conversations"] == 2

    first, second = conn.execute(
        "SELECT id, kind, title, app, start_ms, end_ms, body FROM episodes ORDER BY start_ms"
    ).fetchall()
    assert first[1:4] == ("conversation", "Discord conversation", "discord.exe")
    assert (first[4], first[5]) == (LONG_AGO, LONG_AGO + 2 * MINUTE + 4_000)
    assert first[6] == "salut, tu as vu le nouveau jeu ? non, raconte il sort le mois prochain"
    assert second[6] == "the next call"
    people = conn.execute(
        "SELECT speaker_id, speech_ms FROM episode_people WHERE episode_id = ? ORDER BY speaker_id",
        (first[0],),
    ).fetchall()
    assert people == [(me, 4_000), (minlo, 4_000), (stranger, 4_000)]
    used = {
        r[0] for r in conn.execute("SELECT item_id FROM episode_items WHERE item_kind = 'segment'")
    }
    assert {s1, s2, s3} <= used
    assert len(used) == 4  # the lone mic segment was left alone


def test_a_long_pause_splits_conversations_and_a_recent_one_waits(conn: Connection) -> None:
    add_segment(conn, LONG_AGO)
    add_segment(conn, LONG_AGO + 4 * MINUTE)  # 4 minutes of silence: the same call
    add_segment(conn, LONG_AGO + 15 * MINUTE)  # 11 minutes: a new one
    add_segment(conn, NOW - 6 * MINUTE)  # too recent: the call may not be over
    build_episodes(conn, NOW)
    assert len(episodes(conn)) == 2
    build_episodes(conn, NOW + 10 * MINUTE)  # ten minutes later it is closed
    assert len(episodes(conn)) == 3


def test_voice_chat_inside_a_game_is_a_conversation(conn: Connection) -> None:
    add_game(conn, LONG_AGO - 10 * MINUTE, LONG_AGO + HOUR, process="elden.exe")
    add_segment(conn, LONG_AGO, sources=("elden.exe",), text="on prend la gauche")
    add_segment(conn, LONG_AGO + 2 * HOUR, sources=("elden.exe",), text="no game running: ignored")
    build_episodes(conn, NOW)
    assert [k for k, _, _ in episodes(conn)].count("conversation") == 1


def test_untranscribed_or_empty_segments_are_not_conversation_material(conn: Connection) -> None:
    add_segment(conn, LONG_AGO, status="pending", text=None)
    add_segment(conn, LONG_AGO + MINUTE, status="empty", text="")
    assert build_episodes(conn, NOW)["conversations"] == 0


def test_a_conversation_waits_for_transcription_of_earlier_audio(conn: Connection) -> None:
    add_segment(conn, LONG_AGO)
    add_job(conn, "transcribe", "transcribe:9", LONG_AGO + MINUTE)  # still to run
    assert build_episodes(conn, NOW)["conversations"] == 0
    conn.execute("UPDATE jobs SET state = 'done'")
    assert build_episodes(conn, NOW)["conversations"] == 1


def test_the_conversation_scan_remembers_how_far_it_got(conn: Connection) -> None:
    add_segment(conn, LONG_AGO)
    build_episodes(conn, NOW)
    since = get_int(conn, CONVERSATIONS_SINCE, 0)
    assert since == horizon_ms(conn, NOW) - CLOSE_GAP_MS - MIC_MARGIN_MS
    build_episodes(conn, NOW - HOUR)  # a pass never moves it back
    assert get_int(conn, CONVERSATIONS_SINCE, 0) == since


def test_a_long_call_seen_over_many_passes_is_one_episode(conn: Connection) -> None:
    start = LONG_AGO - 3 * HOUR
    add_segment(conn, start - 20_000, stream="mic", sources=(), text="allô ?")  # just before
    for i in range(90):  # a three-hour call, transcribed as it goes, a pass now and then
        at = start + i * 2 * MINUTE
        add_segment(conn, at, text=f"phrase {i}")
        if i % 10 == 9:
            build_episodes(conn, at + MINUTE)
            assert episodes(conn) == []  # still going: nothing is final yet
    build_episodes(conn, NOW)
    assert [k for k, _, _ in episodes(conn)] == ["conversation"]
    linked = conn.execute(
        "SELECT count(*) FROM episode_items WHERE item_kind = 'segment'"
    ).fetchone()[0]
    assert linked == 91  # every line of the call and the microphone "allô" before it


# --- browsing and work ---------------------------------------------------------------------


def test_runs_of_browser_visits_become_browsing_and_other_apps_work(conn: Connection) -> None:
    t = LONG_AGO
    add_visit(
        conn,
        "chrome.exe",
        "Recette de crêpes",
        t,
        t + 3 * MINUTE,
        url="https://cuisine.example/crepes",
    )
    add_visit(conn, "chrome.exe", "Météo Paris", t + 3 * MINUTE + 30_000, t + 5 * MINUTE)
    add_visit(conn, "code.exe", "main.py - Akashic", t + 6 * MINUTE, t + 20 * MINUTE)
    add_visit(conn, "code.exe", "test_main.py - Akashic", t + 21 * MINUTE, t + 30 * MINUTE)

    assert build_episodes(conn, NOW)["app_runs"] == 2

    browsing, work = conn.execute(
        "SELECT kind, title, app, url, body FROM episodes ORDER BY start_ms"
    ).fetchall()
    assert browsing[:3] == ("browsing", "Recette de crêpes", "chrome.exe")
    assert browsing[3] == "https://cuisine.example/crepes"
    assert "Météo Paris" in browsing[4]
    assert work[:3] == ("work", "main.py - Akashic", "code.exe")
    assert work[4].startswith("Code\n")
    assert sum(build_episodes(conn, NOW + HOUR).values()) == 0


def test_short_runs_recent_runs_and_game_time_are_not_episodes(conn: Connection) -> None:
    t = LONG_AGO
    add_visit(conn, "chrome.exe", "Quick look", t, t + MINUTE)  # under three minutes
    add_visit(conn, "code.exe", "Brief edit", t + 10 * MINUTE, t + 12 * MINUTE)  # under five
    add_game(conn, t + 30 * MINUTE, t + 90 * MINUTE)
    add_visit(conn, "game.exe", "The Game", t + 31 * MINUTE, t + 80 * MINUTE)  # during the game
    add_visit(conn, "chrome.exe", "Right now", NOW - 20 * MINUTE, NOW - 2 * MINUTE)
    assert build_episodes(conn, NOW)["app_runs"] == 0
    assert [k for k, _, _ in episodes(conn)] == ["game"]
    # Once the recent run is old enough to be closed it is built; the dropped ones stay dropped.
    assert build_episodes(conn, NOW + 30 * MINUTE)["app_runs"] == 1


def test_episodes_are_searchable_and_their_vectors_follow_their_text(conn: Connection) -> None:
    add_media(conn, "Le fabuleux destin", LONG_AGO, LONG_AGO + 10 * MINUTE)
    build_episodes(conn, NOW)
    (episode_id,) = (r[0] for r in conn.execute("SELECT id FROM episodes"))
    hits = conn.execute("SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH 'fabuleux'")
    assert [r[0] for r in hits] == [episode_id]
    conn.execute(
        "INSERT INTO embeddings VALUES ('episode', ?, 'm', 2, x'0000803f00000000', 0, 0)",
        (episode_id,),
    )
    conn.execute("UPDATE episodes SET start_ms = start_ms + 1")  # not text: vector stays
    assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 1
    conn.execute("UPDATE episodes SET body = 'Un film.' WHERE id = ?", (episode_id,))
    assert conn.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0  # made again later
    conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    assert not conn.execute(
        "SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH 'fabuleux'"
    ).fetchall()
