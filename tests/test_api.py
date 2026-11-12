"""The local API over a real loopback socket: auth, host check, search, frames, headers."""

from __future__ import annotations

import http.client
import io
import json
from datetime import datetime
from typing import TYPE_CHECKING, cast

import pytest
from PIL import Image

from akashic.api.models import Downloads
from akashic.api.server import (
    OPEN_CODE_TTL_S,
    UI_DIR,
    ApiServer,
    Controls,
    Services,
    day_range_ms,
    new_token,
)
from akashic.pipeline import models
from akashic.pipeline.models import ModelSpec, Needed
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from akashic.config import Paths


NOON = int(datetime(2026, 11, 8, 12).astimezone().timestamp() * 1000)  # local noon
BAD_TOKEN = "nope"  # noqa: S105 - deliberately wrong


def _webp() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (800, 400), (200, 100, 50)).save(out, "WEBP")
    return out.getvalue()


class Api:
    def __init__(self, server: ApiServer, token: str, paused: list[bool]) -> None:
        self.server = server
        self.token = token
        self.paused_calls = paused

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        token: str | None = "",
        host: str | None = None,
        origin: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        headers: dict[str, str] = {}
        if origin is not None:
            headers["Origin"] = origin
        if token is not None:
            headers["Cookie"] = f"akashic_session={token or self.token}"
        if host is not None:
            headers["Host"] = host
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        result = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, body)
        conn.close()
        return result

    def get(
        self, path: str, *, token: str | None = "", host: str | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request(path, token=token, host=host)

    def obj(self, path: str) -> dict[str, object]:
        status, _, body = self.get(path)
        assert status == 200, body
        return cast("dict[str, object]", json.loads(body))

    def rows(self, path: str) -> list[dict[str, object]]:
        status, _, body = self.get(path)
        assert status == 200, body
        return cast("list[dict[str, object]]", json.loads(body))


@pytest.fixture
def api(paths: Paths) -> Iterator[Api]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    frames.add(
        NewFrame(NOON + 1_000, 0, "click", "chrome.exe", "Café", None, "bonjour le monde", "uia")
    )
    frames.add(
        NewFrame(
            NOON + 2_000, 1, "idle", "code.exe", "main.py", None, "hello world", "ocr", _webp()
        )
    )
    audio = AudioStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    said = audio.add(
        NewSegment("mic", NOON + 3_000, NOON + 5_000, 1_500, ("discord.exe",)), b"OggS-fake", 0
    )
    audio.set_transcript(said, "done", "fr", "Salut Minlo, on joue au café ?")
    audio.add(NewSegment("system", NOON + 6_000, NOON + 8_000, 1_000), b"OggS-other", 0)  # pending
    paused: list[bool] = []
    token = new_token()
    controls = Controls(
        lambda: {"capture": "ready"}, lambda: paused.append(True), lambda: paused.append(False)
    )
    pending: list[Callable[[], None]] = []
    tiny = ModelSpec("tiny", "Tiny test model", "MIT", "https://huggingface.co/x/y/resolve/abc", ())
    wanted = [Needed(tiny, "Testing"), Needed(models.PYANNOTE_SEGMENTATION, "Speakers")]
    downloads = Downloads(
        paths.models_dir, paths.runtime_dir, lambda: wanted, run_in=pending.append
    )
    services = Services(conn, frames, token, controls, audio, downloads=downloads)
    server = ApiServer(services)
    server.start()
    yield Api(server, token, paused)
    server.stop()
    conn.close()


def test_the_open_link_holds_a_one_time_code_never_the_token(api: Api) -> None:
    url = api.server.open_url()
    assert url.startswith(f"http://127.0.0.1:{api.server.port}/open?code=")
    assert api.token not in url
    code = url.split("code=")[1]
    status, headers, _ = api.get(f"/open?code={code}", token=None)
    assert status == 303
    assert headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"akashic_session={api.token}")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    assert api.get(f"/open?code={code}", token=None)[0] == 403  # used up
    assert api.get("/open?code=made-up", token=None)[0] == 403
    assert api.get("/open", token=None)[0] == 403


def test_an_open_code_expires() -> None:
    services = Services(None, None, "t", Controls(dict, lambda: None, lambda: None))  # type: ignore[arg-type]
    code = services.new_open_code(now=100.0)
    assert not services.take_open_code(code, now=100.0 + OPEN_CODE_TTL_S + 1)
    fresh = services.new_open_code(now=200.0)
    assert services.take_open_code(fresh, now=201.0)
    assert not services.take_open_code(fresh, now=201.0)


def test_a_change_from_another_origin_is_refused(api: Api) -> None:
    assert api.request("/api/pause", method="POST", origin="https://evil.example")[0] == 403
    assert api.paused_calls == []
    ok = api.request("/api/pause", method="POST", origin=f"http://127.0.0.1:{api.server.port}")
    assert ok[0] == 200
    assert api.request("/api/status", origin="https://evil.example")[0] == 200  # reads are fine


def test_models_list_and_download_buttons(api: Api) -> None:
    rows = api.rows("/api/models")
    assert [(r["key"], r["status"], r["needed_for"]) for r in rows] == [
        ("tiny", "ready", "Testing"),  # no files: nothing to fetch
        ("pyannote-segmentation-3.0", "missing", "Speakers"),
    ]
    assert api.request("/api/models/unknown/download", method="POST")[0] == 404
    status, _, body = api.request("/api/models/download-all", method="POST")
    assert (status, json.loads(body)) == (202, {"queued": ["pyannote-segmentation-3.0"]})
    assert api.rows("/api/models")[1]["status"] == "queued"  # waiting for its thread
    busy = api.request("/api/models/pyannote-segmentation-3.0/download", method="POST")
    assert busy[0] == 409
    assert api.request("/api/models/download-all", method="POST")[0] == 409
    assert api.request("/api/models", token=BAD_TOKEN)[0] == 401


def test_api_requires_the_token(api: Api) -> None:
    assert api.get("/api/status", token=None)[0] == 401
    assert api.get("/api/status", token=BAD_TOKEN)[0] == 401
    assert api.get("/api/status")[0] == 200


def test_foreign_host_header_is_rejected(api: Api) -> None:
    assert api.get("/api/status", host="evil.example")[0] == 403
    assert api.get("/", host="evil.example:80")[0] == 403
    assert api.get("/api/status", host=f"localhost:{api.server.port}")[0] == 200


def test_ui_is_served_without_token_and_with_strict_headers(api: Api) -> None:
    status, headers, body = api.get("/", token=None)
    assert status == 200
    assert b"Akashic" in body
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert api.get("/static/app.js", token=None)[0] == 200


def test_ui_references_only_local_assets() -> None:
    for name in ("index.html", "app.css", "app.js"):
        text = (UI_DIR / name).read_text("utf-8")
        assert "http://" not in text, name
        assert "https://" not in text, name


def test_status(api: Api) -> None:
    status = api.obj("/api/status")
    assert status["frames"] == 2
    assert "latest_ts_ms" not in status
    assert status["paused"] is False
    assert status["apps"] == ["chrome.exe", "code.exe"]


def test_pause_calls_the_controller(api: Api) -> None:
    assert api.request("/api/pause?paused=true", method="POST", token=None)[0] == 401
    assert api.request("/api/pause?paused=true", method="POST")[0] == 200
    assert api.request("/api/pause?paused=false", method="POST")[0] == 200
    assert api.paused_calls == [True, False]


def test_search_and_browse(api: Api) -> None:
    hits = api.rows("/api/search?q=hello")
    assert [h["id"] for h in hits] == [2]
    assert hits[0]["has_image"] is True
    assert "\x02hello\x03" in str(hits[0]["snippet"])
    everything = api.rows("/api/search?q=")
    assert [h["id"] for h in everything] == [2, 1]
    only = api.rows("/api/search?q=&process=chrome.exe")
    assert [h["id"] for h in only] == [1]
    assert api.get("/api/search?limit=0")[0] == 422


def test_frame_detail_and_neighbors(api: Api) -> None:
    first = api.obj("/api/frames/1")
    assert first["text"] == "bonjour le monde"
    assert (first["prev_id"], first["next_id"]) == (None, 2)
    assert first["has_image"] is False
    assert api.get("/api/frames/99")[0] == 404


def test_frame_image_full_and_thumbnail(api: Api) -> None:
    status, headers, body = api.get("/api/frames/2/image")
    assert status == 200
    assert headers["content-type"] == "image/webp"
    assert Image.open(io.BytesIO(body)).size == (800, 400)
    thumb = api.get("/api/frames/2/image?w=200")[2]
    assert Image.open(io.BytesIO(thumb)).size == (200, 100)
    assert api.get("/api/frames/2/image?w=200")[2] == thumb  # served from the cache
    assert api.get("/api/frames/1/image")[0] == 404  # no screenshot kept
    assert api.get("/api/frames/2/image?w=5")[0] == 422


def test_timeline_by_local_day(api: Api) -> None:
    start, end = day_range_ms("2026-11-08")
    assert end - start in (23 * 3_600_000, 24 * 3_600_000, 25 * 3_600_000)
    assert start < NOON < end
    assert [h["id"] for h in api.rows("/api/timeline?day=2026-11-08")] == [1, 2]
    assert api.rows("/api/timeline?day=2026-11-09") == []
    assert api.get("/api/timeline?day=nonsense")[0] == 422
    assert api.get("/api/timeline?day=0001-01-01")[0] == 422


def test_missing_image_file_is_a_404(api: Api, paths: Paths) -> None:
    for f in paths.media_dir.rglob("*.enc"):
        f.unlink()
    assert api.get("/api/frames/2/image")[0] == 404


def test_transcript_search_and_browse(api: Api) -> None:
    found = api.rows("/api/transcripts?q=cafe")
    assert [t["id"] for t in found] == [1]
    assert "café" in str(found[0]["snippet"])
    assert (found[0]["stream"], found[0]["language"], found[0]["sources"]) == (
        "mic",
        "fr",
        ["discord.exe"],
    )
    browsed = api.rows("/api/transcripts")
    assert [t["id"] for t in browsed] == [1]  # the untranscribed segment is not listed
    assert api.rows("/api/transcripts?q=cafe&start_ms=0&end_ms=1") == []
    assert api.rows("/api/transcripts?q=zzzz") == []
    assert api.get("/api/transcripts?limit=0")[0] == 422


def test_audio_playback_endpoint(api: Api, paths: Paths) -> None:
    status, headers, body = api.get("/api/audio/1")
    assert (status, headers["content-type"], body) == (200, "audio/ogg", b"OggS-fake")
    assert api.get("/api/audio/99")[0] == 404
    assert api.get("/api/audio/1", token=None)[0] == 401
    assert "media-src 'self' blob:" in api.get("/", token=None)[1]["content-security-policy"]
    for f in paths.media_dir.rglob("*.enc"):
        f.unlink()
    assert api.get("/api/audio/1")[0] == 404


def test_audio_endpoint_without_an_audio_store(paths: Paths) -> None:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    token = new_token()
    server = ApiServer(Services(conn, frames, token, Controls(dict, lambda: None, lambda: None)))
    server.start()
    try:
        assert Api(server, token, []).get("/api/audio/1")[0] == 404
    finally:
        server.stop()
        conn.close()
