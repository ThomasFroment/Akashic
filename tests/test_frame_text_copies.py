"""Screen text is a derived copy too; removing it must clear every saved answer citing it.

Mirrors tests/test_transcript_copies.py for windows: expiry, replacement and deletion invalidate
the answer that cited a window and every later reply in its thread, in the same transaction,
while user questions, unrelated answers and screenshot-only expiry stay untouched.
"""

from __future__ import annotations

import http.client
import io
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

import pytest
from ask_fakes import FakeChat, ask, make
from memory_data import (
    HOUR,
    NOW,
    add_episode,
    add_frame,
    add_monitor,
    add_window,
    app_window,
    new_frame,
    window_of,
)
from PIL import Image

from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import DEFAULT_CONFIG_TOML
from akashic.pipeline.ask import ask_job
from akashic.storage import db
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.jobs import Job
from akashic.storage.retention import (
    PURGERS,
    delete_all_recorded_data,
    delete_spent_frames,
    expire_frame_text,
    expire_screenshots,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.storage.crypto import DataKey
    from akashic.storage.media import MediaStore

CLEARED = ("failed", "", "[]")


def saved_answer(
    conn: Connection,
    window: int | None,
    ts_ms: int = NOW,
    *,
    thread: int | None = None,
    lineage: bool = True,
) -> int:
    """An answer citing the frame of ``window`` (lineage is kept per window)."""
    sources: list[dict[str, object]] = []
    if window is not None:
        (frame,) = conn.execute(
            "SELECT frame_id FROM frame_windows WHERE id = ?", (window,)
        ).fetchone()
        sources = [
            {"kind": "frame", "id": frame, "window_id": window, "start_ms": ts_ms, "text": "x"}
        ]
    cursor = conn.execute(
        "INSERT INTO questions (asked_ms, text, state, answer, sources, plan, thread_id)"
        " VALUES (0, 'What did I read?', 'done', 'secret screen answer [1]', ?, '{}', ?)",
        (json.dumps(sources), thread),
    )
    assert cursor.lastrowid is not None
    if window is not None and lineage:
        conn.execute("INSERT INTO question_windows VALUES (?, ?)", (cursor.lastrowid, window))
    return cursor.lastrowid


def row(conn: Connection, question: int) -> tuple[str, str, str]:
    r = conn.execute("SELECT state, answer, sources FROM questions WHERE id = ?", (question,))
    return r.fetchone()


def intact(conn: Connection, question: int) -> bool:
    return row(conn, question)[:2] == ("done", "secret screen answer [1]")


def count(conn: Connection, sql: str, *params: object) -> int:
    value: tuple[int] = conn.execute(sql, params).fetchone()
    return value[0]


@pytest.fixture
def thread(conn: Connection) -> dict[str, int]:
    frame = add_frame(conn, NOW - 3 * HOUR, "secretphrase page", "Page")
    other_frame = add_frame(conn, NOW - 2 * HOUR, "retainedphrase autre", "Autre")
    window, other = window_of(conn, frame), window_of(conn, other_frame)
    root = saved_answer(conn, window, NOW - 3 * HOUR)
    reply = saved_answer(conn, other, NOW - 2 * HOUR, thread=root)  # cites a different window
    unrelated = saved_answer(conn, other, NOW - 2 * HOUR)
    episode_only = saved_answer(conn, None)
    return {
        "frame": frame,
        "window": window,
        "other_frame": other_frame,
        "other": other,
        "root": root,
        "reply": reply,
        "unrelated": unrelated,
        "episode_only": episode_only,
    }


# --- lifecycle ----------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["expire", "category", "replace", "delete"])
def test_changing_frame_text_clears_the_answer_and_later_replies_only(
    conn: Connection, media: MediaStore, thread: dict[str, int], action: str
) -> None:
    window = thread["window"]
    if action == "expire":
        assert expire_frame_text(conn, media, NOW - 3 * HOUR + 1) == 1
    elif action == "category":
        assert PURGERS["frame_text"](conn, media, NOW + 1) == 2  # everything past the cutoff
    elif action == "replace":
        conn.execute("UPDATE frame_windows SET text = 'replacementphrase' WHERE id = ?", (window,))
    else:
        conn.execute("DELETE FROM frames WHERE id = ?", (thread["frame"],))
    question_text = "What did I read?"
    for cleared in (thread["root"], thread["reply"]):
        assert row(conn, cleared) == CLEARED
        assert conn.execute("SELECT text FROM questions WHERE id = ?", (cleared,)).fetchone() == (
            question_text,
        )  # the user's question stays
        assert conn.execute("SELECT plan FROM questions WHERE id = ?", (cleared,)).fetchone() == (
            None,
        )
    if action == "category":
        assert intact(conn, thread["episode_only"])  # cites no frame
        assert row(conn, thread["unrelated"]) == CLEARED  # cites the other, expired frame too
    else:
        assert intact(conn, thread["unrelated"])
        assert intact(conn, thread["episode_only"])
    assert count(conn, "SELECT count(*) FROM question_windows WHERE window_id = ?", window) == 0


def test_the_cleared_answer_leaves_no_excerpt_anywhere_in_the_row(
    conn: Connection, media: MediaStore, thread: dict[str, int]
) -> None:
    expire_frame_text(conn, media, NOW - 3 * HOUR + 1)
    dump = conn.execute(
        "SELECT answer || sources || coalesce(plan, '') || coalesce(error, '')"
        " FROM questions WHERE id IN (?, ?)",
        (thread["root"], thread["reply"]),
    ).fetchall()
    assert all("secret" not in cell.casefold() and "excerpt" not in cell for (cell,) in dump)


def test_screenshot_only_expiry_leaves_answers_untouched(
    conn: Connection, media: MediaStore, key: DataKey
) -> None:
    add_monitor(conn)
    frames = FrameStore(conn, media)
    frame = frames.add(new_frame(NOW - 3 * HOUR, text="keptphrase", image=b"img"))
    window = window_of(conn, frame)
    root = saved_answer(conn, window, NOW - 3 * HOUR)
    reply = saved_answer(conn, window, NOW - 3 * HOUR, thread=root)
    assert expire_screenshots(conn, media, NOW) == 1
    assert intact(conn, root)
    assert intact(conn, reply)
    assert count(conn, "SELECT count(*) FROM question_windows") == 2
    assert conn.execute("SELECT text FROM frame_windows WHERE id = ?", (window,)).fetchone() == (
        "keptphrase",
    )
    del key


def test_unchanged_text_and_first_time_text_do_not_invalidate(
    conn: Connection, thread: dict[str, int]
) -> None:
    conn.execute("UPDATE frame_windows SET text = text, title = 'renamed'")
    pending = window_of(conn, add_frame(conn, NOW, "x"))
    conn.execute(
        "UPDATE frame_windows SET text = NULL, text_source = NULL WHERE id = ?", (pending,)
    )
    conn.execute("UPDATE frame_windows SET text = 'ocr result' WHERE id = ?", (pending,))
    for question in (thread["root"], thread["reply"], thread["unrelated"]):
        assert intact(conn, question)


def test_deleting_a_frame_after_its_text_expired_changes_nothing_more(
    conn: Connection, media: MediaStore, thread: dict[str, int]
) -> None:
    expire_frame_text(conn, media, NOW + 1)
    assert row(conn, thread["episode_only"])[:2] == ("done", "secret screen answer [1]")
    assert delete_spent_frames(conn, NOW + 1) == 2
    assert count(conn, "SELECT count(*) FROM questions") == 4
    assert count(conn, "SELECT count(*) FROM question_windows") == 0


def test_a_failed_text_removal_restores_every_copy(
    conn: Connection, thread: dict[str, int]
) -> None:
    def interrupted() -> None:
        with db.transaction(conn):
            conn.execute("UPDATE frame_windows SET text = NULL WHERE id = ?", (thread["window"],))
            assert row(conn, thread["root"]) == CLEARED  # the trigger ran in this transaction
            raise RuntimeError("interrupt")

    with pytest.raises(RuntimeError, match="interrupt"):
        interrupted()
    assert intact(conn, thread["root"])
    assert intact(conn, thread["reply"])
    assert (
        count(conn, "SELECT count(*) FROM question_windows WHERE window_id = ?", thread["window"])
        == 1
    )


# --- lineage recorded by the Ask job -------------------------------------------------------


RECIPE_PLAN = json.dumps(
    {
        "time_text": "",
        "people": [],
        "kinds": [],
        "keywords": ["fictionalsecret"],
        "semantic_query": "fictionalsecret",
    }
)


def recipe_chat(on_piece: Callable[[], None] | None = None) -> FakeChat:
    """Answers from the screen text; ``on_piece`` runs while the answer streams."""
    chat = FakeChat(pieces=("Tu as lu une recette [1].",), plan=RECIPE_PLAN)
    if on_piece is not None:
        chat.on_piece = on_piece
    return chat


def run_ask(conn: Connection, chat: FakeChat) -> int:
    handler, _, _ = make(conn, chat)
    job = ask(conn, "quelle recette fictionalsecret ai-je lue ?")
    handler.run(job)
    return cast("int", job.payload["question_id"])


def test_the_ask_job_records_the_windows_it_cited(conn: Connection) -> None:
    frame = add_frame(conn, NOW - 2 * HOUR, "fictionalsecret recette des crêpes", "Recettes")
    window = window_of(conn, frame)
    question = run_ask(conn, recipe_chat())
    assert row(conn, question)[0] == "done"
    assert conn.execute(
        "SELECT window_id FROM question_windows WHERE question_id = ?", (question,)
    ).fetchall() == [(window,)]
    conn.execute("UPDATE frame_windows SET text = 'autre' WHERE id = ?", (window,))
    assert row(conn, question) == CLEARED


def test_a_follow_up_job_copies_the_lineage_of_the_thread_onto_itself(conn: Connection) -> None:
    frame = add_frame(conn, NOW - 2 * HOUR, "fictionalsecret recette des crêpes", "Recettes")
    window = window_of(conn, frame)
    first = run_ask(conn, recipe_chat())
    # The second turn cites an episode only, so its windows can only be inherited.
    add_episode(conn, "video", NOW - HOUR, NOW - HOUR + 60_000, "zzzunmatched video")
    other_plan = json.dumps({"keywords": ["zzzunmatched"], "semantic_query": "zzzunmatched"})
    handler, _, _ = make(conn, FakeChat(pieces=("Une vidéo [1].",), plan=other_plan))
    second = cast(
        "int",
        conn.execute(
            "INSERT INTO questions (asked_ms, text, thread_id) VALUES (?, ?, ?)",
            (NOW, "et ensuite ? zzzunmatched", first),
        ).lastrowid,
    )
    new = ask_job(second)
    handler.run(Job(2, new.kind, new.key, new.payload, new.data_ms, 1))
    for question in (first, second):
        assert conn.execute(
            "SELECT window_id FROM question_windows WHERE question_id = ?", (question,)
        ).fetchall() == [(window,)]


def test_a_follow_up_inherits_the_lineage_of_earlier_turns(conn: Connection) -> None:
    frame = add_frame(conn, NOW - 2 * HOUR, "fictionalsecret recette des crêpes", "Recettes")
    first = run_ask(conn, recipe_chat())
    cursor = conn.execute(
        "INSERT INTO questions (asked_ms, text, thread_id) VALUES (?, 'et ensuite ?', ?)",
        (NOW, first),
    )
    follow_up = cursor.lastrowid
    assert follow_up is not None
    # The follow-up ends up with no window of its own but the first turn's windows.
    conn.execute(
        "UPDATE questions SET state = 'done', answer = 'suite', sources = '[]' WHERE id = ?",
        (follow_up,),
    )
    conn.execute("DELETE FROM frames WHERE id = ?", (frame,))
    assert row(conn, first) == CLEARED
    assert row(conn, follow_up) == CLEARED  # later reply in the thread


def test_a_question_in_flight_cannot_publish_text_removed_meanwhile(conn: Connection) -> None:
    frame = add_frame(conn, NOW - 2 * HOUR, "fictionalsecret recette des crêpes", "Recettes")
    window = window_of(conn, frame)

    def remove_text() -> None:
        conn.execute(
            "UPDATE frame_windows SET text = NULL, text_source = NULL WHERE id = ?", (window,)
        )

    question = run_ask(conn, recipe_chat(remove_text))
    state, answer, sources = row(conn, question)
    assert (state, answer, sources) == CLEARED
    assert "fictionalsecret" not in answer + sources


def test_queued_and_working_questions_are_failed_when_text_changes(conn: Connection) -> None:
    window = window_of(conn, add_frame(conn, NOW, "secretphrase"))
    queued = conn.execute("INSERT INTO questions (asked_ms, text) VALUES (0, 'q')").lastrowid
    working = conn.execute(
        "INSERT INTO questions (asked_ms, text, state) VALUES (0, 'w', 'working')"
    ).lastrowid
    done = saved_answer(conn, None)
    assert queued is not None
    assert working is not None
    conn.execute("UPDATE frame_windows SET text = 'new' WHERE id = ?", (window,))
    assert row(conn, queued)[0] == "failed"
    assert row(conn, working)[0] == "failed"
    assert intact(conn, done)


# --- full deletion --------------------------------------------------------------------------


@pytest.mark.usefixtures("thread")
def test_full_deletion_leaves_the_lineage_table_empty(
    conn: Connection, media: MediaStore, paths: Paths
) -> None:
    assert count(conn, "SELECT count(*) FROM question_windows") > 0
    delete_all_recorded_data(conn, media)
    assert count(conn, "SELECT count(*) FROM question_windows") == 0
    assert count(conn, "SELECT count(*) FROM questions") == 0
    assert list(paths.media_dir.iterdir()) == []


def test_question_deletion_cascades_to_its_lineage(
    conn: Connection, thread: dict[str, int]
) -> None:
    conn.execute("DELETE FROM questions WHERE id = ?", (thread["root"],))
    assert (
        count(conn, "SELECT count(*) FROM question_windows WHERE question_id = ?", thread["root"])
        == 0
    )
    assert count(conn, "SELECT count(*) FROM question_windows") >= 1


def test_a_reused_window_id_does_not_inherit_the_old_lineage(
    conn: Connection, thread: dict[str, int]
) -> None:
    last = add_frame(conn, NOW, "last frame text")
    last_window = window_of(conn, last)
    cited = saved_answer(conn, last_window, NOW)
    conn.execute("DELETE FROM frames WHERE id = ?", (last,))
    assert row(conn, cited) == CLEARED
    reused = add_frame(conn, NOW + 5, "brand new text")
    reused_window = window_of(conn, reused)
    assert reused_window == last_window  # SQLite reuses the highest free rowid
    where = "SELECT count(*) FROM question_windows WHERE window_id = ?"
    assert count(conn, where, reused_window) == 0
    conn.execute("UPDATE frame_windows SET text = 'edited' WHERE id = ?", (reused_window,))
    for question in (thread["root"], thread["reply"], thread["unrelated"]):
        assert intact(conn, question)  # nothing else was dragged along


def test_a_reused_id_of_a_window_deleted_without_text_inherits_no_lineage(
    conn: Connection, thread: dict[str, int]
) -> None:
    """An answer may cite a text-less window (title match); deleting it must still drop the link."""
    last = add_frame(conn, NOW, "soon gone")
    last_window = window_of(conn, last)
    conn.execute(
        "UPDATE frame_windows SET text = NULL, text_source = NULL WHERE id = ?", (last_window,)
    )
    cited = saved_answer(conn, last_window, NOW)
    where = "SELECT count(*) FROM question_windows WHERE window_id = ?"
    assert count(conn, where, last_window) == 1
    conn.execute("DELETE FROM frames WHERE id = ?", (last,))
    assert count(conn, where, last_window) == 0
    reused = window_of(conn, add_frame(conn, NOW + 5, "brand new text"))
    assert reused == last_window
    conn.execute("UPDATE frame_windows SET text = 'edited' WHERE id = ?", (reused,))
    assert intact(conn, cited)  # the unrelated answer is not cleared by the new window's expiry
    for question in (thread["root"], thread["reply"], thread["unrelated"]):
        assert intact(conn, question)


def test_a_question_citing_only_another_window_of_the_frame_is_not_failed(
    conn: Connection,
) -> None:
    frame = add_frame(conn, NOW, "chrome words")
    edited, bystander = window_of(conn, frame), add_window(conn, frame, 1, "code words")
    cites_edited = saved_answer(conn, edited)
    cites_other = saved_answer(conn, bystander)
    conn.execute("UPDATE frame_windows SET text = 'changed' WHERE id = ?", (edited,))
    assert row(conn, cites_edited) == CLEARED
    assert intact(conn, cites_other)
    where = "SELECT count(*) FROM question_windows WHERE window_id = ?"
    assert count(conn, where, bystander) == 1


def test_deleting_a_frame_fails_the_questions_citing_its_windows(conn: Connection) -> None:
    frame = add_frame(conn, NOW, "chrome words")
    second = add_window(conn, frame, 1, "code words")
    cites = saved_answer(conn, second)
    conn.execute("DELETE FROM frames WHERE id = ?", (frame,))
    assert row(conn, cites) == CLEARED
    assert count(conn, "SELECT count(*) FROM question_windows") == 0


# --- FrameStore.finish_capture -------------------------------------------------------------------


def test_finish_capture_on_an_empty_window_fails_nothing(
    conn: Connection, media: MediaStore, thread: dict[str, int]
) -> None:
    add_monitor(conn)
    shot = replace(app_window(), capture_webp=b"webp")
    frame = FrameStore(conn, media).add(NewFrame(NOW, 0, "game", None, (shot,)))
    FrameStore(conn, media).finish_capture(window_of(conn, frame), "ocr text")
    for question in (thread["root"], thread["reply"], thread["unrelated"]):
        assert intact(conn, question)


# --- thumbnails ------------------------------------------------------------------------------


def webp(color: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (120, 60), color).save(out, "WEBP")
    return out.getvalue()


def red_share(data: bytes) -> float:
    with Image.open(io.BytesIO(data)) as image:
        rgb = image.convert("RGB")
        pixel = rgb.getpixel((rgb.width // 2, rgb.height // 2))
    assert isinstance(pixel, tuple)
    r, g, b = pixel[:3]
    return r / max(r + g + b, 1)


@dataclass
class Api:
    server: ApiServer
    token: str
    conn: Connection
    media: MediaStore

    def call(self, method: str, path: str) -> tuple[int, bytes]:
        request = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=30)
        try:
            request.request(method, path, headers={"Cookie": f"akashic_session={self.token}"})
            response = request.getresponse()
            return response.status, response.read()
        finally:
            request.close()


@pytest.fixture
def api(paths: Paths, key: DataKey, media: MediaStore) -> Iterator[Api]:
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    add_monitor(conn)
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
    try:
        yield Api(server, token, conn, media)
    finally:
        server.stop()
        conn.close()


@pytest.mark.parametrize("category", ["screenshots", "frame_text"])
@pytest.mark.parametrize("width", [None, 64, 128])
def test_no_width_serves_deleted_pixels_after_a_category_delete(
    api: Api, category: str, width: int | None
) -> None:
    store = FrameStore(api.conn, api.media)
    query = "" if width is None else f"?w={width}"
    old = store.add(new_frame(NOW, image=webp("red")))
    for warm in ("?w=64", "?w=128", ""):  # fill the cache at every width
        assert api.call("GET", f"/api/frames/{old}/image{warm}")[0] == 200
    assert api.call("DELETE", f"/api/settings/storage/{category}")[0] == 200
    if category == "frame_text":
        # Frame text deletion keeps the screenshot; remove it too so the id can be reused.
        assert api.call("DELETE", "/api/settings/storage/screenshots")[0] == 200
    new = store.add(new_frame(NOW + 1, image=webp("blue")))
    assert new == old, "SQLite did not reuse the row id; the scenario needs it"
    status, body = api.call("GET", f"/api/frames/{new}/image{query}")
    assert status == 200
    assert red_share(body) < 0.2


def test_a_reused_id_with_another_file_never_shows_the_old_thumbnail(api: Api) -> None:
    """Even without any category delete: the cache is keyed by the stored file as well."""
    store = FrameStore(api.conn, api.media)
    old = store.add(new_frame(NOW, image=webp("red")))
    status, body = api.call("GET", f"/api/frames/{old}/image?w=64")
    assert status == 200
    assert red_share(body) > 0.8
    api.conn.execute("DELETE FROM frames WHERE id = ?", (old,))  # a path other than the API
    new = store.add(new_frame(NOW + 5, image=webp("blue")))
    assert new == old
    status, body = api.call("GET", f"/api/frames/{new}/image?w=64")
    assert status == 200
    assert red_share(body) < 0.2


def test_a_cached_thumbnail_is_still_served_for_an_unchanged_frame(api: Api) -> None:
    store = FrameStore(api.conn, api.media)
    frame = store.add(new_frame(NOW, image=webp("red")))
    first = api.call("GET", f"/api/frames/{frame}/image?w=64")
    second = api.call("GET", f"/api/frames/{frame}/image?w=64")
    assert first == second
    assert first[0] == 200
