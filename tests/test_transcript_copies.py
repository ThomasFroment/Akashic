"""Transcript lifecycle must invalidate every saved copy, atomically, without losing other data."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
import pytest
from memory_data import HOUR, NOW, add_episode, add_media, add_segment

from akashic.pipeline import episodes
from akashic.pipeline.episodes import build_conversations, build_media
from akashic.storage import db, embeddings
from akashic.storage.crypto import DataKey
from akashic.storage.media import MediaStore
from akashic.storage.retention import expire_transcripts

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection


def saved_answer(conn: Connection, segment: int, *, thread: int | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO questions (asked_ms, text, state, answer, sources, plan, thread_id)"
        " VALUES (0, 'What happened?', 'done', 'secret answer [1]', ?, '{}', ?)",
        (json.dumps([{"kind": "line", "id": segment, "text": "secret excerpt"}]), thread),
    )
    assert cursor.lastrowid is not None
    conn.execute("INSERT INTO question_segments VALUES (?, ?)", (cursor.lastrowid, segment))
    return cursor.lastrowid


def speech_episodes(conn: Connection) -> tuple[int, int, int, int]:
    first = add_segment(conn, NOW - 3 * HOUR, text="secretphrase ancien rendez-vous")
    second = add_segment(conn, NOW - 3 * HOUR + 5000, text="retainedphrase nouvelle idée")
    add_media(conn, "A video", NOW - 3 * HOUR, NOW - 2 * HOUR)
    # The same recordings can contribute both to a call and to the video's soundtrack.
    conn.execute("UPDATE audio_segments SET sources = '[]'")
    assert build_media(conn, NOW) == 1
    conn.execute("UPDATE audio_segments SET sources = '[\"discord.exe\"]'")
    assert build_conversations(conn, NOW) == 1
    video = int(conn.execute("SELECT id FROM episodes WHERE kind = 'video'").fetchone()[0])
    call = int(conn.execute("SELECT id FROM episodes WHERE kind = 'conversation'").fetchone()[0])
    return first, second, video, call


@pytest.mark.parametrize("action", ["expire", "replace", "delete"])
def test_transcript_changes_rebuild_digests_and_clear_answers_and_later_turns(
    conn: Connection, tmp_path: Path, action: str
) -> None:
    first, second, video, call = speech_episodes(conn)
    other = add_episode(conn, "work", 1, 2, "Unrelated", "independentphrase")
    root = saved_answer(conn, first)
    later = saved_answer(conn, second, thread=root)
    untouched = saved_answer(conn, second)
    for episode in (video, call, other):
        embeddings.put(conn, "episode", episode, "m", np.ones(2, dtype=np.float32), 0, 0)
    if action == "expire":
        media = MediaStore(tmp_path / "media", DataKey.generate().subkey("media"))
        assert expire_transcripts(conn, media, NOW - 3 * HOUR + 1) == 1
    elif action == "replace":
        conn.execute(
            "UPDATE audio_segments SET transcript = 'replacementphrase' WHERE id = ?", (first,)
        )
    else:
        conn.execute("DELETE FROM audio_segments WHERE id = ?", (first,))
    for episode in (video, call):
        title, body = conn.execute(
            "SELECT title, body FROM episodes WHERE id = ?", (episode,)
        ).fetchone()
        assert title
        assert "secretphrase" not in body
        assert "retainedphrase" in body
        assert ("replacementphrase" in body) == (action == "replace")
        assert embeddings.get(conn, "episode", episode, "m") is None
    assert conn.execute(
        "SELECT count(*) FROM episodes_fts WHERE episodes_fts MATCH 'secretphrase'"
    ).fetchone() == (0,)
    assert conn.execute(
        "SELECT count(*) FROM episodes_fts WHERE episodes_fts MATCH 'retainedphrase'"
    ).fetchone() == (2,)
    assert conn.execute("SELECT body FROM episodes WHERE id = ?", (other,)).fetchone() == (
        "independentphrase",
    )
    assert embeddings.get(conn, "episode", other, "m") is not None
    for question in (root, later):
        assert conn.execute(
            "SELECT state, answer, sources, plan FROM questions WHERE id = ?", (question,)
        ).fetchone() == ("failed", "", "[]", None)
    assert conn.execute("SELECT answer FROM questions WHERE id = ?", (untouched,)).fetchone() == (
        "secret answer [1]",
    )
    assert (
        conn.execute("SELECT transcript FROM audio_segments WHERE id = ?", (second,))
        .fetchone()[0]
        .startswith("retainedphrase")
    )


def test_deletion_still_finds_answer_copies_after_episode_retention(conn: Connection) -> None:
    first, _, _, _ = speech_episodes(conn)
    question = saved_answer(conn, first)
    conn.execute("DELETE FROM episodes")
    conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (first,))
    assert conn.execute("SELECT answer FROM questions WHERE id = ?", (question,)).fetchone() == (
        "",
    )


def test_failed_deletion_rolls_back_all_derived_changes(conn: Connection) -> None:
    first, _, _, call = speech_episodes(conn)
    question = saved_answer(conn, first)

    def interrupt() -> None:
        with db.transaction(conn):
            conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (first,))
            raise RuntimeError("interrupt")

    with pytest.raises(RuntimeError, match="interrupt"):
        interrupt()
    assert (
        "secretphrase"
        in conn.execute("SELECT body FROM episodes WHERE id = ?", (call,)).fetchone()[0]
    )
    assert conn.execute(
        "SELECT state, answer FROM questions WHERE id = ?", (question,)
    ).fetchone() == ("done", "secret answer [1]")


def test_retranscription_repopulates_digest_but_does_not_restore_old_answer(
    conn: Connection,
) -> None:
    first, _, _, call = speech_episodes(conn)
    question = saved_answer(conn, first)
    conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (first,))
    conn.execute("UPDATE audio_segments SET transcript = 'correctedphrase' WHERE id = ?", (first,))
    body = conn.execute("SELECT body FROM episodes WHERE id = ?", (call,)).fetchone()[0]
    assert "correctedphrase" in body
    assert "secretphrase" not in body
    assert conn.execute("SELECT answer FROM questions WHERE id = ?", (question,)).fetchone() == (
        "",
    )


def test_unchanged_transcript_and_audio_removal_leave_derived_text(conn: Connection) -> None:
    first, _, _, call = speech_episodes(conn)
    question = saved_answer(conn, first)
    conn.execute("UPDATE audio_segments SET transcript = transcript, audio_path = NULL")
    assert (
        "secretphrase"
        in conn.execute("SELECT body FROM episodes WHERE id = ?", (call,)).fetchone()[0]
    )
    assert conn.execute(
        "SELECT state, answer FROM questions WHERE id = ?", (question,)
    ).fetchone() == ("done", "secret answer [1]")


def test_removing_all_speech_keeps_episode_metadata_and_no_transcript_text(
    conn: Connection,
) -> None:
    speech_episodes(conn)
    conn.execute("UPDATE audio_segments SET transcript = NULL")
    bodies = dict(conn.execute("SELECT kind, body FROM episodes"))
    assert bodies == {"conversation": "", "video": "A video - A Channel (Zen)"}


def test_concurrent_transcript_expiry_does_not_restore_digest_or_skip_remaining_speech(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = add_segment(conn, NOW - 3 * HOUR, text="removed secretphrase")
    add_segment(conn, NOW - 3 * HOUR + 5000, text="retainedphrase")
    original = episodes._conversation_clusters

    def read_then_expire(
        conn: Connection, horizon: int, since_ms: int
    ) -> tuple[list[episodes._Cluster], int | None]:
        found = original(conn, horizon, since_ms)
        conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (first,))
        return found

    with monkeypatch.context() as patch:
        patch.setattr(episodes, "_conversation_clusters", read_then_expire)
        assert build_conversations(conn, NOW) == 0
    assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (0,)
    assert build_conversations(conn, NOW) == 1
    assert conn.execute("SELECT body FROM episodes").fetchone() == ("retainedphrase",)


def test_media_digest_cannot_publish_speech_removed_after_its_read(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = add_segment(conn, NOW - 3 * HOUR, text="removed secretphrase", sources=("zen.exe",))
    add_media(conn, "A video", NOW - 3 * HOUR, NOW - 2 * HOUR)
    original = episodes._segments

    def read_then_expire(conn: Connection, start_ms: int, end_ms: int) -> list[episodes._Segment]:
        found = original(conn, start_ms, end_ms)
        conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (first,))
        return found

    with monkeypatch.context() as patch:
        patch.setattr(episodes, "_segments", read_then_expire)
        assert build_media(conn, NOW) == 0
    assert build_media(conn, NOW) == 1
    assert conn.execute("SELECT body FROM episodes").fetchone() == ("A video - A Channel (Zen)",)
