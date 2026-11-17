"""Transcript text rules and the transcribe job handler (Whisper itself is faked)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from akashic.pipeline.transcribe import (
    MAX_GAIN,
    PREVIOUS_MAX_CHARS,
    PROMPT_MAX_CHARS,
    TARGET_RMS,
    LoadFailure,
    TranscribeHandler,
    Transcript,
    build_prompt,
    is_hallucination,
    normalize,
    normalize_loudness,
    trim_overlap,
)
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.jobs import DEFER_MS, DeferJob, Job, PermanentJobError
from akashic.storage.media import MediaStore
from akashic.storage.opus import encode_opus

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

    from akashic.config import Paths


# --- text rules ----------------------------------------------------------------------------


def test_normalize_folds_case_accents_and_punctuation() -> None:
    assert normalize("C\u2019est GÉNIAL, peut-être — non ?") == "c'est genial peut etre non"
    assert normalize("  ...  ") == ""


def test_prompt_takes_whole_entries_up_to_the_cap() -> None:
    assert build_prompt(()) is None
    assert build_prompt(("Minlo", "Elden Ring")) == "Minlo, Elden Ring"
    prompt = build_prompt(("a" * 500, "b" * 250, "c" * 100), max_chars=PROMPT_MAX_CHARS)
    assert prompt == f"{'a' * 500}, {'b' * 250}"  # the third would not fit whole
    assert build_prompt(("x" * 900,)) is None  # a single entry longer than the cap is skipped


def test_prompt_ends_with_what_was_just_said() -> None:
    assert build_prompt(("Minlo",), "et puis on a joué") == "Minlo. et puis on a joué"
    assert build_prompt((), "  et puis\n on a joué ") == "et puis on a joué"
    long = " ".join(["mot"] * 100)  # 399 characters
    tail = build_prompt((), long)
    assert tail is not None
    assert len(tail) <= PREVIOUS_MAX_CHARS
    assert tail.startswith("mot")  # cut at a word, never inside one
    full = build_prompt(("v" * 799,), "context")  # the vocabulary comes first
    assert full == "v" * 799


def test_quiet_speech_is_boosted_and_loud_speech_left_alone() -> None:
    tone = np.sin(np.arange(16_000) / 10).astype(np.float32)
    quiet = normalize_loudness(0.02 * tone)
    rms = float(np.sqrt(np.mean(np.square(quiet))))
    assert rms == pytest.approx(TARGET_RMS, rel=0.05)
    whisper_quiet = normalize_loudness(0.001 * tone)  # at most MAX_GAIN louder
    assert float(np.abs(whisper_quiet).max()) == pytest.approx(0.001 * MAX_GAIN, rel=0.01)
    loud = 0.5 * tone
    assert normalize_loudness(loud) is loud
    silence = np.zeros(100, dtype=np.float32)
    assert normalize_loudness(silence) is silence
    empty = np.zeros(0, dtype=np.float32)
    assert normalize_loudness(empty) is empty


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("we went to the big store yesterday and", "yesterday and then we left", "then we left"),
        # the boundary cut the first word in half: "morning" became "ning"
        ("went to the big store yesterday morning", "ning yesterday morning we left", "we left"),
        # accents, case, punctuation and French elisions don't matter
        ("il a dit : c'est génial", "C\u2019est génial, et on a ri.", "et on a ri."),
        # nothing in common: untouched
        ("hello there", "completely different words", "completely different words"),
        # a single shared word is not an overlap
        ("hello there", "there you are", "there you are"),
        # all of it is the overlap
        ("a b c d", "c d", ""),
        # the match must not end inside a hyphenated token
        ("rendez vous peut", "vous peut-être demain", "vous peut-être demain"),
        ("", "anything at all", "anything at all"),
    ],
)
def test_trim_overlap(previous: str, current: str, expected: str) -> None:
    assert trim_overlap(previous, current) == expected


def test_known_hallucinations_are_recognised_whole_only() -> None:
    assert is_hallucination("Sous-titres réalisés par la communauté d'Amara.org")
    assert is_hallucination("Thanks for watching!")
    assert is_hallucination("Merci d'avoir regardé cette vidéo.")
    assert not is_hallucination("thanks for watching the game last night, it was great")
    assert not is_hallucination("merci")


# --- handler -------------------------------------------------------------------------------


class FakeTranscriber:
    def __init__(self, *results: Transcript | Exception | None) -> None:
        self.results = list(results)
        self.load_reason: str | None = None
        self.calls: list[tuple[int, str | None]] = []
        self.model_present = True
        self.unloads = 0
        self.forced = 0

    def available(self) -> bool:
        return self.model_present

    def load_error(self) -> str | None:
        return self.load_reason

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript | None:
        self.calls.append((len(samples), prompt))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def unload_if_idle(self, *, force: bool = False) -> None:
        self.unloads += 1
        self.forced += force


@pytest.fixture
def audio(conn: Connection, paths: Paths) -> AudioStore:
    return AudioStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))


def add_segment(audio: AudioStore, start: int = 1_000_000, **kw: object) -> int:
    fields: dict[str, object] = {
        "stream": "mic",
        "start_ms": start,
        "end_ms": start + 2_000,
        "speech_ms": 1_500,
    }
    fields.update(kw)
    tone = (0.3 * np.sin(np.arange(16_000) / 10)).astype(np.float32)
    return audio.add(NewSegment(**fields), encode_opus(tone), now_ms=0)  # type: ignore[arg-type]


def job_for(segment_id: int) -> Job:
    return Job(1, "transcribe", f"transcribe:{segment_id}", {"segment_id": segment_id}, 0, 1)


def test_transcript_is_stored_with_its_language(audio: AudioStore) -> None:
    seg = add_segment(audio)
    fake = FakeTranscriber(Transcript("  Salut Minlo  ", "fr"))
    TranscribeHandler(audio, fake, ("Minlo", "Elden Ring"), "test-model", lambda: True).run(
        job_for(seg)
    )
    stored = audio.get(seg)
    assert stored is not None
    assert (stored.status, stored.language, stored.transcript) == ("done", "fr", "Salut Minlo")
    ((samples, prompt),) = fake.calls
    assert abs(samples - 16_000) < 800  # the decoded recording, 16 kHz
    assert prompt == "Minlo, Elden Ring"


def test_the_previous_line_is_context_and_confidence_is_kept(
    audio: AudioStore, conn: Connection
) -> None:
    first = add_segment(audio, 1_000_000)
    second = add_segment(audio, 1_030_000)  # 28 s after the first ended: same conversation
    late = add_segment(audio, 1_200_000)  # minutes later: no context
    other = add_segment(audio, 1_031_000, stream="system")  # another stream: no context
    fake = FakeTranscriber(
        Transcript("on parlait du jeu", "fr", 0.8),
        Transcript("et de la sortie", "fr", 0.4),
        Transcript("autre chose", "fr"),
        Transcript("heard", "en"),
    )
    handler = TranscribeHandler(audio, fake, ("Minlo",), "test-model", lambda: True)
    for seg in (first, second, late, other):
        handler.run(job_for(seg))
    assert [p for _, p in fake.calls] == [
        "Minlo",
        "Minlo. on parlait du jeu",
        "Minlo",
        "Minlo",
    ]
    rows = conn.execute("SELECT confidence FROM audio_segments ORDER BY id").fetchall()
    assert rows == [(0.8,), (0.4,), (None,), (None,)]
    assert audio.requeue(second, 0)
    assert conn.execute(
        "SELECT confidence FROM audio_segments WHERE id = ?", (second,)
    ).fetchone() == (None,)


def test_no_speech_and_hallucinations_become_empty(audio: AudioStore) -> None:
    a, b = add_segment(audio, 1_000_000), add_segment(audio, 2_000_000)
    handler = TranscribeHandler(
        audio,
        FakeTranscriber(None, Transcript("Thanks for watching!", "en")),
        (),
        "test-model",
        lambda: True,
    )
    handler.run(job_for(a))
    handler.run(job_for(b))
    for seg in (a, b):
        stored = audio.get(seg)
        assert stored is not None
        assert (stored.status, stored.transcript) == ("empty", None)


def test_overlap_with_the_previous_segment_is_removed(audio: AudioStore) -> None:
    first = add_segment(audio, 1_000_000)
    second = add_segment(audio, 1_028_000, continues_id=first)
    handler = TranscribeHandler(
        audio,
        FakeTranscriber(
            Transcript("on a commencé à jouer hier soir", "fr"),
            Transcript("hier soir et ensuite on a gagné", "fr"),
        ),
        (),
        "test-model",
        lambda: True,
    )
    handler.run(job_for(first))
    handler.run(job_for(second))
    stored = audio.get(second)
    assert stored is not None
    assert stored.transcript == "et ensuite on a gagné"


def test_a_continuation_that_is_all_overlap_is_empty(audio: AudioStore) -> None:
    first = add_segment(audio, 1_000_000)
    second = add_segment(audio, 1_028_000, continues_id=first)
    handler = TranscribeHandler(
        audio,
        FakeTranscriber(Transcript("a b c d e", "en"), Transcript("d e", "en")),
        (),
        "test-model",
        lambda: True,
    )
    handler.run(job_for(first))
    handler.run(job_for(second))
    stored = audio.get(second)
    assert stored is not None
    assert stored.status == "empty"


def test_a_continuation_is_kept_whole_when_the_previous_has_no_transcript(
    audio: AudioStore,
) -> None:
    first = add_segment(audio, 1_000_000)
    second = add_segment(audio, 1_028_000, continues_id=first)
    handler = TranscribeHandler(
        audio,
        FakeTranscriber(None, Transcript("bonjour tout le monde", "fr")),
        (),
        "test-model",
        lambda: True,
    )
    handler.run(job_for(first))
    handler.run(job_for(second))
    stored = audio.get(second)
    assert stored is not None
    assert stored.transcript == "bonjour tout le monde"


def test_already_transcribed_segment_is_not_done_twice(audio: AudioStore) -> None:
    seg = add_segment(audio)
    fake = FakeTranscriber(Transcript("one", "en"))
    handler = TranscribeHandler(audio, fake, (), "test-model", lambda: True)
    handler.run(job_for(seg))
    handler.run(job_for(seg))  # e.g. the worker died before completing the job
    assert len(fake.calls) == 1


def test_bad_jobs_fail_for_good(audio: AudioStore) -> None:
    handler = TranscribeHandler(audio, FakeTranscriber(), (), "test-model", lambda: True)
    with pytest.raises(PermanentJobError):
        handler.run(Job(1, "transcribe", "k", {}, 0, 1))
    with pytest.raises(PermanentJobError):
        handler.run(job_for(424242))


def test_missing_audio_marks_the_segment_failed(audio: AudioStore, paths: Paths) -> None:
    seg = add_segment(audio)
    for f in paths.media_dir.rglob("*.enc"):
        f.unlink()
    with pytest.raises(PermanentJobError):
        TranscribeHandler(audio, FakeTranscriber(), (), "test-model", lambda: True).run(
            job_for(seg)
        )
    stored = audio.get(seg)
    assert stored is not None
    assert stored.status == "failed"


def test_give_up_marks_failed_and_blocked_follows_the_model(audio: AudioStore) -> None:
    seg = add_segment(audio)
    fake = FakeTranscriber()
    handler = TranscribeHandler(audio, fake, (), "test-model", lambda: True)
    handler.give_up(job_for(seg))
    handler.give_up(Job(2, "transcribe", "k", {}, 0, 1))  # nothing to mark: ignored
    stored = audio.get(seg)
    assert stored is not None
    assert stored.status == "failed"
    assert handler.blocked() is None
    fake.model_present = False
    assert handler.blocked() == "model missing"
    handler.idle()
    assert fake.unloads == 1


def test_blocked_reports_a_missing_model_first_then_the_load_error(audio: AudioStore) -> None:
    fake = FakeTranscriber()
    handler = TranscribeHandler(audio, fake, (), "test-model", lambda: True)
    fake.load_reason = "model failed to load (RuntimeError)"
    assert handler.blocked() == "model failed to load (RuntimeError)"
    fake.model_present = False
    assert handler.blocked() == "model missing"


def test_a_decode_failure_after_a_load_failure_defers_and_leaves_the_segment_pending(
    audio: AudioStore,
) -> None:
    seg = add_segment(audio)
    fake = FakeTranscriber(RuntimeError("CUDA driver version is insufficient"))
    fake.load_reason = "model failed to load (RuntimeError)"
    handler = TranscribeHandler(audio, fake, (), "test-model", lambda: True)
    with pytest.raises(DeferJob, match=r"model failed to load \(RuntimeError\)"):
        handler.run(job_for(seg))
    stored = audio.get(seg)
    assert stored is not None
    assert stored.status == "pending"


def test_any_other_decode_failure_is_still_a_counted_failure(audio: AudioStore) -> None:
    seg = add_segment(audio)
    fake = FakeTranscriber(ValueError("bad frame"))
    handler = TranscribeHandler(audio, fake, (), "test-model", lambda: True)
    with pytest.raises(ValueError, match="bad frame"):
        handler.run(job_for(seg))


@pytest.mark.parametrize(
    ("stream", "ready", "queued"),
    [("mic", False, 1), ("system", True, 1), ("system", False, 0)],
)
def test_speakers_jobs_follow_the_stream_and_the_voice_models(
    audio: AudioStore, conn: Connection, stream: str, *, ready: bool, queued: int
) -> None:
    seg = add_segment(audio, stream=stream)
    fake = FakeTranscriber(Transcript("bonjour", "fr"))
    TranscribeHandler(audio, fake, (), "test-model", lambda: ready).run(job_for(seg))
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'speakers'").fetchone()[0] == queued


def test_watch_records_and_reraises_a_failure_and_clears_on_success() -> None:
    failure = LoadFailure(lambda: 100.0)
    with pytest.raises(ImportError), failure.watch():
        raise ImportError("secret text")
    assert failure.reason() == "model failed to load (ImportError)"
    with failure.watch():
        pass
    assert failure.reason() is None


def test_a_load_failure_is_reported_for_the_defer_window_then_forgotten() -> None:
    now = [100.0]
    failure = LoadFailure(lambda: now[0])
    assert failure.reason() is None
    failure.record(ImportError("secret text"))
    assert failure.reason() == "model failed to load (ImportError)"
    now[0] += DEFER_MS / 1000 - 1
    assert failure.reason() == "model failed to load (ImportError)"
    now[0] += 1
    assert failure.reason() is None
    failure.record(RuntimeError())
    failure.clear()
    assert failure.reason() is None
