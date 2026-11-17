"""Missing Whisper and voice models block nothing: capture rows, episodes and the mic speaker
still flow, and the system speakers jobs follow once the voice models are downloaded."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import numpy as np
from clock_fakes import ManualClock
from memory_data import HOUR, MINUTE, NOW, add_game, add_visit

from akashic.config import Config
from akashic.config import Scheduler as SchedulerSettings
from akashic.pipeline.main import housekeeping
from akashic.pipeline.runner import JobRunner
from akashic.pipeline.scheduler import Scheduler
from akashic.pipeline.speakers import LocalTurn, SpeakersHandler, model_paths
from akashic.pipeline.transcribe import TranscribeHandler, Transcript
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.jobs import JobQueue
from akashic.storage.opus import encode_opus
from akashic.storage.speakers import SpeakerStore

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

    from akashic.child import ChildContext
    from akashic.config import Paths
    from akashic.storage.media import MediaStore


class Idle:
    def cpu_percent(self) -> float:
        return 1.0

    def gpu_percent(self) -> float | None:
        return 1.0


class Whisper:
    def __init__(self) -> None:
        self.present = True

    def available(self) -> bool:
        return self.present

    def load_error(self) -> str | None:
        return None

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript:
        del samples, prompt
        return Transcript("bonjour tout le monde", "fr", 0.9)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


class Voices:
    def __init__(self) -> None:
        self.present = False

    def available(self) -> bool:
        return self.present

    def turns(self, samples: NDArray[np.float32]) -> list[LocalTurn]:
        return [LocalTurn(0, 3_000, 3_000, samples)]

    def embed(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        del samples
        return np.eye(256, dtype=np.float32)[0]

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


def _add(audio: AudioStore, stream: str, start: int) -> int:
    tone = (0.3 * np.sin(np.arange(16_000) / 10)).astype(np.float32)
    segment = NewSegment(stream, start, start + 3_000, 2_500)  # type: ignore[arg-type]
    return audio.add(segment, encode_opus(tone), now_ms=start)


def _speakers_keys(conn: Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT key FROM jobs WHERE kind = 'speakers'")]


def _turns(conn: Connection, segment_id: int) -> int:
    return conn.execute(
        "SELECT count(*) FROM speaker_turns WHERE segment_id = ?", (segment_id,)
    ).fetchone()[0]


def test_missing_models_block_nothing_and_system_speakers_follow_the_download(
    conn: Connection, paths: Paths, media: MediaStore
) -> None:
    audio, clock = AudioStore(conn, media), ManualClock()
    whisper, voices = Whisper(), Voices()
    queue = JobQueue(conn)
    runner = JobRunner(
        queue,
        Scheduler(conn, Idle(), SchedulerSettings()),
        [
            TranscribeHandler(audio, whisper, (), "test-model", voices.available),
            SpeakersHandler(audio, SpeakerStore(conn), voices, now_ms=clock),
        ],
        clock,
    )
    ctx = cast("ChildContext", SimpleNamespace(conn=conn, config=Config(), paths=paths))

    start = NOW - 6 * HOUR
    mic = _add(audio, "mic", start)
    systems = [_add(audio, "system", start + 10 * MINUTE * n) for n in (1, 2)]
    for _ in range(4):  # three transcriptions and the mic speakers job, oldest data first
        assert runner.step() == "ran"
    assert _speakers_keys(conn) == [f"speakers:{mic}"]  # none for the system segments
    assert _turns(conn, mic) == 1  # the mic needs no voice model
    assert conn.execute("SELECT is_self FROM speakers").fetchall() == [(1,)]

    whisper.present = False  # the Whisper model goes missing: recording still adds segments
    waiting = _add(audio, "system", start + HOUR)
    add_game(conn, NOW - 5 * HOUR, NOW - 4 * HOUR)
    add_visit(conn, "code.exe", "main.py - Akashic", NOW - 8 * HOUR, NOW - 7 * HOUR)
    housekeeping(ctx, queue, NOW, voices.available)
    assert conn.execute("SELECT kind FROM episodes ORDER BY kind").fetchall() == [
        ("game",),
        ("work",),
    ]
    assert runner.step() == "deferred"  # only the blocked transcription is left
    assert conn.execute(
        "SELECT status FROM audio_segments WHERE id = ?", (waiting,)
    ).fetchone() == ("pending",)

    for file in model_paths(paths.models_dir):  # the voice models arrive
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"x")
    voices.present = True
    housekeeping(ctx, queue, NOW, voices.available)
    housekeeping(ctx, queue, NOW, voices.available)
    assert sorted(_speakers_keys(conn)) == sorted(f"speakers:{i}" for i in (mic, *systems))
    for _ in range(2):
        assert runner.step() == "ran"
    assert [_turns(conn, s) for s in systems] == [1, 1]
