"""The speakers endpoints over a real loopback socket."""

from __future__ import annotations

import http.client
import json
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest

from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.speakers import NewTurn, SpeakerStore

if TYPE_CHECKING:
    from collections.abc import Iterator

    from akashic.config import Paths

type Json = dict[str, object]


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    def call(self, method: str, path: str, body: Json | None = None) -> tuple[int, object]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Cookie": f"akashic_session={self.token}", "Content-Type": "application/json"}
        conn.request(method, path, None if body is None else json.dumps(body), headers)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, cast("object", data)


def unit(*values: float) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
    v = np.zeros(256, dtype=np.float32)
    v[: len(values)] = values
    return v / np.linalg.norm(v)


@pytest.fixture
def client(paths: Paths) -> Iterator[Client]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    media = MediaStore(paths.media_dir, key.subkey("media"))
    audio = AudioStore(conn, media)
    store = SpeakerStore(conn)
    for i, direction in enumerate([unit(1, 0), unit(0, 1)]):
        seg = audio.add(
            NewSegment("system", 1_000 * (i + 1), 1_000 * (i + 1) + 3_000, 2_500), b"x", 0
        )
        audio.set_transcript(seg, "done", "en", f"hello from voice {i}")
        store.record_turns(seg, [NewTurn(0, 3_000, 3_000, direction)], now_ms=5)
    token = new_token()
    controls = Controls(dict, lambda: None, lambda: None)
    server = ApiServer(
        Services(conn, FrameStore(conn, media), token, controls, audio, speakers=store)
    )
    server.start()
    yield Client(server.port, token)
    server.stop()
    conn.close()


def test_listing_naming_and_merging(client: Client) -> None:
    status, found = client.call("GET", "/api/speakers")
    assert status == 200
    speakers = cast("list[Json]", found)
    assert [s["turns"] for s in speakers] == [1, 1]
    assert speakers[0]["sample"] is not None
    first, second = (cast("int", s["id"]) for s in speakers)

    assert client.call("POST", f"/api/speakers/{first}/name", {"name": "Minlo"})[0] == 200
    _, again = client.call("GET", "/api/speakers")
    assert [s["name"] for s in cast("list[Json]", again)] == ["Minlo", None]

    _, turns = client.call("GET", f"/api/speakers/{first}/turns")
    (turn,) = cast("list[Json]", turns)
    assert turn["text"] == "hello from voice 0"

    status, moved = client.call("POST", f"/api/turns/{turn['id']}/assign", {"speaker_id": second})
    assert (status, cast("Json", moved)["speaker_id"]) == (200, second)

    assert client.call("POST", f"/api/speakers/{first}/merge", {"into": second})[0] == 200
    _, last = client.call("GET", "/api/speakers")
    assert [s["turns"] for s in cast("list[Json]", last)] == [2]


def test_bad_requests_are_404_or_409(client: Client) -> None:
    assert client.call("POST", "/api/speakers/999/name", {"name": "x"})[0] == 404
    assert client.call("POST", "/api/speakers/1/merge", {"into": 1})[0] == 409
    assert client.call("POST", "/api/turns/999/assign", {"speaker_id": None})[0] == 404
    assert client.call("POST", "/api/speakers/1/merge", {"into": 999})[0] == 404


def test_transcripts_carry_the_speaker_name(client: Client) -> None:
    client.call("POST", "/api/speakers/1/name", {"name": "Minlo"})
    _, found = client.call("GET", "/api/transcripts")
    names = {cast("str", u["snippet"]): u["speaker"] for u in cast("list[Json]", found)}
    assert names == {"hello from voice 0": "Minlo", "hello from voice 1": None}
    _, hits = client.call("GET", "/api/transcripts?q=voice")
    assert {u["speaker"] for u in cast("list[Json]", hits)} == {"Minlo", None}
