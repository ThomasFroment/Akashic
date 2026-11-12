"""The Debug page: process info, drop records, scheduler readings, and the /api/debug routes."""

from __future__ import annotations

import http.client
import json
from typing import TYPE_CHECKING, Any

import pytest

from akashic.api.debug import DebugSources
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.debuginfo import ProcessInfo
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.config import Paths

NOW = 1_000_000


# --- log files ---------------------------------------------------------------------------------

APP_LOG = "2026-11-08 10:00:03,000 1 app INFO started\n"
CAPTURE_LOG = (
    "2026-11-08 10:00:01,000 2 capture INFO ready\n"
    "2026-11-08 10:00:04,000 2 capture WARNING device failed\n"
)
WORKER_LOG = (
    "cut-off start of the file\n"
    "2026-11-08 10:00:02,000 3 worker DEBUG polling\n"
    "2026-11-08 10:00:05,000 3 worker ERROR job failed\n"
    "Traceback (most recent call last):\n"
    "  File x\n"
    "2026-11-08 10:00:06,000 3 worker INFO back\n"
)


def _write_logs(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "app.log").write_text(APP_LOG, encoding="utf-8")
    (logs_dir / "capture.log").write_text(CAPTURE_LOG, encoding="utf-8")
    (logs_dir / "worker.log").write_text(WORKER_LOG, encoding="utf-8")
    (logs_dir / "worker.log.1").write_text(
        "2026-11-07 09:00:00,000 3 worker INFO yesterday\n", encoding="utf-8"
    )
    (logs_dir / "notes.txt").write_text("not a log", encoding="utf-8")


# --- the routes --------------------------------------------------------------------------------


class Client:
    def __init__(self, server: ApiServer, token: str) -> None:
        self.server, self.token = server, token

    def call(self, path: str, method: str = "GET", token: str | None = None) -> tuple[int, Any]:
        c = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        c.request(method, path, headers={"Cookie": f"akashic_session={token or self.token}"})
        resp = c.getresponse()
        body = resp.read()
        c.close()
        return resp.status, json.loads(body)


@pytest.fixture
def client(paths: Paths, conn: Connection) -> Iterator[Client]:
    key = DataKey.generate()
    media = MediaStore(paths.media_dir, key.subkey("media"))
    audio = AudioStore(conn, media)
    seg = audio.add(NewSegment("mic", NOW, NOW + 2_000, 1_500, ("discord.exe",)), b"OggS", NOW)
    assert seg == 1
    frames = FrameStore(conn, media)
    frames.add(NewFrame(NOW, 0, "click", "chrome.exe", "Café", None, "x" * 500, "uia"))
    ProcessInfo(conn).publish("worker", {"deferred_reason": "game", "pending_jobs": 1}, 5)
    _write_logs(paths.logs_dir)
    paths.config_file.write_text('log_level = "INFO"\n', encoding="utf-8")
    (paths.media_dir / "audio").mkdir(parents=True, exist_ok=True)
    token = new_token()
    sources = DebugSources(paths.root, paths.logs_dir, lambda: {"audio.energy_floor": 0.015})
    server = ApiServer(
        Services(
            conn,
            frames,
            token,
            Controls(lambda: {"capture": "ready", "worker": "ready"}, lambda: None, lambda: None),
            audio,
            sources,
        )
    )
    server.start()
    yield Client(server, token)
    server.stop()


def test_overview_collects_what_the_page_shows(client: Client) -> None:
    status, o = client.call("/api/debug/overview")
    assert status == 200
    assert o["processes"] == {"capture": "ready", "worker": "ready"}
    assert o["info"]["worker"]["data"] == {"deferred_reason": "game", "pending_jobs": 1}
    assert o["jobs"]["counts"] == [{"kind": "transcribe", "state": "pending", "n": 1}]
    (job,) = o["jobs"]["open"]
    assert job["attempts"] == 0
    assert job["error"] is None
    (segment,) = o["audio"]["recent"]
    assert segment["status"] == "pending"
    assert "transcript" not in segment
    assert o["frames"]["total"] == 1
    assert o["settings"] == {"audio.energy_floor": 0.015}
    assert o["schema"] == len(db.load_migrations())


def test_every_debug_route_needs_the_token(client: Client) -> None:
    for path in ("overview", "logs", "logs/files"):
        assert client.call(f"/api/debug/{path}", token="wrong")[0] == 401  # noqa: S106
    for gone in ("tables", "table/frames", "config"):  # trimmed in the review
        assert client.call(f"/api/debug/{gone}")[0] == 404


def test_logs_endpoint(client: Client) -> None:
    status, body = client.call("/api/debug/logs")
    assert status == 200
    assert body["total"] == 6
    assert [e["msg"].split("\n")[0] for e in body["entries"]][-2:] == ["job failed", "back"]
    _, warn = client.call("/api/debug/logs?level=warning")
    assert [e["level"] for e in warn["entries"]] == ["WARNING", "ERROR"]
    _, found = client.call("/api/debug/logs?q=device")
    assert [(e["src"], e["pid"], e["logger"]) for e in found["entries"]] == [
        ("capture", 2, "capture")
    ]
    _, rotated = client.call("/api/debug/logs?name=worker.log.1")
    assert [e["msg"] for e in rotated["entries"]] == ["yesterday"]
    _, files = client.call("/api/debug/logs/files")
    assert [f["name"] for f in files] == ["app.log", "capture.log", "worker.log", "worker.log.1"]
    assert client.call("/api/debug/logs?name=notes.txt")[0] == 404
    assert client.call("/api/debug/logs?name=..%2Fdb%2Fkeyfile")[0] == 404


def test_retry_makes_waiting_jobs_runnable(client: Client, conn: Connection) -> None:
    conn.execute("UPDATE jobs SET state = 'failed', attempts = 5, run_after_ms = 9999999999999")
    assert client.call("/api/debug/jobs/retry", "POST", token="wrong")[0] == 401  # noqa: S106
    assert client.call("/api/debug/jobs/retry", "POST") == (200, {"retried": 1})
    row = conn.execute("SELECT state, attempts, run_after_ms, error FROM jobs").fetchone()
    assert tuple(row) == ("pending", 0, 0, None)


def test_retranscribe_forgets_the_text_and_queues_the_segment_again(
    client: Client, conn: Connection
) -> None:
    conn.execute("UPDATE audio_segments SET status = 'done', language = 'fr', transcript = 'salut'")
    conn.execute("UPDATE jobs SET state = 'done'")
    assert client.call("/api/debug/segments/1/retranscribe", "POST") == (200, {"queued": 1})
    seg = conn.execute("SELECT status, language, transcript FROM audio_segments").fetchone()
    assert tuple(seg) == ("pending", None, None)
    assert conn.execute("SELECT key, state, attempts FROM jobs").fetchall() == [
        ("transcribe:1", "pending", 0)
    ]
    assert conn.execute(
        "SELECT count(*) FROM audio_segments_fts WHERE audio_segments_fts MATCH 'salut'"
    ).fetchone() == (0,)
    assert client.call("/api/debug/segments/99/retranscribe", "POST")[0] == 404
