"""Known defects of episode building and the media sessions that feed it (audit Phase 2).

Each ``known_defect`` test asserts the behaviour the audit asks for and fails today for the
reason the finding gives. Plain tests here are baselines the findings rely on (they pass today).
Rows are inserted with plain SQL (``memory_data``), media sessions come from ``MediaTracker``
against a fake OS source, and nothing loads a model.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from capture_fakes import FakeMedia
from defects import known_defect
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

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.media import MAX_POLL_GAP_MS, PAUSE_CLOSE_MS, MediaTracker
from akashic.capture.ports import MediaState
from akashic.config import Exclusions as UserExclusions
from akashic.pipeline.episodes import (
    BROWSER_PROCESSES,
    MAX_BODY_CHARS,
    build_episodes,
)
from akashic.search.hybrid import search_memory
from akashic.search.planner import heuristic_plan
from akashic.storage.retention import (
    delete_spent_segments,
    expire_app_sessions,
    expire_episodes,
    expire_media_sessions,
    expire_transcripts,
)
from akashic.storage.sessions import MediaLog
from akashic.storage.speakers import SpeakerStore

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.capture.ports import PlaybackState
    from akashic.storage.media import MediaStore

LONG_AGO = NOW - 6 * HOUR  # long closed: every builder may use it
NBSP = chr(0xA0)  # the non-breaking space of French typography ("Tu viens ?")


def episodes(conn: Connection) -> list[tuple[str, str, str | None, str]]:
    """(kind, title, app, body) of every episode, oldest first."""
    return conn.execute(
        "SELECT kind, title, app, body FROM episodes ORDER BY start_ms, id"
    ).fetchall()


def linked(conn: Connection, item_kind: str) -> set[int]:
    return {
        int(r[0])
        for r in conn.execute("SELECT item_id FROM episode_items WHERE item_kind = ?", (item_kind,))
    }


# --- #3: one digest format, built twice -----------------------------------------------------


# Pins the retained_episode_bodies view: delete or rewrite with M06.2.
def _python_and_view_bodies(conn: Connection) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT e.body, r.body FROM episodes e JOIN retained_episode_bodies r ON r.id = e.id"
        " ORDER BY e.id"
    ).fetchall()


def test_the_deletion_view_rebuilds_the_same_digest_as_the_builders(conn: Connection) -> None:
    """Baseline for #3: on ordinary evidence the SQL view and the Python builders agree."""
    add_media(conn, "Les secrets du jeu", LONG_AGO, LONG_AGO + 10 * MINUTE, artist="Chaîne")
    add_segment(conn, LONG_AGO + MINUTE, text="aujourd'hui, stratégie !", sources=("zen.exe",))
    add_media(
        conn, "Song", LONG_AGO + HOUR, LONG_AGO + HOUR + 5 * MINUTE, app="Spotify", artist=None
    )
    me = add_speaker(conn, None, is_self=True)
    add_segment(conn, LONG_AGO + 2 * HOUR, text="salut Minlo, ça va ?")
    add_segment(conn, LONG_AGO + 2 * HOUR + 4_500, text="yes, and you?", stream="mic", speaker=me)
    build_episodes(conn, NOW)
    pairs = _python_and_view_bodies(conn)
    assert len(pairs) == 3
    for built, rebuilt in pairs:
        assert built == rebuilt


def test_a_clipped_long_digest_matches_the_view(conn: Connection) -> None:
    """Baseline for #3: the 6,000-character clip agrees when the cut lands on a plain word."""
    for i in range(40):
        add_segment(conn, LONG_AGO + i * 5_000, text=f"phrase numéro {i} " + "blabla " * 30)
    build_episodes(conn, NOW)
    ((built, rebuilt),) = _python_and_view_bodies(conn)
    assert len(built) == MAX_BODY_CHARS
    assert built == rebuilt


@known_defect(3, "SQL rtrim and Python rstrip clip a digest differently")
def test_a_clip_ending_on_a_non_breaking_space_matches_the_view(conn: Connection) -> None:
    # French typography puts a non-breaking space before "?". When the 6,000-character clip
    # lands right after one, rstrip() removes it but SQLite's rtrim() keeps it.
    filler = ("tu viens ce soir " * 400)[: MAX_BODY_CHARS - 2]
    add_segment(conn, LONG_AGO, text=filler + NBSP + "? on lance la partie", length_ms=25_000)
    build_episodes(conn, NOW)
    ((built, rebuilt),) = _python_and_view_bodies(conn)
    assert built == rebuilt


# --- #11 and #165: app identity -------------------------------------------------------------

_SPOKEN_NAMES = {
    "chrome.exe": "Chrome",
    "brave.exe": "Brave",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "zen.exe": "Zen",
    "opera.exe": "Opera",
    "vivaldi.exe": "Vivaldi",
}
_UNKNOWN_TO_PLANNER = {
    "msedge.exe": known_defect(165, "the planner only knows 'msedge', not 'Edge'"),
    "opera.exe": known_defect(11, "the planner's browser list differs from the episodes' list"),
    "vivaldi.exe": known_defect(11, "the planner's browser list differs from the episodes' list"),
}


@pytest.mark.parametrize(
    "process",
    [
        pytest.param(p, marks=_UNKNOWN_TO_PLANNER[p]) if p in _UNKNOWN_TO_PLANNER else p
        for p in sorted(BROWSER_PROCESSES)
    ],
)
def test_every_browser_the_episodes_know_can_be_named_in_a_question(process: str) -> None:
    plan = heuristic_plan(f"What did I read on {_SPOKEN_NAMES[process]}?", NOW)
    assert plan.apps == (process,)


@known_defect(165, "video episodes store the display name, the app filter compares 'chrome'")
def test_a_chrome_video_is_found_when_the_question_names_chrome(conn: Connection) -> None:
    add_media(conn, "Les secrets du jeu", LONG_AGO, LONG_AGO + 10 * MINUTE, app="Google Chrome")
    build_episodes(conn, NOW)
    plan = heuristic_plan("What video did I watch on Chrome?", NOW)
    assert plan.apps == ("chrome.exe",)
    found = search_memory(conn, plan, None, "m")
    assert [e.title for e in found.episodes] == ["Les secrets du jeu"]


@known_defect(165, "'on Edge' sets no app filter, so the Chrome video answers too")
def test_an_edge_question_finds_only_the_edge_video(conn: Connection) -> None:
    add_media(conn, "Chrome video", LONG_AGO, LONG_AGO + 10 * MINUTE, app="Google Chrome")
    add_media(
        conn, "Edge video", LONG_AGO + HOUR, LONG_AGO + HOUR + 10 * MINUTE, app="Microsoft Edge"
    )
    build_episodes(conn, NOW)
    found = search_memory(conn, heuristic_plan("What video did I watch on Edge?", NOW), None, "m")
    assert [e.title for e in found.episodes] == ["Edge video"]


@known_defect(166, "app_matches_source('Microsoft Edge', ('msedge.exe',)) is False")
def test_an_edge_video_keeps_its_soundtrack(conn: Connection) -> None:
    add_media(conn, "Dragons", LONG_AGO, LONG_AGO + 10 * MINUTE, app="Microsoft Edge")
    heard = add_segment(conn, LONG_AGO + MINUTE, text="les dragons volent", sources=("msedge.exe",))
    build_episodes(conn, NOW)
    ((_, _, _, body),) = episodes(conn)
    assert "les dragons volent" in body
    assert heard in linked(conn, "video_segment")


# --- #32: retention shorter for episodes than for their sources -----------------------------


@known_defect(32, "expired media and game episodes are rebuilt from the kept sessions")
def test_expired_episodes_are_not_built_again_from_kept_sessions(
    conn: Connection, media: MediaStore
) -> None:
    add_media(conn, "Old video", LONG_AGO, LONG_AGO + 10 * MINUTE)
    add_segment(conn, LONG_AGO + MINUTE, text="ce qui a été dit", sources=("zen.exe",))
    add_game(conn, LONG_AGO + HOUR, LONG_AGO + 2 * HOUR)
    build_episodes(conn, NOW)
    assert {k for k, *_ in episodes(conn)} == {"video", "game"}
    expire_episodes(conn, media, NOW)  # episodes kept for less time than sessions
    assert episodes(conn) == []
    rebuilt = build_episodes(conn, NOW + MINUTE)
    assert episodes(conn) == []
    assert sum(rebuilt.values()) == 0


# --- #46: speaker merge and turn reassignment -----------------------------------------------


def _people(conn: Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT speaker_id FROM episode_people ORDER BY 1")]


@known_defect(46, "merge deletes the source speaker and episode_people cascades away")
def test_a_merged_voice_stays_a_participant_of_its_conversations(conn: Connection) -> None:
    minlo = add_speaker(conn, "Minlo")
    unnamed = add_speaker(conn, None)  # Minlo's second profile, not yet merged
    add_segment(conn, LONG_AGO, text="on lance une partie ?", speaker=unnamed)
    build_episodes(conn, NOW)
    assert _people(conn) == [unnamed]
    SpeakerStore(conn).merge(unnamed, minlo)
    assert _people(conn) == [minlo]


@known_defect(46, "reassign_turn leaves episode_people stale")
def test_a_reassigned_turn_updates_the_conversation_participants(conn: Connection) -> None:
    minlo = add_speaker(conn, "Minlo")
    stranger = add_speaker(conn, None)
    segment = add_segment(conn, LONG_AGO, text="on lance une partie ?", speaker=stranger)
    build_episodes(conn, NOW)
    (turn,) = conn.execute("SELECT id FROM speaker_turns WHERE segment_id = ?", (segment,))
    SpeakerStore(conn).reassign_turn(int(turn[0]), minlo, now_ms=NOW)
    assert _people(conn) == [minlo]


# --- #164: SQLite reuses the rowids of deleted sessions and segments -------------------------


@known_defect(164, "a reused media_sessions rowid matches an old episode item")
def test_a_video_with_a_reused_session_id_still_becomes_an_episode(
    conn: Connection, media: MediaStore
) -> None:
    first = add_media(conn, "Old video", LONG_AGO - 3 * HOUR, LONG_AGO - 2 * HOUR)
    build_episodes(conn, LONG_AGO)
    expire_media_sessions(conn, media, LONG_AGO)  # session rows go, the episode stays
    again = add_media(conn, "New video", LONG_AGO, LONG_AGO + 10 * MINUTE)
    assert again == first  # SQLite handed out the same rowid again
    build_episodes(conn, NOW)
    assert [t for _, t, *_ in episodes(conn)] == ["Old video", "New video"]


@known_defect(164, "a reused game_sessions rowid matches an old episode item")
def test_a_game_with_a_reused_session_id_still_becomes_an_episode(
    conn: Connection, media: MediaStore
) -> None:
    first = add_game(conn, LONG_AGO - 3 * HOUR, LONG_AGO - 2 * HOUR)
    build_episodes(conn, LONG_AGO)
    expire_app_sessions(conn, media, LONG_AGO)
    again = add_game(conn, LONG_AGO, LONG_AGO + HOUR, process="other.exe")
    assert again == first
    build_episodes(conn, NOW)
    assert [a for k, _, a, _ in episodes(conn) if k == "game"] == ["game.exe", "other.exe"]


@known_defect(164, "conversation:<id> collides and build_conversations stops advancing")
def test_conversations_continue_after_the_newest_segments_expired(
    conn: Connection, media: MediaStore
) -> None:
    t0 = NOW - 20 * HOUR
    first = add_segment(conn, t0, text="the first call")
    build_episodes(conn, t0 + HOUR)
    expire_transcripts(conn, media, t0 + HOUR)
    delete_spent_segments(conn, t0 + HOUR)
    assert conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0] == 0
    again = add_segment(conn, t0 + 2 * HOUR, text="a later call")
    add_segment(conn, t0 + 4 * HOUR, text="an even later call")
    assert again == first
    for later in (NOW, NOW + MINUTE, NOW + HOUR):  # every pass, not just the first
        build_episodes(conn, later)
    bodies = [b for k, _, _, b in episodes(conn) if k == "conversation"]
    assert "a later call" in bodies
    assert "an even later call" in bodies


# --- #168 to #174, #178: what a media or conversation episode contains ------------------------


@known_defect(168, "a video claims every source-less segment in its span")
def test_a_video_does_not_claim_unattributed_speech_during_a_call(conn: Connection) -> None:
    add_media(conn, "Background video", LONG_AGO, LONG_AGO + 20 * MINUTE)
    add_segment(conn, LONG_AGO + MINUTE, text="tu m'entends ?", sources=("discord.exe",))
    unknown = add_segment(conn, LONG_AGO + 2 * MINUTE, text="on lance la partie", sources=())
    build_episodes(conn, NOW)
    assert unknown not in linked(conn, "video_segment")


@known_defect(169, "media bodies are never rebuilt once a segment is transcribed late")
def test_a_video_gains_a_soundtrack_line_transcribed_after_it_was_built(conn: Connection) -> None:
    add_media(conn, "Dragons", LONG_AGO, LONG_AGO + 10 * MINUTE)
    late = add_segment(conn, LONG_AGO + MINUTE, text=None, status="failed", sources=("zen.exe",))
    build_episodes(conn, NOW)
    conn.execute(
        "UPDATE audio_segments SET status = 'done', transcript = 'les dragons volent' WHERE id = ?",
        (late,),
    )  # a retried job, or a re-transcription
    build_episodes(conn, NOW + MINUTE)
    ((_, _, _, body),) = episodes(conn)
    assert "les dragons volent" in body


@known_defect(169, "the conversation watermark has passed a segment that becomes done late")
def test_a_call_line_transcribed_late_still_joins_a_conversation(conn: Connection) -> None:
    add_segment(conn, LONG_AGO, text="salut Minlo")
    late = add_segment(conn, LONG_AGO + MINUTE, text=None, status="failed")
    build_episodes(conn, NOW)
    conn.execute(
        "UPDATE audio_segments SET status = 'done', transcript = 'on parle du jeu' WHERE id = ?",
        (late,),
    )
    build_episodes(conn, NOW + MINUTE)
    assert late in linked(conn, "segment")


@known_defect(170, "without a voice app the conversation takes the first source, not the game")
def test_in_game_voice_chat_is_named_after_the_game(conn: Connection) -> None:
    add_game(conn, LONG_AGO - 10 * MINUTE, LONG_AGO + HOUR, process="elden.exe")
    # The sampler sorts sources, so a browser playing alongside comes first.
    add_segment(conn, LONG_AGO, text="on prend la gauche", sources=("chrome.exe", "elden.exe"))
    build_episodes(conn, NOW)
    ((title, app),) = conn.execute(
        "SELECT title, app FROM episodes WHERE kind = 'conversation'"
    ).fetchall()
    assert app == "elden.exe"
    assert title == "Elden conversation"


@known_defect(171, "media played under 60 s never becomes an episode")
def test_a_short_clip_played_to_the_end_is_a_video(conn: Connection) -> None:
    add_media(conn, "Un short marrant", LONG_AGO, LONG_AGO + 40_000)
    build_episodes(conn, NOW)
    assert [(k, t) for k, t, *_ in episodes(conn)] == [("video", "Un short marrant")]


@known_defect(172, "_runs splits on every kind switch, so alternating apps leave no episode")
def test_alternating_between_an_editor_and_docs_is_one_work_stretch(conn: Connection) -> None:
    t = LONG_AGO
    for i in range(10):  # 20 minutes: two minutes of code, two of documentation, repeated
        process, title = ("code.exe", "main.py") if i % 2 == 0 else ("chrome.exe", "Python docs")
        add_visit(conn, process, title, t + i * 2 * MINUTE, t + (i + 1) * 2 * MINUTE - 1_000)
    build_episodes(conn, NOW)
    assert [k for k, *_ in episodes(conn)] in (["work"], ["browsing"])


@known_defect(173, "media waits for the shared horizon, which also counts speaker jobs")
def test_a_video_is_built_while_an_older_speaker_job_waits(conn: Connection) -> None:
    add_job(conn, "speakers", "speakers:1", LONG_AGO - HOUR)  # deferred while gaming, say
    add_media(conn, "Dragons", LONG_AGO, LONG_AGO + 10 * MINUTE)
    build_episodes(conn, NOW)
    assert [k for k, *_ in episodes(conn)] == ["video"]


@known_defect(174, "a body rebuild re-reads the channel from expired media_sessions")
def test_a_rebuilt_video_body_keeps_its_channel(conn: Connection, media: MediaStore) -> None:
    add_media(conn, "Dragons", LONG_AGO, LONG_AGO + 10 * MINUTE, artist="Chaîne Nature")
    add_segment(conn, LONG_AGO + MINUTE, text="les dragons volent", sources=("zen.exe",))
    build_episodes(conn, NOW)
    expire_media_sessions(conn, media, NOW)
    expire_transcripts(conn, media, NOW)  # the soundtrack expires: the body is rebuilt
    ((_, _, _, body),) = episodes(conn)
    assert "les dragons volent" not in body
    assert "Chaîne Nature" in body


@known_defect(178, "conversation digests carry no speaker labels")
def test_a_conversation_digest_says_who_spoke(conn: Connection) -> None:
    me = add_speaker(conn, None, is_self=True)
    minlo = add_speaker(conn, "Minlo")
    add_segment(conn, LONG_AGO, text="il sort le mois prochain", speaker=minlo)
    add_segment(conn, LONG_AGO + 5_000, text="trop bien", stream="mic", sources=(), speaker=me)
    build_episodes(conn, NOW)
    ((_, _, _, body),) = episodes(conn)
    assert "Minlo: il sort le mois prochain" in body
    assert "You: trop bien" in body


# --- edge cases the builders must survive ----------------------------------------------------


def test_a_call_across_midnight_is_one_conversation(conn: Connection) -> None:
    midnight = NOW - 12 * HOUR  # 2026-11-08 00:00 UTC
    for i in range(12):  # 23:30 to 00:25, a line every five minutes minus a second
        add_segment(conn, midnight - 30 * MINUTE + i * (5 * MINUTE - 1_000), text=f"ligne {i}")
    build_episodes(conn, NOW)
    ((kind, _, _, body),) = episodes(conn)
    assert kind == "conversation"
    assert body.startswith("ligne 0 ")
    assert body.endswith(" ligne 11")


def test_many_voices_in_one_call_are_all_participants(conn: Connection) -> None:
    voices = [add_speaker(conn, f"Voice {i}") for i in range(12)]
    for i, voice in enumerate(voices):
        add_segment(conn, LONG_AGO + i * 10_000, text=f"je suis {i}", speaker=voice)
    build_episodes(conn, NOW)
    assert _people(conn) == voices


# --- #142, #145, #146, #167: the media sessions episodes are built from ----------------------


@dataclass
class Rig:
    tracker: MediaTracker
    source: FakeMedia
    conn: Connection
    clock: list[int]

    def at(self, seconds: float, state: PlaybackState | None = None) -> None:
        if state is not None:
            self.source.states = [MediaState("C1", "Elden Ring", "Chan", state)]
        self.clock[0] = int(seconds * 1000)
        self.tracker.poll()

    def rows(self) -> list[tuple[int, int, int, str | None]]:
        return self.conn.execute(
            "SELECT start_ms, end_ms, closed, url FROM media_sessions ORDER BY id"
        ).fetchall()


@pytest.fixture
def rig(conn: Connection) -> Rig:
    clock = [0]
    source = FakeMedia(names={"C1": "Google Chrome"})
    tracker = MediaTracker(
        source=source,
        log_store=MediaLog(conn),
        exclusions=Exclusions(UserExclusions()),
        focused_window=lambda: None,
        now_ms=lambda: clock[0],
        interval_s=0.01,
        browsers=BrowserState(),
    )
    return Rig(tracker, source, conn, clock)


def _play_one_minute(rig: Rig) -> None:
    for second in range(0, 61, 2):
        rig.at(second, "playing")


@known_defect(167, "a paused video's end_ms keeps advancing and it closes at 'now'")
def test_a_paused_video_ends_when_it_was_paused(rig: Rig) -> None:
    _play_one_minute(rig)
    for second in range(62, 62 + 5 * 60, 2):
        rig.at(second, "paused")
    assert rig.rows()[0][1] <= 62_000  # five minutes into the pause it has not grown
    for second in range(362, 62 + PAUSE_CLOSE_MS // 1000 + 4, 2):
        rig.at(second, "paused")
    ((_, end_ms, closed, _),) = rig.rows()
    assert closed == 1
    assert end_ms <= 62_000


@known_defect(142, "a paused video closes at the wake time after a suspend")
def test_a_paused_video_does_not_span_the_night_after_a_suspend(rig: Rig) -> None:
    _play_one_minute(rig)
    rig.at(62, "paused")
    rig.at(64, "paused")
    rig.at(9 * 3600, "paused")  # the PC slept; the first poll after waking
    ((_, end_ms, closed, _),) = rig.rows()
    assert closed == 1
    assert end_ms <= 64_000


@known_defect(142, "a playing video seen again after a suspend continues the same row")
def test_a_suspend_while_playing_ends_the_row_at_the_last_poll(rig: Rig) -> None:
    _play_one_minute(rig)
    rig.at(9 * 3600, "playing")
    rig.tracker.close_all()
    first = rig.rows()[0]
    assert first[1] <= 60_000 + MAX_POLL_GAP_MS


def _frame(conn: Connection, ts_ms: int, process: str, title: str, url: str) -> None:
    conn.execute(
        "INSERT INTO frames (ts_ms, monitor, process, window_title, url, trigger)"
        " VALUES (?, 0, ?, ?, ?, 'idle')",
        (ts_ms, process, title, url),
    )


@known_defect(145, "the first frame whose title contains the media title is locked in")
def test_a_search_page_seen_first_does_not_become_the_video_address(rig: Rig) -> None:
    _frame(
        rig.conn, 500, "chrome.exe", "Elden Ring - Recherche Google", "https://google.com/search"
    )
    rig.at(1, "playing")  # the video tab's frame does not exist yet
    _frame(rig.conn, 3_000, "chrome.exe", "Elden Ring - YouTube", "https://youtube.com/watch?v=a")
    for second in range(3, 30, 2):
        rig.at(second, "playing")
    assert rig.rows()[0][3] == "https://youtube.com/watch?v=a"


@known_defect(145, "find_url ignores which process showed the title")
def test_the_video_address_comes_from_the_browser_that_plays_it(rig: Rig) -> None:
    _frame(rig.conn, 400, "chrome.exe", "Elden Ring - YouTube", "https://youtube.com/watch?v=c")
    _frame(rig.conn, 500, "zen.exe", "Elden Ring - YouTube", "https://youtube.com/watch?v=z")
    rig.at(1, "playing")
    assert rig.rows()[0][3] == "https://youtube.com/watch?v=c"


@known_defect(146, "a lock (or a UAC prompt) closes every media row while audio still records")
def test_a_brief_uac_prompt_does_not_split_a_video(rig: Rig) -> None:
    rig.source.states = [MediaState("C1", "Elden Ring", "Chan", "playing")]
    stop, paused = threading.Event(), threading.Event()
    script = [False] * 5 + [True] + [False] * 5  # one poll sees the secure desktop

    def locked() -> bool:
        rig.clock[0] += 2_000
        if not script:
            stop.set()
            return False
        return script.pop(0)

    rig.tracker.run(stop, paused, locked)
    assert len(rig.rows()) == 1
