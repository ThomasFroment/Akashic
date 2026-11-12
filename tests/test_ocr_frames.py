"""Deferred OCR of game screenshots, and transcript search."""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from PIL import Image

from akashic.pipeline.jobs import Job, JobQueue, PermanentJobError
from akashic.pipeline.ocr_frames import OcrFrameHandler
from akashic.search import transcripts
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
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


@pytest.fixture
def frames(conn: Connection, paths: Paths) -> FrameStore:
    return FrameStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))


def game_frame(frames: FrameStore, ts: int = 1000, **kw: object) -> int:
    fields: dict[str, object] = {"ts_ms": ts, "monitor": 0, "trigger": "game", "image_webp": webp()}
    fields.update(kw)
    return frames.add(NewFrame(**fields))  # type: ignore[arg-type]


def job(frame_id: int) -> Job:
    return Job(1, "ocr_frame", f"ocr_frame:{frame_id}", {"frame_id": frame_id}, 0, 1)


class OcrFactoryHolder:
    def __init__(self, ocr: Ocr | None) -> None:
        self.ocr, self.builds = ocr, 0

    def __call__(self) -> OcrEngine:
        self.builds += 1
        if self.ocr is None:
            raise RuntimeError
        return self.ocr


# --- queueing ------------------------------------------------------------------------------


def test_only_textless_game_screenshots_are_queued(frames: FrameStore, conn: Connection) -> None:
    wanted = game_frame(frames, 1)
    game_frame(frames, 2, image_webp=None)  # black frame: nothing to read
    game_frame(frames, 3, text="already", text_source="uia")
    game_frame(frames, 4, trigger="idle")
    assert conn.execute("SELECT key FROM jobs").fetchall() == [(f"ocr_frame:{wanted}",)]


# --- handler -------------------------------------------------------------------------------


def test_text_is_read_and_becomes_searchable(frames: FrameStore, conn: Connection) -> None:
    frame_id = game_frame(frames)
    ocr = Ocr()
    factory = OcrFactoryHolder(ocr)
    handler = OcrFrameHandler(frames, factory)
    handler.run(job(frame_id))
    stored = frames.get(frame_id)
    assert stored is not None
    assert (stored.text, stored.text_source) == ("Victory Royale", "ocr")
    assert conn.execute(
        "SELECT rowid FROM frames_fts WHERE frames_fts MATCH 'victory'"
    ).fetchall() == [(frame_id,)]
    handler.run(job(frame_id))  # already read: no second OCR
    other = game_frame(frames, 2000)
    handler.run(job(other))
    assert (ocr.calls, factory.builds) == (2, 1)  # the engine is built once
    assert handler.blocked() is None
    handler.give_up(job(frame_id))
    handler.idle()


def test_nothing_is_stored_when_no_text_is_found(frames: FrameStore) -> None:
    frame_id = game_frame(frames)
    OcrFrameHandler(frames, OcrFactoryHolder(Ocr("  "))).run(job(frame_id))
    stored = frames.get(frame_id)
    assert stored is not None
    assert stored.text is None


def test_missing_or_imageless_frames_are_skipped(frames: FrameStore) -> None:
    ocr = Ocr()
    handler = OcrFrameHandler(frames, OcrFactoryHolder(ocr))
    handler.run(job(999))
    handler.run(job(game_frame(frames, image_webp=None)))
    assert ocr.calls == 0


def test_unrecoverable_problems_fail_the_job_for_good(frames: FrameStore, paths: Paths) -> None:
    frame_id = game_frame(frames)
    with pytest.raises(PermanentJobError):
        OcrFrameHandler(frames, OcrFactoryHolder(Ocr())).run(Job(1, "ocr_frame", "k", {}, 0, 1))
    with pytest.raises(PermanentJobError, match="OCR engine"):
        OcrFrameHandler(frames, OcrFactoryHolder(None)).run(job(frame_id))
    bad = game_frame(frames, 5000, image_webp=b"not an image")
    with pytest.raises(PermanentJobError, match="readable"):
        OcrFrameHandler(frames, OcrFactoryHolder(Ocr())).run(job(bad))
    for f in paths.media_dir.rglob("*.enc"):
        f.unlink()
    with pytest.raises(PermanentJobError, match="gone"):
        OcrFrameHandler(frames, OcrFactoryHolder(Ocr())).run(job(frame_id))


def test_queued_ocr_job_is_claimed_as_heavy_work(frames: FrameStore, conn: Connection) -> None:
    frame_id = game_frame(frames)
    claimed = JobQueue(conn).claim([OcrFrameHandler.kind], 10**9)
    assert claimed is not None
    assert claimed.payload == {"frame_id": frame_id}
    assert OcrFrameHandler.heavy


# --- transcript search ---------------------------------------------------------------------


def test_transcript_search_and_browse(conn: Connection, paths: Paths) -> None:
    audio = AudioStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))
    first = audio.add(NewSegment("mic", 1_000, 3_000, 1_500, ("discord.exe",)), b"x", 0)
    second = audio.add(NewSegment("system", 5_000, 7_000, 1_500), b"x", 0)
    pending = audio.add(NewSegment("mic", 9_000, 10_000, 500), b"x", 0)
    audio.set_transcript(first, "done", "fr", "On parle du café de Minlo")
    audio.set_transcript(second, "done", "en", "Minlo said something about a game")
    assert pending

    found = transcripts.search(conn, "minlo")
    assert [u.id for u in found] == [second, first]  # newest first
    assert found[1].sources == ("discord.exe",)
    assert "\x02Minlo\x03" in found[1].snippet
    assert [u.id for u in transcripts.search(conn, "cafe")] == [first]  # accents folded
    assert [u.id for u in transcripts.search(conn, "gam")] == [second]  # last word is a prefix
    assert transcripts.search(conn, "minlo", start_ms=4_000) == [found[0]]
    assert transcripts.search(conn, "minlo", limit=1, offset=1)[0].id == first
    assert transcripts.search(conn, "  ?! ") == []

    assert [u.id for u in transcripts.browse(conn)] == [second, first]
    assert [u.id for u in transcripts.browse(conn, newest_first=False)] == [first, second]
    assert [u.id for u in transcripts.browse(conn, 0, 4_000)] == [first]
