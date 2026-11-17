"""The ask job: plan, search and answer one question, with fake models."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest
import sqlcipher3
from ask_fakes import PLAN_OK, FakeChat, FakeEmbedder, FlushClock, ask, make
from defects import known_defect
from memory_data import HOUR, MINUTE, NOW, add_episode, add_segment, add_speaker

from akashic.pipeline.ask import AskHandler, ask_job, plan_json, stored_plan
from akashic.pipeline.llm import LlamaError
from akashic.search.planner import Plan
from akashic.search.timeparse import TimeRange
from akashic.storage import db, embeddings
from akashic.storage.gaps import GapLog, GapSpan
from akashic.storage.jobs import Job, JobQueue

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]


def row(conn: Connection, job: Job) -> dict[str, object]:
    qid = job.payload["question_id"]
    r = conn.execute(
        "SELECT state, step, plan, answer, sources, model, error, finished_ms"
        " FROM questions WHERE id = ?",
        (qid,),
    ).fetchone()
    return dict(
        zip(
            ["state", "step", "plan", "answer", "sources", "model", "error", "finished"],
            r,
            strict=True,
        )
    )


def world(conn: Connection) -> tuple[int, int]:
    """A call with Minlo about a game, and what was said in it."""
    minlo = add_speaker(conn, "Minlo")
    call = add_episode(
        conn, "conversation", NOW - 3 * HOUR, NOW - 2 * HOUR, "Discord call", "game talk",
        people=(minlo,),
    )  # fmt: skip
    add_segment(conn, NOW - 3 * HOUR + MINUTE, text="there is a new game next month", speaker=minlo)
    return call, minlo


def test_a_question_is_planned_searched_and_answered_with_citations(conn: Connection) -> None:
    world(conn)
    handler, chat, _ = make(conn)
    job = ask(conn)
    handler.run(job)
    done = row(conn, job)
    assert done["state"] == "done"
    assert done["step"] == ""
    assert done["answer"] == "He talked about a game [1]."
    assert done["model"] == "chat-model"
    assert done["finished"] == NOW
    sources = json.loads(str(done["sources"]))
    assert sources[0]["n"] == 1
    assert {s["kind"] for s in sources} >= {"episode", "line"}
    plan = json.loads(str(done["plan"]))
    assert plan["people"] == ["Minlo"]
    assert plan["kinds"] == ["conversation"]
    assert plan["semantic"] is False  # no vectors exist yet
    system, user = chat.prompts[0]
    assert "<sources>" in user
    assert "there is a new game next month" in user
    assert "data" in system


def test_nothing_found_answers_without_calling_the_model(conn: Connection) -> None:
    handler, chat, _ = make(conn)
    job = ask(conn, "Qu'est-ce que j'ai regardé hier ?")
    handler.run(job)
    done = row(conn, job)
    assert done["state"] == "done"
    assert str(done["answer"]).startswith("Je n'ai rien trouvé")
    assert done["sources"] == "[]"
    assert done["model"] is None
    assert chat.prompts == []
    assert chat.plans_asked == 1


def test_a_failing_planner_falls_back_to_the_rules(conn: Connection) -> None:
    world(conn)
    handler, chat, _ = make(conn, FakeChat(plan=LlamaError()))
    job = ask(conn)
    handler.run(job)
    assert row(conn, job)["state"] == "done"
    assert json.loads(str(row(conn, job)["plan"]))["people"] == ["Minlo"]  # named in the question
    assert chat.prompts


def test_the_planned_topic_is_embedded_when_vectors_exist(conn: Connection) -> None:
    call, _ = world(conn)
    embeddings.put(conn, "episode", call, "emb", np.array([1.0, 0.0], dtype=np.float32), NOW, NOW)
    handler, _, embedder = make(conn)
    job = ask(conn)
    handler.run(job)
    assert embedder.queries == ["a game"]
    assert json.loads(str(row(conn, job)["plan"]))["semantic"] is True
    assert json.loads(str(row(conn, job)["plan"]))["semantic_query"] == "a game"


def test_saved_planned_topic_round_trips_and_old_plans_remain_readable() -> None:
    plan = Plan(apps=("zen.exe",), semantic_query="backup privacy")
    assert stored_plan(plan_json(plan, semantic=True)) == plan
    assert stored_plan('{"apps": ["zen.exe"]}') == Plan(apps=("zen.exe",))


@pytest.mark.parametrize(
    "embedder",
    [FakeEmbedder(available=False), FakeEmbedder(fail=True)],
    ids=["no embedder", "embedder fails"],
)
def test_without_a_working_embedder_the_search_is_by_words(
    conn: Connection, embedder: FakeEmbedder
) -> None:
    call, _ = world(conn)
    embeddings.put(conn, "episode", call, "emb", np.array([1.0, 0.0], dtype=np.float32), NOW, NOW)
    handler, _, _ = make(conn, embedder=embedder)
    job = ask(conn)
    handler.run(job)
    assert row(conn, job)["state"] == "done"
    assert json.loads(str(row(conn, job)["plan"]))["semantic"] is False


def test_a_stream_that_breaks_is_an_error_to_retry_and_then_a_failed_question(
    conn: Connection,
) -> None:
    world(conn)
    handler, _, _ = make(conn, FakeChat(("Partial",), stream_error=True))
    job = ask(conn)
    with pytest.raises(LlamaError):
        handler.run(job)
    assert row(conn, job)["state"] == "working"
    handler.give_up(job)
    failed = row(conn, job)
    assert failed["state"] == "failed"
    assert failed["error"] == "the model could not answer"
    handler.run(job)  # a retry after giving up does nothing to a finished question
    assert row(conn, job)["state"] == "failed"


def test_the_handler_says_what_it_is_waiting_for(conn: Connection) -> None:
    assert make(conn)[0].blocked() is None
    assert "llama-server" in str(make(conn, FakeChat(available=False))[0].blocked())
    assert "chat model" in str(make(conn, chat_present=False)[0].blocked())
    handler = make(conn)[0]
    assert not handler.heavy  # the user is waiting: the scheduler does not hold it back
    handler.idle()


def test_idle_lets_the_model_unload(conn: Connection) -> None:
    handler, chat, _ = make(conn)
    handler.idle()
    assert chat.unloads == 1


def test_the_job_is_queued_once_per_question(conn: Connection) -> None:
    queue = JobQueue(conn)
    assert queue.enqueue(ask_job(3), NOW)
    assert not queue.enqueue(ask_job(3), NOW)


def follow_up(conn: Connection, thread: int, text: str) -> Job:
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO questions (asked_ms, text, thread_id) VALUES (?, ?, ?)",
            (NOW, text, thread),
        )
    assert cursor.lastrowid is not None
    new = ask_job(cursor.lastrowid)
    return Job(2, new.kind, new.key, new.payload, new.data_ms, 1)


def test_a_follow_up_is_understood_with_the_earlier_turns(conn: Connection) -> None:
    world(conn)

    handler, chat, _ = make(conn)
    first = ask(conn)
    handler.run(first)
    root = first.payload["question_id"]
    assert isinstance(root, int)
    # The follow-up names nobody and no kind: the conversation's filter carries over.
    chat.plan = json.dumps(
        {"time_text": "", "people": [], "kinds": [], "keywords": [], "semantic_query": ""}
    )
    second = follow_up(conn, root, "and when was that?")
    handler.run(second)
    plan = json.loads(str(row(conn, second)["plan"]))
    assert plan["people"] == ["Minlo"]
    assert plan["kinds"] == ["conversation"]
    assert plan["keywords"] == ["game"]
    _, user = chat.prompts[-1]
    assert "<conversation>" in user
    assert "<asked>what did Minlo say about a game?</asked>" in user
    assert "<answered>He talked about a game.</answered>" in user  # old [1] is not a source now
    assert user.index("</conversation>") < user.index("<question>\nand when was that?")


@known_defect(175, "a follow-up embeds 'previous\\nnew' instead of the planned topic")
def test_a_follow_up_vector_embeds_the_planned_topic(conn: Connection) -> None:
    call, _ = world(conn)
    embeddings.put(conn, "episode", call, "emb", np.array([1.0, 0.0], dtype=np.float32), NOW, NOW)
    handler, _, vectors = make(conn)
    first = ask(conn, "what did Minlo say?")
    handler.run(first)
    root = first.payload["question_id"]
    assert isinstance(root, int)
    second = follow_up(conn, root, "and then?")
    handler.run(second)
    planned = json.loads(str(row(conn, second)["plan"]))["semantic_query"]
    assert vectors.queries[-1] == planned
    assert "\n" not in vectors.queries[-1]


def test_day_overview_passes_sample_coverage_through_the_real_ask_path(conn: Connection) -> None:
    for i in range(13):
        add_episode(
            conn,
            "work",
            NOW + i * MINUTE,
            NOW + (i + 1) * MINUTE,
            f"Activity {i}",
            "A distinct supported topic.",
        )
    chat = FakeChat(
        plan=json.dumps(
            {
                "time_text": "today",
                "people": [],
                "kinds": ["work"],
                "keywords": ["activities"],
                "semantic_query": "activities",
            }
        )
    )
    handler, _, _ = make(conn, chat)
    job = ask(conn, "Summarize what I did today.")
    handler.run(job)
    done = row(conn, job)
    assert done["state"] == "done"
    assert json.loads(str(done["plan"]))["overview"]
    sources = json.loads(str(done["sources"]))
    assert len(sources) == 10
    assert "10 of 13 matching episodes" in chat.prompts[-1][1]
    assert "This is a sample" in chat.prompts[-1][1]


def test_saved_answer_tracks_neighbour_speech_and_inherited_evidence(conn: Connection) -> None:
    _, minlo = world(conn)
    neighbour = add_segment(
        conn, NOW - 3 * HOUR + MINUTE + 5000, text="secret neighbour", speaker=minlo
    )
    handler, _, _ = make(conn)
    first = ask(conn)
    handler.run(first)
    root = first.payload["question_id"]
    assert isinstance(root, int)
    sources = json.loads(str(row(conn, first)["sources"]))
    assert any(neighbour in source["segment_ids"] for source in sources)
    second = follow_up(conn, root, "and then?")
    handler.run(second)
    # Inherited dependencies survive independently forgetting the earlier follow-up.
    third = follow_up(conn, root, "tell me more")
    handler.run(third)
    conn.execute("DELETE FROM questions WHERE id = ?", (second.payload["question_id"],))
    assert conn.execute(
        "SELECT 1 FROM question_segments WHERE question_id = ? AND segment_id = ?",
        (third.payload["question_id"], neighbour),
    ).fetchone() == (1,)
    conn.execute("UPDATE audio_segments SET transcript = NULL WHERE id = ?", (neighbour,))
    assert row(conn, first)["answer"] == row(conn, third)["answer"] == ""


def test_episode_sources_track_speech_even_when_it_is_not_a_line_hit(conn: Connection) -> None:
    call, _ = world(conn)
    hidden = add_segment(conn, NOW - 2 * HOUR, text="unmatched distant utterance")
    conn.execute("INSERT INTO episode_items VALUES (?, 'segment', ?)", (call, hidden))
    handler, _, _ = make(conn)
    job = ask(conn)
    handler.run(job)
    assert conn.execute(
        "SELECT 1 FROM question_segments WHERE question_id = ? AND segment_id = ?",
        (job.payload["question_id"], hidden),
    ).fetchone() == (1,)
    conn.execute("DELETE FROM episodes WHERE id = ?", (call,))
    conn.execute("DELETE FROM audio_segments WHERE id = ?", (hidden,))
    assert row(conn, job)["answer"] == ""


@pytest.mark.parametrize("phase", ["planning", "streaming"])
def test_deletion_during_generation_cannot_restore_answer_or_sources(
    conn: Connection, phase: str
) -> None:
    world(conn)
    handler, chat, vectors = make(conn)
    job = ask(conn)

    def expire() -> None:
        conn.execute("UPDATE audio_segments SET transcript = NULL")

    if phase == "planning":
        # Called between the initial row read and evidence publication.
        def expire_before_search(text: str) -> Vector:
            del text
            expire()
            return np.ones(2, dtype=np.float32)

        vectors.embed_query = expire_before_search
        call = int(conn.execute("SELECT id FROM episodes LIMIT 1").fetchone()[0])
        embeddings.put(conn, "episode", call, "emb", np.ones(2, dtype=np.float32), NOW, NOW)
    else:
        chat.on_piece = expire
    handler.run(job)
    stored = row(conn, job)
    assert (stored["state"], stored["answer"], stored["sources"], stored["plan"]) == (
        "failed",
        "",
        "[]",
        None,
    )
    assert "transcripts changed" in str(stored["error"])
    handler.give_up(job)
    assert row(conn, job)["error"] == stored["error"]


def at(hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, 17, hour, minute).astimezone().timestamp() * 1000)


WINDOW = TimeRange(at(14), at(15), "14:00-15:00")
LATER = at(16)
TIMED_PLAN = json.dumps({**json.loads(PLAN_OK), "kinds": [], "time_text": "at 14:30"})


def add_gap(conn: Connection, kind: str, reason: str, start: int, end: int | None) -> None:
    conn.execute(
        "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, ?, ?, ?)",
        (kind, reason, start, end),
    )


def not_recorded(conn: Connection, plan: Plan, now: int = LATER) -> list[GapSpan]:
    return make(conn)[0]._not_recorded(plan, now)


def pairs(spans: list[GapSpan]) -> list[tuple[str, str]]:
    return [(s.kind, s.reason) for s in spans]


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (("conversation",), {"mic", "system"}),
        (("video",), {"system"}),
        (("music",), {"system"}),
        (("browsing",), {"screen", "screen_text"}),
        (("work",), {"screen", "screen_text"}),
        (("game",), {"screen", "screen_text"}),
        (("video", "game"), {"system", "screen", "screen_text"}),
        ((), {"mic", "system", "screen", "screen_text"}),
        (("nonsense",), set[str]()),
    ],
)
def test_the_planned_kinds_choose_which_gap_kinds_are_read(
    conn: Connection, kinds: tuple[str, ...], expected: set[str]
) -> None:
    for kind in ("all", "screen", "screen_text", "mic", "system"):
        add_gap(conn, kind, "paused", at(14, 10), at(14, 20))
    found = not_recorded(conn, Plan(time=WINDOW, kinds=kinds))
    assert {s.kind for s in found} == expected | {"all"}


def test_gaps_of_other_kinds_are_not_listed(conn: Connection) -> None:
    add_gap(conn, "screen", "locked", at(14, 10), at(14, 20))
    add_gap(conn, "mic", "device_missing", at(14, 10), at(14, 20))
    assert not_recorded(conn, Plan(time=WINDOW, kinds=("conversation",))) == [
        GapSpan("mic", "device_missing", at(14, 10), at(14, 20))
    ]
    assert not_recorded(conn, Plan(time=WINDOW, kinds=("video",))) == []


def test_excluded_music_and_the_open_ui_are_left_out(conn: Connection) -> None:
    add_gap(conn, "all", "excluded", at(14, 0), at(14, 10))
    add_gap(conn, "system", "music", at(14, 0), at(14, 10))
    add_gap(conn, "all", "ui_open", at(14, 20), at(14, 30))  # closed: stays
    add_gap(conn, "mic", "ui_open", at(14, 40), None)  # open at now
    for reason in ("backlog", "write_failed", "unknown_sources", "voice_elsewhere", "stopped"):
        add_gap(conn, "system", reason, at(14, 50), at(14, 55))
    found = not_recorded(conn, Plan(time=WINDOW, kinds=("conversation",)))
    assert sorted(pairs(found)) == [
        ("all", "ui_open"),
        ("system", "backlog"),
        ("system", "stopped"),
        ("system", "unknown_sources"),
        ("system", "voice_elsewhere"),
        ("system", "write_failed"),
    ]


def test_spans_are_clipped_to_the_window_and_merged_per_kind_and_reason(conn: Connection) -> None:
    add_gap(conn, "mic", "write_failed", at(13, 50), at(14, 10))
    add_gap(conn, "mic", "write_failed", at(14, 10), at(14, 20))  # touches
    add_gap(conn, "mic", "write_failed", at(14, 15), at(14, 25))  # overlaps
    add_gap(conn, "mic", "write_failed", at(14, 40), at(14, 45))  # apart
    add_gap(conn, "system", "write_failed", at(14, 30), at(15, 30))
    found = not_recorded(conn, Plan(time=WINDOW, kinds=("conversation",)))
    assert found == [
        GapSpan("mic", "write_failed", at(14, 0), at(14, 25)),
        GapSpan("system", "write_failed", at(14, 30), at(15, 0)),
        GapSpan("mic", "write_failed", at(14, 40), at(14, 45)),
    ]


def test_without_a_time_window_gaps_are_not_read(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object) -> list[GapSpan]:
        raise AssertionError("gaps read")

    monkeypatch.setattr("akashic.pipeline.ask.gaps.overlapping", boom)
    assert not_recorded(conn, Plan()) == []


@pytest.mark.parametrize("error", [sqlite3.OperationalError, sqlcipher3.OperationalError])
def test_a_failing_gap_read_is_an_empty_list_and_logs_only_the_type(
    conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: type[Exception],
) -> None:
    def broken(*_: object) -> list[GapSpan]:
        raise error("secret detail")

    monkeypatch.setattr("akashic.pipeline.ask.gaps.overlapping", broken)
    with caplog.at_level(logging.WARNING):
        assert not_recorded(conn, Plan(time=WINDOW)) == []
    assert "OperationalError" in caplog.text
    assert "secret detail" not in caplog.text


def run_at_four(conn: Connection, question: str, chat: FakeChat) -> tuple[str, str]:
    """The (answer, prompt) of a question asked with the clock at 16:00."""
    handler = AskHandler(
        conn, chat, FakeEmbedder(), lambda: True, lambda: True, lambda: LATER, FlushClock()
    )
    job = ask(conn, question)
    handler.run(job)
    prompt = chat.prompts[0][1] if chat.prompts else ""
    return str(row(conn, job)["answer"]), prompt


def three_gaps(conn: Connection) -> None:
    log = GapLog(conn)
    log.add("all", "paused", at(14, 5), at(14, 15))
    log.add("mic", "device_missing", at(14, 20), at(14, 40))
    log.add("system", "voice_elsewhere", at(14, 30), at(14, 50))


def test_a_timed_question_with_nothing_found_lists_every_gap_of_the_window(
    conn: Connection,
) -> None:
    three_gaps(conn)
    answer, prompt = run_at_four(conn, "what happened at 14:30", FakeChat(plan=TIMED_PLAN))
    assert answer.startswith("I found nothing in your memory")
    assert prompt == ""
    assert "2026-11-17 14:05 - 14:15 (everything: recording paused)" in answer
    assert "2026-11-17 14:20 - 14:40 (microphone: device missing)" in answer
    assert "(system sound: speech lost, voice chat on another device)" in answer
    assert answer.index("14:05") < answer.index("14:20") < answer.index("14:30")


def test_a_timed_question_with_evidence_gets_the_line_after_searched(conn: Connection) -> None:
    three_gaps(conn)
    add_segment(conn, at(14, 10), text="there is a new game next month")
    _, prompt = run_at_four(conn, "what happened at 14:30", FakeChat(plan=TIMED_PLAN))
    lines = prompt.splitlines()
    i = next(n for n, text in enumerate(lines) if text.startswith("Searched:"))
    assert lines[i + 1].startswith("Not recorded (")
    for words in ("everything: recording paused", "microphone: device missing", "voice chat"):
        assert words in lines[i + 1]


def test_the_same_database_without_a_time_window_has_no_line(conn: Connection) -> None:
    three_gaps(conn)
    answer, _ = run_at_four(conn, "what did Minlo say", FakeChat())
    assert answer == "I found nothing in your memory that answers that question."
    add_segment(conn, at(14, 10), text="there is a new game next month")
    _, prompt = run_at_four(conn, "what did Minlo say", FakeChat())
    assert prompt
    assert "Not recorded" not in prompt


def test_an_empty_model_answer_falls_back_to_the_reply_with_the_gaps(conn: Connection) -> None:
    three_gaps(conn)
    add_segment(conn, at(14, 10), text="there is a new game next month")
    answer, prompt = run_at_four(conn, "what happened at 14:30", FakeChat(("",), plan=TIMED_PLAN))
    assert prompt
    assert answer.startswith("I found nothing")
    assert "Not recorded: 2026-11-17 14:05" in answer


@pytest.mark.parametrize("error", [sqlite3.OperationalError, sqlcipher3.OperationalError])
def test_a_failing_gap_read_still_answers_without_a_line(
    conn: Connection, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    three_gaps(conn)

    def broken(*_: object) -> list[GapSpan]:
        raise error

    monkeypatch.setattr("akashic.pipeline.ask.gaps.overlapping", broken)
    answer, _ = run_at_four(conn, "what happened at 14:30", FakeChat(plan=TIMED_PLAN))
    assert answer == "I found nothing in your memory that answers that question."
