"""Fakes and builders for the ask job and the embedding handlers: scripted chat, vector models."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
from memory_data import NOW

from akashic.pipeline.ask import AskHandler, ask_job
from akashic.pipeline.jobs import Job
from akashic.pipeline.llm import LlamaError
from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

PLAN_OK = json.dumps(
    {
        "time_text": "",
        "people": ["Minlo"],
        "kinds": ["conversation"],
        "keywords": ["game"],
        "semantic_query": "a game",
    }
)


class FakeChat:
    model = "chat-model"

    def __init__(
        self,
        pieces: tuple[str, ...] = ("He talked about a game [1].",),
        *,
        plan: str | Exception = PLAN_OK,
        available: bool = True,
        stream_error: bool = False,
    ) -> None:
        self.pieces = pieces
        self.plan = plan
        self._available = available
        self.stream_error = stream_error
        self.prompts: list[tuple[str, str]] = []
        self.plans_asked = 0
        self.unloads = 0
        self.on_piece: Callable[[], None] = lambda: None

    def available(self) -> bool:
        return self._available

    def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], max_tokens: int = 0
    ) -> str:
        del system, user, schema, max_tokens
        self.plans_asked += 1
        if isinstance(self.plan, Exception):
            raise self.plan
        return self.plan

    def stream(self, system: str, user: str, max_tokens: int = 0) -> Iterator[str]:
        del max_tokens
        self.prompts.append((system, user))
        for piece in self.pieces:
            self.on_piece()
            yield piece
        if self.stream_error:
            raise LlamaError

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force
        self.unloads += 1


class FakeEmbedder:
    """Embeds questions only: every query gets the same vector."""

    model = "emb"

    def __init__(self, *, available: bool = True, fail: bool = False) -> None:
        self._available = available
        self.fail = fail
        self.queries: list[str] = []

    def available(self) -> bool:
        return self._available

    def embed_query(self, text: str) -> Vector:
        self.queries.append(text)
        if self.fail:
            raise LlamaError
        return np.array([1.0, 0.0], dtype=np.float32)


class DocumentEmbedder:
    """Embeds documents as [1, length] and questions as [0, 1]."""

    model = "m"

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.documents: list[str] = []
        self.queries: list[str] = []
        self.unloads = 0

    def available(self) -> bool:
        return self._available

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        self.documents.extend(texts)
        return [np.array([1.0, float(len(t))], dtype=np.float32) for t in texts]

    def embed_query(self, text: str) -> Vector:
        self.queries.append(text)
        return np.array([0.0, 1.0], dtype=np.float32)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force
        self.unloads += 1


class FlushClock:
    """A seconds clock that is later than the answer flush interval at every look."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 0.3
        return self.t


def make(
    conn: Connection,
    chat: FakeChat | None = None,
    embedder: FakeEmbedder | None = None,
    *,
    chat_present: bool = True,
    embedding_present: bool = True,
) -> tuple[AskHandler, FakeChat, FakeEmbedder]:
    chat = chat or FakeChat()
    embedder = embedder or FakeEmbedder()
    handler = AskHandler(
        conn,
        chat,
        embedder,
        lambda: chat_present,
        lambda: embedding_present,
        lambda: NOW,
        FlushClock(),
    )
    return handler, chat, embedder


def ask(conn: Connection, text: str = "what did Minlo say about a game?") -> Job:
    with db.transaction(conn):
        cursor = conn.execute("INSERT INTO questions (asked_ms, text) VALUES (?, ?)", (NOW, text))
    assert cursor.lastrowid is not None
    new = ask_job(cursor.lastrowid)
    return Job(1, new.kind, new.key, new.payload, new.data_ms, 1)
