"""A small live store with rows on both sides of a range, for the export tests.

The range is [LOW, HIGH). Every kind of evidence has a row before, across the start, inside, and
after the range, each with its own unique word, so a test can search the export for it.
"""

from __future__ import annotations

import struct
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from memory_data import (
    MINUTE,
    NOW,
    add_episode,
    add_frame,
    add_game,
    add_job,
    add_media,
    add_monitor,
    add_segment,
    add_speaker,
    add_visit,
    add_window,
    window_of,
)

from akashic.config import Paths
from akashic.storage import db
from akashic.storage.crypto import unwrap_data_key
from akashic.storage.export import ExportCancel, ExportResult, ExportScope, write_export
from akashic.storage.gaps import GapLog

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.storage.crypto import DataKey
    from akashic.storage.media import MediaStore

LOW = NOW + 10 * MINUTE
HIGH = NOW + 20 * MINUTE
PASSPHRASE = "export passphrase"  # noqa: S105 - a test value
SEG_MS = 4_000
MODEL = "m"


def vec(*values: float) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


@dataclass
class World:
    root: Path
    key: DataKey
    conn: Connection
    media: MediaStore
    ids: dict[str, int] = field(default_factory=dict[str, int])
    files: dict[str, tuple[str, bytes]] = field(default_factory=dict[str, tuple[str, bytes]])


def _file(world: World, name: str, folder: str) -> str:
    data = f"bytes of {name}".encode()
    path = world.media.write_unique(f"{folder}/{name}.bin", data)
    world.files[name] = (path, data)
    return path


def _passages(conn: Connection, table: str, column: str, owner: int) -> None:
    conn.execute(
        f"INSERT INTO {table} ({column}, model, start_char, end_char, dim, vec)"  # noqa: S608
        " VALUES (?, ?, 0, 5, 1, ?)",
        (owner, MODEL, b"\x00\x00"),
    )


def _embedding(conn: Connection, kind: str, owner: int, start_ms: int) -> None:
    conn.execute(
        "INSERT INTO embeddings (owner_kind, owner_id, model, dim, vec, start_ms, created_ms)"
        " VALUES (?, ?, ?, 1, ?, ?, 0)",
        (kind, owner, MODEL, b"\x00\x00", start_ms),
    )


def _frames(world: World) -> None:
    conn, ids = world.conn, world.ids
    cases = {
        "before": (NOW + MINUTE, "quokkabefore"),
        "in": (NOW + 12 * MINUTE, "quokkainside"),
        "edgewin": (LOW + 30_000, "quokkaedgewin"),
        "after": (NOW + 25 * MINUTE, "quokkaafter"),
        "end": (HIGH, "quokkaend"),
    }
    for name, (ts, word) in cases.items():
        title = {"in": "In page", "edgewin": "Edge page"}.get(name, f"title {name}")
        shot = _file(world, f"shot_{name}", "frames")
        frame = add_frame(conn, ts, f"text {word}", title, image_path=shot)
        ids[f"frame_{name}"] = frame
        ids[f"win_{name}"] = window_of(conn, frame)
        _passages(conn, "window_passages", "window_id", ids[f"win_{name}"])
    # A second window waiting for deferred OCR, and a window that is not an app (no content).
    capture = _file(world, "capture_in", "windows")
    other = add_window(conn, ids["frame_in"], 1, None, title="Other")
    conn.execute("UPDATE frame_windows SET capture_path = ? WHERE id = ?", (capture, other))
    ids["win_capture"] = other
    add_window(conn, ids["frame_in"], 2, None, role="shell")


def _monitors(world: World) -> None:
    conn = world.conn
    add_monitor(conn, 0)
    conn.execute("INSERT INTO monitor_layouts (start_ms) VALUES (?)", (NOW,))
    conn.execute(
        "INSERT INTO layout_monitors (layout_id, monitor_id, x0, y0, x1, y1, scale, is_main)"
        " VALUES (1, 0, 0, 0, 100, 100, 1.0, 1)"
    )
    for state, start, end in (
        ("shown", NOW, NOW + 30 * MINUTE),
        ("hidden", NOW + MINUTE, NOW + 2 * MINUTE),
        ("failed", NOW + 5 * MINUTE, LOW),
        ("dropped", NOW + 12 * MINUTE, NOW + 13 * MINUTE),
    ):
        conn.execute(
            "INSERT INTO monitor_states (monitor_id, state, start_ms, checked_ms)"
            " VALUES (0, ?, ?, ?)",
            (state, start, end),
        )


def _segments(world: World) -> None:
    conn, ids = world.conn, world.ids
    starts = {
        "before": NOW + MINUTE,
        "pre": NOW + 8 * MINUTE,
        "touch": LOW - SEG_MS,
        "edge": LOW - 2_000,
        "in": NOW + 12 * MINUTE,
        "after": NOW + 25 * MINUTE,
    }
    for name, start in starts.items():
        seg = add_segment(conn, start, text=f"wombat{name}")
        ids[f"seg_{name}"] = seg
        audio = _file(world, f"audio_{name}", "audio")
        conn.execute("UPDATE audio_segments SET audio_path = ? WHERE id = ?", (audio, seg))
        _passages(conn, "segment_passages", "segment_id", seg)
        _embedding(conn, "segment", seg, start)


def _speakers(world: World) -> None:
    conn, ids = world.conn, world.ids
    names = ("me", "a", "b", "c", "d", "e")
    for name in names:
        ids[f"spk_{name}"] = add_speaker(conn, name, is_self=name == "me")
    junk = vec(9, 9, 9, 9)
    conn.execute("UPDATE speakers SET vec_sum = ?, vec_n = 9", (junk,))
    turns = (
        ("in", "a", vec(1, 0, 0, 0), 1),
        ("before", "a", vec(0, 1, 0, 0), 1),
        ("edge", "a", vec(0, 0, 1, 0), 0),
        ("before", "b", vec(0, 0, 0, 1), 1),
    )
    for seg, speaker, embedding, in_profile in turns:
        conn.execute(
            "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms,"
            " embedding, in_profile) VALUES (?, ?, 0, 1000, 1000, ?, ?)",
            (ids[f"seg_{seg}"], ids[f"spk_{speaker}"], embedding, in_profile),
        )
    conn.execute(
        "UPDATE audio_segments SET speaker_id = ? WHERE id = ?", (ids["spk_e"], ids["seg_in"])
    )


def _sessions(world: World) -> None:
    conn, ids = world.conn, world.ids
    media = {
        "before": (NOW + MINUTE, NOW + 2 * MINUTE),
        "edge": (LOW - MINUTE, LOW + MINUTE),
        "in": (NOW + 12 * MINUTE, NOW + 14 * MINUTE),
        "after": (NOW + 25 * MINUTE, NOW + 26 * MINUTE),
    }
    for name, (start, end) in media.items():
        ids[f"media_{name}"] = add_media(conn, f"platypus{name}", start, end)
        for ts in (start, end - 1_000):
            conn.execute(
                "INSERT INTO media_events (media_session_id, ts_ms, state)"
                " VALUES (?, ?, 'playing')",
                (ids[f"media_{name}"], ts),
            )
    for name, (start, end) in media.items():
        ids[f"visit_{name}"] = add_visit(conn, "chrome.exe", f"visit {name}", start, end)
    ids["game_before"] = add_game(conn, NOW + MINUTE, NOW + 2 * MINUTE)
    ids["game_in"] = add_game(conn, NOW + 12 * MINUTE, NOW + 14 * MINUTE)


def _episodes(world: World) -> None:
    conn, ids = world.conn, world.ids

    def episode(  # noqa: PLR0913, PLR0917 - one row and its items
        name: str,
        kind: str,
        start: int,
        end: int,
        title: str,
        items: tuple[tuple[str, str], ...],
        *,
        body: str = "",
        people: tuple[int, ...] = (),
    ) -> None:
        ids[f"ep_{name}"] = add_episode(conn, kind, start, end, title, body, people=people)
        for item_kind, item in items:
            conn.execute(
                "INSERT INTO episode_items (episode_id, item_kind, item_id) VALUES (?, ?, ?)",
                (ids[f"ep_{name}"], item_kind, ids[item]),
            )

    episode(
        "before",
        "conversation",
        NOW + MINUTE,
        NOW + MINUTE + SEG_MS,
        "call before",
        (("segment", "seg_before"),),
        people=(ids["spk_b"],),
    )
    episode(
        "conv_edge",
        "conversation",
        NOW + 8 * MINUTE,
        LOW + 2_000,
        "call edge",
        (("segment", "seg_pre"), ("segment", "seg_edge")),
    )
    episode(
        "conv_in",
        "conversation",
        NOW + 12 * MINUTE,
        NOW + 12 * MINUTE + SEG_MS,
        "call in",
        (("segment", "seg_in"),),
        people=(ids["spk_d"],),
    )
    episode(
        "video_in",
        "video",
        NOW + 12 * MINUTE,
        NOW + 14 * MINUTE,
        "platypusin",
        (("media_session", "media_in"),),
    )
    episode(
        "video_edge",
        "video",
        LOW - MINUTE,
        LOW + MINUTE,
        "platypusedge",
        (("media_session", "media_edge"),),
    )
    episode(
        "music_gone",
        "music",
        LOW - MINUTE,
        LOW + MINUTE,
        "music gone",
        (("media_session", "media_before"),),
    )
    episode(
        "browse_edge",
        "browsing",
        LOW - MINUTE,
        LOW + MINUTE,
        "Edge page",
        (("app_session", "visit_edge"),),
        body="digest of the edge page",
    )
    conn.execute(
        "UPDATE episodes SET url = 'https://edge.example', app = 'chrome.exe' WHERE id = ?",
        (ids["ep_browse_edge"],),
    )
    episode(
        "browse_lost",
        "browsing",
        LOW - 2 * MINUTE,
        LOW + 2 * MINUTE,
        "Lost page",
        (("app_session", "visit_edge"),),
        body="digest of the lost page",
    )
    episode(
        "browse_in",
        "browsing",
        NOW + 12 * MINUTE,
        NOW + 13 * MINUTE,
        "In page",
        (("app_session", "visit_in"),),
        body="digest of the in page",
    )
    episode(
        "after",
        "browsing",
        NOW + 25 * MINUTE,
        NOW + 26 * MINUTE,
        "page after",
        (("app_session", "visit_after"),),
        body="digest after",
    )
    # Bodies of speech and video episodes are what the evidence says, as the worker writes them.
    conn.execute(
        "UPDATE episodes SET body = (SELECT r.body FROM retained_episode_bodies r"
        " WHERE r.id = episodes.id) WHERE kind IN ('conversation', 'video', 'music')"
    )
    for episode_id, start in conn.execute("SELECT id, start_ms FROM episodes").fetchall():
        _embedding(conn, "episode", episode_id, start)


def _queue_and_state(world: World) -> None:
    conn, ids = world.conn, world.ids
    jobs = (
        ("pend_in", "transcribe", NOW + 12 * MINUTE, "pending"),
        ("pend_out", "transcribe", NOW + 25 * MINUTE, "pending"),
        ("run_in", "embed", NOW + 12 * MINUTE, "running"),
        ("run_out", "embed", NOW + MINUTE, "running"),
        ("done", "transcribe", NOW + 12 * MINUTE, "done"),
        ("failed", "transcribe", NOW + 12 * MINUTE, "failed"),
        ("ask", "ask", NOW + 12 * MINUTE, "pending"),
        ("compare", "stt_compare", NOW + 12 * MINUTE, "pending"),
        ("query", "embed_query", NOW + 12 * MINUTE, "pending"),
    )
    for name, kind, data_ms, state in jobs:
        add_job(conn, kind, f"{kind}:{name}", data_ms, state)
    conn.execute("INSERT INTO questions (asked_ms, text) VALUES (?, 'a private question')", (NOW,))
    conn.execute("INSERT INTO question_segments VALUES (1, ?)", (ids["seg_in"],))
    conn.execute("INSERT INTO question_windows VALUES (1, ?)", (ids["win_in"],))
    conn.execute(
        "INSERT INTO stt_samples (segment_id, reference, reference_language, condition,"
        " draft_model, draft_derived) VALUES (?, 'ref', 'fr', 'c', 'm', 0)",
        (ids["seg_in"],),
    )
    conn.execute(
        "INSERT INTO stt_runs (created_ms, vocabulary, processing_version) VALUES (0, '', 'v')"
    )
    conn.execute(
        "INSERT INTO stt_results (run_id, segment_id, model, revision, reference, terms,"
        " reference_language, condition, draft_model, draft_derived)"
        " VALUES (1, ?, 'm', 'r', 'ref', '[]', 'fr', 'c', 'm', 0)",
        (ids["seg_in"],),
    )
    conn.execute("INSERT INTO process_info VALUES ('worker', 0, '{}')")
    conn.execute("INSERT INTO state VALUES ('capture_alive_ms', 1)")
    conn.execute("INSERT INTO state VALUES ('watermark', 5)")
    _embedding(conn, "query", 77, NOW)


def _gaps(world: World) -> None:
    log = GapLog(world.conn)
    log.add("mic", "paused", NOW + MINUTE, NOW + 2 * MINUTE)
    log.add("mic", "device_missing", NOW + 5 * MINUTE, LOW)
    log.add("screen", "locked", NOW + 8 * MINUTE, NOW + 12 * MINUTE)
    log.add("system", "music", NOW + 13 * MINUTE, NOW + 14 * MINUTE)
    log.add("all", "ui_open", NOW + 18 * MINUTE, NOW + 25 * MINUTE)
    log.open("screen_text", "ocr_unavailable", NOW + 15 * MINUTE)


def build_world(root: Path, key: DataKey, conn: Connection, media: MediaStore) -> World:
    world = World(root, key, conn, media)
    _monitors(world)
    _frames(world)
    _segments(world)
    _speakers(world)
    _sessions(world)
    _episodes(world)
    _queue_and_state(world)
    _gaps(world)
    return world


def open_live(world: World) -> Connection:
    """A new connection to the live store, as the app hands the export."""
    return db.connect(Paths(world.root).db_file, world.key.subkey("db"))


def run_export(  # noqa: PLR0913 - the arguments of write_export, with test defaults
    world: World,
    target: Path,
    scope: ExportScope | None = None,
    *,
    cancel: ExportCancel | None = None,
    report: Callable[[int, int], None] = lambda _done, _total: None,
    open_db: Callable[[], Connection] | None = None,
) -> ExportResult:
    return write_export(
        open_db=open_db or (lambda: open_live(world)),
        media=world.media,
        target=target,
        passphrase=PASSPHRASE,
        scope=scope or ExportScope(),
        cancel=cancel or ExportCancel(),
        report=report,
    )


@contextmanager
def open_export(
    target: Path, passphrase: str = PASSPHRASE
) -> Generator[tuple[Connection, DataKey]]:
    """The finished export opened as a data folder: its database and its data key."""
    paths = Paths(target)
    new_key = unwrap_data_key(paths.keyfile.read_bytes(), passphrase)
    conn = db.connect(paths.db_file, new_key.subkey("db"))
    try:
        yield conn, new_key
    finally:
        conn.close()


@contextmanager
def exported(
    world: World, tmp_path: Path, scope: ExportScope, name: str = "out"
) -> Generator[Connection]:
    run_export(world, tmp_path / name, scope)
    with open_export(tmp_path / name) as (conn, _key):
        yield conn
