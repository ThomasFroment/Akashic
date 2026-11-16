"""Deferred OCR of window captures: storing them, the job, and the handler."""

from __future__ import annotations

import io
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import sqlcipher3
from memory_data import add_monitor, app_window
from PIL import Image

from akashic.pipeline.ocr_windows import OcrWindowHandler
from akashic.storage import db
from akashic.storage.crypto import CryptoError, DataKey
from akashic.storage.frames import FrameStore, NewFrame, NewWindow
from akashic.storage.jobs import Job, JobQueue, PermanentJobError
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.capture.ports import OcrEngine
    from akashic.config import Paths


def webp() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 20, 30)).save(out, "WEBP")
    return out.getvalue()


class Ocr:
    def __init__(self, text: str = "Victory Royale") -> None:
        self.text, self.calls = text, 0

    def recognize(self, image: Image.Image) -> str:
        self.calls += 1
        assert image.mode == "RGB"
        return self.text


class OcrFactoryHolder:
    def __init__(self, ocr: Ocr | None) -> None:
        self.ocr, self.builds = ocr, 0

    def __call__(self) -> OcrEngine:
        self.builds += 1
        if self.ocr is None:
            raise RuntimeError
        return self.ocr


@pytest.fixture
def frames(conn: Connection, paths: Paths) -> FrameStore:
    add_monitor(conn)
    return FrameStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))


def captured(**kw: object) -> NewWindow:
    return replace(app_window(**kw), capture_webp=webp())  # type: ignore[arg-type]


def add_game(
    frames: FrameStore,
    ts: int = 1_000,
    windows: tuple[NewWindow, ...] | None = None,
    *,
    trigger: str = "game",
) -> int:
    shown = windows if windows is not None else (captured(text=None),)
    return frames.add(NewFrame(ts, 0, trigger, webp(), shown))


def window_ids(conn: Connection, frame_id: int) -> list[int]:
    rows = conn.execute("SELECT id FROM frame_windows WHERE frame_id = ? ORDER BY z", (frame_id,))
    return [r[0] for r in rows]


def capture_path(conn: Connection, window_id: int) -> str | None:
    row = conn.execute("SELECT capture_path FROM frame_windows WHERE id = ?", (window_id,))
    return row.fetchone()[0]


def job_for(window_id: object) -> Job:
    return Job(1, "ocr_window", f"ocr_window:{window_id}", {"window_id": window_id}, 0, 1)


def media_files(paths: Paths) -> list[Path]:
    return [p for p in paths.media_dir.rglob("*") if p.is_file()]


# --- storing captures ------------------------------------------------------------------------


def test_a_capture_is_stored_encrypted_and_queues_one_job(
    frames: FrameStore, conn: Connection, paths: Paths
) -> None:
    frame_id = add_game(frames, 5_000)
    (window_id,) = window_ids(conn, frame_id)
    path = capture_path(conn, window_id)
    assert path is not None
    assert re.fullmatch(r"\d{4}/\d{2}/\d{2}/5000_m0_z0\.webp", path)
    assert frames.window_capture(window_id) == frames.media.read(path)
    assert webp() not in [p.read_bytes() for p in media_files(paths)]  # nothing in plaintext
    ((kind, key, payload, data_ms),) = conn.execute(
        "SELECT kind, key, payload, data_ms FROM jobs"
    ).fetchall()
    assert (kind, key, json.loads(payload), data_ms) == (
        "ocr_window",
        f"ocr_window:{window_id}",
        {"window_id": window_id},
        5_000,
    )


def test_a_taken_capture_name_gets_a_suffix(frames: FrameStore, conn: Connection) -> None:
    paths_: list[str | None] = []
    for _ in range(2):
        (window_id,) = window_ids(conn, add_game(frames, 5_000))
        paths_.append(capture_path(conn, window_id))
    assert paths_[0] is not None
    assert paths_[1] == paths_[0].removesuffix(".webp") + "_1.webp"


def test_a_frame_with_n_captured_windows_gets_n_captures_and_jobs(
    frames: FrameStore, conn: Connection
) -> None:
    windows = (
        captured(z=0, hwnd=1),
        captured(z=1, hwnd=2, focused=False),
        app_window(z=2, hwnd=3, focused=False),
    )
    ids = window_ids(conn, add_game(frames, 7_000, windows))
    paths_ = [capture_path(conn, i) for i in ids]
    assert [p is not None for p in paths_] == [True, True, False]
    assert [p.rsplit("_", 1)[1] for p in paths_ if p] == ["z0.webp", "z1.webp"]
    assert conn.execute("SELECT key FROM jobs ORDER BY id").fetchall() == [
        (f"ocr_window:{ids[0]}",),
        (f"ocr_window:{ids[1]}",),
    ]


@pytest.mark.parametrize("trigger", ["game", "click", "idle"])
def test_windows_without_a_capture_get_no_file_and_no_job(
    frames: FrameStore, conn: Connection, paths: Paths, trigger: str
) -> None:
    frame_id = frames.add(NewFrame(1_000, 0, trigger, None, (app_window(),)))
    (window_id,) = window_ids(conn, frame_id)
    assert capture_path(conn, window_id) is None
    assert conn.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    assert media_files(paths) == []
    assert frames.window_capture(window_id) is None


def test_no_ocr_frame_job_is_queued(frames: FrameStore, conn: Connection) -> None:
    add_game(frames)
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'ocr_frame'").fetchone() == (0,)


# --- the database refuses bad captures ------------------------------------------------------


@pytest.mark.parametrize("role", ["hidden", "shell"])
def test_a_capture_on_a_hidden_or_shell_row_is_refused_and_everything_is_removed(
    frames: FrameStore, conn: Connection, paths: Paths, role: str
) -> None:
    hwnd = None if role == "hidden" else 5
    bad = NewWindow(role, 1, hwnd, 0, 0, 10, 10, capture_webp=webp())  # type: ignore[arg-type]
    with pytest.raises(sqlcipher3.IntegrityError):
        add_game(frames, 1_000, (captured(z=0), bad))
    assert conn.execute("SELECT count(*) FROM frames").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM frame_windows").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    assert media_files(paths) == []


def test_a_capture_on_a_window_with_text_is_refused_and_files_are_removed(
    frames: FrameStore, conn: Connection, paths: Paths
) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        add_game(frames, 1_000, (captured(text="already read"),))
    assert conn.execute("SELECT count(*) FROM frames").fetchone() == (0,)
    assert media_files(paths) == []


# --- reading a capture ----------------------------------------------------------------------


def test_window_capture_is_none_without_a_row_or_a_capture(
    frames: FrameStore, conn: Connection
) -> None:
    assert frames.window_capture(999) is None
    plain = window_ids(conn, add_game(frames, 1_000, (app_window(),)))[0]
    assert frames.window_capture(plain) is None


def test_window_capture_fails_on_a_missing_or_undecryptable_file(
    frames: FrameStore, conn: Connection, paths: Paths
) -> None:
    (window_id,) = window_ids(conn, add_game(frames))
    (file,) = media_files(paths)[-1:]
    raw = file.read_bytes()
    file.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises(CryptoError):
        frames.window_capture(window_id)
    file.unlink()
    with pytest.raises(FileNotFoundError):
        frames.window_capture(window_id)


# --- handler --------------------------------------------------------------------------------


def test_handler_is_heavy_and_never_blocked(frames: FrameStore, conn: Connection) -> None:
    window_id = window_ids(conn, add_game(frames))[0]
    claimed = JobQueue(conn).claim([OcrWindowHandler.kind], 10**9)
    assert claimed is not None
    assert claimed.payload == {"window_id": window_id}
    assert OcrWindowHandler.kind == "ocr_window"
    assert OcrWindowHandler.heavy
    assert OcrWindowHandler(frames, OcrFactoryHolder(Ocr())).blocked() is None


def test_text_is_read_stored_and_searchable_and_the_capture_goes(
    frames: FrameStore, conn: Connection, paths: Paths
) -> None:
    (first,) = window_ids(conn, frame_id := add_game(frames))
    ocr = Ocr()
    factory = OcrFactoryHolder(ocr)
    handler = OcrWindowHandler(frames, factory)
    handler.run(job_for(first))
    stored = frames.get(frame_id)
    assert stored is not None
    assert (stored.windows[0].text, stored.windows[0].text_source) == (
        "Victory Royale",
        "ocr_deferred",
    )
    assert conn.execute(
        "SELECT rowid FROM frame_windows_fts WHERE frame_windows_fts MATCH 'victory'"
    ).fetchall() == [(first,)]
    assert capture_path(conn, first) is None
    assert [p for p in media_files(paths) if "_z0" in p.name] == []
    handler.run(job_for(first))  # already read: no second OCR
    (second,) = window_ids(conn, add_game(frames, 2_000))
    handler.run(job_for(second))
    assert (ocr.calls, factory.builds) == (2, 1)


@pytest.mark.parametrize("blank", ["", "   \n "])
def test_blank_ocr_stores_no_text_and_still_drops_the_capture(
    frames: FrameStore, conn: Connection, paths: Paths, blank: str
) -> None:
    (window_id,) = window_ids(conn, frame_id := add_game(frames))
    OcrWindowHandler(frames, OcrFactoryHolder(Ocr(blank))).run(job_for(window_id))
    stored = frames.get(frame_id)
    assert stored is not None
    assert stored.windows[0].text is None
    assert capture_path(conn, window_id) is None
    assert [p for p in media_files(paths) if "_z0" in p.name] == []


def test_a_gone_window_or_one_without_a_capture_is_skipped(
    frames: FrameStore, conn: Connection
) -> None:
    ocr = Ocr()
    handler = OcrWindowHandler(frames, OcrFactoryHolder(ocr))
    handler.run(job_for(999))
    plain = window_ids(conn, add_game(frames, 1_000, (app_window(),)))[0]
    handler.run(job_for(plain))
    assert ocr.calls == 0


@pytest.mark.parametrize("payload", [{}, {"window_id": "3"}, {"window_id": None}])
def test_a_job_without_an_integer_window_id_fails_for_good(
    frames: FrameStore, payload: dict[str, object]
) -> None:
    handler = OcrWindowHandler(frames, OcrFactoryHolder(Ocr()))
    with pytest.raises(PermanentJobError):
        handler.run(Job(1, "ocr_window", "k", payload, 0, 1))


def test_a_missing_capture_file_fails_the_job_for_good(
    frames: FrameStore, conn: Connection, paths: Paths
) -> None:
    (window_id,) = window_ids(conn, add_game(frames))
    for f in media_files(paths):
        if "_z0" in f.name:
            f.unlink()
    with pytest.raises(PermanentJobError):
        OcrWindowHandler(frames, OcrFactoryHolder(Ocr())).run(job_for(window_id))


def test_an_undecryptable_capture_fails_the_job_for_good(
    frames: FrameStore, conn: Connection, paths: Paths
) -> None:
    (window_id,) = window_ids(conn, add_game(frames))
    for f in media_files(paths):
        if "_z0" in f.name:
            f.write_bytes(b"garbage" * 10)
    with pytest.raises(PermanentJobError):
        OcrWindowHandler(frames, OcrFactoryHolder(Ocr())).run(job_for(window_id))


def test_a_capture_that_is_not_an_image_fails_the_job_for_good(
    frames: FrameStore, conn: Connection
) -> None:
    bad = replace(app_window(), capture_webp=b"not an image")
    (window_id,) = window_ids(conn, add_game(frames, 1_000, (bad,)))
    with pytest.raises(PermanentJobError):
        OcrWindowHandler(frames, OcrFactoryHolder(Ocr())).run(job_for(window_id))


def test_a_missing_ocr_engine_fails_the_job_for_good(frames: FrameStore, conn: Connection) -> None:
    (window_id,) = window_ids(conn, add_game(frames))
    with pytest.raises(PermanentJobError):
        OcrWindowHandler(frames, OcrFactoryHolder(None)).run(job_for(window_id))


def test_giving_up_deletes_the_capture_and_clears_the_reference(
    frames: FrameStore, conn: Connection, paths: Paths
) -> None:
    (window_id,) = window_ids(conn, add_game(frames))
    handler = OcrWindowHandler(frames, OcrFactoryHolder(Ocr()))
    handler.give_up(job_for(window_id))
    assert capture_path(conn, window_id) is None
    assert [p for p in media_files(paths) if "_z0" in p.name] == []
    handler.give_up(job_for(window_id))  # no capture left
    handler.give_up(Job(1, "ocr_window", "k", {}, 0, 1))  # no id


@pytest.fixture
def other_writer(paths: Paths, key: DataKey) -> Iterator[Connection]:
    c = db.connect(paths.db_file, key.subkey("db"))
    yield c
    c.close()


def test_a_busy_database_keeps_the_capture_for_a_retry(
    frames: FrameStore, conn: Connection, other_writer: Connection
) -> None:
    (window_id,) = window_ids(conn, add_game(frames))
    path = capture_path(conn, window_id)
    assert path is not None
    conn.execute("PRAGMA busy_timeout = 0")
    other_writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlcipher3.OperationalError):
            frames.finish_capture(window_id, "text")
    finally:
        other_writer.execute("ROLLBACK")
    assert frames.media.exists(path)
    assert capture_path(conn, window_id) == path
    OcrWindowHandler(frames, OcrFactoryHolder(Ocr())).run(job_for(window_id))
    assert capture_path(conn, window_id) is None
