"""Retention: what each limit removes, what it keeps, and the daily job that applies it."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from memory_data import add_monitor, app_window, new_frame, window_of

from akashic.config import RETENTION_TYPES, Config
from akashic.pipeline.main import housekeeping
from akashic.pipeline.retention import RetentionHandler, retention_job
from akashic.pipeline.sessions import derive_app_sessions
from akashic.search import keyword, transcripts
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.frames import FrameStore, NewFrame, NewWindow
from akashic.storage.jobs import Job, JobQueue, NewJob, enqueue_in
from akashic.storage.retention import (
    DAY_MS,
    PURGERS,
    apply_retention,
    delete_all_recorded_data,
    delete_spent_frames,
    unreleased_media_references,
)
from akashic.storage.usage import WEEK_MS, storage_report

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.storage.media import MediaStore

NOW = 20_400 * DAY_MS + 43_200_000  # noon, November 2026; every age below is relative to it


def ago(days: float) -> int:
    return NOW - int(days * DAY_MS)


@pytest.fixture
def frames(conn: Connection, media: MediaStore) -> FrameStore:
    add_monitor(conn)
    return FrameStore(conn, media)


@pytest.fixture
def audio(conn: Connection, media: MediaStore) -> AudioStore:
    return AudioStore(conn, media)


def frame(
    frames: FrameStore, age_days: float, text: str | None = "hello", *, image: bool = True
) -> int:
    return frames.add(
        new_frame(
            ago(age_days),
            trigger="click",
            process="zen.exe",
            text=text,
            image=b"webp" if image else None,
        )
    )


def segment(
    audio: AudioStore, conn: Connection, age_days: float, transcript: str | None = "bonjour"
) -> int:
    start = ago(age_days)
    seg_id = audio.add(NewSegment("system", start, start + 5_000, 4_000), b"opus", NOW)
    conn.execute("DELETE FROM jobs")  # the tests decide which jobs exist
    if transcript is None:
        return seg_id
    audio.set_transcript(seg_id, "done", "fr", transcript)
    conn.execute("DELETE FROM jobs")
    return seg_id


def count(conn: Connection, sql: str, *params: object) -> int:
    row: tuple[int] = conn.execute(sql, params).fetchone()
    return row[0]


def run(conn: Connection, media: MediaStore, **limits: int | None) -> dict[str, int]:
    return apply_retention(conn, media, limits, NOW)


# --- nothing configured ---------------------------------------------------------------------


def test_forever_deletes_nothing(
    conn: Connection, media: MediaStore, frames: FrameStore, audio: AudioStore
) -> None:
    frame(frames, 500)
    segment(audio, conn, 500)
    assert run(conn, media) == {}
    assert run(conn, media, screenshots=None, raw_audio=None) == {}
    assert count(conn, "SELECT count(*) FROM frames WHERE image_path IS NOT NULL") == 1
    assert count(conn, "SELECT count(*) FROM audio_segments WHERE audio_path IS NOT NULL") == 1


def test_every_purger_has_a_known_retention_type() -> None:
    assert set(PURGERS) <= set(RETENTION_TYPES)


# --- screen ---------------------------------------------------------------------------------


def test_expired_screenshots_lose_the_image_but_keep_the_row_and_text(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    old, new = frame(frames, 40), frame(frames, 5)
    old_path = frames.get(old).image_path  # type: ignore[union-attr]  # just stored
    assert old_path is not None
    assert run(conn, media, screenshots=30) == {"screenshots": 1}
    stored = frames.get(old)
    assert stored is not None
    assert (stored.image_path, stored.windows[0].text) == (None, "hello")
    assert not media.exists(old_path)
    assert frames.get(new).image_path is not None  # type: ignore[union-attr]
    assert run(conn, media, screenshots=30) == {"screenshots": 0}  # idempotent
    assert keyword.search(conn, "hello", keyword.Filters(0, 2**62, None), 10, 0)


def test_empty_date_folders_are_removed_with_the_last_file(
    conn: Connection, media: MediaStore, frames: FrameStore, paths: Paths
) -> None:
    frame(frames, 40)
    keep = frame(frames, 5)
    assert any(p.is_dir() for p in paths.media_dir.iterdir())
    run(conn, media, screenshots=30)
    left = [p for p in paths.media_dir.rglob("*") if p.is_file()]
    assert len(left) == 1
    assert frames.get(keep).image_path is not None  # type: ignore[union-attr]
    assert len([p for p in paths.media_dir.rglob("*") if p.is_dir() and not any(p.iterdir())]) == 0


def capture_frame(frames: FrameStore, age_days: float, *, image: bool = True) -> int:
    shot = replace(app_window(), capture_webp=b"webp")
    return frames.add(NewFrame(ago(age_days), 0, "game", b"webp" if image else None, (shot,)))


def capture_of(conn: Connection, frame_id: int) -> tuple[int, str | None]:
    return conn.execute(
        "SELECT id, capture_path FROM frame_windows WHERE frame_id = ?", (frame_id,)
    ).fetchone()


def test_a_capture_waiting_for_deferred_ocr_is_kept_but_its_frame_image_is_not(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    waiting = capture_frame(frames, 40)
    window_id, path = capture_of(conn, waiting)
    assert path is not None
    assert run(conn, media, screenshots=30) == {"screenshots": 1}  # the frame image only
    assert frames.get(waiting).image_path is None  # type: ignore[union-attr]
    assert capture_of(conn, waiting)[1] == path
    assert media.exists(path)
    conn.execute("UPDATE jobs SET state = 'running'")
    assert run(conn, media, screenshots=30) == {"screenshots": 0}
    conn.execute("UPDATE jobs SET state = 'done'")
    assert run(conn, media, screenshots=30) == {"screenshots": 1}
    assert capture_of(conn, waiting) == (window_id, None)
    assert not media.exists(path)


def test_a_capture_with_no_job_or_a_failed_job_expires_when_old_enough(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    orphan, failed, recent = (
        capture_frame(frames, 40),
        capture_frame(frames, 41),
        capture_frame(frames, 5),
    )
    conn.execute("DELETE FROM jobs WHERE key = ?", (f"ocr_window:{capture_of(conn, orphan)[0]}",))
    conn.execute(
        "UPDATE jobs SET state = 'failed' WHERE state = 'pending' AND data_ms < ?", (ago(30),)
    )
    assert run(conn, media, screenshots=30) == {"screenshots": 4}  # 2 images + 2 captures
    assert capture_of(conn, orphan)[1] is None
    assert capture_of(conn, failed)[1] is None
    assert capture_of(conn, recent)[1] is not None


def test_a_locked_or_foreign_capture_keeps_its_reference_and_is_reported(
    conn: Connection, media: MediaStore, frames: FrameStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked, foreign = capture_frame(frames, 40, image=False), capture_frame(frames, 41, image=False)
    conn.execute("UPDATE jobs SET state = 'done'")
    locked_path = capture_of(conn, locked)[1]
    conn.execute(
        "UPDATE frame_windows SET capture_path = '../outside.webp' WHERE frame_id = ?", (foreign,)
    )
    real_delete = media.delete

    def delete(rel_path: str) -> None:
        if rel_path == locked_path:
            raise PermissionError
        real_delete(rel_path)

    monkeypatch.setattr(media, "delete", delete)
    assert run(conn, media, screenshots=30) == {
        "screenshots": 0,
        "bad_media_paths": 1,
        "locked_media_files": 1,
    }
    assert capture_of(conn, locked)[1] == locked_path
    assert capture_of(conn, foreign)[1] == "../outside.webp"
    assert unreleased_media_references(conn, media, "screenshots", ago(30)) == {
        "bad_media_paths": 1,
        "locked_media_files": 1,
    }


def test_a_frame_with_a_capture_left_is_not_deleted_as_spent(
    conn: Connection, frames: FrameStore
) -> None:
    held = capture_frame(frames, 40, image=False)
    free = capture_frame(frames, 41, image=False)
    conn.execute("UPDATE frame_windows SET capture_path = NULL WHERE frame_id = ?", (free,))
    assert delete_spent_frames(conn, ago(30)) == 1
    assert frames.get(held) is not None
    assert frames.get(free) is None


def test_delete_all_removes_captures_and_counts_them(
    conn: Connection, media: MediaStore, frames: FrameStore, paths: Paths
) -> None:
    capture_frame(frames, 1)  # an image and a capture
    assert delete_all_recorded_data(conn, media)["screenshots"] == 2
    assert [p for p in paths.media_dir.rglob("*") if p.is_file()] == []


def test_a_file_that_cannot_be_deleted_is_skipped_and_retried_next_time(
    conn: Connection, media: MediaStore, frames: FrameStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stuck, fine = frame(frames, 40), frame(frames, 41)
    real_delete = media.delete
    stuck_path = frames.get(stuck).image_path  # type: ignore[union-attr]

    def delete(rel_path: str) -> None:
        if rel_path == stuck_path:
            raise PermissionError
        real_delete(rel_path)

    monkeypatch.setattr(media, "delete", delete)
    assert run(conn, media, screenshots=30) == {"screenshots": 1, "locked_media_files": 1}
    assert frames.get(stuck).image_path == stuck_path  # type: ignore[union-attr]
    assert frames.get(fine).image_path is None  # type: ignore[union-attr]
    monkeypatch.undo()
    assert run(conn, media, screenshots=30) == {"screenshots": 1}


def test_a_vanished_file_still_clears_its_reference(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    gone = frame(frames, 40)
    media.delete(frames.get(gone).image_path)  # type: ignore[union-attr, arg-type]  # crashed earlier
    assert run(conn, media, screenshots=30) == {"screenshots": 1}
    assert frames.get(gone).image_path is None  # type: ignore[union-attr]


def test_expired_frame_text_leaves_the_image_and_the_search_index(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    old = frame(frames, 40, text="needle")
    assert keyword.search(conn, "needle", keyword.Filters(0, 2**62, None), 10, 0)
    assert run(conn, media, frame_text=30) == {"frame_text": 1}
    stored = frames.get(old)
    assert stored is not None
    assert (stored.windows[0].text, stored.windows[0].text_source) == (None, None)
    assert stored.image_path is not None
    assert not keyword.search(conn, "needle", keyword.Filters(0, 2**62, None), 10, 0)


def test_a_frame_row_goes_only_when_screenshot_and_text_have_both_expired(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    old = frame(frames, 40)
    run(conn, media, screenshots=30)  # text is kept forever
    assert frames.get(old) is not None
    run(conn, media, frame_text=30)  # the image is kept forever
    assert frames.get(old) is not None
    both = run(conn, media, screenshots=30, frame_text=30)
    assert both["frame_rows"] == 1
    assert frames.get(old) is None
    assert count(conn, "SELECT count(*) FROM frame_windows") == 0
    assert (
        count(conn, "SELECT count(*) FROM frame_windows_fts WHERE frame_windows_fts MATCH 'hello'")
        == 0
    )


def test_different_limits_for_image_and_text_use_the_longer_one_for_the_row(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    mid = frame(frames, 20)  # image expired (10 d), text still within 30 d
    run(conn, media, screenshots=10, frame_text=30)
    stored = frames.get(mid)
    assert stored is not None
    assert (stored.image_path, stored.windows[0].text) == (None, "hello")


def test_text_expiry_clears_every_window_of_old_frames_only(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    old = frames.add(
        NewFrame(
            ago(40),
            0,
            "idle",
            None,
            (
                app_window(z=0, hwnd=1, text="first words"),
                app_window(z=1, hwnd=2, text="second words", focused=False),
            ),
        )
    )
    new = frame(frames, 5, text="recent")
    assert run(conn, media, frame_text=30) == {"frame_text": 2}
    kept = frames.get(old)
    assert kept is not None
    assert [(w.text, w.text_source) for w in kept.windows] == [(None, None), (None, None)]
    assert [w.text for w in frames.get(new).windows] == ["recent"]  # type: ignore[union-attr]


def test_spent_frames_are_the_old_ones_without_image_or_window_text(
    conn: Connection, frames: FrameStore
) -> None:
    hidden = NewWindow("hidden", 1, None, 0, 0, 10, 10)
    spent = frames.add(NewFrame(ago(40), 0, "idle", None, (app_window(), hidden)))
    with_text = frame(frames, 40, text="kept words", image=False)
    with_image = frames.add(new_frame(ago(40), image=b"webp"))
    recent = frames.add(NewFrame(ago(5), 0, "idle"))
    assert delete_spent_frames(conn, ago(30)) == 1
    assert frames.get(spent) is None
    assert count(conn, "SELECT count(*) FROM frame_windows WHERE frame_id = ?", spent) == 0
    for frame_id in (with_text, with_image, recent):
        assert frames.get(frame_id) is not None


def test_embedding_expiry_removes_window_passages_of_old_frames(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    old, new = frame(frames, 40), frame(frames, 5)
    for frame_id in (old, new):
        window = window_of(conn, frame_id)
        conn.execute("INSERT INTO window_passages VALUES (?, 'm', 0, 5, 1, x'0000')", (window,))
    assert run(conn, media, embeddings=30) == {"embeddings": 1}
    assert conn.execute("SELECT window_id FROM window_passages").fetchall() == [
        (window_of(conn, new),)
    ]


def test_the_frame_text_size_sums_every_window_by_the_frame_time(
    conn: Connection, paths: Paths, frames: FrameStore
) -> None:
    both = (
        app_window(z=0, hwnd=1, text="abc"),
        app_window(z=1, hwnd=2, text="héllo", focused=False),
    )
    frames.add(NewFrame(ago(40), 0, "idle", None, both))
    frames.add(NewFrame(ago(1), 0, "idle", None, both))
    by_kind = {u.kind: u for u in storage_report(conn, paths, NOW).usage}
    assert (by_kind["frame_text"].bytes, by_kind["frame_text"].bytes_7d) == (18, 9)


# --- audio ----------------------------------------------------------------------------------


def test_expired_raw_audio_keeps_the_transcript(
    conn: Connection, media: MediaStore, audio: AudioStore
) -> None:
    seg_id = segment(audio, conn, 40)
    stored = audio.get(seg_id)
    assert stored is not None
    assert stored.audio_path is not None
    path = stored.audio_path
    assert run(conn, media, raw_audio=30) == {"raw_audio": 1}
    after = audio.get(seg_id)
    assert after is not None
    assert (after.audio_path, after.transcript) == (None, "bonjour")
    assert not media.exists(path)
    assert audio.read_file(after) is None


def test_audio_still_waiting_for_transcription_or_speakers_is_kept(
    conn: Connection, media: MediaStore, audio: AudioStore
) -> None:
    untranscribed = segment(audio, conn, 40, transcript=None)  # status pending
    waiting_speakers = segment(audio, conn, 41)
    enqueue_in(conn, NewJob("speakers", f"speakers:{waiting_speakers}", 1), NOW)
    assert run(conn, media, raw_audio=30) == {"raw_audio": 0}
    conn.execute("UPDATE jobs SET state = 'done'")
    audio.set_transcript(untranscribed, "done", "en", "hi")  # queues its own speakers job
    assert run(conn, media, raw_audio=30) == {"raw_audio": 1}  # only the one whose job is done
    conn.execute("UPDATE jobs SET state = 'done'")
    assert run(conn, media, raw_audio=30) == {"raw_audio": 1}


def test_expired_transcripts_leave_the_audio_and_the_search_index(
    conn: Connection, media: MediaStore, audio: AudioStore
) -> None:
    seg_id = segment(audio, conn, 40)
    assert transcripts.search(conn, "bonjour", 0, 2**62)
    assert run(conn, media, transcripts=30) == {"transcripts": 1}
    stored = audio.get(seg_id)
    assert stored is not None
    assert (stored.transcript, stored.audio_path is not None) == (None, True)
    assert not transcripts.search(conn, "bonjour", 0, 2**62)


def test_a_segment_goes_with_its_turns_once_audio_and_transcript_have_expired(
    conn: Connection, media: MediaStore, audio: AudioStore
) -> None:
    old, recent = segment(audio, conn, 40), segment(audio, conn, 2)
    conn.execute("INSERT INTO speakers (name, created_ms) VALUES ('Minlo', 1)")
    conn.execute(
        "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms)"
        " VALUES (?, 1, 0, 1000, 1000)",
        (old,),
    )
    done = run(conn, media, raw_audio=30, transcripts=30)
    assert done["audio_rows"] == 1
    assert audio.get(old) is None
    assert audio.get(recent) is not None
    assert count(conn, "SELECT count(*) FROM speaker_turns") == 0
    assert count(conn, "SELECT count(*) FROM speakers WHERE name = 'Minlo'") == 1  # profiles stay
    assert not transcripts.search(conn, "bonjour", 0, ago(10))


# --- sessions -------------------------------------------------------------------------------


# Pins the unread media_events and media_sessions_fts: delete or rewrite with #90.
def media_session(conn: Connection, age_days: float, *, closed: bool = True) -> int:
    cursor = conn.execute(
        "INSERT INTO media_sessions (app_id, app, title, start_ms, end_ms, closed)"
        " VALUES ('a', 'Zen', 'A video', ?, ?, ?)",
        (ago(age_days) - 60_000, ago(age_days), int(closed)),
    )
    assert cursor.lastrowid is not None
    conn.execute(
        "INSERT INTO media_events (media_session_id, ts_ms, state) VALUES (?, ?, 'playing')",
        (cursor.lastrowid, ago(age_days)),
    )
    return cursor.lastrowid


# Pins the unread media_events and media_sessions_fts: delete or rewrite with #90.
def test_expired_media_sessions_go_with_events_and_index_but_open_ones_stay(
    conn: Connection, media: MediaStore
) -> None:
    media_session(conn, 40)
    still_open = media_session(conn, 40, closed=False)
    recent = media_session(conn, 5)
    assert run(conn, media, media_sessions=30) == {"media_sessions": 1}
    ids = [r[0] for r in conn.execute("SELECT id FROM media_sessions ORDER BY id")]
    assert ids == [still_open, recent]
    assert count(conn, "SELECT count(*) FROM media_events") == 2
    assert (
        count(
            conn, "SELECT count(*) FROM media_sessions_fts WHERE media_sessions_fts MATCH 'video'"
        )
        == 2
    )


def test_expired_app_and_game_sessions_go_but_the_newest_app_session_stays(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    for days in (50, 45, 40):  # three old focus periods, then silence
        frame(frames, days)
    derive_app_sessions(conn, NOW)
    assert count(conn, "SELECT count(*) FROM app_sessions") == 3
    conn.execute(
        "INSERT INTO game_sessions (process, start_ms, end_ms, closed) VALUES ('g.exe', ?, ?, 1)",
        (ago(41), ago(41)),
    )
    removed = run(conn, media, app_sessions=30)
    assert removed == {"app_sessions": 3}  # two app sessions and the game
    assert count(conn, "SELECT count(*) FROM app_sessions") == 1
    assert count(conn, "SELECT count(*) FROM game_sessions") == 0
    # The kept row is the watermark: frames that still exist are not turned into sessions again.
    assert derive_app_sessions(conn, NOW) == 0
    assert count(conn, "SELECT count(*) FROM app_sessions") == 1


# --- the job --------------------------------------------------------------------------------


def test_no_job_when_everything_is_kept_forever() -> None:
    assert retention_job({"screenshots": None, "raw_audio": None}, NOW) is None
    assert retention_job({}, NOW) is None


def test_one_job_per_day_and_per_change_of_limits() -> None:
    a = retention_job({"screenshots": 30}, NOW)
    assert a is not None
    assert a == retention_job(
        {"screenshots": 30, "raw_audio": None}, NOW + 3_600_000 - 1
    )  # same day
    assert retention_job({"screenshots": 30}, NOW + 2 * DAY_MS) != a  # another day
    assert retention_job({"screenshots": 60}, NOW) != a  # other limits
    assert retention_job({"screenshots": 30, "raw_audio": 7}, NOW) != a
    assert a.data_ms == 0  # before any backlog


def test_the_handler_applies_the_limits_through_the_queue(
    conn: Connection, media: MediaStore, frames: FrameStore
) -> None:
    old = frame(frames, 40)
    queue = JobQueue(conn)
    job = retention_job({"screenshots": 30}, NOW)
    assert job is not None
    assert queue.enqueue(job, NOW)
    assert not queue.enqueue(job, NOW)  # the housekeeping tick repeats every 30 s
    handler = RetentionHandler(conn, media, {"screenshots": 30}, clock_ms=lambda: NOW)
    assert (handler.kind, handler.heavy, handler.blocked()) == ("retention", False, None)
    claimed = queue.claim([handler.kind], NOW)
    assert isinstance(claimed, Job)
    handler.run(claimed)
    queue.complete(claimed.id, NOW)
    handler.give_up(claimed)
    handler.idle()
    assert frames.get(old).image_path is None  # type: ignore[union-attr]


def test_the_log_line_has_counts_only(
    conn: Connection, media: MediaStore, frames: FrameStore, caplog: pytest.LogCaptureFixture
) -> None:
    frame(frames, 40, text="secret words")
    handler = RetentionHandler(conn, media, {"screenshots": 30, "frame_text": 30}, lambda: NOW)
    caplog.set_level("INFO")
    handler.run(Job(1, "retention", "retention:x", {}, 0, 1))
    assert "screenshots=1" in caplog.text
    assert "secret" not in caplog.text


# --- disk usage -----------------------------------------------------------------------------


def test_usage_counts_files_by_kind_and_text_by_length(
    conn: Connection, paths: Paths, frames: FrameStore, audio: AudioStore
) -> None:
    frame(frames, 40, text="héllo")  # 6 bytes in UTF-8, recorded 40 "days" before NOW
    frame(frames, 1, text="abc")
    segment(audio, conn, 1, transcript="bonjour")
    report = storage_report(conn, paths, NOW)
    by_kind = {u.kind: u for u in report.usage}
    assert (by_kind["frame_text"].bytes, by_kind["frame_text"].bytes_7d) == (9, 3)
    assert (by_kind["transcripts"].bytes, by_kind["transcripts"].bytes_7d) == (7, 7)
    assert by_kind["screenshots"].bytes > 0
    assert by_kind["raw_audio"].bytes > 0
    # Files were written just now, which is "this week" only when NOW is close to the real time.
    assert report.total_bytes == (
        by_kind["screenshots"].bytes + by_kind["raw_audio"].bytes + report.database_bytes
    )
    far_future = storage_report(conn, paths, NOW + 10 * WEEK_MS * 100)
    assert {u.kind: u.bytes_7d for u in far_future.usage}["screenshots"] == 0
    assert report.free_bytes is not None


def test_usage_of_an_empty_data_directory_is_zero(conn: Connection, paths: Paths) -> None:
    report = storage_report(conn, paths, NOW)
    assert [u.bytes for u in report.usage] == [0, 0, 0, 0, 0, 0]
    assert report.database_bytes > 0


def test_worker_housekeeping_queues_one_pass_per_day_and_none_when_all_forever(
    conn: Connection, paths: Paths
) -> None:
    queue = JobQueue(conn)

    def tick(**limits: int | None) -> None:
        retention = {**dict.fromkeys(RETENTION_TYPES), **limits}
        ctx = SimpleNamespace(conn=conn, config=Config(retention_days=retention), paths=paths)
        housekeeping(ctx, queue, NOW, lambda: False)  # type: ignore[arg-type]  # only conn, config and paths are used

    tick()
    assert queue.pending_count(["retention"]) == 0
    tick(screenshots=30)
    tick(screenshots=30)  # every 30 s: still one job
    assert queue.pending_count(["retention"]) == 1
    tick(screenshots=60)  # limits changed and applied by a restart: a new pass
    assert queue.pending_count(["retention"]) == 2


# --- episodes and vectors ------------------------------------------------------------------


def add_episode_with_vector(conn: Connection, end_days_ago: float, kind: str = "work") -> int:
    cursor = conn.execute(
        "INSERT INTO episodes (kind, ref, start_ms, end_ms, title) VALUES (?, ?, ?, ?, 'E')",
        (
            kind,
            f"{kind}:{end_days_ago}",
            ago(end_days_ago + 0.1),
            ago(end_days_ago),
        ),
    )
    episode = int(cursor.lastrowid or 0)
    conn.execute(
        "INSERT INTO embeddings VALUES ('episode', ?, 'm', 2, x'0000803f00000000', ?, 0)",
        (episode, ago(end_days_ago + 0.1)),
    )
    conn.execute("INSERT INTO episode_items VALUES (?, 'app_session', 1)", (episode,))
    return episode


def test_expired_episodes_go_with_their_links_and_vectors_but_not_the_evidence(
    conn: Connection, media: MediaStore, audio: AudioStore
) -> None:
    old = add_episode_with_vector(conn, 40)
    new = add_episode_with_vector(conn, 5)
    seg = segment(audio, conn, 40, transcript="kept evidence")
    assert run(conn, media, episodes=30) == {"episodes": 1}
    assert [r[0] for r in conn.execute("SELECT id FROM episodes")] == [new]
    assert count(conn, "SELECT count(*) FROM episode_items WHERE episode_id = ?", old) == 0
    assert count(conn, "SELECT count(*) FROM embeddings") == 1
    assert count(conn, "SELECT count(*) FROM episodes_fts WHERE episodes_fts MATCH 'E'") == 1
    assert audio.get(seg) is not None


def test_expired_vectors_are_deleted_alone(conn: Connection, media: MediaStore) -> None:
    add_episode_with_vector(conn, 40)
    add_episode_with_vector(conn, 5)
    assert run(conn, media, embeddings=30) == {"embeddings": 1}
    assert count(conn, "SELECT count(*) FROM episodes") == 2
    assert count(conn, "SELECT count(*) FROM embeddings") == 1


def test_storage_report_counts_vectors_and_episodes(conn: Connection, paths: Paths) -> None:
    add_episode_with_vector(conn, 40)
    add_episode_with_vector(conn, 1)
    by_kind = {u.kind: u for u in storage_report(conn, paths, NOW).usage}
    assert (by_kind["embeddings"].bytes, by_kind["embeddings"].bytes_7d) == (16, 8)
    assert by_kind["episodes"].bytes == 2 * len("E")
    assert by_kind["episodes"].bytes_7d == len("E")
