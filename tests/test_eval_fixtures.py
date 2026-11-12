"""The Ask evaluation's fixtures and harness (``scripts/eval_ask.py``), without any model.

The eval databases are built and indexed exactly as the script builds them, with a deterministic
bag-of-words embedder in place of llama-server, and questions run through the real
``AskHandler`` with a scripted chat model. This covers everything up to the model call:
production indexing (#113), the fixtures' realism (#113, #123, #125), the question metadata
(#115, #118, #127) and the CLI (#126). ``scripts`` is put on ``sys.path``; the script modules
are typed as ``Any`` here.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest
from defects import known_defect

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))
ev: Any = importlib.import_module("eval_ask")
scoring: Any = importlib.import_module("eval_scoring")
quality: Any = importlib.import_module("eval_quality")
hard: Any = importlib.import_module("eval_hard_memory")

TODAY = datetime(2026, 11, 10, 12, tzinfo=UTC)  # only its date is used: day 0 of the world
DIM = 64


class HashEmbedder:
    """Deterministic bag of words: shared words make vectors close, nothing else does."""

    model = "fake-embedding"

    def __init__(self) -> None:
        self.documents = 0
        self.queries: list[str] = []

    def available(self) -> bool:
        return True

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        self.documents += len(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> Vector:
        self.queries.append(text)
        return self._vector(text)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force

    @staticmethod
    def _vector(text: str) -> Vector:
        vector = np.zeros(DIM, dtype=np.float32)
        for word in re.findall(r"\w+", scoring.plain(text)):
            vector[zlib.crc32(word.encode()) % DIM] += 1.0
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return vector / norm


class ScriptedChat:
    """Returns the given plans and answers in order, as llama-server would stream them."""

    model = "scripted-chat"

    def __init__(self, plans: Sequence[Mapping[str, object]], answers: Sequence[str]) -> None:
        self.plans = [json.dumps(p) for p in plans]
        self.answers = list(answers)
        self.planned: list[str] = []

    def available(self) -> bool:
        return True

    def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], max_tokens: int = 0
    ) -> str:
        del system, schema, max_tokens
        self.planned.append(user)
        return self.plans.pop(0)

    def stream(self, system: str, user: str, max_tokens: int = 0) -> Iterator[str]:
        del system, user, max_tokens
        yield self.answers.pop(0)

    def unload_if_idle(self, *, force: bool = False) -> None:
        del force


def plan(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "time_text": "",
        "people": [],
        "kinds": [],
        "keywords": [],
        "semantic_query": "",
    }
    return {**base, **fields}


@pytest.fixture(scope="module")
def memories(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[tuple[int, int, int], Any]]:
    folder = tmp_path_factory.mktemp("eval")
    built: dict[tuple[int, int, int], Any] = {}
    for n, now in enumerate(sorted({q.now for _, q in ev.ALL})):
        built[now] = ev.build_memory(folder / f"eval-{n}.db", TODAY, now, HashEmbedder())
    yield built
    for memory in built.values():
        memory.conn.close()


def by_id(qid: str) -> tuple[str, Any]:
    return next((i, q) for i, q in ev.ALL if i == qid)


# --- the databases are indexed like production (#113) --------------------------------------


def test_the_eval_indexes_through_the_embed_handler_like_production(
    memories: dict[tuple[int, int, int], Any],
) -> None:
    for memory in memories.values():
        counts = memory.index
        assert counts["segment_passages"] > 0
        assert counts["frame_passages"] > 0
        assert counts["episode_vectors"] == len(memory.ids)
        assert counts["segment_vectors"] == 0  # no legacy whole-segment vectors
        conn = memory.conn
        unembedded = conn.execute(
            "SELECT count(*) FROM audio_segments s WHERE length(s.transcript) >= 12 AND NOT"
            " EXISTS (SELECT 1 FROM segment_passages p WHERE p.segment_id = s.id)"
        ).fetchone()[0]
        assert unembedded == 0
        assert conn.execute("SELECT count(*) FROM jobs WHERE state != 'done'").fetchone()[0] == 0


def test_every_video_has_frames(memories: dict[tuple[int, int, int], Any]) -> None:
    conn = memories[ev.DEFAULT_NOW].conn
    videos = conn.execute(
        "SELECT count(*) FROM frames f JOIN episodes e ON f.ts_ms BETWEEN e.start_ms AND e.end_ms"
        " WHERE e.kind = 'video'"
    ).fetchone()[0]
    assert videos >= 8


def test_a_window_holds_at_least_eight_videos(memories: dict[tuple[int, int, int], Any]) -> None:
    conn = memories[ev.DEFAULT_NOW].conn
    start, end = ev.local_ms(TODAY, 2, 12), ev.local_ms(TODAY, 2, 18)
    count = conn.execute(
        "SELECT count(*) FROM episodes WHERE kind = 'video' AND start_ms >= ? AND end_ms <= ?",
        (start, end),
    ).fetchone()[0]
    assert count >= 8
    counted = [q for _, q in ev.ALL if q.count is not None]
    assert counted
    assert all(q.count == count and q.window == (2, 12, 18) for q in counted)


def test_each_memory_holds_only_what_had_happened_by_its_now(
    memories: dict[tuple[int, int, int], Any],
) -> None:
    assert set(memories) == {(0, 18, 0), (0, 9, 0), (-1, 0, 40)}
    morning, evening, night = memories[0, 9, 0], memories[0, 18, 0], memories[-1, 0, 40]
    assert "brambleheart_call" in morning.ids
    assert "lisbon_call" not in morning.ids
    assert "late_video" not in evening.ids
    assert {"late_video", "late_call", "balloon_video"} <= set(night.ids)
    for memory in memories.values():
        latest = memory.conn.execute("SELECT max(end_ms) FROM episodes").fetchone()[0]
        assert latest <= memory.at_ms


def test_the_fixtures_hold_unclear_and_misattributed_speech(
    memories: dict[tuple[int, int, int], Any],
) -> None:
    lines = [(ep, ln) for ep in ev.WORLD for ln in ep.lines]
    conversation = [ln for ep, ln in lines if ep.kind == "conversation"]
    assert any(ln.speaker is None for ln in conversation)  # unattributed (#123)
    assert any(ln.said_by and ln.said_by != ln.speaker for ln in conversation)  # mis-attributed
    assert any(len(ln.turns) >= 2 for ln in conversation)  # two speakers in one segment
    assert any(ln.confidence is not None and ln.confidence < 0.55 for ln in conversation)
    conn = memories[ev.DEFAULT_NOW].conn
    stored = conn.execute("SELECT count(*) FROM audio_segments WHERE confidence < 0.55").fetchone()[
        0
    ]
    nulls = conn.execute(
        "SELECT count(*) FROM audio_segments s JOIN episode_items i ON i.item_id = s.id"
        " AND i.item_kind = 'segment' JOIN episodes e ON e.id = i.episode_id"
        " WHERE e.kind = 'conversation' AND s.speaker_id IS NULL"
    ).fetchone()[0]
    mixed = conn.execute(
        "SELECT count(*) FROM (SELECT segment_id FROM speaker_turns GROUP BY segment_id"
        " HAVING count(DISTINCT speaker_id) > 1)"
    ).fetchone()[0]
    assert stored >= 3
    assert nulls >= 2
    assert mixed >= 1
    assert sum(q.hedge for _, q in ev.ALL) >= 3


# --- the questions (#115, #116, #118, #127) -------------------------------------------------


def test_every_question_has_a_split_and_a_language() -> None:
    assert len(ev.QUESTIONS) == 29
    assert {q.split for q in ev.QUESTIONS} == {"dev"}
    assert {q.split for q in ev.HELDOUT} == {"heldout"}
    assert 18 <= len(ev.HELDOUT) <= 25
    assert {q.lang for _, q in ev.ALL} == {"en", "fr", "mixed"}
    assert {q.lang for q in ev.HELDOUT} == {"en", "fr", "mixed"}
    assert len({qid for qid, _ in ev.ALL}) == len(ev.ALL)


def test_every_abstain_question_names_plausible_distractors() -> None:
    abstain = [q for _, q in ev.ALL if q.abstain]
    assert abstain
    assert all(q.forbid for q in abstain)


def test_video_questions_forbid_other_day_and_other_kind_titles() -> None:
    first = ev.QUESTIONS[0]
    assert {"rust", "hollow depths", "clair de lune"} <= set(first.forbid)


def test_the_gold_answers_are_in_the_gold_episodes_and_nowhere_else() -> None:
    assert ev.validate(ev.ALL, TODAY) == []


def test_the_hard_set_is_valid_tagged_and_probes_both_query_kinds(tmp_path: Path) -> None:
    questions = hard.HARD_QUESTIONS
    assert {q.split for q in questions} == {"dev"}
    assert {q.lang for q in questions} == {"en", "fr"}
    assert all(q.forbid for q in questions if q.abstain)
    assert all(q.topic for q in questions)  # the gold semantic_query (#113)
    world = hard.hard_world(12)
    assert ev.validate(hard.HARD, hard.TODAY, world) == []
    assert ev.validate(hard.HARD, hard.TODAY, hard.hard_world(240)) == []
    conn = hard.db.connect(tmp_path / "hard.db", b"k" * 32)
    try:
        hard.db.migrate(conn)
        with hard.db.transaction(conn):
            ids = hard.build_world(conn, hard.TODAY, world)
        embedder = HashEmbedder()
        indexed = hard.backfill(conn, embedder)
        assert indexed["passages"] > 0
        assert indexed["production_index"]["episode_vectors"] == len(ids)
        assert indexed["production_index"]["segment_vectors"] == 0
        queries = {
            "question": [embedder.embed_query(q.text) for q in questions],
            "topic": [embedder.embed_query(q.topic) for q in questions],
        }
        cases = hard.retrieval_probe(conn, hard.TODAY, ids, 1, queries)["cases"]
    finally:
        conn.close()
    assert len(cases) == 2 * 2 * len(questions)
    assert {c["query"] for c in cases} == {"question", "topic"}


def test_the_held_out_set_is_frozen() -> None:
    assert scoring.definitions_sha256(ev.HELDOUT) == ev.HELDOUT_SHA256


def test_target_matrix_keeps_regression_clocks_threads_and_standalone_apart() -> None:
    matrix = ev.target_matrix()
    assert len(matrix) == 18
    assert {q.now for _, q in matrix} == {(0, 18, 0), (0, 9, 0), (0, 0, 40)}
    assert sum(not q.thread for _, q in matrix) == 6
    assert all(q.split == "dev" for _, q in matrix)
    assert len({i for i, _ in matrix}) == 18
    assert ev.validate(matrix, TODAY) == []


def test_the_held_out_set_covers_the_production_conditions() -> None:
    held = ev.HELDOUT
    assert {q.now for q in held} >= {(0, 18, 0), (0, 9, 0), (-1, 0, 40)}
    threads: dict[str, list[Any]] = {}
    for q in held:
        if q.thread:
            threads.setdefault(q.thread, []).append(q)
    assert any(
        group[0].people == ("Minlo",) and group[1].cite_all == ("bread_video", "storm_video")
        for group in threads.values()
    )
    assert any(q.abstain for q in held)
    assert any(q.count is not None for q in held)
    assert any(q.people and not q.abstain for q in held)  # a person and a topic


@pytest.mark.parametrize(
    ("qid", "old_alternatives"),
    [
        ("6", (("quatre", "4", "four"),)),
        ("8", (("500", "five hundred"),)),
        ("10", (("lumière", "light"), ("vite", "fast", "plus"))),
        (
            "29",
            (
                ("lisbon", "lisbonne"),
                ("ask.py", "akashic", "aurora", "wikipedia", "editor", "code"),
            ),
        ),
    ],
)
def test_the_old_alternatives_fail_validation(
    qid: str, old_alternatives: tuple[tuple[str, ...], ...]
) -> None:
    _, q = by_id(qid)
    old = scoring.Q(q.text, old_alternatives, cite=q.cite, cite_all=q.cite_all, allow=q.allow)
    assert ev.validate([(qid, old)], TODAY) != []


def test_a_non_answer_no_longer_passes_the_storm_question() -> None:
    _, q = by_id("10")
    answer = "Je n'en sais pas plus sur la lumière de cet éclair [1]."
    old = all(
        any(scoring.plain(a) in scoring.plain(answer) for a in g)
        for g in (("lumière", "light"), ("vite", "fast", "plus"))
    )
    assert old
    assert not scoring.judge(q, answer, ("storm_video",)).facts


# --- the harness up to the model call (#113, #126) -----------------------------------------


def test_a_thread_is_asked_in_one_conversation(memories: dict[tuple[int, int, int], Any]) -> None:
    memory = memories[ev.DEFAULT_NOW]
    chat = ScriptedChat(
        [
            plan(
                time_text="yesterday morning",
                people=["Minlo"],
                keywords=["game"],
                semantic_query="a game",
            ),
            plan(time_text="yesterday afternoon", kinds=["video"], semantic_query="video"),
        ],
        ["Minlo talked about Ashfall Frontier [1].", "I found nothing in your memory."],
    )
    embedder = HashEmbedder()
    said: list[str] = []
    questions = ev.selected({"h2"}, ev.ALL)
    assert [qid for qid, _ in questions] == ["h1", "h2"]
    results = ev.ask_all(memory, chat, embedder, questions, say=said.append)
    assert [r.qid for r in results] == ["h1", "h2"]
    rows = memory.conn.execute(
        "SELECT id, thread_id, state FROM questions ORDER BY id DESC LIMIT 2"
    ).fetchall()
    assert rows[0][1] == rows[1][0]  # the second question belongs to the first one's thread
    assert {r[2] for r in rows} == {"done"}
    assert "What did Minlo tell me yesterday morning" in chat.planned[1]  # earlier turn shown
    first = results[0]
    assert first.plan["semantic_query"] == "a game"
    assert "minlo_call" in first.sourced
    assert first.verdict.plan_ok
    json.dumps([ev.result_json(r) for r in results])
    assert len(said) == 2


@known_defect(175, "a thread's second target question inherits Minlo from the first")
def test_the_second_target_question_in_a_thread_plans_no_person(
    memories: dict[tuple[int, int, int], Any],
) -> None:
    memory = memories[ev.DEFAULT_NOW]
    chat = ScriptedChat(
        [
            plan(
                time_text="yesterday morning",
                people=["Minlo"],
                keywords=["game"],
                semantic_query="a game",
            ),
            plan(time_text="yesterday afternoon", kinds=["video"], semantic_query="video"),
        ],
        ["Minlo talked about Ashfall Frontier [1].", "Sourdough [1] and storms [2]."],
    )
    results = ev.ask_all(memory, chat, HashEmbedder(), ev.selected({"h2"}, ev.ALL), say=print)
    second = results[1]
    assert second.plan["people"] == []
    assert second.verdict.plan_ok


def test_questions_asked_at_night_use_that_clock(memories: dict[tuple[int, int, int], Any]) -> None:
    memory = memories[-1, 0, 40]
    qid, q = by_id("h8")
    chat = ScriptedChat([plan(time_text="tonight", kinds=["video"])] * 2, ["Sleep [1]."] * 2)
    results = ev.ask_all(memory, chat, HashEmbedder(), [(qid, q)], repeats=2, say=print)
    assert [r.repeat for r in results] == [0, 1]
    asked = memory.conn.execute("SELECT DISTINCT asked_ms FROM questions").fetchall()
    assert asked == [(memory.at_ms,)]


@known_defect(177, "a topic-less video question keeps 5 of the 8 videos in its window")
def test_the_count_question_gets_every_video_of_its_window(
    memories: dict[tuple[int, int, int], Any],
) -> None:
    memory = memories[ev.DEFAULT_NOW]
    qid, q = by_id("h10")
    chat = ScriptedChat(
        [plan(time_text="the day before yesterday in the afternoon", kinds=["video"])],
        ["You watched eight videos."],
    )
    result = ev.ask_all(memory, chat, HashEmbedder(), [(qid, q)], say=print)[0]
    assert result.verdict.plan_ok
    assert set(q.cite_all) <= set(result.sourced)


def test_the_cli_parses_the_new_options() -> None:
    args = ev.parser().parse_args(
        ["m", "--repeats", "3", "--output", "o.json", "--compare", "b.json"]
    )
    assert (args.repeats, args.output, args.compare) == (3, Path("o.json"), Path("b.json"))
    with pytest.raises(SystemExit) as exited:
        ev.main(["--help"])
    assert exited.value.code == 0


def test_the_cli_compares_two_stored_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base, new = tmp_path / "base.json", tmp_path / "new.json"
    row = {"id": "h1", "question": "q", "ok": False}
    base.write_text(json.dumps({"heldout_sha256": "x", "models": {"m": [row]}}), encoding="utf-8")
    new.write_text(
        json.dumps({"heldout_sha256": "x", "models": {"m": [{**row, "ok": True}]}}),
        encoding="utf-8",
    )
    assert ev.main(["--compare", str(base), "--against", str(new)]) == 0
    out = capsys.readouterr().out
    assert "m: 0/1 -> 1/1 passed runs" in out
    assert "h1 0/1 -> 1/1  q" in out


def test_the_cli_refuses_a_changed_held_out_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ev, "HELDOUT_SHA256", "0" * 64)
    assert ev.main(["some-model"]) == 2  # before any server or data folder is touched


def test_the_cli_needs_a_model() -> None:
    with pytest.raises(SystemExit) as exited:
        ev.main([])
    assert exited.value.code == 2


def test_speaker_comparisons_use_identical_labels_and_separate_wrong_from_unknown() -> None:
    labels = {"solo": "Minlo", "mixed": None, "stranger": None}
    assert quality.attribution_counts(
        labels, {"solo": "Minlo", "mixed": "Minlo", "stranger": None}
    ) == {
        "correct": 1,
        "wrong": 1,
        "unknown": 1,
        "total": 3,
    }
    with pytest.raises(ValueError, match="identical"):
        quality.attribution_counts(labels, {"solo": "Minlo"})


@pytest.mark.parametrize("case", quality.D9_CASES, ids=lambda c: c.key)
def test_d9_scorer_separates_possible_matches_from_factual_support(case: Any) -> None:  # noqa: ANN401 - script dataclass
    result = quality.evidence_score(
        case, factual_claim=True, possible_match=False, recording_link=False, cannot_answer=False
    )
    supported = case.key in {"clear_support", "independent_corroboration"}
    assert result["answer_protected"] is supported
    if not supported:
        assert result["unsupported_claims"] == 1
        protected = quality.evidence_score(
            case,
            factual_claim=False,
            possible_match=True,
            recording_link=True,
            cannot_answer=True,
            conflict_disclosed=True,
        )
        assert protected["answer_protected"]


def test_hallucination_detection_rejection_and_real_loss_are_separate() -> None:
    assert quality.hallucination_counts(
        {"ghost": True, "echo": True, "real": False, "quiet": False},
        {"ghost": "unclear", "echo": "reject", "real": "reject", "quiet": "unclear"},
    ) == {"hallucinations": 2, "detected": 2, "rejected": 1, "real_rejected": 1, "real_unclear": 1}


def test_capture_rows_builder_search_delay_and_content_free_backlog(conn: Any) -> None:  # noqa: ANN401 - SQLCipher fixture
    import time  # noqa: PLC0415

    from memory_data import add_job, add_media  # noqa: PLC0415

    from akashic.pipeline.episodes import build_episodes, horizon_ms  # noqa: PLC0415
    from akashic.search.hybrid import search_memory  # noqa: PLC0415
    from akashic.search.planner import Plan  # noqa: PLC0415

    now = ev.local_ms(TODAY, 0, 18)
    start = now - 3_600_000
    add_media(conn, "Independent lighthouse video", start, start + 120_000)
    add_job(conn, "transcribe", "pending-reference", start - 60_000)
    started = time.monotonic()
    build_episodes(conn, now)
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 0
    # M03 owns exposing these counters; here only a content-free measurement oracle is supplied.
    counts = conn.execute(
        "SELECT count(*),min(start_ms) FROM media_sessions WHERE closed=1"
    ).fetchone()
    assert counts == (1, start)
    assert now - counts[1] == 3_600_000
    assert now - horizon_ms(conn, now) == 3_660_000
    conn.execute("UPDATE jobs SET state='done'")
    assert build_episodes(conn, now)["media"] == 1
    found = search_memory(conn, Plan(kinds=("video",), keywords=("lighthouse",)), None, "fake")
    assert any("lighthouse" in e.title for e in found.episodes)
    assert time.monotonic() - started < 5  # local synthetic latency, not everyday freshness


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            c,
            id=c.key,
            marks=known_defect(186, "D9 uncertain evidence remains ordinary factual support"),
        )
        if c.key != "clear_support"
        else pytest.param(c, id=c.key)
        for c in quality.D9_CASES
    ],
)
def test_d9_current_ask_persistence_boundary(case: Any, tmp_path: Path) -> None:  # noqa: ANN401 - script labels
    world = tuple(
        ev.Ep(
            f"recording{i}",
            "conversation",
            1,
            (10, i * 5),
            2,
            "Discord call",
            "Discord.exe",
            people=("Minlo",),
            lines=(ev.Line(s.author, s.fact, confidence=0.9 if s.clear else 0.2),),
        )
        for i, s in enumerate(case.sources)
    )
    memory = ev.build_memory(tmp_path / "d9.db", TODAY, ev.DEFAULT_NOW, HashEmbedder(), world)
    if case.key == "duplicate_copies":
        # Two digest sightings refer to the same original transcript, not two independent clips.
        first, second = memory.conn.execute("SELECT id FROM audio_segments ORDER BY id").fetchall()
        memory.conn.execute(
            "UPDATE episode_items SET item_id=? WHERE item_kind='segment' AND item_id=?",
            (first[0], second[0]),
        )
        memory.conn.execute("DELETE FROM audio_segments WHERE id=?", (second[0],))
    reply = ("Minlo said " if case.author else "Apparently ") + case.fact + " [1]."
    chat = ScriptedChat([plan(keywords=["patch"], semantic_query="patch")], [reply])

    def ignore_line(_line: str) -> None:
        pass

    try:
        ev.ask_all(
            memory,
            chat,
            HashEmbedder(),
            [("d9", scoring.Q("What about the patch?"))],
            say=ignore_line,
        )
        saved = memory.conn.execute("SELECT answer,sources,state FROM questions").fetchone()
        assert saved is not None
        assert saved[2] == "done"
        sources = json.loads(saved[1])
        assert sources, "fixture must reach the source publication boundary"
        if case.key == "clear_support":
            assert case.fact in saved[0]
            assert any(case.fact in s["text"] for s in sources)
        elif case.key == "uncertain_author":
            assert "Minlo said" not in saved[0], "clear words do not establish the author"
        elif case.key == "conflict":
            assert "conflict" in saved[0].lower(), "clear/flagged conflict must be disclosed"
        else:
            # Persist a separate role so the UI/follow-up cannot promote a possible match.
            possible = [s for s in sources if s.get("role") == "possible_match"]
            assert possible
            assert any(s["kind"] == "line" and s["id"] > 0 for s in possible)
            if case.key != "independent_corroboration":
                assert saved[0] != reply, "a hedge cannot turn a possible match into a fact"
                assert re.search(
                    r"cannot|can't|too uncertain|not reliable", saved[0], re.IGNORECASE
                )
    finally:
        memory.conn.close()
