"""Transaction safety, the continues_id index, and retention that survives bad references."""

from __future__ import annotations

import http.client
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import sqlcipher3
from memory_data import NOW, add_segment

from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import DEFAULT_CONFIG_TOML
from akashic.storage import db, retention
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import CryptoError, DataKey
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.retention import DAY_MS, PURGERS, apply_retention

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.storage.media import MediaStore

OLD = NOW - 30 * DAY_MS
RECENT = NOW - DAY_MS


def screenshot(conn: Connection, media: MediaStore, ts_ms: int, text: str | None = "t") -> int:
    return FrameStore(conn, media).add(NewFrame(ts_ms, 0, "idle", text=text, image_webp=b"synth"))


def clip(conn: Connection, media: MediaStore, start_ms: int, transcript: str = "bonjour") -> int:
    audio = AudioStore(conn, media)
    seg = audio.add(NewSegment("system", start_ms, start_ms + 5_000, 4_000), b"opus", NOW)
    conn.execute("DELETE FROM jobs")
    audio.set_transcript(seg, "done", "fr", transcript)
    conn.execute("DELETE FROM jobs")
    return seg


def count(conn: Connection, sql: str, *params: object) -> int:
    row: tuple[int] = conn.execute(sql, params).fetchone()
    return row[0]


# --- the continues_id index (#44) ------------------------------------------------------------


def continues_indexes(conn: Connection) -> list[list[str]]:
    names = [r[0] for r in conn.execute("SELECT name FROM pragma_index_list('audio_segments')")]
    return [[str(c[2]) for c in conn.execute(f"PRAGMA index_info('{n}')")] for n in names]


def test_a_fresh_store_indexes_continues_id(conn: Connection) -> None:
    assert any(cols[:1] == ["continues_id"] for cols in continues_indexes(conn))
    plan = " ".join(
        str(r[3])
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM audio_segments WHERE continues_id=1"
        )
    )
    assert "INDEX" in plan


def test_deleting_a_continued_segment_clears_the_child_link(conn: Connection) -> None:
    first = add_segment(conn, NOW, text="premier")
    second = add_segment(conn, NOW + 10_000, text="second")
    conn.execute("UPDATE audio_segments SET continues_id = ? WHERE id = ?", (first, second))
    conn.execute("DELETE FROM audio_segments WHERE id = ?", (first,))
    assert conn.execute(
        "SELECT continues_id FROM audio_segments WHERE id = ?", (second,)
    ).fetchone() == (None,)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- transaction() (#49) ---------------------------------------------------------------------


def test_a_body_error_survives_even_after_sqlite_rolled_back_itself(conn: Connection) -> None:
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.execute(
        "CREATE TRIGGER t_full BEFORE INSERT ON t BEGIN SELECT RAISE(ROLLBACK, 'disk full'); END"
    )
    with pytest.raises(sqlcipher3.Error, match="disk full"), db.transaction(conn) as tx:
        tx.execute("INSERT INTO t VALUES (1)")
    assert not conn.in_transaction


def test_a_plain_body_error_is_reraised_unchanged_and_rolled_back(conn: Connection) -> None:
    conn.execute("CREATE TABLE t (v INTEGER)")
    boom = ValueError("boom")

    def body() -> None:
        with db.transaction(conn) as tx:
            tx.execute("INSERT INTO t VALUES (1)")
            raise boom

    with pytest.raises(ValueError, match="boom") as raised:
        body()
    assert raised.value is boom
    assert not conn.in_transaction
    assert count(conn, "SELECT count(*) FROM t") == 0


def test_a_failed_commit_raises_the_commit_error_and_the_next_transaction_works(
    conn: Connection,
) -> None:
    conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    with pytest.raises(sqlcipher3.IntegrityError), db.transaction(conn) as tx:
        tx.execute("INSERT INTO child VALUES (42)")  # only checked at COMMIT
    assert not conn.in_transaction
    assert count(conn, "SELECT count(*) FROM child") == 0
    with db.transaction(conn) as tx:
        tx.execute("INSERT INTO parent VALUES (1)")
    assert count(conn, "SELECT count(*) FROM parent") == 1


def test_a_busy_database_times_out_cleanly_and_a_later_pass_completes(
    paths: Paths, key: DataKey, media: MediaStore
) -> None:
    db_key = key.subkey("db")
    conn = db.connect(paths.db_file, db_key)
    db.migrate(conn)
    other = db.connect(paths.db_file, db_key)
    try:
        screenshot(conn, media, OLD)
        conn.execute("PRAGMA busy_timeout = 50")
        other.execute("BEGIN IMMEDIATE")  # holds the write lock
        with pytest.raises(sqlcipher3.OperationalError), db.transaction(conn):
            pytest.fail("the body must not run without the write lock")
        assert not conn.in_transaction
        with pytest.raises(sqlcipher3.OperationalError):
            apply_retention(conn, media, {"screenshots": 7}, NOW)
        assert not conn.in_transaction
        other.execute("ROLLBACK")
        assert apply_retention(conn, media, {"screenshots": 7}, NOW) == {"screenshots": 1}
    finally:
        other.close()
        conn.close()


# --- retention and bad media references (#56) ------------------------------------------------

BAD_PATHS = ["../../outside.webp", "..", "/abs/outside.webp", "a/../../b.webp"]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_a_bad_screenshot_path_is_skipped_and_everything_else_expires(
    conn: Connection, media: MediaStore, paths: Paths, bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    outside = paths.root.parent / "outside.webp"
    outside.write_bytes(b"not ours")
    first_good = screenshot(conn, media, OLD)
    broken = screenshot(conn, media, OLD + 1)
    conn.execute("UPDATE frames SET image_path = ? WHERE id = ?", (bad, broken))
    last_good = screenshot(conn, media, OLD + 2)
    kept = screenshot(conn, media, RECENT)
    with caplog.at_level(logging.DEBUG):
        removed = apply_retention(conn, media, {"screenshots": 7}, NOW)
    assert removed == {"screenshots": 2, "bad_media_paths": 1}
    paths_after = dict(conn.execute("SELECT id, image_path FROM frames"))
    assert paths_after[first_good] is None
    assert paths_after[last_good] is None
    assert paths_after[broken] == bad  # the reference is unchanged
    assert paths_after[kept] is not None  # not yet expired
    assert outside.read_bytes() == b"not ours"
    # the recent frame and the file the re-pointed row used to own are the only ones left
    assert len(list(paths.media_dir.rglob("*.enc"))) == 2
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "outside" not in messages
    assert bad not in messages
    assert "MediaPathError" in messages


def test_a_bad_audio_path_is_skipped_and_never_blocks_later_passes(
    conn: Connection, media: MediaStore
) -> None:
    broken = clip(conn, media, OLD)
    good = clip(conn, media, OLD + 10_000)
    conn.execute("UPDATE audio_segments SET audio_path = '../x.opus' WHERE id = ?", (broken,))
    for _ in range(2):  # a second pass behaves the same and does not raise
        assert apply_retention(conn, media, {"raw_audio": 7}, NOW) == {
            "raw_audio": 1 if _ == 0 else 0,
            "bad_media_paths": 1,
        }
    assert conn.execute(
        "SELECT audio_path FROM audio_segments WHERE id = ?", (good,)
    ).fetchone() == (None,)
    assert conn.execute(
        "SELECT audio_path FROM audio_segments WHERE id = ?", (broken,)
    ).fetchone() == ("../x.opus",)
    # Fixing the reference lets the next pass finish the job.
    conn.execute("UPDATE audio_segments SET audio_path = NULL WHERE id = ?", (broken,))
    assert apply_retention(conn, media, {"raw_audio": 7}, NOW) == {"raw_audio": 0}


def test_a_bad_row_does_not_hide_rows_beyond_the_first_batch(
    conn: Connection, media: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "BATCH", 2)
    ids = [screenshot(conn, media, OLD + i) for i in range(5)]
    conn.execute(
        "UPDATE frames SET image_path = '../bad.webp' WHERE id IN (?, ?)", (ids[0], ids[1])
    )
    removed = apply_retention(conn, media, {"screenshots": 7}, NOW)
    assert removed == {"screenshots": 3, "bad_media_paths": 2}
    assert count(conn, "SELECT count(*) FROM frames WHERE image_path IS NOT NULL") == 2


def test_locked_files_are_counted_separately_from_bad_references(
    conn: Connection, media: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck = screenshot(conn, media, OLD)
    bad = screenshot(conn, media, OLD + 1)
    screenshot(conn, media, OLD + 2)
    conn.execute("UPDATE frames SET image_path = '../bad.webp' WHERE id = ?", (bad,))
    stuck_path = conn.execute("SELECT image_path FROM frames WHERE id = ?", (stuck,)).fetchone()[0]
    real = media.delete

    def delete(rel_path: str) -> None:
        if rel_path == stuck_path:
            raise PermissionError
        real(rel_path)

    monkeypatch.setattr(media, "delete", delete)
    removed = apply_retention(conn, media, {"screenshots": 7}, NOW)
    assert removed == {"screenshots": 1, "bad_media_paths": 1, "locked_media_files": 1}


def test_clean_retention_has_no_extra_keys(conn: Connection, media: MediaStore) -> None:
    screenshot(conn, media, OLD)
    assert apply_retention(conn, media, {"screenshots": 7}, NOW) == {"screenshots": 1}


# --- per-category deletion over the API (#73) ------------------------------------------------


@dataclass
class Api:
    server: ApiServer
    token: str
    conn: Connection
    media: MediaStore

    def delete(self, path: str) -> tuple[int, dict[str, object]]:
        request = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
        try:
            request.request("DELETE", path, headers={"Cookie": f"akashic_session={self.token}"})
            response = request.getresponse()
            return response.status, json.loads(response.read())
        finally:
            request.close()


@pytest.fixture
def api(paths: Paths, key: DataKey, media: MediaStore) -> Iterator[Api]:
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    token = new_token()
    stopped = Controls(dict, lambda: None, lambda: None, while_stopped=lambda action: action())
    services = Services(
        conn,
        FrameStore(conn, media),
        token,
        stopped,
        audio=AudioStore(conn, media),
        config_file=paths.config_file,
    )
    server = ApiServer(services)
    server.start()
    try:
        yield Api(server, token, conn, media)
    finally:
        server.stop()
        conn.close()


def detail(body: dict[str, object]) -> str:
    return str(body.get("detail", ""))


@pytest.mark.parametrize("kind", ["screenshots", "raw_audio"])
def test_category_delete_with_a_bad_path_is_a_409_with_retry_guidance(api: Api, kind: str) -> None:
    broken = screenshot(api.conn, api.media, OLD)
    good = screenshot(api.conn, api.media, OLD + 1)
    seg_bad = clip(api.conn, api.media, OLD)
    seg_good = clip(api.conn, api.media, OLD + 10_000)
    api.conn.execute("UPDATE frames SET image_path = '../x.webp' WHERE id = ?", (broken,))
    api.conn.execute("UPDATE audio_segments SET audio_path = '../x' WHERE id = ?", (seg_bad,))
    status, body = api.delete(f"/api/settings/storage/{kind}")
    assert status == 409
    assert "Delete all data" in detail(body)
    # The rest was removed (committed changes stay) and the bad reference is untouched.
    table, column, good_id, bad_id = (
        ("frames", "image_path", good, broken)
        if kind == "screenshots"
        else ("audio_segments", "audio_path", seg_good, seg_bad)
    )
    sql = f"SELECT {column} FROM {table} WHERE id = ?"  # noqa: S608 - fixed test names
    assert api.conn.execute(sql, (good_id,)).fetchone() == (None,)
    assert api.conn.execute(sql, (bad_id,)).fetchone()[0] is not None


def test_category_delete_with_a_locked_file_is_a_409(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    screenshot(api.conn, api.media, OLD)

    def locked(rel_path: str) -> None:
        raise PermissionError(rel_path)

    monkeypatch.setattr(api.media, "delete", locked)
    status, body = api.delete("/api/settings/storage/screenshots")
    assert status == 409
    assert "retry" in detail(body)
    monkeypatch.undo()
    assert api.delete("/api/settings/storage/screenshots")[0] == 200  # idempotent retry


@pytest.mark.parametrize("error", [PermissionError("in use"), CryptoError("bad blob")])
def test_a_purger_error_is_a_409_not_a_500(
    api: Api, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*args: object) -> int:
        del args
        raise error

    monkeypatch.setitem(PURGERS, "raw_audio", fail)
    status, body = api.delete("/api/settings/storage/raw_audio")
    assert status == 409
    assert "Delete all data" in detail(body)


def test_a_clean_category_delete_is_a_200_with_the_exact_count(api: Api) -> None:
    for i in range(3):
        screenshot(api.conn, api.media, OLD + i)
    status, body = api.delete("/api/settings/storage/screenshots")
    assert status == 200
    assert body["deleted"] == 3  # screenshots; rows keep text so no row is removed
    assert api.delete("/api/settings/storage/screenshots") == (200, {"deleted": 0})


def test_a_bad_screenshot_path_does_not_block_other_categories(api: Api) -> None:
    broken = screenshot(api.conn, api.media, OLD)
    api.conn.execute("UPDATE frames SET image_path = '../x.webp' WHERE id = ?", (broken,))
    assert api.delete("/api/settings/storage/frame_text")[0] == 200


def test_delete_all_still_clears_a_store_with_a_bad_reference(api: Api) -> None:
    broken = screenshot(api.conn, api.media, OLD)
    screenshot(api.conn, api.media, OLD + 1)
    api.conn.execute("UPDATE frames SET image_path = '../x.webp' WHERE id = ?", (broken,))
    assert api.delete("/api/settings/storage/screenshots")[0] == 409
    status, _ = api.delete("/api/settings/storage")
    assert status == 200
    assert count(api.conn, "SELECT count(*) FROM frames") == 0


def test_unreleased_references_report_is_empty_for_kinds_without_files(
    conn: Connection, media: MediaStore
) -> None:
    screenshot(conn, media, OLD)
    assert retention.unreleased_media_references(conn, media, "transcripts", NOW) == {
        "bad_media_paths": 0,
        "locked_media_files": 0,
    }
