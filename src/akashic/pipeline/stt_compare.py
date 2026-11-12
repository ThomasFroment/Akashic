"""Compare selected local recordings using hand-corrected references, one model at a time."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, ClassVar

from akashic.pipeline.jobs import PermanentJobError
from akashic.pipeline.speech import make_transcriber
from akashic.pipeline.transcribe import decode_segment, normalize
from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.pipeline.jobs import Job
    from akashic.pipeline.transcribe import Transcriber
    from akashic.storage.audio import AudioStore

STT_COMPARE = "stt_compare"


def _numbers() -> dict[str, str]:
    en = [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
        "thirteen",
        "fourteen",
        "fifteen",
        "sixteen",
        "seventeen",
        "eighteen",
        "nineteen",
    ]
    fr = [
        "zero",
        "un",
        "deux",
        "trois",
        "quatre",
        "cinq",
        "six",
        "sept",
        "huit",
        "neuf",
        "dix",
        "onze",
        "douze",
        "treize",
        "quatorze",
        "quinze",
        "seize",
        "dix-sept",
        "dix-huit",
        "dix-neuf",
    ]
    result = {normalize(word): str(i) for words in (en, fr) for i, word in enumerate(words)}
    for tens, english, french in (
        (20, "twenty", "vingt"),
        (30, "thirty", "trente"),
        (40, "forty", "quarante"),
        (50, "fifty", "cinquante"),
        (60, "sixty", "soixante"),
        (70, "seventy", "soixante dix"),
        (80, "eighty", "quatre vingt"),
        (90, "ninety", "quatre vingt dix"),
    ):
        result[english] = result[french] = str(tens)
        for unit in range(1, 10):
            result[f"{english} {en[unit]}"] = str(tens + unit)
            base, suffix = (
                ("soixante", fr[10 + unit])
                if tens == 70  # noqa: PLR2004 - French numeral grammar
                else (("quatre vingt", fr[10 + unit]) if tens == 90 else (french, fr[unit]))  # noqa: PLR2004
            )
            result[normalize(f"{base} {suffix}")] = str(tens + unit)
            if unit == 1 and tens < 80:  # noqa: PLR2004 - no et in quatre-vingt-un
                result[normalize(f"{base} et {suffix}")] = str(tens + unit)
    result["une"] = "1"
    result["quatre vingts"] = "80"
    return result


NUMBERS = _numbers()


def comparison_normalize(text: str) -> str:
    """Symmetric accent/punctuation and FR/EN 0-99 formatting (no fuzzy rewriting)."""
    words = normalize(text).split()
    output: list[str] = []
    at = 0
    while at < len(words):
        for size in range(min(5, len(words) - at), 0, -1):
            phrase = " ".join(words[at : at + size])
            if phrase in NUMBERS:
                output.append(NUMBERS[phrase])
                at += size
                break
        else:
            output.append(words[at])
            at += 1
    return " ".join(output)


def _distance(gold: list[str], heard: list[str]) -> tuple[int, int]:
    previous = list(range(len(heard) + 1))
    for i, word in enumerate(gold, 1):
        row = [i]
        for j, actual in enumerate(heard, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (word != actual)))
        previous = row
    return previous[-1], len(gold)


def word_errors(reference: str, prediction: str) -> tuple[int, int]:
    """Levenshtein word distance; shared accent/case/punctuation normalization, no rewrite."""
    return _distance(
        comparison_normalize(reference).split(), comparison_normalize(prediction).split()
    )


def character_errors(reference: str, prediction: str) -> tuple[int, int]:
    """CER on normalized characters, including spaces; empty reference retains insertions."""
    return _distance(list(comparison_normalize(reference)), list(comparison_normalize(prediction)))


def term_score(terms: list[str], reference: str, prediction: str) -> tuple[int, int]:
    gold, heard = f" {comparison_normalize(reference)} ", f" {comparison_normalize(prediction)} "
    wanted = {comparison_normalize(t) for t in terms if comparison_normalize(t)}
    wanted = {term for term in wanted if f" {term} " in gold}
    return sum(f" {term} " in heard for term in wanted), len(wanted)


class STTCompareHandler:
    kind: ClassVar[str] = STT_COMPARE
    heavy: ClassVar[bool] = True

    def __init__(
        self,
        conn: Connection,
        audio: AudioStore,
        models_dir: Path,
        factory: Callable[[Path, float], Transcriber] = make_transcriber,
    ) -> None:
        self._conn, self._audio, self._models_dir = conn, audio, models_dir
        self._factory = factory

    def blocked(self) -> str | None:
        return None

    def run(self, job: Job) -> None:
        run_id = job.payload.get("run_id")
        if not isinstance(run_id, int):
            msg = "comparison job needs a run ID"
            raise PermanentJobError(msg)
        run = self._conn.execute(
            "SELECT vocabulary FROM stt_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            return
        vocabulary: list[str] = json.loads(run[0])
        with db.transaction(self._conn):
            self._conn.execute("UPDATE stt_runs SET state = 'running' WHERE id = ?", (run_id,))
        models = self._conn.execute(
            "SELECT DISTINCT model FROM stt_results WHERE run_id = ? ORDER BY model", (run_id,)
        ).fetchall()
        for (model,) in models:
            transcriber = self._factory(self._models_dir / model, 0.0)
            try:
                rows = self._conn.execute(
                    "SELECT segment_id, reference, terms, previous_reference, "
                    "overlap_reference FROM stt_results"
                    " WHERE run_id = ? AND model = ? AND prediction IS NULL AND error IS NULL",
                    (run_id, model),
                ).fetchall()
                for segment_id, reference, terms_json, previous, overlap in rows:
                    self._one(
                        run_id,
                        model,
                        segment_id,
                        reference,
                        terms_json,
                        transcriber,
                        vocabulary,
                        previous,
                        overlap,
                    )
            finally:
                transcriber.unload_if_idle(force=True)
        with db.transaction(self._conn):
            self._conn.execute("UPDATE stt_runs SET state = 'done' WHERE id = ?", (run_id,))

    def _one(  # noqa: PLR0913, PLR0917 - one persisted comparison result
        self,
        run_id: int,
        model: str,
        segment_id: int,
        reference: str,
        terms_json: str,
        transcriber: Transcriber,
        vocabulary: list[str],
        previous: str,
        overlap: str,
    ) -> None:
        started = time.monotonic()
        prediction = language = error = None
        errors = words = hits = total = None
        try:
            segment = self._audio.get(segment_id)
            samples = self._audio.read_samples(segment) if segment else None
            if samples is None or not transcriber.available():
                error = "FileNotFoundError"
            else:
                # Same clip and vocabulary, no history from a preceding model's prediction.
                result = decode_segment(transcriber, samples, tuple(vocabulary), previous, overlap)
                prediction = result.text if result else ""
                language = result.language if result else None
                errors, words = word_errors(reference, prediction)
                hits, total = term_score(json.loads(terms_json), reference, prediction)
        except Exception as exc:  # noqa: BLE001 - one failed model/clip must remain inspectable
            error = type(
                exc
            ).__name__  # never persist exception messages containing captured content
        # Failed decodes score as full deletions on the same labelled clips; never disappear.
        errors, words = word_errors(reference, prediction or "")
        chars_error, chars = character_errors(reference, prediction or "")
        hits, total = term_score(json.loads(terms_json), reference, prediction or "")
        with db.transaction(self._conn):
            self._conn.execute(
                "UPDATE stt_results SET prediction=?, language=?, elapsed_ms=?, errors=?, words=?,"
                " term_hits=?, term_total=?, error=?, char_errors=?, chars=? "
                "WHERE run_id=? AND segment_id=? AND model=?",
                (
                    prediction,
                    language,
                    int((time.monotonic() - started) * 1000),
                    errors,
                    words,
                    hits,
                    total,
                    error,
                    chars_error,
                    chars,
                    run_id,
                    segment_id,
                    model,
                ),
            )

    def give_up(self, job: Job) -> None:
        with db.transaction(self._conn):
            self._conn.execute(
                "UPDATE stt_runs SET state = 'failed' WHERE id = ?", (job.payload.get("run_id"),)
            )

    def idle(self, *, force: bool = False) -> None:
        """Each model is already released in the run's finally block."""
