"""The memory endpoints over a real loopback socket."""

from __future__ import annotations

import http.client
import json
from datetime import datetime
from typing import TYPE_CHECKING, Final, cast

import numpy as np
import pytest
from memory_data import (
    MINUTE,
    add_episode,
    add_frame,
    add_segment,
    add_speaker,
)

from akashic.api import memory as memory_api
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import DEFAULT_EMBEDDING_MODEL
from akashic.storage import embeddings, passages
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths

type Json = dict[str, object]


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    def get(self, path: str) -> tuple[int, object]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Cookie": f"akashic_session={self.token}"})
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, cast("object", data)


@pytest.fixture
def client(paths: Paths, conn: Connection) -> Iterator[Client]:
    key = DataKey.generate()
    token = new_token()
    server = ApiServer(
        Services(
            conn,
            FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media"))),
            token,
            Controls(dict, lambda: None, lambda: None),
        )
    )
    server.start()
    yield Client(server.port, token)
    server.stop()


# One fixed "now" for the test and the route: noon today, local time. With the real clock, a run
# that crossed midnight between the fixture and the request asked about the wrong "today".
NOON_MS: Final = int(
    datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    * 1000
)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse
    monkeypatch.setattr(memory_api, "_now_ms", lambda: NOON_MS)


def today_ms() -> int:
    """A moment inside the pinned today: just after its midnight."""
    noon = datetime.fromtimestamp(NOON_MS / 1000).astimezone()
    midnight = noon.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000) + 30_000


def test_search_answers_the_video_question_from_the_episodes_alone(
    client: Client, conn: Connection
) -> None:
    start = today_ms()
    add_episode(conn, "video", start, start + MINUTE, "Cooking show", "pasta tutorial")
    add_episode(conn, "game", start, start + MINUTE, "A game")
    status, found = client.get("/api/memory/search?q=" + "what%20video%20did%20I%20watch%20today")
    assert status == 200
    out = cast("Json", found)
    assert [e["title"] for e in cast("list[Json]", out["episodes"])] == ["Cooking show"]
    plan = cast("Json", out["plan"])
    assert plan["kinds"] == ["video"]
    window = cast("Json", plan["time"])
    assert window["label"] == "what video did I watch today"
    assert cast("int", window["start_ms"]) <= start < NOON_MS <= cast("int", window["end_ms"])
    assert out["semantic"] is False


def test_search_finds_what_a_named_person_said_and_labels_it(
    client: Client, conn: Connection
) -> None:
    minlo = add_speaker(conn, "Minlo")
    me = add_speaker(conn, None, is_self=True)
    start = today_ms()
    segment = add_segment(conn, start, text="did you see the new game trailer", speaker=minlo)
    add_segment(conn, start + MINUTE, text="not yet", stream="mic", sources=(), speaker=me)
    episode = add_episode(
        conn, "conversation", start, start + 2 * MINUTE, "Call", people=(minlo, me)
    )
    conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (episode, segment))
    add_frame(conn, start, "game trailer page", "Trailer")
    _, found = client.get(
        "/api/memory/search?q=" + "what%20did%20Minlo%20say%20today%20about%20the%20game"
    )
    out = cast("Json", found)
    (hit,) = cast("list[Json]", out["episodes"])
    assert (hit["kind"], hit["people"]) == ("conversation", ["Minlo", "You"])
    (line,) = cast("list[Json]", out["lines"])
    assert line["speaker"] == "Minlo"
    assert line["segment_id"] == segment
    assert line["episode_id"] == episode
    assert line["text"] == "did you see the new game trailer"
    # A screen keyword match cannot establish what Minlo said.
    assert out["frames"] == []
    assert cast("Json", out["plan"])["people"] == ["Minlo"]


def test_search_rejects_empty_and_oversized_questions(client: Client) -> None:
    assert client.get("/api/memory/search?q=")[0] == 422
    assert client.get("/api/memory/search?q=" + "x" * 501)[0] == 422
    assert client.get("/api/memory/search?q=x&limit=0")[0] == 422
    assert client.get("/api/memory/search")[0] == 422


def test_frame_only_semantic_search_returns_matching_excerpt_and_link(
    client: Client, conn: Connection
) -> None:
    fact = "Les sauvegardes sont chiffrées sur un disque externe."
    text = "Introduction. " * 100 + fact
    target = add_frame(conn, today_ms(), text)
    vector = np.array([1, 0], dtype=np.float32)
    values = [
        vector if "sauvegardes" in text[a:b] else np.array([0, 1], dtype=np.float32)
        for a, b in passages.windows(text)
    ]
    passages.publish(conn, target, DEFAULT_EMBEDDING_MODEL, text, values, kind="frame")
    embeddings.put(
        conn,
        "query",
        embeddings.query_id("backup privacy", DEFAULT_EMBEDDING_MODEL),
        DEFAULT_EMBEDDING_MODEL,
        vector,
        today_ms(),
        today_ms(),
    )
    status, data = client.get("/api/memory/search?q=backup%20privacy")
    assert status == 200
    found = cast("Json", data)
    assert found["semantic"] is True
    (hit,) = cast("list[Json]", found["frames"])
    assert hit["id"] == target
    assert fact in str(hit["snippet"])


def test_the_episode_list_is_a_timeline_that_can_be_filtered_by_kind(
    client: Client, conn: Connection
) -> None:
    add_episode(conn, "game", 1_000, 2_000, "Later")
    add_episode(conn, "video", 100, 500, "Earlier")
    add_episode(conn, "video", 9_000, 9_500, "Out of range")
    _, found = client.get("/api/episodes?start_ms=0&end_ms=5000")
    assert [e["title"] for e in cast("list[Json]", found)] == ["Earlier", "Later"]
    _, videos = client.get("/api/episodes?start_ms=0&end_ms=5000&kind=video")
    assert [e["title"] for e in cast("list[Json]", videos)] == ["Earlier"]
    assert client.get("/api/episodes?start_ms=5&end_ms=6")[1] == []
    assert client.get("/api/episodes?start_ms=0&end_ms=5000&kind=nope")[0] == 422
    assert client.get("/api/episodes?start_ms=0")[0] == 422


def test_an_episode_comes_with_what_was_said_and_a_picture_to_open(
    client: Client, conn: Connection
) -> None:
    episode = add_episode(conn, "video", 1_000, 90_000, "Video", "body")
    later = add_segment(conn, 20_000, text="deuxième phrase", sources=("zen.exe",))
    earlier = add_segment(conn, 10_000, text="première phrase", sources=("zen.exe",))
    for s in (later, earlier):
        conn.execute("INSERT INTO episode_items VALUES (?, 'video_segment', ?)", (episode, s))
    frame = add_frame(conn, 5_000, "text", "Video - Chrome")
    conn.execute("UPDATE episodes SET app = 'Google Chrome' WHERE id = ?", (episode,))
    conn.execute("UPDATE frames SET image_path = 'x.enc' WHERE id = ?", (frame,))
    status, found = client.get(f"/api/episodes/{episode}")
    assert status == 200
    out = cast("Json", found)
    assert out["title"] == "Video"
    assert out["frame_id"] == frame
    assert [line["text"] for line in cast("list[Json]", out["lines"])] == [
        "première phrase",
        "deuxième phrase",
    ]


def test_an_unknown_episode_is_a_404_and_one_without_picture_has_none(
    client: Client, conn: Connection
) -> None:
    assert client.get("/api/episodes/999")[0] == 404
    episode = add_episode(conn, "work", 1, 2, "Quiet")
    _, found = client.get(f"/api/episodes/{episode}")
    assert cast("Json", found)["frame_id"] is None
    assert cast("Json", found)["lines"] == []


def test_episode_image_requires_the_episodes_app(client: Client, conn: Connection) -> None:
    episode = add_episode(conn, "conversation", 1_000, 90_000, "Discord call")
    conn.execute("UPDATE episodes SET app = 'discord.exe' WHERE id = ?", (episode,))
    unrelated = add_frame(conn, 2_000, "task manager")
    matching = add_frame(conn, 4_000, "call")
    conn.execute("UPDATE frames SET image_path = 'x.enc' WHERE id IN (?, ?)", (unrelated, matching))
    conn.execute("UPDATE frames SET process = 'taskmgr.exe' WHERE id = ?", (unrelated,))
    conn.execute("UPDATE frames SET process = 'discord.exe' WHERE id = ?", (matching,))
    _, found = client.get(f"/api/episodes/{episode}")
    assert cast("Json", found)["frame_id"] == matching
    conn.execute("DELETE FROM frames WHERE id = ?", (matching,))
    _, found = client.get(f"/api/episodes/{episode}")
    assert cast("Json", found)["frame_id"] is None


def test_video_image_requires_its_url_not_another_tab(client: Client, conn: Connection) -> None:
    episode = add_episode(conn, "video", 1_000, 90_000, "A video")
    conn.execute(
        "UPDATE episodes SET app = 'Google Chrome', url = 'https://example.org/video' WHERE id = ?",
        (episode,),
    )
    frame = add_frame(conn, 2_000, "unrelated tab")
    conn.execute(
        "UPDATE frames SET image_path = 'x.enc', url = 'https://example.org/other' WHERE id = ?",
        (frame,),
    )
    _, found = client.get(f"/api/episodes/{episode}")
    assert cast("Json", found)["frame_id"] is None
    conn.execute("UPDATE frames SET url = 'https://example.org/video' WHERE id = ?", (frame,))
    _, found = client.get(f"/api/episodes/{episode}")
    assert cast("Json", found)["frame_id"] == frame


def test_the_memory_endpoints_need_the_token(client: Client) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    conn.request("GET", "/api/memory/search?q=x")
    assert conn.getresponse().status == 401
    conn.close()
