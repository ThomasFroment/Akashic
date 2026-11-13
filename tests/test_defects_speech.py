"""Known defects of speech and capture (audit Phase 3): segmenting, transcribing, speakers,
screen and audio capture.

Each ``known_defect`` test asserts the behaviour the audit asks for and fails today for the
reason the finding gives; plain tests are the baselines and bounds those findings rely on.
Whisper, the speech models, ONNX Runtime and the OS are faked; nothing loads a model.
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import sys
import threading
import time
import types
from collections import Counter
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from audio_fakes import WINDOW_MS, Clock, FakeDetector, FakeDevices, FakeSessions, silence, speech
from capture_fakes import (
    SCREEN,
    FakeMedia,
    FakeOcr,
    FakeScreen,
    FakeUia,
    FakeWindows,
    blank,
    with_text_block,
)
from capture_fakes import window as plain_window
from defects import known_defect
from waits import wait_for

from akashic.capture import audio as audio_module
from akashic.capture.audio import (
    AudioSettings,
    Segmenter,
    SegmentWriter,
    SourceSampler,
    StreamRecorder,
    Utterance,
)
from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.gaming import GameSettings, is_game_window
from akashic.capture.media import MediaTracker
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.ports import AudioDevice, MediaState, Rect, WindowInfo
from akashic.capture.screen import ScreenCapture
from akashic.capture.vad import WINDOW
from akashic.config import Exclusions as UserExclusions
from akashic.pipeline import whisper
from akashic.pipeline.jobs import Job
from akashic.pipeline.models import CANARY_V2, QWEN3_ASR
from akashic.pipeline.ocr_frames import OcrFrameHandler
from akashic.pipeline.sessions import derive_app_sessions
from akashic.pipeline.speakers import LocalTurn, OnnxVoiceModels, SpeakersHandler
from akashic.pipeline.speech import TransformerTranscriber
from akashic.pipeline.transcribe import (
    UNCLEAR_BELOW,
    TranscribeHandler,
    Transcript,
    trim_overlap,
)
from akashic.pipeline.whisper import WhisperTranscriber
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.opus import encode_opus
from akashic.storage.sessions import GameLog, MediaLog
from akashic.storage.speakers import NewTurn, SpeakerStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from sqlite3 import Connection

    from numpy.typing import NDArray
    from PIL import Image

    from akashic.capture.ports import AudioKind
    from akashic.config import Paths

    type Vec = NDArray[np.float32]

T0 = 1_794_000_000_000


def tone(seconds: float) -> NDArray[np.float32]:
    n = int(seconds * 16_000)
    return (0.3 * np.sin(np.arange(n) / 10)).astype(np.float32)


@pytest.fixture
def audio(conn: Connection, paths: Paths) -> AudioStore:
    return AudioStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))


def add_segment(  # noqa: PLR0913 - one audio_segments row and its audio
    audio: AudioStore,
    start_ms: int,
    seconds: float = 3.0,
    *,
    stream: AudioKind = "system",
    sources: tuple[str, ...] = ("discord.exe",),
    continues_id: int | None = None,
) -> int:
    length = int(seconds * 1000)
    new = NewSegment(stream, start_ms, start_ms + length, length, sources, continues_id)
    return audio.add(new, encode_opus(tone(seconds)), now_ms=0)


def job(kind: str, segment_id: int) -> Job:
    return Job(1, kind, f"{kind}:{segment_id}", {"segment_id": segment_id}, 0, 1)


# --- #131 and the 30 s cut: the Segmenter ----------------------------------------------------


class SegmenterRig:
    def __init__(self, settings: AudioSettings | None = None) -> None:
        self.stats: Counter[str] = Counter()
        self.seg = Segmenter(settings or AudioSettings(), self.stats)
        self.ts = 0
        self.out: list[Utterance] = []

    def feed(self, samples: NDArray[np.float32]) -> None:
        for i in range(0, len(samples), WINDOW):
            window = samples[i : i + WINDOW]
            probability = 0.9 if float(window.max()) > 0.05 else 0.0
            self.out.extend(self.seg.feed(self.ts, window, probability))
            self.ts += WINDOW_MS


CUT_WINDOWS = -(-AudioSettings().max_segment_ms // WINDOW_MS)  # 938 windows reach 30 s
OVERLAP_MS = AudioSettings().overlap_ms // WINDOW_MS * WINDOW_MS  # whole windows: 1,984 ms


@known_defect(131, "a continuation is gated on its fresh speech only and drops as 'short'")
def test_a_word_spoken_just_after_the_30_s_cut_is_kept() -> None:
    rig = SegmenterRig()
    rig.feed(speech(CUT_WINDOWS))  # "... je l'aime" up to the cut
    rig.feed(speech(10))  # "pas": 320 ms after the cut
    rig.feed(silence(60))
    assert rig.stats["dropped_short"] == 0
    first, second = rig.out
    assert second.continues
    assert second.end_ms > first.end_ms


def test_a_continuation_with_enough_new_speech_is_kept() -> None:
    """Baseline for #131: 640 ms after the cut clears the 400 ms gate."""
    rig = SegmenterRig()
    rig.feed(speech(CUT_WINDOWS))
    rig.feed(speech(20))
    rig.feed(silence(60))
    first, second = rig.out
    assert second.continues
    assert second.start_ms == first.end_ms - OVERLAP_MS
    assert first.end_ms - first.start_ms <= AudioSettings().max_segment_ms + WINDOW_MS


def test_three_minutes_of_unbroken_speech_become_overlapping_continuations() -> None:
    rig = SegmenterRig()
    rig.feed(speech(180_000 // WINDOW_MS))
    rig.feed(silence(60))
    settings = AudioSettings()
    assert len(rig.out) == 7
    assert not rig.out[0].continues
    assert all(u.continues for u in rig.out[1:])
    for before, after in zip(rig.out, rig.out[1:], strict=False):
        assert after.start_ms == before.end_ms - OVERLAP_MS
        assert after.end_ms - after.start_ms <= settings.max_segment_ms + WINDOW_MS
    covered = rig.out[-1].end_ms - rig.out[0].start_ms
    assert covered >= 180_000


# --- #137: the overlap after a cut ------------------------------------------------------------


@known_defect(137, "trim_overlap needs the exact last words; the half word at the cut differs")
@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (
            "je pense qu'on devrait aller au ma",
            "aller au marché demain matin",
            "marché demain matin",
        ),
        ("we should go to the sta", "go to the station tomorrow", "station tomorrow"),
        ("on se retrouve sur le serv", "sur le server ce soir, ok?", "server ce soir, ok?"),
    ],
)
def test_the_repeated_words_go_even_when_the_cut_word_differs(
    previous: str, current: str, expected: str
) -> None:
    assert trim_overlap(previous, current) == expected


# --- the transcribe job: #128, #130, #138, #140 ----------------------------------------------


class RecordingTranscriber:
    """Returns a scripted transcript (or the prompt it was given) and records the prompts."""

    def __init__(self, text: str | None = None, confidence: float | None = 0.9) -> None:
        self.text = text
        self.confidence = confidence
        self.prompts: list[str | None] = []

    def available(self) -> bool:
        return True

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript | None:
        del samples
        self.prompts.append(prompt)
        text = self.text if self.text is not None else (prompt or "")
        return Transcript(text, "fr", self.confidence) if text else None

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


def stored(conn: Connection, segment_id: int) -> tuple[str, str | None, float | None]:
    return conn.execute(
        "SELECT status, transcript, confidence FROM audio_segments WHERE id = ?", (segment_id,)
    ).fetchone()


@known_defect(130, "previous_transcript needs end_ms <= start_ms, so the continued one is skipped")
def test_a_continuation_is_prompted_with_the_segment_it_continues(audio: AudioStore) -> None:
    first = add_segment(audio, T0, 30.0)
    audio.set_transcript(first, "done", "fr", "on parlait du nouveau jeu de Minlo", 0.9)
    later = add_segment(audio, T0 + 28_000, 10.0, continues_id=first)  # 2 s of overlap
    transcriber = RecordingTranscriber("de Minlo, il sort demain")
    TranscribeHandler(audio, transcriber, (), "test-model").run(job("transcribe", later))
    (prompt,) = transcriber.prompts
    assert prompt is not None
    assert "nouveau jeu de Minlo" in prompt


def test_a_segment_after_a_pause_is_prompted_with_what_was_just_said(
    conn: Connection, audio: AudioStore
) -> None:
    """Baseline for #130: without overlap the previous line does reach the prompt."""
    first = add_segment(audio, T0, 3.0)
    audio.set_transcript(first, "done", "fr", "on parlait du nouveau jeu", 0.9)
    later = add_segment(audio, T0 + 5_000, 3.0)
    transcriber = RecordingTranscriber("il sort demain")
    TranscribeHandler(audio, transcriber, (), "test-model").run(job("transcribe", later))
    assert transcriber.prompts == ["on parlait du nouveau jeu"]
    assert stored(conn, later)[:2] == ("done", "il sort demain")


@known_defect(128, "a transcript that only repeats the prompt is stored as a confident line")
def test_a_prompt_echo_on_noise_is_not_stored_as_a_confident_line(
    conn: Connection, audio: AudioStore
) -> None:
    segment = add_segment(audio, T0)  # game noise: Whisper answers with its prompt
    vocabulary = ("Minlo", "Elden Ring", "Valorant", "Akashic")
    TranscribeHandler(audio, RecordingTranscriber(None), vocabulary, "test-model").run(
        job("transcribe", segment)
    )
    status, text, confidence = stored(conn, segment)
    assert text == "Minlo, Elden Ring, Valorant, Akashic" or status != "done"
    assert status != "done" or (confidence is not None and confidence < UNCLEAR_BELOW)


@pytest.mark.parametrize(
    "heard",
    [
        "See you tomorrow for the raid. Thanks for watching!",
        "Merci d'avoir regardé cette vidéo. Bon, on lance la partie ?",
    ],
)
def test_a_spoken_outro_is_kept_verbatim(conn: Connection, audio: AudioStore, heard: str) -> None:
    """A real sentence is never deleted by phrase matching, whatever it resembles."""
    segment = add_segment(audio, T0)
    TranscribeHandler(audio, RecordingTranscriber(heard), (), "test-model").run(
        job("transcribe", segment)
    )
    assert stored(conn, segment)[:2] == ("done", heard)


@known_defect(138, "credit hallucinations are rejected only when they are the whole transcript")
def test_an_edge_credit_line_is_removed_or_not_stored_as_clear(
    conn: Connection, audio: AudioStore
) -> None:
    credit = "Sous-titres réalisés par la communauté d'Amara.org"
    segment = add_segment(audio, T0)
    heard = f"On se retrouve demain pour la partie. {credit}"
    TranscribeHandler(audio, RecordingTranscriber(heard), (), "test-model").run(
        job("transcribe", segment)
    )
    _, text, confidence = stored(conn, segment)
    assert text is not None
    assert "On se retrouve demain pour la partie." in text
    assert credit not in text or (confidence is not None and confidence < UNCLEAR_BELOW)


@pytest.mark.parametrize("heard", ["   ", "Sous-titres réalisés par la communauté d'Amara.org"])
def test_an_empty_or_credit_only_transcript_is_empty(
    conn: Connection, audio: AudioStore, heard: str
) -> None:
    """Baseline for #138: the whole-transcript rule works."""
    segment = add_segment(audio, T0)
    TranscribeHandler(audio, RecordingTranscriber(heard), (), "test-model").run(
        job("transcribe", segment)
    )
    assert stored(conn, segment)[:2] == ("empty", None)


@known_defect(140, "named voices never reach the Whisper prompt; only config vocabulary does")
def test_the_names_given_to_voices_are_in_the_prompt(conn: Connection, audio: AudioStore) -> None:
    conn.execute("INSERT INTO speakers (name, is_self, created_ms) VALUES ('Minlo', 0, 0)")
    segment = add_segment(audio, T0)
    transcriber = RecordingTranscriber("salut Minlo")
    TranscribeHandler(audio, transcriber, (), "test-model").run(job("transcribe", segment))
    (prompt,) = transcriber.prompts
    assert prompt is not None
    assert "Minlo" in prompt


# --- the faster-whisper adapter: #129, #139 --------------------------------------------------


class Piece:
    def __init__(self, text: str, avg_logprob: float, no_speech_prob: float) -> None:
        self.text, self.start, self.end = text, 0.0, 1.0
        self.avg_logprob, self.no_speech_prob = avg_logprob, no_speech_prob


class RuleFollowingModel:
    """Applies faster-whisper's own no-speech rule to the thresholds it is given: a piece is
    skipped when no_speech_prob > no_speech_threshold and avg_logprob <= log_prob_threshold."""

    pieces: dict[str, list[Piece]]  # by decoding language
    languages: list[tuple[str, float]]

    def __init__(self, path: str, **kwargs: Any) -> None:  # noqa: ANN401
        del path, kwargs

    def detect_language(self, samples: NDArray[np.float32]) -> tuple[str, float, Any]:
        del samples
        return self.languages[0][0], self.languages[0][1], self.languages

    def transcribe(self, samples: NDArray[np.float32], **kw: Any) -> tuple[list[Piece], None]:  # noqa: ANN401
        del samples
        kept = [
            p
            for p in self.pieces[kw["language"]]
            if not (
                p.no_speech_prob > kw["no_speech_threshold"]
                and p.avg_logprob <= kw["log_prob_threshold"]
            )
        ]
        return kept, None


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> type[RuleFollowingModel]:
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = RuleFollowingModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setattr(whisper, "add_cuda_dll_directories", lambda: None)
    RuleFollowingModel.languages = [("fr", 0.9), ("en", 0.1)]
    return RuleFollowingModel


@known_defect(
    129,
    "the ghost's no_speech_prob is dropped after decoding, so no reject/unclear outcome can be"
    " judged or persisted",
)
def test_a_ghost_on_silence_keeps_its_no_speech_statistic(
    fake_whisper: type[RuleFollowingModel], tmp_path: Path
) -> None:
    fake_whisper.pieces = {"fr": [Piece(" Merci.", avg_logprob=-1.3, no_speech_prob=0.93)]}
    result = WhisperTranscriber(tmp_path, 60.0).transcribe(tone(2.0), None)
    assert result is not None
    assert result.text == "Merci."  # the decode thresholds are unchanged: the ghost is decoded
    assert getattr(result, "no_speech_prob", None) == pytest.approx(0.93)


def test_real_speech_with_a_low_no_speech_probability_is_kept(
    fake_whisper: type[RuleFollowingModel], tmp_path: Path
) -> None:
    """Baseline for #129: the rule must not drop confident speech."""
    fake_whisper.pieces = {"fr": [Piece(" Ouais, on y va.", avg_logprob=-0.3, no_speech_prob=0.05)]}
    result = WhisperTranscriber(tmp_path, 60.0).transcribe(tone(2.0), None)
    assert result is not None
    assert result.text == "Ouais, on y va."


@known_defect(139, "a short French reply is forced to English by audio-only detection")
def test_a_short_french_reply_is_decoded_as_french(
    fake_whisper: type[RuleFollowingModel], tmp_path: Path
) -> None:
    fake_whisper.languages = [("en", 0.52), ("fr", 0.46)]
    fake_whisper.pieces = {
        "en": [Piece(" Why, Carmen.", avg_logprob=-1.4, no_speech_prob=0.1)],
        "fr": [Piece(" Ouais, carrément.", avg_logprob=-0.2, no_speech_prob=0.1)],
    }
    result = WhisperTranscriber(tmp_path, 60.0).transcribe(tone(1.0), None)
    assert result is not None
    assert (result.text, result.language) == ("Ouais, carrément.", "fr")


# --- the Transformers speech models: #43, #141 -----------------------------------------------


class _Inputs(dict[str, Any]):
    def to(self, *args: object) -> _Inputs:
        del args
        return self


class _Scores:
    def __init__(self, value: float) -> None:
        self.value = value

    def float(self) -> _Scores:
        return self

    def mean(self) -> _Scores:
        return self

    def item(self) -> float:
        return self.value


class FakeProcessor:
    """Qwen3-ASR and Canary processors: records requests, decodes to scripted text."""

    def __init__(self, language: str = "German") -> None:
        self.language = language
        self.requests: list[dict[str, object]] = []

    def apply_transcription_request(self, **kwargs: object) -> _Inputs:
        self.requests.append(kwargs)
        return _Inputs(input_ids=np.zeros((1, 3)), language=kwargs.get("source_language"))

    def decode(self, output: object, **kwargs: object) -> list[Any]:
        del output
        if kwargs.get("return_format") == "parsed":  # Qwen
            asked = [
                str(v)
                for r in self.requests
                for v in r.values()
                if str(v).casefold() in {"french", "english", "fr", "en"}
            ]
            language = asked[-1] if asked else self.language
            return [{"language": language, "transcription": "ouais carrément"}]
        return ["ouais carrément"]  # Canary


class FakeModel:
    device = "cuda"
    dtype = "bfloat16"

    def generate(self, **kwargs: Any) -> Any:  # noqa: ANN401
        """Canary asks for scores (French scores better here); Qwen gets plain token ids."""
        if not kwargs.get("return_dict_in_generate"):
            return np.zeros((1, 6))
        score = -0.1 if kwargs.get("language") == "fr" else -1.5
        return types.SimpleNamespace(sequences=np.zeros((1, 3)), scores=score)

    def compute_transition_scores(self, sequences: object, scores: float, **kw: object) -> _Scores:
        del sequences, kw
        return _Scores(scores)


def transformer(tmp_path: Path, key: str, processor: FakeProcessor) -> TransformerTranscriber:
    transcriber = TransformerTranscriber(tmp_path / key, 60.0)
    transcriber._torch = types.SimpleNamespace(
        inference_mode=contextlib.nullcontext,
        cuda=types.SimpleNamespace(empty_cache=lambda: None),
    )
    transcriber._processor = processor
    transcriber._model = FakeModel()
    return transcriber


@known_defect(43, "Qwen3-ASR is not constrained to French or English and stores 'und'")
def test_qwen_only_ever_returns_french_or_english(tmp_path: Path) -> None:
    result = transformer(tmp_path, QWEN3_ASR.key, FakeProcessor("German")).transcribe(
        tone(2.0), None
    )
    assert result is not None
    assert result.language in {"fr", "en"}


@known_defect(141, "Canary computes a mean log-probability and returns confidence=None")
def test_canary_reports_a_confidence(tmp_path: Path) -> None:
    result = transformer(tmp_path, CANARY_V2.key, FakeProcessor()).transcribe(tone(2.0), None)
    assert result is not None
    assert result.language == "fr"  # the better-scoring decode, as today
    assert result.confidence is not None
    assert result.confidence == pytest.approx(math.exp(-0.1), rel=0.05)


# --- speakers: #132, #133, #134, #135 ----------------------------------------------------------


def voice(*direction: float) -> Vec:
    padded = np.zeros(256, dtype=np.float32)
    padded[: len(direction)] = direction
    return (padded / np.linalg.norm(padded)).astype(np.float32)


ALICE, BOB = voice(1, 0), voice(0, 1)


def segment_speaker(conn: Connection, segment_id: int) -> int | None:
    return conn.execute(
        "SELECT speaker_id FROM audio_segments WHERE id = ?", (segment_id,)
    ).fetchone()[0]


class _OnnxOptions:
    log_severity_level = 0


class _OnnxSession:
    """segmentation-3.0 hearing one voice alone in every frame; WeSpeaker always says ALICE."""

    def __init__(self, path: str, options: object, providers: list[str]) -> None:
        del path, options, providers

    def run(self, outputs: object, feeds: dict[str, NDArray[np.float32]]) -> list[Any]:
        del outputs
        if "feats" in feeds:
            return [np.asarray([ALICE])]
        logits = np.zeros((1, 589, 7), dtype=np.float32)
        logits[0, :, 1] = 1.0  # class 1: local speaker 0 alone
        return [logits]


def _diarize(
    conn: Connection,
    audio: AudioStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seconds: float,
) -> int:
    module = types.ModuleType("onnxruntime")
    module.SessionOptions = _OnnxOptions  # type: ignore[attr-defined]
    module.InferenceSession = _OnnxSession  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "onnxruntime", module)
    models = OnnxVoiceModels(tmp_path / "seg.onnx", tmp_path / "emb.onnx", idle_unload_s=60)
    segment = add_segment(audio, T0, seconds)
    SpeakersHandler(audio, SpeakerStore(conn), models, now_ms=lambda: 1).run(
        job("speakers", segment)
    )
    return segment


def test_a_single_voice_segment_of_whole_windows_is_attributed(
    conn: Connection, audio: AudioStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Baseline for #132: two full 10 s windows of one voice give the segment its author."""
    segment = _diarize(conn, audio, monkeypatch, tmp_path, 20.0)
    assert segment_speaker(conn, segment) is not None


@known_defect(132, "the last 10 s window of a one-voice segment yields an unidentified short turn")
def test_a_single_voice_segment_keeps_its_author_whatever_its_length(
    conn: Connection, audio: AudioStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    segment = _diarize(conn, audio, monkeypatch, tmp_path, 20.6)  # and a 0.6 s last window
    assert segment_speaker(conn, segment) is not None


class FakeVoices:
    """Scripted turns; the embedding is read from the turn's samples (1.0 ALICE, 2.0 BOB)."""

    def __init__(self, turns: list[LocalTurn]) -> None:
        self._turns = turns

    def available(self) -> bool:
        return True

    def turns(self, samples: NDArray[np.float32]) -> list[LocalTurn]:
        del samples
        return self._turns

    def embed(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        return ALICE if float(samples[0]) == 1.0 else BOB

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


def local(start_ms: int, seconds: float, who: float) -> LocalTurn:
    ms = int(seconds * 1000)
    return LocalTurn(start_ms, start_ms + ms, ms, np.full(ms * 16, who, dtype=np.float32))


@known_defect(133, "the previous speaker heard in the 2 s overlap makes the segment unattributed")
def test_a_continuation_spoken_by_one_voice_is_attributed_to_it(
    conn: Connection, audio: AudioStore
) -> None:
    store = SpeakerStore(conn)
    store.record_turns(add_segment(audio, T0 - 60_000), [NewTurn(0, 3000, 3000, ALICE)], 1)
    store.record_turns(add_segment(audio, T0 - 50_000), [NewTurn(0, 3000, 3000, BOB)], 1)
    alice = int(conn.execute("SELECT min(id) FROM speakers").fetchone()[0])
    first = add_segment(audio, T0, 30.0)
    later = add_segment(audio, T0 + 28_000, 14.0, continues_id=first)
    # Bob's last words are in the 2 s overlap that the transcript trims; Alice speaks the rest.
    voices = FakeVoices([local(0, 1.5, 2.0), local(2_000, 12.0, 1.0)])
    SpeakersHandler(audio, store, voices, now_ms=lambda: 2).run(job("speakers", later))
    assert segment_speaker(conn, later) == alice


def _profile(conn: Connection, vector: Vec, name: str) -> int:
    cursor = conn.execute(
        "INSERT INTO speakers (name, created_ms, vec_sum, vec_n) VALUES (?, 0, ?, 1)",
        (name, vector.tobytes()),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _profile_sizes(conn: Connection) -> list[int]:
    return [int(r[0]) for r in conn.execute("SELECT vec_n FROM speakers ORDER BY id")]


@known_defect(134, "a match at the bare 0.5 threshold updates the profile centroid")
def test_a_barely_similar_voice_does_not_drift_into_a_profile(
    conn: Connection, audio: AudioStore
) -> None:
    _profile(conn, ALICE, "Minlo")
    near = voice(0.52, math.sqrt(1 - 0.52**2))  # cosine 0.52 with Minlo
    SpeakerStore(conn, 0.5).record_turns(add_segment(audio, T0), [NewTurn(0, 3000, 3000, near)], 1)
    assert _profile_sizes(conn) == [1]


@known_defect(134, "the profile is updated even when the runner-up is just as close")
def test_an_ambiguous_match_updates_no_profile(conn: Connection, audio: AudioStore) -> None:
    _profile(conn, ALICE, "Minlo")
    _profile(conn, BOB, "Max")
    between = voice(0.70, 0.71)
    SpeakerStore(conn, 0.5).record_turns(
        add_segment(audio, T0), [NewTurn(0, 3000, 3000, between)], 1
    )
    assert _profile_sizes(conn) == [1, 1]


def test_a_clear_match_still_improves_its_profile(conn: Connection, audio: AudioStore) -> None:
    """Baseline for #134: a confident match keeps teaching the profile."""
    _profile(conn, ALICE, "Minlo")
    _profile(conn, BOB, "Max")
    SpeakerStore(conn, 0.5).record_turns(
        add_segment(audio, T0), [NewTurn(0, 3000, 3000, voice(1, 0.05))], 1
    )
    assert _profile_sizes(conn) == [2, 1]


@known_defect(135, "every unmatched system turn of 2 s or more creates a profile")
def test_a_youtube_voice_does_not_become_a_speaker(conn: Connection, audio: AudioStore) -> None:
    segment = add_segment(audio, T0, sources=("chrome.exe",))  # a video's narrator
    SpeakersHandler(audio, SpeakerStore(conn), FakeVoices([local(0, 3.0, 1.0)])).run(
        job("speakers", segment)
    )
    assert conn.execute("SELECT count(*) FROM speakers").fetchone()[0] == 0


def test_a_new_voice_on_discord_becomes_a_speaker(conn: Connection, audio: AudioStore) -> None:
    """Baseline for #135: real call voices still create profiles."""
    segment = add_segment(audio, T0, sources=("discord.exe",))
    SpeakersHandler(audio, SpeakerStore(conn), FakeVoices([local(0, 3.0, 1.0)])).run(
        job("speakers", segment)
    )
    assert conn.execute("SELECT count(*) FROM speakers").fetchone()[0] == 1


def test_many_voices_in_one_long_call_each_get_a_profile(
    conn: Connection, audio: AudioStore
) -> None:
    store = SpeakerStore(conn, 0.5)
    for i in range(16):  # orthogonal voices: none matches another
        vector = np.zeros(256, dtype=np.float32)
        vector[i] = 1.0
        store.record_turns(add_segment(audio, T0 + i * 4_000), [NewTurn(0, 3000, 3000, vector)], 1)
    assert _profile_sizes(conn) == [1] * 16


# --- audio capture: #152, #160, #161 ---------------------------------------------------------


SETTINGS = AudioSettings(preroll_ms=96, hangover_ms=320, min_speech_ms=128)


def utterance(start_ms: int, windows: int = 40) -> Utterance:
    samples = speech(windows)
    end_ms = start_ms + windows * WINDOW_MS
    return Utterance(start_ms, end_ms, samples, windows * WINDOW_MS, continues=False)


@known_defect(152, "two 1 Hz samples that only saw Spotify drop a short remark as music")
def test_a_short_remark_over_music_is_not_dropped_on_two_samples(audio: AudioStore) -> None:
    clock = Clock(T0)
    sampler = SourceSampler(FakeSessions(frozenset({"spotify.exe"})), clock)
    sampler.sample()
    clock.now = T0 + 1_000
    sampler.sample()  # Minlo's 1.3 s remark fell between the samples that saw Discord
    stats: Counter[str] = Counter()
    writer = SegmentWriter(audio, sampler, Exclusions(UserExclusions()), clock, stats)
    assert writer.write("system", utterance(T0, 40)) is not None
    assert stats["dropped_music"] == 0


class RecorderRig:
    def __init__(self) -> None:
        self.devices = FakeDevices()
        self.out: list[tuple[str, Utterance]] = []
        self.recorder = StreamRecorder(
            kind="mic",
            devices=self.devices,
            detector=FakeDetector(),
            settings=SETTINGS,
            sink=lambda k, u: self.out.append((k, u)),
            clock_ms=Clock(T0),
            stats=Counter(),
        )
        self.stop, self.paused = threading.Event(), threading.Event()
        self.thread = threading.Thread(
            target=self.recorder.run, args=(self.stop, self.paused), daemon=True
        )

    def consumed(self) -> bool:
        return bool(self.devices.opened) and not self.devices.opened[0][1].script


def test_stopping_capture_keeps_the_utterance_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "READ_TIMEOUT_S", 0.01)
    rig = RecorderRig()
    rig.devices.scripts["mic"].append([speech(30)])  # still talking when the app stops
    rig.thread.start()
    assert wait_for(rig.consumed, 5)
    rig.stop.set()
    rig.thread.join(timeout=5)
    assert len(rig.out) == 1


def test_a_device_change_keeps_the_utterance_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "READ_TIMEOUT_S", 0.01)
    monkeypatch.setattr(audio_module, "DEVICE_CHECK_S", 0.0)
    rig = RecorderRig()
    rig.devices.scripts["mic"].append([speech(30)])
    rig.thread.start()
    assert wait_for(rig.consumed, 5)
    rig.devices.devices["mic"] = AudioDevice("mic-2", "Headset")  # Windows switched devices
    assert wait_for(lambda: len(rig.devices.opened) == 2, 5)
    rig.stop.set()
    rig.thread.join(timeout=5)
    assert len(rig.out) == 1


def test_pausing_still_drops_the_utterance_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline for #160: pause means nothing is kept."""
    monkeypatch.setattr(audio_module, "READ_TIMEOUT_S", 0.01)
    rig = RecorderRig()
    rig.devices.scripts["mic"].append([speech(30)])
    rig.thread.start()
    assert wait_for(rig.consumed, 5)
    rig.paused.set()
    assert wait_for(lambda: rig.devices.opened[0][1].closed, 5)
    rig.stop.set()
    rig.thread.join(timeout=5)
    assert rig.out == []


class FlakyAudioStore(AudioStore):
    """The database is busy for the first write."""

    def __init__(self, conn: Connection, media: MediaStore) -> None:
        super().__init__(conn, media)
        self.failures = 1

    def add(self, segment: NewSegment, opus: bytes, now_ms: int) -> int:
        if self.failures:
            self.failures -= 1
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)
        return super().add(segment, opus, now_ms)


def test_a_segment_is_written_once_the_database_is_free(conn: Connection, paths: Paths) -> None:
    store = FlakyAudioStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))
    clock = Clock(T0 + 1_000)
    sampler = SourceSampler(FakeSessions(frozenset({"discord.exe"})), clock)
    sampler.sample()
    writer = SegmentWriter(store, sampler, Exclusions(UserExclusions()), clock, Counter())
    stop = threading.Event()
    thread = threading.Thread(target=writer.run, args=(stop,), daemon=True)
    thread.start()
    writer.submit("system", utterance(T0))
    try:
        assert wait_for(
            lambda: conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0] == 1, 5
        )
    finally:
        stop.set()
        thread.join(timeout=5)


class FlakyMediaLog(MediaLog):
    def __init__(self, conn: Connection) -> None:
        super().__init__(conn)
        self.failures = 1

    def close(self, session_id: int, *, end_ms: int, played_ms: int) -> None:
        if self.failures:
            self.failures -= 1
            msg = "database is locked"
            raise sqlite3.OperationalError(msg)
        super().close(session_id, end_ms=end_ms, played_ms=played_ms)


def test_a_media_row_closes_once_the_database_is_free(conn: Connection) -> None:
    clock = [0]
    source = FakeMedia(names={"Z1": "Zen"})
    tracker = MediaTracker(
        source=source,
        log_store=FlakyMediaLog(conn),
        exclusions=Exclusions(UserExclusions()),
        focused_window=lambda: None,
        now_ms=lambda: clock[0],
        browsers=BrowserState(),
    )
    source.states = [MediaState("Z1", "Video", "Chan", "playing")]
    tracker.poll()
    source.states = []
    clock[0] = 2_000
    with contextlib.suppress(sqlite3.OperationalError):
        tracker.poll()  # the close hits a busy database
    clock[0] = 4_000
    tracker.poll()
    assert conn.execute("SELECT closed FROM media_sessions").fetchone()[0] == 1


# --- screen capture: #15, #144, #149, #150, #153 ---------------------------------------------


class CountingStop(threading.Event):
    """Records the waits of a capture loop without waiting; stops it after ``limit`` waits."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.timeouts.append(timeout)
        if len(self.timeouts) >= self.limit:
            self.set()
        return self.is_set()


class BusyWindows(FakeWindows):
    """The user keeps pressing keys: the last-input time changes on every poll."""

    def last_input_ms(self) -> int:
        self.last_input += 1
        return self.last_input


def screen_capture(  # noqa: PLR0913 - the fakes a test wants to drive
    conn: Connection,
    paths: Paths,
    screen: FakeScreen,
    windows: FakeWindows,
    *,
    clock: Callable[[], float] = time.monotonic,
    uia: FakeUia | None = None,
) -> tuple[ScreenCapture, FrameStore]:
    store = FrameStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))
    counter = iter(range(1_000_000, 10**9))
    capture = ScreenCapture(
        screen=screen,
        windows=windows,
        uia=uia or FakeUia(),
        gate=OcrGate(FakeOcr()),
        store=store,
        exclusions=Exclusions(UserExclusions()),
        games=GameLog(conn),
        game_settings=GameSettings(interval_s=60.0),
        clock=clock,
        now_ms=lambda: next(counter),
        browsers=BrowserState(),
    )
    return capture, store


def game_window(rect: Rect = SCREEN) -> WindowInfo:
    return WindowInfo(7, "game.exe", "The Game", rect, has_frame=False)


def fake_clock(step: float = 0.5) -> Callable[[], float]:
    now = [0.0]

    def clock() -> float:
        now[0] += step
        return now[0]

    return clock


def test_the_capture_loop_never_spins_while_a_game_is_played(
    conn: Connection, paths: Paths
) -> None:
    windows = BusyWindows(focused=game_window())
    capture, _ = screen_capture(conn, paths, FakeScreen(), windows, clock=fake_clock())
    stop = CountingStop(40)
    capture.run(stop, threading.Event())
    assert conn.execute("SELECT count(*) FROM game_sessions").fetchone()[0] == 1
    assert all(t is not None and t > 0 for t in stop.timeouts[5:])


def test_the_capture_loop_never_spins_without_monitors(conn: Connection, paths: Paths) -> None:
    windows = BusyWindows(focused=plain_window())
    capture, _ = screen_capture(conn, paths, FakeScreen(rects=[]), windows, clock=fake_clock())
    stop = CountingStop(40)
    capture.run(stop, threading.Event())
    assert all(t is not None and t > 0 for t in stop.timeouts[5:])


def stored_triggers(conn: Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT trigger FROM frames ORDER BY id")]


def test_normal_capture_resumes_when_the_game_ends_and_old_triggers_are_dropped(
    conn: Connection, paths: Paths
) -> None:
    screen = FakeScreen()
    windows = FakeWindows(focused=game_window())
    capture, _ = screen_capture(conn, paths, screen, windows, clock=fake_clock())
    capture.tick(0.0)
    capture.note("input", 0.0)  # noted during the game
    capture.note("focus", 0.0)
    capture.tick(1.0)  # still in the game: nothing but the game screenshot
    windows.focused = plain_window()
    capture.tick(10.0)  # the game is over: normal capture on this very tick
    triggers = stored_triggers(conn)
    assert "idle" in triggers
    assert "input" not in triggers  # triggers noted during the game were dropped, not replayed
    capture.tick(11.0)  # the focus change away from the game fires normally
    before = stored_triggers(conn)
    capture.note("input", 12.0)
    screen.frames[0] = with_text_block(blank())
    capture.tick(13.0)
    assert stored_triggers(conn) == [*before, "input"]  # a new input trigger fires again


def test_normal_capture_resumes_when_the_monitors_come_back(conn: Connection, paths: Paths) -> None:
    screen = FakeScreen(rects=[])
    windows = FakeWindows(focused=plain_window())
    capture, _ = screen_capture(conn, paths, screen, windows)
    capture.tick(0.0)
    capture.note("input", 0.0)  # noted while there is nothing to capture
    capture.note("focus", 0.0)
    capture.tick(1.0)
    assert screen.grabs == 0
    assert stored_triggers(conn) == []
    screen.rects = [SCREEN]
    capture.tick(10.0)  # monitor list refreshed: idle capture runs
    assert stored_triggers(conn) == ["idle"]  # the stale input/focus triggers are not replayed
    capture.note("input", 10.5)
    screen.frames[0] = with_text_block(blank())
    capture.tick(11.0)
    assert stored_triggers(conn) == ["idle", "input"]


MON_B = Rect(800, 0, 1600, 600)


def input_frames(conn: Connection) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute("SELECT process FROM frames WHERE trigger = 'input' ORDER BY id")
    ]


@known_defect(144, "scrolling an off-centre window captures the window at the monitor's centre")
def test_scrolling_a_side_window_captures_that_window(conn: Connection, paths: Paths) -> None:
    screen = FakeScreen(rects=[SCREEN, MON_B])
    windows = FakeWindows(focused=plain_window())
    discord = WindowInfo(9, "discord.exe", "#general", Rect(800, 0, 1100, 600))
    windows.at[MON_B.center] = WindowInfo(8, "vlc.exe", "Movie", Rect(1100, 0, 1600, 600))
    windows.at[(900, 300)] = discord
    capture, _ = screen_capture(conn, paths, screen, windows)
    capture.tick(0.0)
    capture.tick(0.4)
    windows.cursor = (900, 300)  # scrolling the Discord chat, which does not have the focus
    capture.note("input", 2.0)
    capture.tick(2.5)
    assert "discord.exe" in input_frames(conn)


@known_defect(144, "on the focused monitor a scrolled unfocused window is never captured")
def test_scrolling_a_window_beside_the_focused_one_captures_it(
    conn: Connection, paths: Paths
) -> None:
    windows = FakeWindows(focused=plain_window(rect=Rect(0, 0, 400, 600)))
    side = WindowInfo(9, "discord.exe", "#general", Rect(400, 0, 800, 600))
    windows.at[(600, 300)] = side
    capture, _ = screen_capture(conn, paths, FakeScreen(), windows)
    capture.tick(0.0)
    capture.tick(0.4)
    windows.cursor = (600, 300)
    capture.note("input", 2.0)
    capture.tick(2.5)
    assert "discord.exe" in input_frames(conn)


@known_defect(149, "app sessions are derived from focused frames only")
def test_a_stream_watched_on_a_side_monitor_gets_a_time_span(conn: Connection) -> None:
    for minute in range(10):
        ts = T0 + minute * 60_000
        conn.execute(
            "INSERT INTO frames (ts_ms, monitor, process, window_title, trigger, focused)"
            " VALUES (?, 0, 'code.exe', 'main.py', 'idle', 1)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO frames (ts_ms, monitor, process, window_title, trigger, focused)"
            " VALUES (?, 1, 'vlc.exe', 'Le match', 'idle', 0)",
            (ts + 1_000,),
        )
    derive_app_sessions(conn, T0 + HOUR_MS)
    processes = {r[0] for r in conn.execute("SELECT process FROM app_sessions")}
    assert "vlc.exe" in processes


HOUR_MS = 3_600_000


@known_defect(150, "game detection is a deny-list: fullscreen players and RDP count as games")
@pytest.mark.parametrize(
    "process", ["mpc-be64.exe", "potplayer64.exe", "ms-teams.exe", "mstsc.exe"]
)
def test_fullscreen_players_and_remote_desktop_are_not_games(process: str) -> None:
    fullscreen = WindowInfo(7, process, "Fullscreen", SCREEN, has_frame=False)
    assert not is_game_window(fullscreen, [SCREEN], GameSettings())


class SizeOcr:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    def recognize(self, image: Image.Image) -> str:
        self.sizes.append(image.size)
        return "Minlo a rejoint le salon"


@known_defect(153, "deferred game OCR reads the 1920 px WebP, not the full-resolution frame")
def test_deferred_game_ocr_reads_the_full_resolution_frame(conn: Connection, paths: Paths) -> None:
    uhd = Rect(0, 0, 3840, 2160)
    screen = FakeScreen(rects=[uhd], frames={0: blank("gray", (3840, 2160))})
    capture, store = screen_capture(conn, paths, screen, FakeWindows(focused=game_window(uhd)))
    capture.tick(0.0)
    (frame_id,) = (int(r[0]) for r in conn.execute("SELECT id FROM frames"))
    ocr = SizeOcr()
    OcrFrameHandler(store, lambda: ocr).run(
        Job(1, "ocr_frame", f"ocr_frame:{frame_id}", {"frame_id": frame_id}, 0, 1)
    )
    assert ocr.sizes == [(3840, 2160)]
