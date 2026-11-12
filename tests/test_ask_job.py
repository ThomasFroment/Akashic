"""The ask job: plan, search and answer one question, with fake models."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
import pytest
from ask_fakes import FakeChat, FakeEmbedder, ask, make
from defects import known_defect
from memory_data import HOUR, MINUTE, NOW, add_episode, add_segment, add_speaker

from akashic.pipeline.ask import ask_job, plan_json, stored_plan
from akashic.pipeline.jobs import Job, JobQueue
from akashic.pipeline.llm import LlamaError
from akashic.search.planner import Plan
from akashic.storage import db, embeddings

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
