"""Reference: today's speakers job, run through the app's own code.

- ``akashic.pipeline.speakers.SpeakersHandler.run`` (fixed 10 s windows from 0 via
  ``OnnxVoiceModels.turns``/``window_turns``; embeds turns >= MIN_IDENTIFY_MS),
- ``akashic.storage.speakers.SpeakerStore.record_turns`` (``_identify``: threshold 0.5,
  MIN_IDENTIFY_MS 1 s, MIN_UPDATE_MS 2 s centroid update / new profile; ``_refresh_segment``),

on an in-memory SQLite database (sqlcipher3 without a key) holding the app's ``speakers`` and
``speaker_turns`` tables from migration 0005 and the ``audio_segments`` columns those queries use.
Nothing here re-implements a rule; the only glue is the fake audio store and job.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
import sqlcipher3

from common import SRC, Attribution, Segment, SharedModels, unit

from akashic.pipeline.speakers import SpeakersHandler
from akashic.storage.speakers import MIN_UPDATE_MS, SpeakerStore

NAME = "C6_ref_rerun"

_SCHEMA = (SRC / "akashic/storage/migrations/0005_speakers.sql").read_text(encoding="utf-8")
_SCHEMA = _SCHEMA.split("-- Transcripts that exist already")[0]  # drop the jobs backfill


class _Audio:
    """Just what SpeakersHandler.run reads from AudioStore."""

    def __init__(self) -> None:
        self.samples: dict[int, np.ndarray] = {}

    def get(self, segment_id: int) -> Any:
        if segment_id not in self.samples:
            return None
        n = len(self.samples[segment_id])
        return SimpleNamespace(id=segment_id, stream="system", start_ms=0, end_ms=n // 16)

    def read_samples(self, segment: Any) -> np.ndarray:
        return self.samples[segment.id]


class Store:
    def __init__(self) -> None:
        self.conn = sqlcipher3.connect(":memory:", isolation_level=None)
        self.conn.execute(
            "CREATE TABLE audio_segments (id INTEGER PRIMARY KEY, speaker_id INTEGER,"
            " start_ms INTEGER NOT NULL DEFAULT 0, audio_path TEXT, transcript TEXT)"
        )
        self.conn.executescript(_SCHEMA)
        self.speakers = SpeakerStore(self.conn)  # DEFAULT_THRESHOLD
        self.audio = _Audio()
        self.handler: SpeakersHandler | None = None


def make_store() -> Store:
    return Store()


def enroll(store: Store, models: SharedModels, clips: list[np.ndarray]) -> int:
    """A named profile built from accepted turns: every >= 2 s turn of the clips (the turns
    that would have built or updated it in the app) summed into vec_sum, as _add_to_profile."""
    conn = store.conn
    cur = conn.execute("INSERT INTO speakers (name, created_ms) VALUES ('enrolled', 0)")
    pid = int(cur.lastrowid)
    for clip in clips:
        for t in models.turns(clip):
            if t.speech_ms >= MIN_UPDATE_MS:
                SpeakerStore._add_to_profile(conn, pid, unit(models.embed(t.samples)))  # noqa: SLF001
    return pid


def attribute(store: Store, models: SharedModels, seg: Segment) -> Attribution:
    if store.handler is None:
        store.handler = SpeakersHandler(store.audio, store.speakers, models, now_ms=lambda: 0)
    conn = store.conn
    before = {int(r[0]): int(r[1]) for r in conn.execute("SELECT id, vec_n FROM speakers")}
    conn.execute("INSERT INTO audio_segments (id) VALUES (?)", (seg.segment_id,))
    store.audio.samples[seg.segment_id] = seg.samples
    store.handler.run(SimpleNamespace(payload={"segment_id": seg.segment_id}))
    del store.audio.samples[seg.segment_id]
    author = conn.execute(
        "SELECT speaker_id FROM audio_segments WHERE id = ?", (seg.segment_id,)
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT start_ms, end_ms, speaker_id, in_profile FROM speaker_turns"
        " WHERE segment_id = ? ORDER BY id",
        (seg.segment_id,),
    ).fetchall()
    updates = [
        (int(sid), "create" if int(sid) not in before else "update", int(a), int(b))
        for a, b, sid, inp in rows
        if inp and sid is not None
    ]
    return Attribution(
        None if author is None else int(author),
        [(int(a), int(b), None if s is None else int(s)) for a, b, s, _ in rows],
        updates,
    )


def centroid(store: Store, profile_id: int) -> np.ndarray | None:
    row = store.conn.execute(
        "SELECT vec_sum FROM speakers WHERE id = ?", (profile_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return unit(np.frombuffer(bytes(row[0]), dtype=np.float32).copy())
