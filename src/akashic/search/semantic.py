"""Getting the vector of a search question.

The API process has no embedding model: the worker owns llama-server. So the API queues an
``embed_query`` job (at the front of the queue), waits a few seconds for the vector to appear in
the database, and reads it. If the worker cannot answer in time (model not downloaded, a game
running and the server not started, worker busy) the caller gets None and searches by words only.
The database is only held while reading or queueing, never while waiting.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from akashic.pipeline.embed import query_job
from akashic.pipeline.jobs import JobQueue
from akashic.storage import embeddings

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from sqlite3 import Connection

    import numpy as np
    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]
    type Access = Callable[[], AbstractContextManager[Connection]]

WAIT_S: Final = 8.0
POLL_S: Final = 0.1


def semantic_ready(conn: Connection, model: str) -> bool:
    """Whether any vector made by ``model`` exists: without one there is nothing to compare."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM embeddings WHERE owner_kind = 'episode' AND model = :model)"
        " OR EXISTS (SELECT 1 FROM segment_passages WHERE model = :model)"
        " OR EXISTS (SELECT 1 FROM frame_passages WHERE model = :model)",
        {"model": model},
    ).fetchone()
    return bool(row[0])


def query_vector(  # noqa: PLR0913 - collaborators and timing, all injected for tests
    access: Access,
    text: str,
    model: str,
    now_ms: Callable[[], int],
    *,
    wait_s: float = WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> Vector | None:
    """The vector of ``text``, or None when it cannot be had in ``wait_s`` seconds."""
    owner_id = embeddings.query_id(text, model)
    with access() as conn:
        if not text.strip() or not semantic_ready(conn, model):
            return None
        cached = embeddings.get(conn, "query", owner_id, model)
        if cached is not None:
            return cached
        JobQueue(conn).enqueue(query_job(text, model, now_ms()), now_ms())
    waited = 0.0
    while waited < wait_s:
        sleep(POLL_S)
        waited += POLL_S
        with access() as conn:
            found = embeddings.get(conn, "query", owner_id, model)
        if found is not None:
            return found
    return None
