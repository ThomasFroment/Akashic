"""The local API over a real loopback socket: auth, host check, search, frames, headers."""

from __future__ import annotations

import http.client
import io
import json
import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, cast

import pytest
from memory_data import add_monitor, add_visit, app_window, new_frame
from PIL import Image

from akashic.api import frames as api_frames
from akashic.api.models import Downloads
from akashic.api.server import (
    OPEN_CODE_TTL_S,
    UI_DIR,
    ApiServer,
    Controls,
    Services,
    new_token,
)
from akashic.api.timeline import day_range_ms
from akashic.pipeline import models
from akashic.pipeline.models import ModelSpec, Needed
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import CryptoError, DataKey
from akashic.storage.frames import FrameStore, NewFrame, NewWindow
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
    add_monitor(conn, 0)
    add_monitor(conn, 1)
    frames.add(
        new_frame(
            NOON + 1_000, 0, "click", process="chrome.exe", title="Café", text="bonjour le monde"
        )
    )
    frames.add(
        new_frame(
            NOON + 2_000,
            1,
            "idle",
            process="code.exe",
            title="main.py",
            text="hello world",
            source="ocr",
            image=_webp(),
        )
    )
    add_visit(conn, "chrome.exe", "Café", NOON + 1_000, NOON + 2_000)
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
    assert "apps" not in status


def test_apps_lists_the_processes_seen(api: Api) -> None:
    status, _, body = api.get("/api/apps")
    assert status == 200
    assert json.loads(body) == ["chrome.exe", "code.exe"]


def test_pause_calls_the_controller(api: Api) -> None:
    assert api.request("/api/pause?paused=true", method="POST", token=None)[0] == 401
    assert api.request("/api/pause?paused=true", method="POST")[0] == 200
    assert api.request("/api/pause?paused=false", method="POST")[0] == 200
    assert api.paused_calls == [True, False]


def test_search_and_browse(api: Api) -> None:
    hits = api.rows("/api/search?q=hello")
    assert [h["id"] for h in hits] == [2]
    assert set(hits[0]) == {
        "id",
        "window_id",
        "ts_ms",
        "monitor_id",
        "process",
        "title",
        "url",
        "has_image",
        "snippet",
    }
    assert (hits[0]["process"], hits[0]["title"], hits[0]["monitor_id"]) == (
        "code.exe",
        "main.py",
        1,
    )
    assert hits[0]["has_image"] is True
    assert "\x02hello\x03" in str(hits[0]["snippet"])
    everything = api.rows("/api/search?q=")
    assert [h["id"] for h in everything] == [2, 1]
    only = api.rows("/api/search?q=&process=chrome.exe")
    assert [h["id"] for h in only] == [1]
    assert api.get("/api/search?limit=0")[0] == 422


def test_frame_detail_and_neighbors(api: Api) -> None:
    first = api.obj("/api/frames/1")
    assert set(first) == {"id", "ts_ms", "monitor_id", "has_image", "prev_id", "next_id", "windows"}
    assert (first["id"], first["monitor_id"]) == (1, 0)
    assert (first["prev_id"], first["next_id"]) == (None, None)  # frame 2 is on another monitor
    assert first["has_image"] is False
    (window,) = cast("list[dict[str, object]]", first["windows"])
    assert set(window) == {
        "id",
        "role",
        "process",
        "title",
        "url",
        "rect",
        "z",
        "focused",
        "painted",
        "text",
        "text_source",
    }
    assert window["text"] == "bonjour le monde"
    assert (window["process"], window["title"], window["rect"], window["z"]) == (
        "chrome.exe",
        "Café",
        [0, 0, 100, 100],
        0,
    )
    assert (window["focused"], window["text_source"]) == (True, "uia")
    assert api.get("/api/frames/99")[0] == 404


def test_frame_detail_lists_windows_front_to_back_without_the_hwnd(api: Api) -> None:
    services = api.server._services
    frame_id = services.frames.add(
        NewFrame(
            NOON + 9_000,
            1,
            "idle",
            None,
            (
                NewWindow("shell", 3, 5, 0, 0, 800, 40),
                app_window(process="code.exe", title="b", text="beta", z=1, hwnd=22, focused=False),
                NewWindow("hidden", 2, None, 10, 10, 20, 20),
                app_window(process="chrome.exe", title="a", text="alpha", z=0, hwnd=11),
            ),
        )
    )
    detail = api.obj(f"/api/frames/{frame_id}")
    windows = cast("list[dict[str, object]]", detail["windows"])
    assert [(w["z"], w["role"]) for w in windows] == [
        (0, "app"),
        (1, "app"),
        (2, "hidden"),
        (3, "shell"),
    ]
    assert set(windows[2]) == {"id", "role", "rect", "z"}
    assert set(windows[3]) == {"id", "role", "rect", "z"}
    assert windows[3]["rect"] == [0, 0, 800, 40]
    assert "hwnd" not in json.dumps(detail)


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


def test_strip_by_local_day(api: Api) -> None:
    start, end = day_range_ms("2026-11-08")
    assert end - start in (23 * 3_600_000, 24 * 3_600_000, 25 * 3_600_000)
    assert start < NOON < end
    bands = api.obj("/api/strip?day=2026-11-08")["bands"]
    assert bands == [
        {"start_ms": NOON + 1_000, "end_ms": NOON + 2_000, "process": "chrome.exe", "title": "Café"}
    ]
    assert api.obj("/api/strip?day=2026-11-09") == {"bands": []}
    assert api.get("/api/strip?day=nonsense")[0] == 422
    assert api.get("/api/strip?day=0001-01-01")[0] == 422
    assert api.get("/api/timeline?day=2026-11-08")[0] == 404


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


def _lock_is_free(api: Api) -> bool:
    services = api.server._services
    got: list[bool] = []

    def probe() -> None:
        acquired = services._lock.acquire(timeout=1)
        got.append(acquired)
        if acquired:
            services._lock.release()

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(5)
    return got == [True]


def test_image_decrypt_and_thumbnail_run_outside_the_db_lock(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = api.server._services
    seen: list[bool] = []
    read, render_real = services.frames.image, api_frames._render

    def image(frame: object) -> bytes | None:
        seen.append(_lock_is_free(api))
        return read(frame)  # type: ignore[arg-type]

    def render(webp: bytes, width: int | None, crop: object) -> bytes | None:
        seen.append(_lock_is_free(api))
        return render_real(webp, width, crop)  # type: ignore[arg-type]

    monkeypatch.setattr(services.frames, "image", image)
    monkeypatch.setattr(api_frames, "_render", render)
    assert api.get("/api/frames/2/image?w=100")[0] == 200
    assert seen == [True, True]


def test_audio_read_runs_outside_the_db_lock(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = api.server._services.audio
    assert audio is not None
    seen: list[bool] = []
    read = audio.read_file

    def read_file(segment: object) -> bytes | None:
        seen.append(_lock_is_free(api))
        return read(segment)  # type: ignore[arg-type]

    monkeypatch.setattr(audio, "read_file", read_file)
    assert api.get("/api/audio/1")[0] == 200
    assert seen == [True]


def test_thumbnail_cache_hit_skips_decrypt_and_full_size_is_not_cached(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = api.server._services
    thumb = api.get("/api/frames/2/image?w=100")[2]
    reads: list[int] = []
    read = services.frames.image

    def counting(frame: object) -> bytes | None:
        reads.append(1)
        return read(frame)  # type: ignore[arg-type]

    monkeypatch.setattr(services.frames, "image", counting)
    assert api.get("/api/frames/2/image?w=100")[2] == thumb
    assert reads == []
    api.get("/api/frames/2/image")
    api.get("/api/frames/2/image")
    assert len(reads) == 2
    assert not any(key[3] == 0 for key in services._thumbs)


def test_put_thumb_ignores_a_stale_generation(api: Api) -> None:
    services = api.server._services
    key = (2, "p.webp", 0, 100)
    stale = services.thumb_generation()
    services.clear_thumbs()
    assert services.thumb_generation() == stale + 1
    services.put_thumb(key, b"x", stale)
    assert services.thumb(key) is None
    services.put_thumb(key, b"x", services.thumb_generation())
    assert services.thumb(key) == b"x"
    services.clear_thumbs()
    assert services.thumb(key) is None


def test_image_file_removed_after_the_row_read_is_a_404(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(_frame: object) -> bytes | None:
        raise FileNotFoundError

    monkeypatch.setattr(api.server._services.frames, "image", gone)
    status, _, body = api.get("/api/frames/2/image?w=100")
    assert status == 404
    assert b"image file is missing" in body


def test_image_decrypt_failure_is_a_500_logging_only_the_frame_id(
    api: Api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_frame: object) -> bytes | None:
        raise CryptoError("secret detail")

    monkeypatch.setattr(api.server._services.frames, "image", broken)
    with caplog.at_level(logging.WARNING):
        assert api.get("/api/frames/2/image")[0] == 500
    assert "frame 2" in caplog.text
    assert "secret detail" not in caplog.text


def test_audio_error_responses(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = api.server._services.audio
    assert audio is not None

    def missing(_segment: object) -> bytes | None:
        return None

    monkeypatch.setattr(audio, "read_file", missing)
    assert api.get("/api/audio/1")[0] == 404
    for error in (FileNotFoundError(), CryptoError("x")):

        def failing(_s: object, error: Exception = error) -> bytes:
            raise error

        monkeypatch.setattr(audio, "read_file", failing)
        assert api.get("/api/audio/1")[0] == 404
