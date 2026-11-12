"""Turning audio segments into transcripts: the job handler and the text rules around Whisper.

Whisper itself sits behind the ``Transcriber`` protocol (``pipeline/whisper.py``), so everything
here runs on any machine with a fake. The rules that matter for quality:

- Language is French or English only, decided per segment.
- A user vocabulary (friends, games, projects) is passed as Whisper's initial prompt, whole
  entries only, capped to the prompt window.
- Segments cut at the maximum length overlap by a couple of seconds; the repeated words are
  removed from the later transcript.
- A few well-known hallucinations (subtitle credits Whisper invents on noise) are dropped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Final, Protocol

import numpy as np

from akashic.fold import fold
from akashic.pipeline.jobs import PermanentJobError

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from akashic.pipeline.jobs import Job
    from akashic.storage.audio import AudioStore

log = logging.getLogger(__name__)

LANGUAGES: Final = ("fr", "en")
# Whole entries only. Whisper's prompt window is about 224 tokens; this is screenpipe's cap.
PROMPT_MAX_CHARS: Final = 800
MIN_OVERLAP_WORDS: Final = 2
MAX_OVERLAP_WORDS: Final = 40
MAX_SKIPPED_WORDS: Final = 2  # leading words of the new text that may be a cut-off fragment
# The end of what was said just before (same stream, within a minute) is added to the prompt:
# Whisper hears a short, quiet segment much better with the sentence it continues.
PREVIOUS_MAX_CHARS: Final = 200
PREVIOUS_WITHIN_MS: Final = 60_000
# Quiet speech is boosted to about this level before Whisper (never turned down, at most +20 dB).
TARGET_RMS: Final = 0.1
MAX_GAIN: Final = 10.0
# A transcript whose confidence (exp of Whisper's mean log-probability per token) is below this
# is shown and cited as "unclear". About an average log-probability of -0.6; tune on real data.
UNCLEAR_BELOW: Final = 0.55

# Whisper invents these on noise and silence. Compared whole, normalised (see ``normalize``).
HALLUCINATIONS: Final = frozenset(
    {
        "thanks for watching",
        "thank you for watching",
        "please subscribe",
        "subtitles by the amara org community",
        "merci d'avoir regarde cette video",
        "merci d'avoir regarde",
        "sous titres realises par la communaute d'amara org",
        "sous titrage st 501",
        "sous titrage societe radio canada",
    }
)
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")  # curly apostrophes are straightened first


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str  # "fr" or "en"
    confidence: float | None = None  # 0-1, see UNCLEAR_BELOW; None when unknown


class Transcriber(Protocol):
    def available(self) -> bool:
        """The model is in place (it is downloaded once, explicitly)."""
        ...

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript | None:
        """Text of 16 kHz mono ``samples``, or None when Whisper heard no speech."""
        ...

    def unload_if_idle(self, *, force: bool = False) -> None:
        """Free the model's VRAM after the configured idle time."""
        ...


def normalize(text: str) -> str:
    """Lower-case words without accents or punctuation, single-spaced. An apostrophe inside a
    word is kept ("c'est"), so French elisions stay one word."""
    return " ".join(_WORD.findall(fold(text.replace("\u2019", "'"))))


def build_prompt(
    vocabulary: tuple[str, ...], previous: str | None = None, max_chars: int = PROMPT_MAX_CHARS
) -> str | None:
    """The vocabulary as a comma-separated list (as many whole entries as fit), then the end of
    what was said just before, in the room the vocabulary left."""
    chosen: list[str] = []
    length = 0
    for entry in vocabulary:
        extra = len(entry) + (2 if chosen else 0)
        if length + extra > max_chars:
            break
        chosen.append(entry)
        length += extra
    words = ", ".join(chosen)
    tail = _tail(previous or "", min(PREVIOUS_MAX_CHARS, max_chars - length - 1))
    if tail:
        words = f"{words}. {tail}" if words else tail
    return words or None


def _tail(text: str, limit: int) -> str:
    """The last ``limit`` characters of ``text``, starting at a word."""
    text = " ".join(text.split())
    if limit <= 0 or not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    space = cut.find(" ")
    return cut[space + 1 :] if space >= 0 else ""


def normalize_loudness(samples: NDArray[np.float32]) -> NDArray[np.float32]:
    """Boost quiet speech towards ``TARGET_RMS`` (up to ``MAX_GAIN``); loud audio is unchanged."""
    if samples.size == 0:
        return samples
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if rms <= 0.0:
        return samples
    gain = min(max(TARGET_RMS / rms, 1.0), MAX_GAIN)
    if gain == 1.0:
        return samples
    return np.clip(samples * np.float32(gain), -1.0, 1.0).astype(np.float32)


def is_hallucination(text: str) -> bool:
    return normalize(text) in HALLUCINATIONS


def trim_overlap(previous: str, current: str) -> str:
    """Remove from the start of ``current`` the words that end ``previous``.

    A segment cut at the maximum length begins with the last seconds of the one before it, so
    its transcript starts by repeating that transcript's last words. Up to two leading words of
    ``current`` may be a fragment of a word cut by the boundary and are skipped when matching.
    """
    prev_words = normalize(previous).split()[-MAX_OVERLAP_WORDS:]
    tokens = current.split()
    words: list[str] = []
    owner: list[int] = []  # index of the token each word came from
    for index, token in enumerate(tokens):
        for word in normalize(token).split():
            words.append(word)
            owner.append(index)
    for skip in range(MAX_SKIPPED_WORDS + 1):
        for size in range(min(len(prev_words), len(words) - skip), MIN_OVERLAP_WORDS - 1, -1):
            end = skip + size
            if prev_words[-size:] != words[skip:end]:
                continue
            if end == len(words):
                return ""  # nothing new after the overlap
            if owner[end] == owner[end - 1]:
                continue  # the match would end inside a token like "peut-etre"
            return " ".join(tokens[owner[end] :])
    return current


def decode_segment(
    transcriber: Transcriber,
    samples: NDArray[np.float32],
    vocabulary: tuple[str, ...],
    previous: str | None = None,
    overlap: str | None = None,
) -> Transcript | None:
    """Current production decode boundary, shared with fixed-reference comparisons.

    ``previous`` supplies prompt context; ``overlap`` is the actual continuation predecessor.
    Keeping those separate preserves the current production behavior.
    """
    result = transcriber.transcribe(normalize_loudness(samples), build_prompt(vocabulary, previous))
    text = result.text.strip() if result else ""
    if result and overlap:
        text = trim_overlap(overlap, text).strip()
    if not result or not text or is_hallucination(text):
        return None
    return Transcript(text, result.language, result.confidence)


class TranscribeHandler:
    """Job handler for ``transcribe``: heavy (GPU), so the scheduler gates it."""

    kind: ClassVar[str] = "transcribe"
    heavy: ClassVar[bool] = True

    def __init__(
        self,
        audio: AudioStore,
        transcriber: Transcriber,
        vocabulary: tuple[str, ...],
        model_key: str,
    ) -> None:
        self._audio = audio
        self._transcriber = transcriber
        self._vocabulary = vocabulary
        self._model_key = model_key

    def blocked(self) -> str | None:
        return None if self._transcriber.available() else "model missing"

    def run(self, job: Job) -> None:
        segment_id = job.payload.get("segment_id")
        if not isinstance(segment_id, int):
            msg = "transcribe job without a segment id"
            raise PermanentJobError(msg)
        segment = self._audio.get(segment_id)
        if segment is None:
            msg = "segment no longer exists"
            raise PermanentJobError(msg)
        if segment.status != "pending":
            return  # already handled (the job ran, then the worker died before completing it)
        samples = self._audio.read_samples(segment)
        if samples is None:
            self._audio.set_transcript(segment_id, "failed", None, None)
            msg = "raw audio is gone"
            raise PermanentJobError(msg)
        previous = self._audio.previous_transcript(
            segment.stream, segment.start_ms, PREVIOUS_WITHIN_MS
        )
        earlier = (
            self._audio.get(segment.continues_id) if segment.continues_id is not None else None
        )
        result = decode_segment(
            self._transcriber,
            samples,
            self._vocabulary,
            previous,
            earlier.transcript if earlier else None,
        )
        if result is None:
            self._audio.set_transcript(segment_id, "empty", None, None, model=self._model_key)
            return
        self._audio.set_transcript(
            segment_id,
            "done",
            result.language,
            result.text,
            result.confidence,
            model=self._model_key,
        )

    def give_up(self, job: Job) -> None:
        """The job failed for good: say so on the segment, so it isn't 'pending' forever."""
        segment_id = job.payload.get("segment_id")
        if isinstance(segment_id, int):
            self._audio.set_transcript(segment_id, "failed", None, None)

    def idle(self, *, force: bool = False) -> None:
        self._transcriber.unload_if_idle(force=force)
