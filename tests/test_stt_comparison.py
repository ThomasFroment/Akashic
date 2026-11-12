"""Comparison integrity, metrics, expiry and model release, using invented audio/text."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
import pytest
import sqlcipher3
from api_client import Client
from memory_data import add_segment
from stt_rows import insert_result, insert_run, insert_sample

from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.api.stt import comparison_scores
from akashic.config import DEFAULT_CONFIG_TOML
from akashic.pipeline.jobs import Job
from akashic.pipeline.stt_compare import (
    STT_COMPARE,
    STTCompareHandler,
    character_errors,
    term_score,
    word_errors,
)
from akashic.pipeline.transcribe import TranscribeHandler, Transcript, build_prompt
from akashic.storage.audio import AudioStore
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from numpy.typing import NDArray

    from akashic.config import Paths
    from akashic.pipeline.transcribe import Transcriber
    from akashic.storage.audio import StoredSegment


@pytest.mark.parametrize(
    ("reference", "heard", "expected"),
    [
        ("J'ai la flemme.", "j'ai la flamme", (1, 3)),
        ("Ocarina of Time avec des mods", "Ocarina of Time avec des modes", (1, 6)),
        ("Le jeu est prêt", "le jeu est pret", (0, 4)),
        ("not available", "available", (1, 2)),
        ("", "invented speech", (2, 0)),
    ],
)
def test_word_errors(reference: str, heard: str, expected: tuple[int, int]) -> None:
    assert word_errors(reference, heard) == expected


def test_important_terms_are_full_words_or_phrases_present_in_reference() -> None:
    assert term_score(
        ["mods", "Ocarina of Time", "absent"],
        "Ocarina of Time, des mods",
        "Ocarina of Time, des modes",
    ) == (1, 2)


class SyntheticAudio(AudioStore):
    def read_samples(self, segment: StoredSegment) -> NDArray[np.float32]:
        del segment
        return np.full(1600, 0.1, dtype=np.float32)


class Recognizer:
    def __init__(self, name: str, released: list[str], *, fail: bool = False) -> None:
        self.name, self.released, self.fail = name, released, fail

    def available(self) -> bool:
        return True

    def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript:
        assert samples.size == 1600
        assert prompt == "mods"
        if self.fail:
            raise RuntimeError("invented private content must not become an error message")
        return Transcript("Le jeu accepte des mods", "fr")

    def unload_if_idle(self, *, force: bool = False) -> None:
        assert force
        self.released.append(self.name)


def seed_run(conn: Connection) -> tuple[int, int]:
    segment = add_segment(conn, 1000, text="Original noisy transcript")
    insert_sample(conn, segment, "Le jeu accepte des mods", '["mods"]')
    run = insert_run(conn, '["mods"]')
    for model in ("one", "two"):
        insert_result(conn, run, segment, model, "Le jeu accepte des mods", '["mods"]')
    return run, segment


def test_comparison_keeps_originals_and_releases_models_even_after_failure(
    conn: Connection, paths: Paths
) -> None:
    run, segment = seed_run(conn)
    released: list[str] = []

    def factory(folder: Path, idle: float) -> Transcriber:
        assert idle == 0
        return Recognizer(folder.name, released, fail=folder.name == "one")

    audio = SyntheticAudio(conn, MediaStore(paths.media_dir, bytes(32)))
    handler = STTCompareHandler(conn, audio, paths.models_dir, factory)
    handler.run(Job(1, STT_COMPARE, "test", {"run_id": run}, 0, 1))
    rows = conn.execute(
        "SELECT model,prediction,errors,term_hits,error FROM stt_results ORDER BY model"
    ).fetchall()
    assert rows == [
        ("one", None, 5, 0, "RuntimeError"),
        ("two", "Le jeu accepte des mods", 0, 1, None),
    ]
    assert released == ["one", "two"]
    assert audio.get(segment).transcript == "Original noisy transcript"  # pyright: ignore[reportOptionalMemberAccess]  # seeded above
    handler.run(Job(2, STT_COMPARE, "retry", {"run_id": run}, 0, 2))
    assert conn.execute("SELECT count(*) FROM stt_results").fetchone()[0] == 2


def test_transcript_expiry_removes_comparison_copies(conn: Connection) -> None:
    run, segment = seed_run(conn)
    conn.execute("UPDATE stt_results SET prediction='derived private text' WHERE run_id=?", (run,))
    conn.execute("UPDATE audio_segments SET transcript=NULL WHERE id=?", (segment,))
    assert conn.execute("SELECT count(*) FROM stt_samples").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM stt_results").fetchone()[0] == 0
    assert json.loads(conn.execute("SELECT vocabulary FROM stt_runs").fetchone()[0]) == ["mods"]


def test_source_deleted_during_comparison_is_not_republished(
    conn: Connection, paths: Paths
) -> None:
    run, segment = seed_run(conn)

    class Deleting(Recognizer):
        def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript:
            conn.execute("DELETE FROM audio_segments WHERE id=?", (segment,))
            return super().transcribe(samples, prompt)

    audio = SyntheticAudio(conn, MediaStore(paths.media_dir, bytes(32)))
    STTCompareHandler(
        conn, audio, paths.models_dir, lambda folder, _idle: Deleting(folder.name, [])
    ).run(Job(1, STT_COMPARE, "test", {"run_id": run}, 0, 1))
    assert conn.execute("SELECT count(*) FROM stt_results").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("reference", "heard"),
    [
        ("12 mods", "douze mods"),
        ("twenty-one games", "21 games"),
        ("quatre-vingt-douze", "92"),
        ("soixante et onze", "71"),
    ],
)
def test_numbers_and_hyphens_normalize_symmetrically(reference: str, heard: str) -> None:
    assert word_errors(reference, heard)[0] == 0
    assert word_errors(heard, reference)[0] == 0
    assert character_errors(reference, heard)[0] == 0


def test_names_negations_and_empty_references_remain_measurement_errors() -> None:
    assert word_errors("Minlo ne vient pas", "Milo vient")[0] == 3
    assert term_score(["Minlo", "pas"], "Minlo ne vient pas", "Milo vient") == (0, 2)
    assert character_errors("", "ghost") == (5, 0)
    assert word_errors("missed speech", "") == (2, 2)


def test_failure_denominators_are_the_same_across_language_condition_and_provenance() -> None:
    rows: list[dict[str, object]] = [
        {
            "model": "one",
            "reference_language": "fr",
            "condition": "quiet",
            "draft_derived": False,
            "prediction": None,
            "error": "RuntimeError",
            "errors": 2,
            "words": 2,
            "char_errors": 10,
            "chars": 10,
            "term_hits": 0,
            "term_total": 1,
        },
        {
            "model": "one",
            "reference_language": "none",
            "condition": "silence",
            "draft_derived": True,
            "prediction": "ghost",
            "error": None,
            "errors": 1,
            "words": 0,
            "char_errors": 5,
            "chars": 0,
            "term_hits": 0,
            "term_total": 0,
        },
    ]
    scores = comparison_scores(rows)
    aggregate = next(s for s in scores if s["dimension"] == "all")
    assert (aggregate["clips"], aggregate["failed"], aggregate["errors"], aggregate["words"]) == (
        2,
        1,
        3,
        2,
    )
    independent = next(s for s in scores if s["dimension"] == "provenance")
    assert (independent["clips"], independent["failed"], independent["words"]) == (1, 1, 2)
    derived_only = comparison_scores([rows[1]])
    absent = next(s for s in derived_only if s["dimension"] == "provenance")
    assert (absent["clips"], absent["words"], absent["failed"]) == (0, 0, 0)


@pytest.mark.parametrize("heard", ["les derniers mots fresh speech", "thanks for watching", ""])
def test_shared_current_production_boundary_uses_fixed_reference(
    heard: str, conn: Connection, paths: Paths
) -> None:
    prompts: list[str | None] = []

    class Fixed(Recognizer):
        def transcribe(self, samples: NDArray[np.float32], prompt: str | None) -> Transcript:
            assert float(samples.max()) == pytest.approx(0.1)
            prompts.append(prompt)
            return Transcript(heard, "mixed", 0.4)

    model = Fixed("fake", [])
    previous = "les derniers mots"
    prior = add_segment(conn, 0, text=previous)
    conn.execute("UPDATE audio_segments SET end_ms=1000 WHERE id=?", (prior,))
    current = add_segment(conn, 2000, text=None, status="pending")
    conn.execute("UPDATE audio_segments SET continues_id=? WHERE id=?", (prior, current))
    audio = SyntheticAudio(conn, MediaStore(paths.media_dir, bytes(32)))
    TranscribeHandler(audio, model, ("Minlo",), "fixed-model-pin").run(
        Job(1, "transcribe", "production", {"segment_id": current}, 0, 1)
    )
    production = audio.get(current)
    assert production is not None
    assert conn.execute(
        "SELECT transcription_model FROM audio_segments WHERE id=?", (current,)
    ).fetchone() == ("fixed-model-pin",)
    insert_sample(conn, current, "fresh speech")
    insert_run(conn, '["Minlo"]', run_id=1)
    insert_result(
        conn, 1, current, "fake", "fresh speech", "[]",
        previous_reference=previous, overlap_reference=previous,
    )  # fmt: skip
    STTCompareHandler(conn, audio, paths.models_dir, lambda _path, _idle: model).run(
        Job(2, STT_COMPARE, "compare", {"run_id": 1}, 0, 1)
    )
    second = conn.execute("SELECT prediction,language,error FROM stt_results").fetchone()
    assert second == (production.transcript or "", production.language, None)
    assert prompts == [build_prompt(("Minlo",), previous)] * 2
    if heard.startswith("les"):
        assert production.transcript == "fresh speech"
    else:
        assert production.status == "empty"


def test_unrelated_expiry_keeps_independent_samples_but_clears_derived_text(
    conn: Connection,
) -> None:
    _, keep = seed_run(conn)
    conn.execute("UPDATE stt_samples SET draft_derived=0 WHERE segment_id=?", (keep,))
    derived = add_segment(conn, 3, text="draft")
    contextual = add_segment(conn, 4, text="draft")
    for segment, flag, previous in ((derived, 1, ""), (contextual, 0, "copied context")):
        insert_sample(conn, segment, "r", draft_derived=flag, previous_reference=previous)
    other = add_segment(conn, 5, text="old unrelated chatter")
    conn.execute("UPDATE audio_segments SET transcript=NULL WHERE id=?", (other,))
    assert conn.execute("SELECT segment_id FROM stt_samples").fetchall() == [(keep,)]
    other = add_segment(conn, 6, text="more chatter")
    conn.execute("DELETE FROM audio_segments WHERE id=?", (other,))
    assert conn.execute("SELECT segment_id FROM stt_samples").fetchall() == [(keep,)]


@pytest.mark.parametrize(
    "insert",
    [
        "INSERT INTO stt_samples (segment_id, reference) VALUES (1, 'r')",
        "INSERT INTO stt_runs (created_ms, vocabulary) VALUES (1, '[]')",
    ],
)
def test_comparison_rows_must_state_their_provenance(conn: Connection, insert: str) -> None:
    add_segment(conn, 1_000, text="draft")
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute(insert)


@pytest.mark.parametrize(
    "omitted", ["condition", "draft_model", "draft_derived", "reference_language"]
)
def test_result_rows_must_state_their_provenance(conn: Connection, omitted: str) -> None:
    segment = add_segment(conn, 1_000, text="draft")
    insert_sample(conn, segment, "r")
    run = insert_run(conn, "[]")
    values = {
        "run_id": run,
        "segment_id": segment,
        "model": "m",
        "revision": "pin",
        "reference": "r",
        "terms": "[]",
        "reference_language": "'fr'",
        "condition": "'quiet'",
        "draft_model": "'draft'",
        "draft_derived": 1,
    }
    del values[omitted]
    sql = f"INSERT INTO stt_results ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})"  # noqa: S608
    # sanity: the full row is accepted, so the failure below is due to the omission alone
    insert_result(conn, run, segment, "ok", "r", "[]")
    params = [v.strip("'") if isinstance(v, str) else v for v in values.values()]
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute(sql, params)


# --- the comparison routes ---------------------------------------------------------------------


@pytest.fixture
def client(conn: Connection, media: MediaStore, paths: Paths) -> Iterator[Client]:
    paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    token = new_token()
    services = Services(
        conn,
        FrameStore(conn, media),
        token,
        Controls(dict, lambda: None, lambda: None),
        config_file=paths.config_file,
    )
    server = ApiServer(services)
    server.start()
    yield Client(server, token, conn)
    server.stop()


def test_speech_comparison_requires_reviewed_references_and_downloaded_models(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.call("/api/stt")[0] == 200
    segment = add_segment(client.conn, 1_000, text="draft recognition")
    client.conn.execute(
        "UPDATE audio_segments SET audio_path='synthetic.opus' WHERE id=?", (segment,)
    )
    body = {
        "segment_id": segment,
        "reference": "Le jeu accepte des mods",
        "terms": ["mods"],
        "reference_language": "fr",
        "condition": "quiet",
    }
    assert client.call("/api/stt/samples", "POST", {**body, "terms": ["missing"]})[0] == 422
    assert client.call("/api/stt/samples", "POST", body)[0] == 200
    model = "faster-whisper-large-v3"
    assert client.call("/api/stt/runs", "POST", {"models": [model]})[0] == 409

    def ready(*_: object) -> bool:
        return True

    monkeypatch.setattr("akashic.api.stt.is_ready", ready)
    status, run = client.call("/api/stt/runs", "POST", {"models": [model]})
    assert status == 200
    assert client.call("/api/stt/runs", "POST", {"models": [model]})[0] == 409
    result = client.conn.execute(
        "SELECT reference,model FROM stt_results WHERE run_id=?", (run["id"],)
    ).fetchone()
    assert result == (body["reference"], model)
    assert client.call(f"/api/stt/samples/{segment}", "DELETE")[0] == 200
    assert client.conn.execute("SELECT count(*) FROM stt_results").fetchone()[0] == 0


def test_empty_clips_conditions_provenance_and_denominators_are_listed(client: Client) -> None:
    segment = add_segment(client.conn, 1_000, text="draft")
    client.conn.execute(
        "UPDATE audio_segments SET audio_path='synthetic.opus',status='empty',transcript=NULL "
        "WHERE id=?",
        (segment,),
    )
    status, recordings = client.call("/api/stt/recordings")
    assert status == 200
    assert recordings[0]["segment_id"] == segment
    assert recordings[0]["transcript"] == ""
    assert "unknown" in recordings[0]["draft_model"]
    body = {
        "segment_id": segment,
        "reference": "",
        "reference_language": "none",
        "condition": "silence",
    }
    assert (
        client.call("/api/stt/samples", "POST", {"segment_id": segment, "reference": ""})[0] == 422
    )
    assert client.call("/api/stt/samples", "POST", body)[0] == 200
    _, overview = client.call("/api/stt")
    assert overview["samples"][0]["condition"] == "silence"
    assert overview["samples"][0]["draft_derived"] is False
    client.conn.execute(
        "UPDATE audio_segments SET transcription_model='known-model-pin' WHERE id=?", (segment,)
    )
    assert client.call("/api/stt/samples", "POST", body)[0] == 200
    _, overview = client.call("/api/stt")
    assert overview["samples"][0]["draft_model"] == "known-model-pin"
