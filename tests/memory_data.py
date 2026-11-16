"""Rows for the episode and search tests, inserted with plain SQL (no capture or models)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from akashic.storage.frames import NewFrame, NewWindow

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.storage.frames import TextSource

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


def add_monitor(conn: Connection, monitor_id: int = 0) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO monitors (id, key, name) VALUES (?, ?, ?)",
        (monitor_id, f"index:{monitor_id}", f"Monitor {monitor_id + 1}"),
    )
    return monitor_id


def add_frame(  # noqa: PLR0913 - a frame with one focused app window, as plain SQL
    conn: Connection,
    ts_ms: int,
    text: str | None,
    title: str = "Window",
    *,
    process: str = "chrome.exe",
    url: str | None = None,
    monitor_id: int = 0,
    trigger: str = "idle",
    source: str | None = "uia",
    image_path: str | None = None,
    focused: bool = True,
    hwnd: int = 1,
) -> int:
    """Returns the frame id; ``window_of`` gives the id of its window."""
    add_monitor(conn, monitor_id)
    cursor = conn.execute(
        "INSERT INTO frames (ts_ms, monitor_id, trigger, image_path) VALUES (?, ?, ?, ?)",
        (ts_ms, monitor_id, trigger, image_path),
    )
    assert cursor.lastrowid is not None
    conn.execute(
        "INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1, process, title, url,"
        " focused, painted, text, text_source)"
        " VALUES (?, 'app', 0, ?, 0, 0, 100, 100, ?, ?, ?, ?, ?, ?, ?)",
        (
            cursor.lastrowid,
            hwnd,
            process,
            title,
            url,
            int(focused),
            int(image_path is not None),
            text,
            source if text is not None else None,
        ),
    )
    return cursor.lastrowid


def add_window(  # noqa: PLR0913 - a window row, as plain SQL
    conn: Connection,
    frame_id: int,
    z: int,
    text: str | None,
    *,
    process: str = "code.exe",
    title: str | None = "Other",
    url: str | None = None,
    hwnd: int | None = None,
    role: str = "app",
    focused: bool = False,
    source: str | None = "uia",
) -> int:
    cursor = conn.execute(
        "INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1, process, title, url,"
        " focused, painted, text, text_source)"
        " VALUES (?, ?, ?, ?, 0, 0, 100, 100, ?, ?, ?, ?, 0, ?, ?)",
        (
            frame_id,
            role,
            z,
            hwnd if hwnd is not None else (z + 10 if role == "app" else None),
            process if role == "app" else None,
            title if role == "app" else None,
            url if role == "app" else None,
            int(focused),
            text,
            source if text is not None else None,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def window_of(conn: Connection, frame_id: int, z: int = 0) -> int:
    row = conn.execute(
        "SELECT id FROM frame_windows WHERE frame_id = ? AND z = ?", (frame_id, z)
    ).fetchone()
    return int(row[0])


def app_window(  # noqa: PLR0913 - the fields of one app row
    *,
    process: str | None = "chrome.exe",
    title: str | None = "Window",
    url: str | None = None,
    text: str | None = None,
    source: TextSource | None = None,
    z: int = 0,
    hwnd: int = 1,
    focused: bool = True,
    painted: bool = False,
) -> NewWindow:
    return NewWindow(
        "app",
        z,
        hwnd,
        0,
        0,
        100,
        100,
        process=process,
        title=title,
        url=url,
        focused=focused,
        painted=painted,
        text=text,
        text_source=source if source is not None or text is None else "uia",
    )


def new_frame(  # noqa: PLR0913 - a frame with one focused app window
    ts_ms: int,
    monitor_id: int = 0,
    trigger: str = "idle",
    *,
    text: str | None = None,
    source: TextSource | None = None,
    image: bytes | None = None,
    process: str | None = "chrome.exe",
    title: str | None = "Window",
    url: str | None = None,
    focused: bool = True,
) -> NewFrame:
    window = app_window(
        process=process,
        title=title,
        url=url,
        text=text,
        source=source,
        focused=focused,
        painted=image is not None,
    )
    return NewFrame(ts_ms, monitor_id, trigger, image, (window,))
