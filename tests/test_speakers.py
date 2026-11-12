"""Speaker identity (gates, profiles, merge, split), the numpy audio parts and the job handler."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
import pytest

from akashic.pipeline.jobs import Job
from akashic.pipeline.speakers import LocalTurn as Local
from akashic.pipeline.speakers import (
    OnnxVoiceModels,
    SpeakersHandler,
    decode_window,
    fbank,
    model_paths,
    window_turns,
)
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.media import MediaStore
from akashic.storage.opus import encode_opus
from akashic.storage.speakers import NewTurn, SpeakerError, SpeakerStore, best_match

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

    from akashic.config import Paths

type Vec = NDArray[np.float32]


def voice(*direction: float) -> Vec:
    """A unit vector in the first dimensions: different directions are different voices."""
    padded = np.zeros(256, dtype=np.float32)
    padded[: len(direction)] = direction
    return padded / np.linalg.norm(padded)


ALICE, BOB = voice(1, 0), voice(0, 1)


@pytest.fixture
def audio(conn: Connection, paths: Paths) -> AudioStore:
    return AudioStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))


@pytest.fixture
def store(conn: Connection) -> SpeakerStore:
    return SpeakerStore(conn, threshold=0.5)


def segment(audio: AudioStore, start: int = 1_000_000, stream: str = "system") -> int:
    tone = (0.3 * np.sin(np.arange(16_000) / 10)).astype(np.float32)
    new = NewSegment(stream, start, start + 3_000, 2_500)  # type: ignore[arg-type]
    return audio.add(new, encode_opus(tone), now_ms=0)


def turn(seconds: float, embedding: Vec | None, start: int = 0) -> NewTurn:
    ms = int(seconds * 1000)
    return NewTurn(start, start + ms, ms, embedding)


def speaker_ids(conn: Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM speakers ORDER BY id")]


# --- matching --------------------------------------------------------------------------------


def test_best_match_needs_the_threshold() -> None:
    profiles = [(1, ALICE), (2, BOB)]
    assert best_match(ALICE, profiles, 0.5) == (1, pytest.approx(1.0))  # type: ignore[comparison-overlap]
    assert best_match(voice(1, 1), [(1, ALICE)], 0.9) is None  # cosine 0.71
    assert best_match(voice(1, 0.1), profiles, 0.5)[0] == 1  # type: ignore[index]
    assert best_match(ALICE, [], 0.5) is None


# --- the gates -------------------------------------------------------------------------------


def test_under_one_second_nobody_is_identified_or_created(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    seg = segment(audio)
    store.record_turns(seg, [turn(0.9, ALICE)], now_ms=1)
    assert speaker_ids(conn) == []
    ((speaker,),) = conn.execute("SELECT speaker_id FROM speaker_turns").fetchall()
    assert speaker is None


def test_a_long_turn_creates_a_speaker_and_later_ones_join_it(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    first, second = segment(audio, 1_000_000), segment(audio, 2_000_000)
    store.record_turns(first, [turn(3, ALICE)], now_ms=1)
    store.record_turns(second, [turn(3, voice(1, 0.2))], now_ms=2)
    (only,) = speaker_ids(conn)
    row = conn.execute("SELECT vec_n FROM speakers WHERE id = ?", (only,)).fetchone()
    assert row[0] == 2
    seg_speaker = conn.execute(
        "SELECT speaker_id FROM audio_segments WHERE id = ?", (second,)
    ).fetchone()
    assert seg_speaker[0] == only


def test_a_medium_turn_matches_but_never_creates_or_updates(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    store.record_turns(segment(audio, 1_000_000), [turn(3, ALICE)], now_ms=1)
    known = segment(audio, 2_000_000)
    stranger = segment(audio, 3_000_000)
    store.record_turns(known, [turn(1.5, ALICE)], now_ms=2)
    store.record_turns(stranger, [turn(1.5, BOB)], now_ms=3)
    (alice,) = speaker_ids(conn)  # Bob was not created
    assert conn.execute("SELECT vec_n FROM speakers").fetchone()[0] == 1  # nor was Alice updated
    rows = dict(conn.execute("SELECT segment_id, speaker_id FROM speaker_turns").fetchall())
    assert rows[known] == alice
    assert rows[stranger] is None


def test_a_different_voice_makes_a_new_speaker(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    store.record_turns(segment(audio, 1_000_000), [turn(3, ALICE)], now_ms=1)
    store.record_turns(segment(audio, 2_000_000), [turn(3, BOB)], now_ms=2)
    assert len(speaker_ids(conn)) == 2


def test_a_mixed_segment_has_no_single_author(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    seg = segment(audio)
    store.record_turns(seg, [turn(2.5, ALICE), turn(4, BOB, start=3_000)], now_ms=1)
    assert conn.execute("SELECT speaker_id FROM audio_segments").fetchone()[0] is None


def test_unknown_speech_prevents_assigning_the_whole_transcript(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    seg = segment(audio)
    store.record_turns(seg, [turn(2.5, ALICE), turn(0.5, None, start=2500)], now_ms=1)
    assert conn.execute("SELECT speaker_id FROM audio_segments").fetchone()[0] is None


def test_merging_and_splitting_refreshes_transcript_authorship(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    seg = segment(audio)
    store.record_turns(seg, [turn(2, ALICE), turn(2, BOB, start=2000)], now_ms=1)
    first, second = speaker_ids(conn)
    store.merge(second, first)
    assert conn.execute("SELECT speaker_id FROM audio_segments").fetchone()[0] == first
    turn_id = conn.execute("SELECT id FROM speaker_turns ORDER BY id DESC LIMIT 1").fetchone()[0]
    store.reassign_turn(turn_id, None, now_ms=2)
    assert conn.execute("SELECT speaker_id FROM audio_segments").fetchone()[0] is None


def test_microphone_segments_are_the_user(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    a, b = segment(audio, 1_000_000, "mic"), segment(audio, 2_000_000, "mic")
    store.record_self(a, 2_500, 3_000, now_ms=1)
    store.record_self(b, 2_500, 3_000, now_ms=2)
    assert len(speaker_ids(conn)) == 1
    (me,) = store.all_speakers()
    assert (me.is_self, me.name, me.turns, me.speech_ms) == (True, "You", 2, 5_000)
    assert store.has_turns(a)
    assert not store.has_turns(a + 100)


# --- what the user does ----------------------------------------------------------------------


def test_naming_and_listing(audio: AudioStore, store: SpeakerStore) -> None:
    store.record_turns(segment(audio, 1_000_000), [turn(3, ALICE)], now_ms=1)
    store.record_turns(segment(audio, 2_000_000), [turn(5, BOB)], now_ms=2)
    first = store.all_speakers()
    assert [s.speech_ms for s in first] == [5_000, 3_000]  # the most talkative first
    assert first[0].sample is not None
    assert first[0].last_ms == 2_000_000
    store.rename(first[0].id, "  Minlo ")
    store.rename(first[1].id, "")
    named = store.all_speakers()
    assert [s.name for s in named] == ["Minlo", None]
    with pytest.raises(SpeakerError, match="no such speaker"):
        store.rename(999, "x")


def test_future_matches_inherit_the_name(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    store.record_turns(segment(audio, 1_000_000), [turn(3, ALICE)], now_ms=1)
    (alice,) = store.all_speakers()
    store.rename(alice.id, "Minlo")
    later = segment(audio, 9_000_000)
    store.record_turns(later, [turn(2.2, voice(1, 0.05))], now_ms=3)
    assert len(speaker_ids(conn)) == 1
    found = conn.execute("SELECT speaker_id FROM audio_segments WHERE id = ?", (later,)).fetchone()
    assert found[0] == alice.id


def test_merging_joins_profiles_turns_and_segments(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    a, b = segment(audio, 1_000_000), segment(audio, 2_000_000)
    store.record_turns(a, [turn(3, ALICE)], now_ms=1)
    store.record_turns(b, [turn(3, BOB)], now_ms=2)
    first, second = speaker_ids(conn)
    store.merge(second, first)
    assert speaker_ids(conn) == [first]
    assert conn.execute("SELECT vec_n FROM speakers").fetchone()[0] == 2
    assert {r[0] for r in conn.execute("SELECT speaker_id FROM audio_segments")} == {first}
    # the merged profile now answers to both voices
    third = segment(audio, 3_000_000)
    store.record_turns(third, [turn(3, BOB)], now_ms=3)
    assert speaker_ids(conn) == [first]


def test_splitting_a_wrong_match_off_into_a_new_speaker(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    a, b = segment(audio, 1_000_000), segment(audio, 2_000_000)
    store.record_turns(a, [turn(3, ALICE)], now_ms=1)
    store.record_turns(b, [turn(3, voice(1, 0.3))], now_ms=2)  # wrongly the same voice
    (alice,) = speaker_ids(conn)
    wrong = conn.execute("SELECT id FROM speaker_turns WHERE segment_id = ?", (b,)).fetchone()[0]
    new = store.reassign_turn(wrong, None, now_ms=5)
    assert new != alice
    assert conn.execute("SELECT vec_n FROM speakers WHERE id = ?", (alice,)).fetchone()[0] == 1
    assert conn.execute("SELECT vec_n FROM speakers WHERE id = ?", (new,)).fetchone()[0] == 1
    seg = conn.execute("SELECT speaker_id FROM audio_segments WHERE id = ?", (b,)).fetchone()
    assert seg[0] == new
    # the old profile is exactly what it was before the wrong turn joined it
    vec = conn.execute("SELECT vec_sum FROM speakers WHERE id = ?", (alice,)).fetchone()[0]
    assert np.allclose(np.frombuffer(vec, dtype=np.float32), ALICE)


# --- numpy parts -----------------------------------------------------------------------------


def test_fbank_shape_and_normalisation() -> None:
    rng = np.random.default_rng(1)
    one_second = rng.normal(0, 0.1, 16_000).astype(np.float32)
    features = fbank(one_second)
    assert features.shape == (98, 80)  # 1 + (16000 - 400) // 160 frames
    assert np.allclose(features.mean(axis=0), 0, atol=1e-4)
    assert fbank(np.zeros(100, dtype=np.float32)).shape == (0, 80)
    assert np.isfinite(fbank(np.zeros(16_000, dtype=np.float32))).all()  # silence: no -inf


def test_powerset_decoding() -> None:
    logits = np.full((5, 7), -5.0, dtype=np.float32)
    for frame, cls in enumerate([0, 1, 3, 4, 6]):
        logits[frame, cls] = 5.0
    assert decode_window(logits).tolist() == [
        [False, False, False],
        [True, False, False],
        [False, False, True],
        [True, True, False],
        [False, True, True],
    ]


FRAME_SAMPLES = 1_600  # 100 ms: a window of 100 frames is 10 s


def test_window_turns_use_only_solo_speech_and_skip_padding() -> None:
    window = np.arange(100 * FRAME_SAMPLES, dtype=np.float32)
    active = np.zeros((100, 3), dtype=np.bool_)
    active[10:40, 0] = True  # speaker 0 alone: 30 frames
    active[40:50, 0] = True
    active[40:50, 1] = True  # overlap: counts for neither
    active[60:62, 2] = True  # 2 frames: below the minimum
    active[70:90, 1] = True  # speaker 1: 20 frames, but frames past 80 are padding
    turns = window_turns(window, active, valid_samples=80 * FRAME_SAMPLES, offset_ms=10_000)
    assert len(turns) == 2  # the 2-frame speaker is dropped
    first, second = turns
    assert (first.start_ms, first.end_ms, first.speech_ms) == (11_000, 14_000, 3_000)
    assert len(first.samples) == 30 * FRAME_SAMPLES
    assert (second.start_ms, second.speech_ms) == (17_000, 1_000)  # frames 70 to 79 only
    assert window_turns(window, np.zeros((0, 3), dtype=np.bool_), 0, 0) == []


# --- the handler -----------------------------------------------------------------------------


class FakeVoices:
    def __init__(self, turns: list[Local]) -> None:
        self._turns = turns
        self.present = True
        self.embedded: list[int] = []
        self.unloads = 0

    def available(self) -> bool:
        return self.present

    def turns(self, samples: NDArray[np.float32]) -> list[Local]:  # noqa: ARG002
        return self._turns

    def embed(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        self.embedded.append(len(samples))
        return ALICE

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force
        self.unloads += 1


def speakers_job(segment_id: int) -> Job:
    return Job(1, "speakers", f"speakers:{segment_id}", {"segment_id": segment_id}, 0, 1)


def local(seconds: float, start: int = 0) -> Local:
    ms = int(seconds * 1000)
    return Local(start, start + ms, ms, np.zeros(int(seconds * 16_000), dtype=np.float32))


def test_handler_identifies_system_audio_and_only_embeds_long_turns(
    conn: Connection, audio: AudioStore, store: SpeakerStore
) -> None:
    seg = segment(audio)
    voices = FakeVoices([local(3), local(0.6, start=4_000)])
    handler = SpeakersHandler(audio, store, voices, now_ms=lambda: 7)
    handler.run(speakers_job(seg))
    assert voices.embedded == [48_000]  # the 0.6 s turn was not embedded
    assert len(speaker_ids(conn)) == 1
    handler.run(speakers_job(seg))  # a second run does nothing
    assert conn.execute("SELECT count(*) FROM speaker_turns").fetchone()[0] == 2
    handler.idle()
    assert voices.unloads == 1


def test_handler_gives_the_microphone_to_the_user_without_the_models(
    audio: AudioStore, store: SpeakerStore
) -> None:
    seg = segment(audio, stream="mic")
    voices = FakeVoices([])
    SpeakersHandler(audio, store, voices, now_ms=lambda: 7).run(speakers_job(seg))
    assert voices.embedded == []
    (me,) = store.all_speakers()
    assert me.is_self


# --- queueing and the real adapter -----------------------------------------------------------


def test_a_finished_transcript_queues_the_speakers_job_once(
    conn: Connection, audio: AudioStore
) -> None:
    seg = segment(audio)
    audio.set_transcript(seg, "empty", None, None)
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'speakers'").fetchone()[0] == 0
    audio.set_transcript(seg, "done", "en", "hello there")
    audio.set_transcript(seg, "done", "en", "hello there again")
    ((key, payload, data_ms),) = conn.execute(
        "SELECT key, payload, data_ms FROM jobs WHERE kind = 'speakers'"
    ).fetchall()
    assert key == f"speakers:{seg}"
    assert json.loads(payload) == {"segment_id": seg}
    assert data_ms == 1_000_000  # the capture time, so old audio is processed first


def test_onnx_adapter_reports_missing_models_and_unloads_nothing(paths: Paths) -> None:
    seg, emb = model_paths(paths.models_dir)
    models = OnnxVoiceModels(seg, emb, idle_unload_s=1.0)
    assert not models.available()
    models.unload_if_idle()  # nothing loaded: nothing to do
    assert seg.parts[-3:] == ("pyannote-segmentation-3.0", "onnx", "model.onnx")
