"""The ask endpoints over a real loopback socket: queue, poll, list, delete."""

from __future__ import annotations

import http.client
import json
from typing import TYPE_CHECKING, cast

import pytest
from memory_data import NOW

from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    def call(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Cookie": f"akashic_session={self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"null")
        conn.close()
        return resp.status, cast("object", payload)


class Env:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.blocked: str | None = None


@pytest.fixture
def env(paths: Paths, conn: Connection) -> Iterator[Env]:
    key = DataKey.generate()
    token = new_token()
    holder: list[Env] = []
    server = ApiServer(
        Services(
            conn,
            FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media"))),
            token,
            Controls(dict, lambda: None, lambda: None),
            ask_blocker=lambda: holder[0].blocked,
        )
    )
    server.start()
    holder.append(Env(Client(server.port, token)))
    yield holder[0]
    server.stop()


def test_a_question_is_stored_and_queued_for_the_worker(env: Env, conn: Connection) -> None:
    status, body = env.client.call("POST", "/api/ask", {"q": "  what video did I watch?  "})
    assert status == 200
    assert isinstance(body, dict)
    assert body["blocked"] is None
    qid = cast("int", body["id"])
    job = conn.execute("SELECT kind, key, data_ms, payload FROM jobs").fetchone()
    assert (job[0], job[1], job[2]) == ("ask", f"ask:{qid}", 0)
    assert json.loads(job[3]) == {"question_id": qid}

    status, got = env.client.call("GET", f"/api/ask/{qid}")
    assert status == 200
    assert isinstance(got, dict)
    assert got["text"] == "what video did I watch?"
    assert got["state"] == "queued"
    assert got["answer"] == ""
    assert got["plan"] is None
    assert got["sources"] is None


def test_greeting_needs_no_memory_sources_or_model(env: Env, conn: Connection) -> None:
    env.blocked = "The chat model is not downloaded yet."
    assert env.client.call("POST", "/api/ask", {"q": "hello"}) == (
        200,
        {"id": None, "answer": "Hello!", "blocked": None},
    )
    assert conn.execute("SELECT count(*) FROM questions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_a_finished_answer_comes_back_with_its_plan_and_sources(env: Env, conn: Connection) -> None:
    conn.execute(
        "INSERT INTO questions (id, asked_ms, text, state, plan, answer, sources, model)"
        " VALUES (5, ?, 'q', 'done', '{\"people\": [\"Minlo\"]}', 'Yes [1].',"
        ' \'[{"n": 1, "kind": "line", "id": 9}]\', \'m\')',
        (NOW,),
    )
    status, got = env.client.call("GET", "/api/ask/5")
    assert status == 200
    assert isinstance(got, dict)
    assert got["plan"] == {"people": ["Minlo"]}
    assert got["sources"] == [{"n": 1, "kind": "line", "id": 9}]
    assert got["answer"] == "Yes [1]."
    assert got["model"] == "m"


def test_a_missing_model_is_reported_and_nothing_is_queued(env: Env, conn: Connection) -> None:
    env.blocked = "The chat model is not downloaded yet."
    status, body = env.client.call("POST", "/api/ask", {"q": "anything"})
    assert status == 200
    assert body == {"id": None, "blocked": "The chat model is not downloaded yet."}
    assert conn.execute("SELECT count(*) FROM questions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("body", [{"q": ""}, {"q": "   "}, {"q": "x" * 501}, {}, {"q": 3}])
def test_bad_questions_are_refused(env: Env, body: object) -> None:
    status, _ = env.client.call("POST", "/api/ask", body)
    assert status == 422


def test_past_questions_are_listed_newest_first(env: Env) -> None:
    for text in ("first", "second", "third"):
        env.client.call("POST", "/api/ask", {"q": text})
    status, listed = env.client.call("GET", "/api/ask?limit=2")
    assert status == 200
    assert isinstance(listed, list)
    rows = cast("list[dict[str, object]]", listed)
    assert [q["text"] for q in rows] == ["third", "second"]
    assert set(rows[0]) == {"id", "asked_ms", "text", "state", "follow_ups", "last_ms"}
    assert env.client.call("GET", "/api/ask?limit=0")[0] == 422


def test_a_follow_up_joins_the_conversation_of_its_first_question(
    env: Env, conn: Connection
) -> None:
    _, first = env.client.call("POST", "/api/ask", {"q": "what did Minlo say?"})
    _, other = env.client.call("POST", "/api/ask", {"q": "another topic"})
    assert isinstance(first, dict)
    assert isinstance(other, dict)
    root = cast("int", first["id"])
    assert first["thread"] == root
    _, second = env.client.call("POST", "/api/ask", {"q": "and after that?", "thread": root})
    assert isinstance(second, dict)
    # Following up on a follow-up still lands in the same conversation.
    _, third = env.client.call(
        "POST", "/api/ask", {"q": "when?", "thread": cast("int", second["id"])}
    )
    assert isinstance(third, dict)
    assert second["thread"] == third["thread"] == root

    status, turns = env.client.call("GET", f"/api/ask/thread/{root}")
    assert status == 200
    rows = cast("list[dict[str, object]]", turns)
    assert [t["text"] for t in rows] == ["what did Minlo say?", "and after that?", "when?"]
    assert {t["thread"] for t in rows} == {root}
    assert env.client.call("GET", f"/api/ask/thread/{second['id']}") == (200, turns)
    assert env.client.call("GET", "/api/ask/thread/999")[0] == 404

    _, listed = env.client.call("GET", "/api/ask")
    convs = cast("list[dict[str, object]]", listed)
    assert [(c["text"], c["follow_ups"]) for c in convs] == [
        ("what did Minlo say?", 2),
        ("another topic", 0),
    ]

    assert env.client.call("POST", "/api/ask", {"q": "x", "thread": 999})[0] == 404
    assert env.client.call("DELETE", f"/api/ask/{root}") == (200, {"deleted": root})
    assert conn.execute("SELECT text FROM questions").fetchall() == [("another topic",)]


def test_a_question_can_be_forgotten(env: Env, conn: Connection) -> None:
    _, body = env.client.call("POST", "/api/ask", {"q": "forget me"})
    assert isinstance(body, dict)
    qid = cast("int", body["id"])
    assert env.client.call("DELETE", f"/api/ask/{qid}") == (200, {"deleted": qid})
    assert env.client.call("GET", f"/api/ask/{qid}")[0] == 404
    assert env.client.call("DELETE", f"/api/ask/{qid}")[0] == 404
    assert conn.execute("SELECT count(*) FROM questions").fetchone()[0] == 0


def test_an_unknown_question_is_404(env: Env) -> None:
    assert env.client.call("GET", "/api/ask/999")[0] == 404


def test_forgetting_a_question_does_not_reuse_its_completed_job(env: Env, conn: Connection) -> None:
    _, first = env.client.call("POST", "/api/ask", {"q": "first question"})
    assert isinstance(first, dict)
    qid = cast("int", first["id"])
    conn.execute("UPDATE jobs SET state = 'done'")
    assert env.client.call("DELETE", f"/api/ask/{qid}")[0] == 200
    _, second = env.client.call("POST", "/api/ask", {"q": "second question"})
    assert isinstance(second, dict)
    assert cast("int", second["id"]) > qid
    assert conn.execute("SELECT count(*) FROM jobs WHERE state = 'pending'").fetchone()[0] == 1


def test_the_endpoints_need_the_token(env: Env) -> None:
    anonymous = Client(env.client.port, "wrong")
    assert anonymous.call("POST", "/api/ask", {"q": "x"})[0] == 401
    assert anonymous.call("GET", "/api/ask")[0] == 401
