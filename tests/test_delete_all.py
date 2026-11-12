"""Full deletion with stopped writers, real encrypted fixtures, and no retention exceptions."""

from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

import numpy as np
import pytest
from memory_data import NOW, add_episode, add_game, add_media, add_speaker, add_visit
from PIL import Image
from store_reads import latest_frames
from stt_rows import insert_result, insert_run, insert_sample
from waits import wait_for

from akashic.api import server as api_server
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import DEFAULT_CONFIG_TOML
from akashic.pipeline.episodes import build_episodes
from akashic.pipeline.sessions import derive_app_sessions
from akashic.storage import embeddings
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.retention import delete_all_recorded_data
from akashic.supervisor import Supervisor

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.storage.crypto import DataKey
    from akashic.storage.media import MediaStore


def seed_history(conn: Connection, media: MediaStore) -> None:
    future = NOW + 365 * 86_400_000
    FrameStore(conn, media).add(NewFrame(future, 0, "game", image_webp=b"synthetic screenshot"))
    audio = AudioStore(conn, media)
    audio.add(NewSegment("mic", future, future + 1000, 1000), b"pending audio", 0)
    done = audio.add(NewSegment("system", NOW, NOW + 4000, 4000), b"finished audio", 0)
    audio.set_transcript(done, "done", "fr", "fictionalphrase un rendez-vous")
    speaker = add_speaker(conn, "Fictional person")
    conn.execute(
        "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms)"
        " VALUES (?, ?, 0, 4000, 4000)",
        (done, speaker),
    )
    video = add_media(conn, "fictionalphrase video", NOW, NOW + 1000, closed=False)
    conn.execute(
        "INSERT INTO media_events (media_session_id, ts_ms, state) VALUES (?, 0, 'playing')",
        (video,),
    )
    add_game(conn, NOW, future, closed=False)
    visit = add_visit(conn, "app.exe", "fictionalphrase window", NOW, future)
    conn.execute("UPDATE app_sessions SET closed = 0, last_frame_id = 1 WHERE id = ?", (visit,))
    episode = add_episode(
        conn, "conversation", NOW, future, "Call", "fictionalphrase digest", people=(speaker,)
    )
    conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (episode, done))
    question = conn.execute(
        "INSERT INTO questions (asked_ms, text, state, answer, sources)"
        " VALUES (0, 'An invented question', 'done', 'fictionalphrase answer', '[]')"
    ).lastrowid
    conn.execute("INSERT INTO question_segments VALUES (?, ?)", (question, done))
    insert_sample(conn, done, "fictionalphrase reference")
    run = insert_run(conn, "[]")
    insert_result(
        conn, run, done, "m", "fictionalphrase reference", "[]", prediction="fictionalphrase output"
    )
    conn.execute("INSERT INTO segment_passages VALUES (?, 'm', 0, 1, 1, x'0000')", (done,))
    for kind, owner in (("episode", episode), ("segment", done), ("query", 123)):
        embeddings.put(conn, kind, owner, "m", np.ones(2, dtype=np.float32), NOW, NOW)
    conn.execute("UPDATE jobs SET state = 'running' WHERE kind = 'speakers'")
    conn.execute(
        "INSERT INTO jobs (kind, key, payload, data_ms, created_ms)"
        " VALUES ('embed_query', 'query', '{\"text\":\"fictionalphrase\"}', 0, 0)"
    )
    conn.execute("INSERT INTO state VALUES ('conversations_since_ms', ?)", (future,))
    conn.execute("INSERT OR REPLACE INTO process_info VALUES ('capture', 0, '{}')")
    media.write_unique("orphans/unreferenced", b"invented orphan audio")


def assert_empty(conn: Connection, paths: Paths) -> None:
    # Enumerate schema tables, so a future recorded-data table cannot silently escape this test.
    tables = [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            " AND name NOT LIKE '%_fts%'"
        )
    ]
    for table in tables:
        assert conn.execute(f'SELECT count(*) FROM "{table}"').fetchone() == (0,), table  # noqa: S608
    for table in ("frames_fts", "audio_segments_fts", "episodes_fts", "media_sessions_fts"):
        assert conn.execute(
            f"SELECT count(*) FROM {table} WHERE {table} MATCH 'fictionalphrase'"  # noqa: S608
        ).fetchone() == (0,)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert list(paths.media_dir.iterdir()) == []


def test_full_deletion_ignores_pending_open_and_future_guards_and_keeps_setup(
    conn: Connection, media: MediaStore, paths: Paths
) -> None:
    seed_history(conn, media)
    interrupted = paths.media_dir / "inflight.enc.123.tmp"
    interrupted.write_bytes(b"invented encrypted temp file")
    keep = [
        paths.keyfile,
        paths.config_file,
        paths.models_dir / "model",
        paths.runtime_dir / "runtime",
        paths.logs_dir / "app.log",
    ]
    for path in keep:
        path.write_bytes(b"setup marker")
    result = delete_all_recorded_data(conn, media)
    assert result["screenshots"] == 1
    assert result["raw_audio"] == 2
    assert_empty(conn, paths)
    assert all(p.read_bytes() == b"setup marker" for p in keep)
    assert sum(delete_all_recorded_data(conn, media).values()) == 0
    assert derive_app_sessions(conn, NOW) == 0
    assert not any(build_episodes(conn, NOW).values())
    assert conn.execute("SELECT count(*) FROM episodes").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM frames").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM audio_segments").fetchone() == (0,)
    # New capture starts from an empty history, without stale watermarks suppressing it.
    store = FrameStore(conn, media)
    store.add(NewFrame(NOW, 0, "click", process="app.exe", text="fresh capture"))
    assert latest_frames(conn, 1)[0].text == "fresh capture"


@pytest.mark.parametrize("failure", ["file", "database"])
def test_interrupted_full_deletion_keeps_database_references_and_retries(
    conn: Connection, media: MediaStore, paths: Paths, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    seed_history(conn, media)
    with monkeypatch.context() as patch:
        if failure == "file":
            original = media.delete_all

            def interrupted() -> None:
                original()
                raise PermissionError("synthetic sharing violation")

            patch.setattr(media, "delete_all", interrupted)
            error = PermissionError
        else:
            conn.execute(
                "CREATE TRIGGER interrupt_delete BEFORE DELETE ON frames"
                " BEGIN SELECT RAISE(ABORT, 'interrupt'); END"
            )
            import sqlcipher3  # noqa: PLC0415 - native driver exception type

            error = sqlcipher3.IntegrityError
        with pytest.raises(error):
            delete_all_recorded_data(conn, media)
    assert conn.execute("SELECT count(*) FROM questions").fetchone() == (1,)
    assert conn.execute(
        "SELECT count(*) FROM audio_segments WHERE audio_path IS NOT NULL"
    ).fetchone() == (2,)
    conn.execute("DROP TRIGGER IF EXISTS interrupt_delete")
    delete_all_recorded_data(conn, media)
    assert_empty(conn, paths)


def call(server: ApiServer, token: str) -> tuple[int, dict[str, object]]:
    request = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    try:
        request.request(
            "DELETE", "/api/settings/storage", headers={"Cookie": f"akashic_session={token}"}
        )
        response = request.getresponse()
        return response.status, json.loads(response.read())
    finally:
        request.close()


@pytest.mark.parametrize("paused", [False, True])
def test_full_api_deletion_stops_real_children_and_restores_their_state(
    conn: Connection, media: MediaStore, paths: Paths, key: DataKey, *, paused: bool
) -> None:

    paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    sup = Supervisor(paths.root, key)
    sup.start(paused=True)
    server: ApiServer | None = None
    try:
        assert wait_for(lambda: set(sup.states().values()) == {"paused"}, 30)
        seed_history(conn, media)
        if not paused:
            sup.send("resume")
        old = [c.proc for c in sup._children.values()]
        observed: list[bool] = []

        def while_stopped(action: Callable[[], None]) -> None:
            def erase() -> None:
                assert set(sup.states().values()) == {"stopped"}
                assert all(c.proc is None for c in sup._children.values())
                assert all(p is not None and p.poll() is not None for p in old)
                action()
                assert_empty(conn, paths)
                observed.append(True)

            sup.while_stopped(erase)

        token = new_token()
        services = Services(
            conn,
            FrameStore(conn, media),
            token,
            Controls(
                sup.states,
                lambda: sup.send("pause"),
                lambda: sup.send("resume"),
                while_stopped=while_stopped,
            ),
            config_file=paths.config_file,
        )
        services.put_thumb((1, "2026/01/01/x.webp", 200), b"old decrypted image")
        server = ApiServer(services)
        server.start()
        assert call(server, "bad")[0] == 401
        status, result = call(server, token)
        assert status == 200, result
        assert observed == [True]
        assert services.thumb((1, "2026/01/01/x.webp", 200)) is None
        assert wait_for(
            lambda: set(sup.states().values()) == ({"paused"} if paused else {"ready"}), 30
        )
        assert all(c.proc not in old for c in sup._children.values())
        assert conn.execute("SELECT count(*) FROM audio_segments").fetchone() == (0,)
    finally:
        if server is not None:
            server.stop()
        sup.stop()


def test_api_refuses_deletion_without_writer_shutdown_and_reports_file_failure(
    conn: Connection, media: MediaStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_history(conn, media)
    paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    token = new_token()
    services = Services(
        conn,
        FrameStore(conn, media),
        token,
        Controls(dict, lambda: None, lambda: None),
        config_file=paths.config_file,
    )
    server = ApiServer(services)
    server.start()
    try:
        assert call(server, token)[0] == 503
        assert conn.execute("SELECT count(*) FROM frames").fetchone() == (1,)

        def no_children(work: Callable[[], None]) -> None:
            work()

        services.controls = replace(services.controls, while_stopped=no_children)

        def fail() -> None:
            raise PermissionError("synthetic failure")

        monkeypatch.setattr(media, "delete_all", fail)
        services.put_thumb((1, "2026/01/01/x.webp", 200), b"old image")
        assert call(server, token)[0] == 409
        assert services.thumb((1, "2026/01/01/x.webp", 200)) is None
        assert conn.execute("SELECT count(*) FROM frames").fetchone() == (1,)
    finally:
        server.stop()


def test_media_clear_does_not_follow_windows_directory_junctions(
    media: MediaStore, paths: Paths, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_bytes(b"outside media")
    nested = paths.media_dir / "nested"
    nested.mkdir()
    for target in (paths.media_dir / "direct-link", nested / "nested-link"):
        subprocess.run(  # noqa: S603 - fixed operation and invented fixture paths
            [os.environ["COMSPEC"], "/c", "mklink", "/J", str(target), str(outside)],
            check=True,
            capture_output=True,
        )
    media.delete_all()
    assert marker.read_bytes() == b"outside media"
    assert list(paths.media_dir.iterdir()) == []


def test_inflight_thumbnail_cannot_restore_cache_after_deletion_and_id_reuse(
    conn: Connection, media: MediaStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    frames = FrameStore(conn, media)

    def add_image(color: str) -> int:
        data = io.BytesIO()
        Image.new("RGB", (100, 100), color).save(data, "WEBP")
        return frames.add(NewFrame(NOW, 0, "click", image_webp=data.getvalue()))

    first = add_image("red")
    first_path = str(frames.get(first).image_path)  # type: ignore[union-attr]
    entered, release, deleting = threading.Event(), threading.Event(), threading.Event()
    thumbnail = api_server._thumbnail

    def rendering(webp: bytes, width: int) -> bytes:
        entered.set()
        assert release.wait(10)
        return thumbnail(webp, width)

    def stopped(action: Callable[[], None]) -> None:
        deleting.set()
        action()  # this fixture has no capture/worker; the subprocess test covers shutdown

    monkeypatch.setattr(api_server, "_thumbnail", rendering)
    token = new_token()
    services = Services(
        conn,
        frames,
        token,
        Controls(dict, lambda: None, lambda: None, while_stopped=stopped),
        config_file=paths.config_file,
    )
    server = ApiServer(services)

    def image() -> bytes:
        request = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
        try:
            request.request(
                "GET",
                f"/api/frames/{first}/image?w=50",
                headers={"Cookie": f"akashic_session={token}"},
            )
            response = request.getresponse()
            assert response.status == 200
            return response.read()
        finally:
            request.close()

    server.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            render = pool.submit(image)
            try:
                assert entered.wait(10)
                deletion = pool.submit(call, server, token)
                assert deleting.wait(10)
                assert not deletion.done()
            finally:
                release.set()
            old_image = render.result(timeout=15)
            assert deletion.result(timeout=15)[0] == 200
        assert services.thumb((first, first_path, 50)) is None
        assert add_image("blue") == first
        assert image() != old_image
    finally:
        release.set()
        server.stop()
