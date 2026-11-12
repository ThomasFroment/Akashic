"""Rows for the episode and search tests, inserted with plain SQL (no capture or models)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlite3 import Connection

MINUTE = 60_000
HOUR = 60 * MINUTE
# 2026-11-08 12:00 UTC: far from any clock edge the tests care about.
NOW = 1_794_139_200_000


def add_speaker(conn: Connection, name: str | None, *, is_self: bool = False) -> int:
    cursor = conn.execute(
        "INSERT INTO speakers (name, is_self, created_ms) VALUES (?, ?, 0)", (name, int(is_self))
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_segment(  # noqa: PLR0913 - a row of the audio_segments table
    conn: Connection,
    start_ms: int,
    *,
    text: str | None = "bonjour tout le monde",
    stream: str = "system",
    sources: tuple[str, ...] = ("discord.exe",),
    length_ms: int = 4_000,
    speaker: int | None = None,
    status: str = "done",
) -> int:
    cursor = conn.execute(
        "INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status,"
        " transcript, speaker_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            stream,
            start_ms,
            start_ms + length_ms,
            length_ms,
            json.dumps(list(sources)),
            status,
            text,
            speaker,
        ),
    )
    assert cursor.lastrowid is not None
    if speaker is not None:
        conn.execute(
            "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms)"
            " VALUES (?, ?, 0, ?, ?)",
            (cursor.lastrowid, speaker, length_ms, length_ms),
        )
    return cursor.lastrowid


def add_media(  # noqa: PLR0913 - a row of the media_sessions table
    conn: Connection,
    title: str,
    start_ms: int,
    end_ms: int,
    *,
    app: str = "Zen",
    artist: str | None = "A Channel",
    url: str | None = None,
    played_ms: int | None = None,
    closed: bool = True,
) -> int:
    cursor = conn.execute(
        "INSERT INTO media_sessions (app_id, app, title, artist, url, start_ms, end_ms, played_ms,"
        " closed) VALUES ('id', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            app,
            title,
            artist,
            url,
            start_ms,
            end_ms,
            end_ms - start_ms if played_ms is None else played_ms,
            int(closed),
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_game(
    conn: Connection, start_ms: int, end_ms: int, *, process: str = "game.exe", closed: bool = True
) -> int:
    cursor = conn.execute(
        "INSERT INTO game_sessions (process, window_title, start_ms, end_ms, closed)"
        " VALUES (?, 'The Game', ?, ?, ?)",
        (process, start_ms, end_ms, int(closed)),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_visit(  # noqa: PLR0913 - a row of the app_sessions table
    conn: Connection,
    process: str,
    title: str,
    start_ms: int,
    end_ms: int,
    *,
    url: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO app_sessions (process, window_title, url, start_ms, end_ms, frame_count,"
        " last_frame_id, closed) VALUES (?, ?, ?, ?, ?, 1, 0, 1)",
        (process, title, url, start_ms, end_ms),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_job(conn: Connection, kind: str, key: str, data_ms: int, state: str = "pending") -> None:
    conn.execute(
        "INSERT INTO jobs (kind, key, data_ms, state, created_ms) VALUES (?, ?, ?, ?, 0)",
        (kind, key, data_ms, state),
    )


def add_episode(  # noqa: PLR0913, PLR0917 - a row of the episodes table and its people
    conn: Connection,
    kind: str,
    start_ms: int,
    end_ms: int,
    title: str,
    body: str = "",
    *,
    people: tuple[int, ...] = (),
) -> int:
    cursor = conn.execute(
        "INSERT INTO episodes (kind, ref, start_ms, end_ms, title, body) VALUES (?, ?, ?, ?, ?, ?)",
        (kind, f"{kind}:{start_ms}:{title}", start_ms, end_ms, title, body),
    )
    assert cursor.lastrowid is not None
    for speaker in people:
        conn.execute(
            "INSERT INTO episode_people (episode_id, speaker_id, speech_ms) VALUES (?, ?, 1000)",
            (cursor.lastrowid, speaker),
        )
    return cursor.lastrowid


def add_frame(conn: Connection, ts_ms: int, text: str, title: str = "Window") -> int:
    cursor = conn.execute(
        "INSERT INTO frames (ts_ms, monitor, process, window_title, text, text_source, trigger)"
        " VALUES (?, 0, 'chrome.exe', ?, ?, 'uia', 'idle')",
        (ts_ms, title, text),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid
