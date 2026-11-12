"""Encrypted media files (names, DST, no overwrite) and the frames table."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import sqlcipher3
from clock_fakes import SWITCH, LocalParis
from memory_data import NOW
from store_reads import latest_frames

from akashic.storage import audio as audio_storage
from akashic.storage import frames
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey, DecryptionError
from akashic.storage.frames import FrameStore, NewFrame, image_rel_path
from akashic.storage.media import MediaPathError, MediaStore
from akashic.storage.retention import DAY_MS, expire_raw_audio, expire_screenshots

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.storage.audio import Stream


def test_media_roundtrip_is_encrypted_at_rest(media: MediaStore, paths: Paths) -> None:
    media.write_unique("2026/11/08/a.webp", b"RIFF-secret-pixels")
    assert media.exists("2026/11/08/a.webp")
    assert media.read("2026/11/08/a.webp") == b"RIFF-secret-pixels"
    on_disk = (paths.media_dir / "2026/11/08/a.webp.enc").read_bytes()
    assert b"secret-pixels" not in on_disk
    assert not list(paths.media_dir.rglob("*.tmp"))
    media.delete("2026/11/08/a.webp")
    media.delete("2026/11/08/a.webp")  # idempotent
    assert not media.exists("2026/11/08/a.webp")


def test_media_file_moved_to_another_name_fails_to_decrypt(media: MediaStore, paths: Paths) -> None:
    media.write_unique("2026/11/08/a.webp", b"data")
    (paths.media_dir / "2026/11/08/a.webp.enc").rename(paths.media_dir / "2026/11/08/b.webp.enc")
    with pytest.raises(DecryptionError):
        media.read("2026/11/08/b.webp")


def test_media_wrong_key_fails(media: MediaStore, paths: Paths) -> None:
    media.write_unique("x/a.webp", b"data")
    other = MediaStore(paths.media_dir, DataKey.generate().subkey("media"))
    with pytest.raises(DecryptionError):
        other.read("x/a.webp")


@pytest.mark.parametrize("bad", ["../escape.webp", "/abs.webp", "a/../../b.webp", ""])
def test_media_rejects_paths_outside_the_directory(media: MediaStore, bad: str) -> None:
    with pytest.raises(MediaPathError):
        media.write_unique(bad, b"x")


def test_media_failed_write_leaves_no_temp_file(
    media: MediaStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: object, target: object) -> None:
        del self, target
        raise OSError

    monkeypatch.setattr("pathlib.Path.rename", boom)
    with pytest.raises(OSError):  # noqa: PT011 - the stub raises a bare OSError
        media.write_unique("x/a.webp", b"data")
    assert not list(paths.media_dir.rglob("*.tmp"))


def test_add_and_read_frame(conn: Connection, media: MediaStore) -> None:
    store = FrameStore(conn, media)
    frame_id = store.add(
        NewFrame(
            ts_ms=1_700_000_000_123,
            monitor=1,
            trigger="click",
            process="chrome.exe",
            window_title="Bonjour",
            url="https://example.com",
            text="un café crème",
            text_source="uia",
            image_webp=b"webp-bytes",
        )
    )
    stored = store.get(frame_id)
    assert stored is not None
    assert (stored.process, stored.url, stored.text_source) == (
        "chrome.exe",
        "https://example.com",
        "uia",
    )
    assert stored.image_path == image_rel_path(1_700_000_000_123, 1)
    assert store.image(stored) == b"webp-bytes"
    assert stored.image_path is not None
    assert stored.image_path.endswith("/1700000000123_m1.webp")


def test_frame_without_image_and_listing(conn: Connection, media: MediaStore) -> None:
    store = FrameStore(conn, media)
    first = store.add(NewFrame(ts_ms=1000, monitor=0, trigger="idle"))
    second = store.add(NewFrame(ts_ms=2000, monitor=0, trigger="focus", text="hello"))
    assert [f.id for f in latest_frames(conn)] == [second, first]
    assert latest_frames(conn, 1)[0].id == second
    plain = store.get(first)
    assert plain is not None
    assert plain.image_path is None
    assert store.image(plain) is None
    assert store.get(999) is None


def test_full_text_search_ignores_diacritics(conn: Connection, media: MediaStore) -> None:
    store = FrameStore(conn, media)
    frame_id = store.add(NewFrame(ts_ms=1, monitor=0, trigger="idle", text="Un café à Paris"))
    rows = conn.execute("SELECT rowid FROM frames_fts WHERE frames_fts MATCH 'cafe'").fetchall()
    assert rows == [(frame_id,)]


def test_failed_row_insert_removes_the_image(
    conn: Connection, media: MediaStore, paths: Paths
) -> None:
    store = FrameStore(conn, media)
    bad = NewFrame(
        ts_ms=5, monitor=0, trigger="idle", text_source="nope",  # pyright: ignore[reportArgumentType]
        image_webp=b"pixels",
    )  # fmt: skip
    with pytest.raises(sqlcipher3.IntegrityError):
        store.add(bad)
    assert not list(paths.media_dir.rglob("*.enc"))
    assert latest_frames(conn) == []


# --- file names: helpers ---------------------------------------------------------------


@pytest.fixture
def frame_store(conn: Connection, media: MediaStore) -> FrameStore:
    return FrameStore(conn, media)


@pytest.fixture
def audio_store(conn: Connection, media: MediaStore) -> AudioStore:
    return AudioStore(conn, media)


def shot(ts_ms: int, monitor: int = 0, data: bytes = b"img") -> NewFrame:
    return NewFrame(ts_ms, monitor, "idle", image_webp=data)


def clip(start_ms: int, stream: Stream = "mic") -> NewSegment:
    return NewSegment(stream, start_ms, start_ms + 3_000, 2_000)


def frame_path(store: FrameStore, frame_id: int) -> str:
    stored = store.get(frame_id)
    assert stored is not None
    assert stored.image_path is not None
    return stored.image_path


def segment_path(store: AudioStore, segment_id: int) -> str:
    stored = store.get(segment_id)
    assert stored is not None
    assert stored.audio_path is not None
    return stored.audio_path


def media_files(paths: Paths) -> list[Path]:
    return sorted(p for p in paths.media_dir.rglob("*") if p.is_file())


# --- file names: the DST fall-back ---------------------------------------------------------


def test_captures_one_real_hour_apart_at_the_fall_back_keep_their_own_bytes(
    monkeypatch: pytest.MonkeyPatch, frame_store: FrameStore, audio_store: AudioStore
) -> None:
    monkeypatch.setattr(frames, "datetime", LocalParis)
    monkeypatch.setattr(audio_storage, "datetime", LocalParis)
    summer = int((SWITCH - timedelta(minutes=30)).timestamp() * 1000)  # 02:30 CEST
    winter = summer + 3_600_000  # 02:30 CET, same wall clock
    first = frame_store.add(shot(summer, data=b"summer"))
    second = frame_store.add(shot(winter, data=b"winter"))
    one, two = frame_path(frame_store, first), frame_path(frame_store, second)
    assert one != two
    assert one.startswith("2026/10/25/")
    stored_one, stored_two = frame_store.get(first), frame_store.get(second)
    assert stored_one is not None
    assert stored_two is not None
    assert frame_store.image(stored_one) == b"summer"
    assert frame_store.image(stored_two) == b"winter"
    seg_a = audio_store.add(clip(summer), b"summer-audio", NOW)
    seg_b = audio_store.add(clip(winter), b"winter-audio", NOW)
    assert segment_path(audio_store, seg_a) != segment_path(audio_store, seg_b)
    a, b = audio_store.get(seg_a), audio_store.get(seg_b)
    assert a is not None
    assert b is not None
    assert audio_store.read_file(a) == b"summer-audio"
    assert audio_store.read_file(b) == b"winter-audio"


def test_deleting_one_of_two_hour_apart_files_leaves_the_other(
    conn: Connection, monkeypatch: pytest.MonkeyPatch, frame_store: FrameStore, media: MediaStore
) -> None:
    monkeypatch.setattr(frames, "datetime", LocalParis)
    summer = int((SWITCH - timedelta(minutes=30)).timestamp() * 1000)
    first = frame_store.add(shot(summer, data=b"summer"))
    second = frame_store.add(shot(summer + 3_600_000, data=b"winter"))
    assert expire_screenshots(conn, media, summer + 1) == 1
    kept = frame_store.get(second)
    assert kept is not None
    assert frame_store.image(kept) == b"winter"
    gone = frame_store.get(first)
    assert gone is not None
    assert gone.image_path is None


# --- naming ------------------------------------------------------------------------------


def test_folder_is_the_local_date_and_the_name_carries_the_utc_instant() -> None:
    ts = int(datetime(2026, 10, 25, 23, 30, tzinfo=UTC).timestamp() * 1000)
    local = datetime.fromtimestamp(ts / 1000, UTC).astimezone()
    image = frames.image_rel_path(ts, 2)
    sound = audio_storage.audio_rel_path(ts, "system")
    assert image == f"{local:%Y/%m/%d}/{ts}_m2.webp"
    assert sound == f"audio/{local:%Y/%m/%d}/{ts}_system.opus"


def test_the_folder_follows_the_local_date_not_the_utc_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frames, "datetime", LocalParis)
    late = int(datetime(2026, 10, 25, 22, 30, tzinfo=UTC).timestamp() * 1000)  # 23:30 Paris
    after_midnight = late + 3_600_000  # 00:30 Paris, 26th
    assert frames.image_rel_path(late, 0).startswith("2026/10/25/")
    assert frames.image_rel_path(after_midnight, 0).startswith("2026/10/26/")
    assert frames.image_rel_path(late, 0).endswith(f"/{late}_m0.webp")


# --- same instant never overwrites ---------------------------------------------------------


def test_frames_with_the_same_instant_and_monitor_get_their_own_files(
    conn: Connection, frame_store: FrameStore, media: MediaStore
) -> None:
    ids = [frame_store.add(shot(NOW, 0, f"image-{i}".encode())) for i in range(3)]
    all_paths = [frame_path(frame_store, i) for i in ids]
    assert len(set(all_paths)) == 3
    for i, frame_id in enumerate(ids):
        stored = frame_store.get(frame_id)
        assert stored is not None
        assert frame_store.image(stored) == f"image-{i}".encode()
    media.delete(all_paths[1])  # independent delete
    assert not media.exists(all_paths[1])
    assert media.read(all_paths[0]) == b"image-0"
    assert media.read(all_paths[2]) == b"image-2"
    assert expire_screenshots(conn, media, NOW + 1) == 3
    assert conn.execute("SELECT count(*) FROM frames WHERE image_path IS NOT NULL").fetchone() == (
        0,
    )


def test_segments_with_the_same_start_and_stream_get_their_own_files(
    conn: Connection, audio_store: AudioStore, media: MediaStore
) -> None:
    ids = [audio_store.add(clip(NOW), f"audio-{i}".encode(), NOW) for i in range(3)]
    all_paths = [segment_path(audio_store, i) for i in ids]
    assert len(set(all_paths)) == 3
    for i, segment_id in enumerate(ids):
        stored = audio_store.get(segment_id)
        assert stored is not None
        assert audio_store.read_file(stored) == f"audio-{i}".encode()
    media.delete(all_paths[0])
    assert media.exists(all_paths[1])
    assert media.exists(all_paths[2])
    conn.execute("UPDATE audio_segments SET status = 'done'")
    conn.execute("DELETE FROM jobs")
    assert expire_raw_audio(conn, media, NOW + DAY_MS) == 3


def test_write_unique_never_replaces_and_returns_the_path_it_used(media: MediaStore) -> None:
    assert media.write_unique("2026/01/01/x.webp", b"a") == "2026/01/01/x.webp"
    second = media.write_unique("2026/01/01/x.webp", b"b")
    third = media.write_unique("2026/01/01/x.webp", b"c")
    assert len({"2026/01/01/x.webp", second, third}) == 3
    assert media.read("2026/01/01/x.webp") == b"a"
    assert media.read(second) == b"b"
    assert media.read(third) == b"c"


def test_a_file_where_the_date_folder_belongs_raises_instead_of_looping(
    media: MediaStore, paths: Paths
) -> None:
    (paths.media_dir / "2026" / "01").mkdir(parents=True)
    (paths.media_dir / "2026" / "01" / "02").write_bytes(b"not a folder")
    outcome: list[object] = []

    def attempt() -> None:
        try:
            media.write_unique("2026/01/02/x.webp", b"a")
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            outcome.append(exc)
        else:
            outcome.append(None)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "write_unique looped forever on a folder error"
    assert len(outcome) == 1
    assert isinstance(outcome[0], OSError)
    assert media.write_unique("2026/01/03/x.webp", b"b") == "2026/01/03/x.webp"  # store still works


def test_different_monitors_and_streams_never_collide(
    frame_store: FrameStore, audio_store: AudioStore
) -> None:
    a, b = frame_store.add(shot(NOW, 0)), frame_store.add(shot(NOW, 1))
    assert frame_path(frame_store, a) != frame_path(frame_store, b)
    c = audio_store.add(clip(NOW, "mic"), b"m", NOW)
    d = audio_store.add(clip(NOW, "system"), b"s", NOW)
    assert segment_path(audio_store, c) != segment_path(audio_store, d)


# --- old formats --------------------------------------------------------------------------


def test_old_format_paths_still_read_and_delete(
    conn: Connection, frame_store: FrameStore, audio_store: AudioStore, media: MediaStore
) -> None:
    old_image = "2023/11/14/221320_123_m1.webp"
    old_audio = "audio/2023/11/14/221320_123_mic.opus"
    media.write_unique(old_image, b"legacy image")
    media.write_unique(old_audio, b"legacy audio")
    conn.execute(
        "INSERT INTO frames (ts_ms, monitor, trigger, image_path)"
        " VALUES (1700000000123, 1, 'idle', ?)",
        (old_image,),
    )
    conn.execute(
        "INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status,"
        " audio_path) VALUES ('mic', 1700000000123, 1700000003123, 2000, '[]', 'done', ?)",
        (old_audio,),
    )
    frame = latest_frames(conn, 1)[0]
    assert frame_store.image(frame) == b"legacy image"
    segment_id = conn.execute("SELECT id FROM audio_segments").fetchone()[0]
    segment = audio_store.get(segment_id)
    assert segment is not None
    assert audio_store.read_file(segment) == b"legacy audio"
    # A new capture at the very same instant coexists with the old-format file.
    new = frame_store.add(shot(1700000000123, 1, b"new image"))
    assert frame_path(frame_store, new) != old_image
    assert expire_screenshots(conn, media, NOW) == 2
    assert not media.exists(old_image)
    assert conn.execute("SELECT image_path FROM frames WHERE id = ?", (new,)).fetchone() == (None,)
    assert expire_raw_audio(conn, media, NOW) == 1
    assert not media.exists(old_audio)


# --- failed inserts remove only the file just written -------------------------------------


def test_a_failed_frame_insert_removes_only_its_own_file(
    conn: Connection, frame_store: FrameStore, media: MediaStore, paths: Paths
) -> None:
    first = frame_store.add(shot(NOW, 0, b"kept"))
    kept_path = frame_path(frame_store, first)
    conn.execute(
        "CREATE TRIGGER no_more BEFORE INSERT ON frames BEGIN SELECT RAISE(ABORT, 'no'); END"
    )
    before = media_files(paths)
    with pytest.raises(Exception, match="no"):
        frame_store.add(shot(NOW, 0, b"doomed"))  # collides with the kept file's name
    assert media_files(paths) == before
    assert media.read(kept_path) == b"kept"


def test_a_failed_segment_insert_removes_only_its_own_file(
    conn: Connection, audio_store: AudioStore, media: MediaStore, paths: Paths
) -> None:
    first = audio_store.add(clip(NOW), b"kept", NOW)
    kept_path = segment_path(audio_store, first)
    conn.execute(
        "CREATE TRIGGER no_more BEFORE INSERT ON audio_segments"
        " BEGIN SELECT RAISE(ABORT, 'no'); END"
    )
    before = media_files(paths)
    with pytest.raises(Exception, match="no"):
        audio_store.add(clip(NOW), b"doomed", NOW)
    assert media_files(paths) == before
    assert media.read(kept_path) == b"kept"
