"""The persistent job queue, the audio segment store and the Opus codec."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from akashic.pipeline.jobs import (
    BACKOFF_BASE_MS,
    BACKOFF_FACTOR,
    BACKOFF_MAX_MS,
    JobQueue,
    NewJob,
    enqueue_in,
)
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment, audio_rel_path
from akashic.storage.crypto import DataKey
from akashic.storage.media import MediaStore
from akashic.storage.opus import SAMPLE_RATE, decode_opus, encode_opus

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Paths


# --- job queue -----------------------------------------------------------------------------


def test_enqueue_is_idempotent(conn: Connection) -> None:
    queue = JobQueue(conn)
    assert queue.enqueue(NewJob("k", "k:1", 10, {"id": 1}), now_ms=0)
    assert not queue.enqueue(NewJob("k", "k:1", 10, {"id": 1}), now_ms=0)
    assert queue.pending_count() == 1


def test_jobs_run_oldest_data_first_not_oldest_queued(conn: Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("k", "late", 300), now_ms=1)
    queue.enqueue(NewJob("k", "early", 100), now_ms=2)
    queue.enqueue(NewJob("k", "middle", 200), now_ms=3)
    order: list[str] = []
    while job := queue.claim(["k"], now_ms=10):
        order.append(job.key)
        queue.complete(job.id, 10)
    assert order == ["early", "middle", "late"]


def test_claim_only_takes_the_requested_kinds(conn: Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("heavy", "h", 1), now_ms=0)
    queue.enqueue(NewJob("light", "l", 2), now_ms=0)
    assert queue.claim([], now_ms=0) is None
    job = queue.claim(["light"], now_ms=0)
    assert job is not None
    assert job.kind == "light"
    assert queue.claim(["light"], now_ms=0) is None
    assert queue.pending_count(["heavy"]) == 1
    assert queue.pending_count(["light"]) == 0


def test_claimed_job_carries_payload_and_attempt_number(conn: Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("k", "k:7", 5, {"segment_id": 7}), now_ms=0)
    job = queue.claim(["k"], now_ms=0)
    assert job is not None
    assert (job.payload, job.attempts, job.data_ms) == ({"segment_id": 7}, 1, 5)
    assert queue.claim(["k"], now_ms=0) is None  # running jobs are not handed out twice


def test_failed_job_is_retried_with_growing_backoff_then_given_up(conn: Connection) -> None:
    queue = JobQueue(conn, max_attempts=3)
    queue.enqueue(NewJob("k", "k:1", 1), now_ms=0)
    now = 0
    delays: list[int] = []
    for attempt in (1, 2):
        job = queue.claim(["k"], now)
        assert job is not None
        assert job.attempts == attempt
        assert queue.fail(job, "Boom", now)
        delays.append(BACKOFF_BASE_MS * BACKOFF_FACTOR ** (attempt - 1))
        assert queue.claim(["k"], now + delays[-1] - 1) is None  # still waiting out the delay
        now += delays[-1]
    job = queue.claim(["k"], now)
    assert job is not None
    assert not queue.fail(job, "Boom", now)  # third failure: gives up
    assert queue.claim(["k"], now + 10**9) is None
    state, error = conn.execute("SELECT state, error FROM jobs").fetchone()
    assert (state, error) == ("failed", "Boom")


def test_permanent_failure_does_not_retry(conn: Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("k", "k:1", 1), now_ms=0)
    job = queue.claim(["k"], 0)
    assert job is not None
    assert not queue.fail(job, "PermanentJobError", 0, permanent=True)
    assert conn.execute("SELECT state FROM jobs").fetchone() == ("failed",)


def test_backoff_is_capped(conn: Connection) -> None:
    queue = JobQueue(conn, max_attempts=50)
    queue.enqueue(NewJob("k", "k:1", 1), now_ms=0)
    for _ in range(12):
        job = queue.claim(["k"], 10**12)
        assert job is not None
        queue.fail(job, "Boom", 0)
    (run_after,) = conn.execute("SELECT run_after_ms FROM jobs").fetchone()
    assert run_after == BACKOFF_MAX_MS


def test_complete_and_purge(conn: Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("k", "k:1", 1), now_ms=0)
    queue.enqueue(NewJob("k", "k:2", 2), now_ms=0)
    for _ in range(2):
        job = queue.claim(["k"], 0)
        assert job is not None
        queue.complete(job.id, 100 if job.key == "k:1" else 900)
    assert queue.purge_done(finished_before_ms=500) == 1
    assert conn.execute("SELECT key FROM jobs").fetchall() == [("k:2",)]


def test_running_jobs_go_back_to_pending_after_a_crash(conn: Connection) -> None:
    queue = JobQueue(conn)
    queue.enqueue(NewJob("k", "k:1", 1), now_ms=0)
    assert queue.claim(["k"], 0) is not None
    assert queue.pending_count() == 0
    assert queue.release_running() == 1
    assert queue.claim(["k"], 0) is not None


def test_enqueue_inside_a_rolled_back_transaction_leaves_nothing(conn: Connection) -> None:
    def enqueue_then_fail() -> None:
        with db.transaction(conn) as tx:
            enqueue_in(tx, NewJob("k", "k:1", 1), 0)
            raise RuntimeError

    with pytest.raises(RuntimeError):
        enqueue_then_fail()
    assert JobQueue(conn).pending_count() == 0


# --- Opus ----------------------------------------------------------------------------------


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x))))


def _tone(seconds: float, freq: float = 440.0, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_opus_round_trip_keeps_length_and_level() -> None:
    samples = _tone(2.0)
    data = encode_opus(samples)
    assert len(data) < len(samples) * 4 / 10  # compressed at least 10x versus float32
    decoded = decode_opus(data)
    assert decoded.dtype == np.float32
    assert abs(len(decoded) - len(samples)) < SAMPLE_RATE * 0.05
    assert _rms(decoded) == pytest.approx(_rms(samples), rel=0.15)


def test_opus_refuses_empty_audio() -> None:
    with pytest.raises(ValueError, match="no audio"):
        encode_opus(np.zeros(0, dtype=np.float32))


# --- audio store ---------------------------------------------------------------------------


@pytest.fixture
def store(conn: Connection, paths: Paths) -> AudioStore:
    return AudioStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))


def _segment(start: int = 1_700_000_000_000, **kw: object) -> NewSegment:
    fields: dict[str, object] = {
        "stream": "mic",
        "start_ms": start,
        "end_ms": start + 2_000,
        "speech_ms": 1_500,
        "sources": ("discord.exe",),
    }
    fields.update(kw)
    return NewSegment(**fields)  # type: ignore[arg-type]  # test helper


def test_add_stores_row_encrypted_file_and_transcription_job(
    store: AudioStore, conn: Connection, paths: Paths
) -> None:
    segment_id = store.add(_segment(), encode_opus(_tone(1.0)), now_ms=5)
    stored = store.get(segment_id)
    assert stored is not None
    assert (stored.stream, stored.status, stored.sources) == ("mic", "pending", ("discord.exe",))
    assert stored.start_ms == 1_700_000_000_000
    assert stored.audio_path == audio_rel_path(1_700_000_000_000, "mic")
    on_disk = list(paths.media_dir.rglob("*.enc"))
    assert len(on_disk) == 1
    assert b"OggS" not in on_disk[0].read_bytes()  # encrypted: no Ogg header in the clear
    samples = store.read_samples(stored)
    assert samples is not None
    assert abs(len(samples) - SAMPLE_RATE) < SAMPLE_RATE * 0.05
    ogg = store.read_file(stored)
    assert ogg is not None
    assert ogg[:4] == b"OggS"
    job = JobQueue(conn).claim(["transcribe"], now_ms=10)
    assert job is not None
    assert job.payload == {"segment_id": segment_id}
    assert job.data_ms == stored.start_ms  # ordered by when it was heard


def test_failed_insert_removes_the_audio_file(
    store: AudioStore, conn: Connection, paths: Paths
) -> None:
    with pytest.raises(Exception, match="FOREIGN KEY"):
        store.add(_segment(continues_id=999), b"data", now_ms=0)
    assert list(paths.media_dir.rglob("*.enc")) == []
    assert conn.execute("SELECT count(*) FROM audio_segments").fetchone() == (0,)
    assert JobQueue(conn).pending_count() == 0


def test_transcript_is_searchable_with_accents_folded(store: AudioStore, conn: Connection) -> None:
    segment_id = store.add(_segment(), b"x", now_ms=0)
    store.set_transcript(segment_id, "done", "fr", "On a parlé du café de Minlo")
    hits = conn.execute(
        "SELECT rowid FROM audio_segments_fts WHERE audio_segments_fts MATCH ?", ("cafe",)
    ).fetchall()
    assert hits == [(segment_id,)]
    store.set_transcript(segment_id, "done", "fr", "rien")  # the old text leaves the index
    assert conn.execute(
        "SELECT count(*) FROM audio_segments_fts WHERE audio_segments_fts MATCH 'cafe'"
    ).fetchone() == (0,)


def test_continuation_link_and_missing_audio(store: AudioStore, paths: Paths) -> None:
    first = store.add(_segment(10_000), b"x", now_ms=0)
    second = store.add(_segment(11_000, continues_id=first), b"y", now_ms=0)
    stored = store.get(second)
    assert stored is not None
    assert stored.continues_id == first
    for f in paths.media_dir.rglob("*.enc"):
        f.unlink()
    assert store.read_samples(stored) is None
    assert store.read_file(stored) is None
    assert store.get(12345) is None
