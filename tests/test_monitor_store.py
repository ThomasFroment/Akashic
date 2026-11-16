"""Monitor history: the schema, MonitorLog, stage_at, latest_valid_frame, retention, Delete all."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import sqlcipher3
from memory_data import add_monitor

from akashic.storage.gaps import GapLog
from akashic.storage.monitors import (
    MonitorAt,
    MonitorLog,
    MonitorRow,
    latest_valid_frame,
    stage_at,
)
from akashic.storage.retention import DAY_MS, apply_retention, delete_all_recorded_data

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.storage.media import MediaStore

NOW = 20_400 * DAY_MS
LEFT = MonitorRow("L", "Left", (0, 0, 1920, 1080), 1.0, is_main=True)
RIGHT = MonitorRow("R", "Right", (1920, 0, 3840, 1080), 1.5, is_main=False)


def count(conn: Connection, table: str) -> int:
    row: tuple[int] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
    return row[0]


def rows(conn: Connection, sql: str, *params: object) -> list[tuple[object, ...]]:
    return [tuple(r) for r in conn.execute(sql, params).fetchall()]


def frame(conn: Connection, monitor_id: int | None, ts: int, *hwnds: int) -> int:
    """A frame of the monitor (a lone monitor 0 when none is named) showing one app per hwnd."""
    if monitor_id is None:
        monitor_id = add_monitor(conn, 0)
    frame_id: int = conn.execute(
        "INSERT INTO frames (ts_ms, monitor_id, trigger) VALUES (?, ?, 'click') RETURNING id",
        (ts, monitor_id),
    ).fetchone()[0]
    for z, hwnd in enumerate(hwnds):
        conn.execute(
            "INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1)"
            " VALUES (?, 'app', ?, ?, 0, 0, 1, 1)",
            (frame_id, z, hwnd),
        )
    return frame_id


@pytest.fixture
def log(conn: Connection) -> MonitorLog:
    return MonitorLog(conn)


@pytest.fixture
def ids(log: MonitorLog) -> dict[str, int]:
    return log.present([LEFT, RIGHT], 100)


# --- schema ----------------------------------------------------------------------------------


def test_schema_columns_avoid_sql_keywords(conn: Connection) -> None:
    def columns(table: str) -> list[str]:
        return [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]

    assert columns("monitors") == ["id", "key", "name"]
    assert columns("monitor_layouts") == ["id", "start_ms"]
    assert columns("layout_monitors") == [
        "layout_id",
        "monitor_id",
        "x0",
        "y0",
        "x1",
        "y1",
        "scale",
        "is_main",
    ]
    assert columns("monitor_states") == ["id", "monitor_id", "state", "start_ms", "checked_ms"]
    assert columns("frame_windows") == [
        "id",
        "frame_id",
        "role",
        "z",
        "hwnd",
        "x0",
        "y0",
        "x1",
        "y1",
        "process",
        "title",
        "url",
        "focused",
        "painted",
        "text",
        "text_source",
        "capture_path",
    ]
    assert "monitor_id" in columns("frames")


def test_frames_need_a_monitor_and_have_a_monitor_ts_index(conn: Connection) -> None:
    frame(conn, None, 10)
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute("INSERT INTO frames (ts_ms, monitor_id, trigger) VALUES (1, NULL, 'idle')")
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute("INSERT INTO frames (ts_ms, monitor_id, trigger) VALUES (1, 99, 'idle')")
    indexes = [
        [c[2] for c in conn.execute(f"PRAGMA index_info('{i[1]}')")]
        for i in conn.execute("PRAGMA index_list('frames')")
    ]
    assert ["monitor_id", "ts_ms"] in indexes


def test_monitor_keys_are_unique(conn: Connection) -> None:
    conn.execute("INSERT INTO monitors (key, name) VALUES ('k', 'a')")
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute("INSERT INTO monitors (key, name) VALUES ('k', 'b')")


def test_state_and_checked_ms_are_constrained(conn: Connection, ids: dict[str, int]) -> None:
    sql = "INSERT INTO monitor_states (monitor_id, state, start_ms, checked_ms) VALUES (?, ?, ?, ?)"
    for state in ("shown", "hidden", "dropped", "failed"):
        conn.execute(sql, (ids["L"], state, 10, 10))
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute(sql, (ids["L"], "other", 10, 10))
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute(sql, (ids["L"], "shown", 10, 9))


def test_deleting_a_monitor_or_a_layout_cascades(conn: Connection, ids: dict[str, int]) -> None:
    MonitorLog(conn).state(ids["L"], "shown", 100)
    conn.execute("DELETE FROM monitors WHERE id = ?", (ids["L"],))
    assert count(conn, "monitor_states") == 0
    assert rows(conn, "SELECT monitor_id FROM layout_monitors") == [(ids["R"],)]
    conn.execute("DELETE FROM monitor_layouts")
    assert count(conn, "layout_monitors") == 0


def test_deleting_a_frame_deletes_its_windows(conn: Connection) -> None:
    frame_id = frame(conn, None, 10, 5, 6)
    conn.execute("DELETE FROM frames WHERE id = ?", (frame_id,))
    assert count(conn, "frame_windows") == 0
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute(
            "INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1)"
            " VALUES (NULL, 'app', 0, 1, 0, 0, 1, 1)"
        )


# --- MonitorLog.present ----------------------------------------------------------------------


def test_present_returns_ids_and_writes_a_layout_run_with_members(
    conn: Connection, log: MonitorLog
) -> None:
    ids = log.present([LEFT, RIGHT], 100)
    assert set(ids) == {"L", "R"}
    assert rows(conn, "SELECT id, key, name FROM monitors ORDER BY key") == [
        (ids["L"], "L", "Left"),
        (ids["R"], "R", "Right"),
    ]
    assert rows(conn, "SELECT start_ms FROM monitor_layouts") == [(100,)]
    assert rows(
        conn,
        "SELECT monitor_id, x0, y0, x1, y1, scale, is_main FROM layout_monitors"
        " ORDER BY monitor_id",
    ) == [
        (ids["L"], 0, 0, 1920, 1080, 1.0, 1),
        (ids["R"], 1920, 0, 3840, 1080, 1.5, 0),
    ]


def test_an_unchanged_set_writes_nothing_and_still_returns_ids(
    conn: Connection, log: MonitorLog
) -> None:
    first = log.present([LEFT, RIGHT], 100)
    assert log.present([RIGHT, LEFT], 200) == first
    assert count(conn, "monitor_layouts") == 1
    assert count(conn, "layout_monitors") == 2


def test_a_changed_set_starts_a_new_run_and_updates_the_name(
    conn: Connection, log: MonitorLog
) -> None:
    first = log.present([LEFT, RIGHT], 100)
    renamed = MonitorRow("R", "Right 2", RIGHT.rect, RIGHT.scale, is_main=False)
    second = log.present([LEFT, renamed], 200)
    assert second == first
    assert rows(conn, "SELECT start_ms FROM monitor_layouts ORDER BY id") == [(100,), (200,)]
    assert rows(conn, "SELECT name FROM monitors WHERE key = 'R'") == [("Right 2",)]
    moved = MonitorRow("L", "Left", (0, 0, 1280, 720), 1.0, is_main=True)
    log.present([moved, renamed], 300)
    assert count(conn, "monitor_layouts") == 3
    log.present([moved], 400)
    assert count(conn, "monitor_layouts") == 4
    assert count(conn, "layout_monitors") == 2 + 2 + 2 + 1


def test_a_new_log_always_starts_a_new_run(conn: Connection) -> None:
    first = MonitorLog(conn).present([LEFT], 100)
    assert MonitorLog(conn).present([LEFT], 200) == first
    assert rows(conn, "SELECT start_ms FROM monitor_layouts ORDER BY id") == [(100,), (200,)]
    assert count(conn, "monitors") == 1


def test_duplicate_keys_in_one_call_are_an_integrity_error(
    conn: Connection, log: MonitorLog
) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        log.present([LEFT, LEFT], 100)
    assert count(conn, "monitor_layouts") == 0


# --- MonitorLog.state ------------------------------------------------------------------------


def test_the_same_state_extends_the_run_and_a_change_starts_one(
    conn: Connection, log: MonitorLog, ids: dict[str, int]
) -> None:
    m = ids["L"]
    log.state(m, "shown", 100)
    log.state(m, "shown", 300)
    log.state(m, "shown", 200)
    assert rows(conn, "SELECT state, start_ms, checked_ms FROM monitor_states") == [
        ("shown", 100, 300)
    ]
    log.state(m, "hidden", 400)
    log.state(ids["R"], "shown", 400)
    assert rows(
        conn,
        "SELECT state, start_ms, checked_ms FROM monitor_states WHERE monitor_id = ? ORDER BY id",
        m,
    ) == [("shown", 100, 300), ("hidden", 400, 400)]
    assert count(conn, "monitor_states") == 3


def test_the_first_call_of_a_new_log_starts_a_run(conn: Connection, ids: dict[str, int]) -> None:
    MonitorLog(conn).state(ids["L"], "shown", 100)
    MonitorLog(conn).state(ids["L"], "shown", 200)
    assert rows(conn, "SELECT start_ms, checked_ms FROM monitor_states ORDER BY id") == [
        (100, 100),
        (200, 200),
    ]


def test_an_unknown_monitor_is_an_integrity_error(conn: Connection, log: MonitorLog) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        log.state(999, "shown", 100)
    assert count(conn, "monitor_states") == 0


# --- stage_at --------------------------------------------------------------------------------


def stage(conn: Connection, ts: int) -> dict[int, MonitorAt]:
    return {m.monitor_id: m for m in stage_at(conn, ts, NOW)}


def test_no_layout_yet_means_an_empty_stage(conn: Connection, ids: dict[str, int]) -> None:
    assert stage_at(conn, 50, NOW) == []
    assert ids


def test_stage_lists_the_members_of_the_latest_layout_in_id_order(
    conn: Connection, log: MonitorLog, ids: dict[str, int]
) -> None:
    log.present([LEFT], 500)
    assert stage_at(conn, 100, NOW) == [
        MonitorAt(ids["L"], "Left", (0, 0, 1920, 1080), 1.0, "not_recording", None, None),
        MonitorAt(ids["R"], "Right", (1920, 0, 3840, 1080), 1.5, "not_recording", None, None),
    ]
    assert [m.monitor_id for m in stage_at(conn, 499, NOW)] == [ids["L"], ids["R"]]
    assert [m.monitor_id for m in stage_at(conn, 500, NOW)] == [ids["L"]]


def test_stage_reports_the_state_run_in_effect_and_its_checked_time(
    conn: Connection, log: MonitorLog, ids: dict[str, int]
) -> None:
    log.state(ids["L"], "shown", 100)
    log.state(ids["L"], "shown", 250)
    log.state(ids["L"], "hidden", 300)
    log.state(ids["R"], "failed", 150)
    at_200 = stage(conn, 200)
    assert (at_200[ids["L"]].state, at_200[ids["L"]].checked_ms) == ("shown", 250)
    assert (at_200[ids["R"]].state, at_200[ids["R"]].checked_ms) == ("failed", 150)
    at_300 = stage(conn, 300)
    assert (at_300[ids["L"]].state, at_300[ids["L"]].checked_ms) == ("hidden", 300)
    assert stage(conn, 120)[ids["R"]].state == "not_recording"


def test_a_shown_monitor_gets_its_latest_frame_even_without_an_image(
    conn: Connection, log: MonitorLog, ids: dict[str, int]
) -> None:
    log.state(ids["L"], "shown", 100)
    log.state(ids["R"], "hidden", 100)
    older = frame(conn, ids["L"], 150)
    newer = frame(conn, ids["L"], 200)
    frame(conn, ids["L"], 400)
    frame(conn, ids["R"], 160)
    assert older != newer
    at = stage(conn, 300)
    assert at[ids["L"]].frame_id == newer
    assert at[ids["R"]].frame_id is None
    assert stage(conn, 120)[ids["L"]].frame_id is None


@pytest.mark.parametrize("kind", ["all", "screen"])
def test_a_screen_stopping_gap_makes_every_monitor_not_recording(
    conn: Connection, log: MonitorLog, ids: dict[str, int], kind: str
) -> None:
    log.state(ids["L"], "shown", 100)
    frame(conn, ids["L"], 150)
    conn.execute(
        "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, 'paused', 200, 300)",
        (kind,),
    )
    for m in stage_at(conn, 250, NOW):
        assert (m.state, m.frame_id, m.checked_ms) == ("not_recording", None, None)
    assert stage(conn, 300)[ids["L"]].state == "shown"


@pytest.mark.parametrize("kind", ["screen_text", "mic", "system"])
def test_other_gaps_do_not_change_the_state(
    conn: Connection, log: MonitorLog, ids: dict[str, int], kind: str
) -> None:
    log.state(ids["L"], "shown", 100)
    conn.execute(
        "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, 'paused', 200, 300)",
        (kind,),
    )
    assert stage(conn, 250)[ids["L"]].state == "shown"


# --- latest_valid_frame ----------------------------------------------------------------------

HWND = 77


@pytest.fixture
def shown(conn: Connection, log: MonitorLog, ids: dict[str, int]) -> int:
    """Monitor L shown since 100, with one frame at 1000 showing HWND."""
    log.state(ids["L"], "shown", 100)
    return frame(conn, ids["L"], 1_000, HWND, 5)


def valid(conn: Connection, ids: dict[str, int], ts: int, hwnd: int = HWND) -> int | None:
    return latest_valid_frame(conn, ids["L"], hwnd, ts, NOW)


def test_an_undisturbed_frame_stays_valid_later(
    conn: Connection, ids: dict[str, int], shown: int
) -> None:
    assert valid(conn, ids, 1_000) == shown
    assert valid(conn, ids, 5_000) == shown


def test_the_frame_must_show_the_window(conn: Connection, ids: dict[str, int], shown: int) -> None:
    assert valid(conn, ids, 2_000, hwnd=5) == shown
    assert valid(conn, ids, 2_000, hwnd=1) is None
    assert valid(conn, ids, 1_000, hwnd=1) is None


def test_unknown_monitor_and_no_frame_give_none(
    conn: Connection, ids: dict[str, int], shown: int
) -> None:
    assert latest_valid_frame(conn, 999, HWND, 2_000, NOW) is None
    assert latest_valid_frame(conn, ids["R"], HWND, 2_000, NOW) is None
    assert valid(conn, ids, 999) is None
    assert shown


def test_the_frame_is_the_latest_one_at_ts(
    conn: Connection, ids: dict[str, int], shown: int
) -> None:
    newer = frame(conn, ids["L"], 1_500, 9)
    assert valid(conn, ids, 2_000) is None
    assert valid(conn, ids, 2_000, hwnd=9) == newer
    assert valid(conn, ids, 1_200) == shown


def test_a_state_other_than_shown_after_the_frame_invalidates_it(
    conn: Connection, log: MonitorLog, ids: dict[str, int], shown: int
) -> None:
    log.state(ids["L"], "hidden", 1_500)
    log.state(ids["L"], "shown", 1_800)
    assert valid(conn, ids, 1_400) == shown
    assert valid(conn, ids, 1_500) is None
    assert valid(conn, ids, 2_000) is None
    assert shown


def test_a_state_in_effect_at_the_frame_must_be_shown(
    conn: Connection, log: MonitorLog, ids: dict[str, int]
) -> None:
    log.state(ids["L"], "dropped", 100)
    stale = frame(conn, ids["L"], 1_000, HWND)
    log.state(ids["L"], "shown", 1_500)
    assert valid(conn, ids, 1_200) is None
    assert valid(conn, ids, 2_000) is None
    assert valid(conn, ids, 1_000) == stale


def test_no_state_run_at_the_frame_means_none_unless_ts_equals_the_frame(
    conn: Connection, ids: dict[str, int]
) -> None:
    only = frame(conn, ids["L"], 50, HWND)
    assert valid(conn, ids, 50) == only
    assert valid(conn, ids, 80) is None


def test_a_layout_without_the_monitor_after_the_frame_invalidates_it(
    conn: Connection, log: MonitorLog, ids: dict[str, int], shown: int
) -> None:
    log.present([RIGHT], 1_500)
    log.present([LEFT, RIGHT], 1_800)
    assert valid(conn, ids, 1_400) == shown
    assert valid(conn, ids, 2_000) is None


def test_no_layout_run_at_the_frame_means_none(conn: Connection, log: MonitorLog) -> None:
    ids = log.present([LEFT], 2_000)
    log.state(ids["L"], "shown", 100)
    early = frame(conn, ids["L"], 1_000, HWND)
    assert valid(conn, ids, 1_000) == early
    assert valid(conn, ids, 1_500) is None


@pytest.mark.parametrize("kind", ["all", "screen"])
def test_a_screen_stopping_gap_after_the_frame_invalidates_it(
    conn: Connection, ids: dict[str, int], shown: int, kind: str
) -> None:
    GapLog(conn).add(kind, "paused", 1_200, 1_300)  # type: ignore[arg-type]  # parametrized kind
    assert valid(conn, ids, 1_100) == shown
    assert valid(conn, ids, 2_000) is None


@pytest.mark.parametrize("kind", ["mic", "system", "screen_text"])
def test_other_gaps_never_invalidate_the_frame(
    conn: Connection, ids: dict[str, int], shown: int, kind: str
) -> None:
    GapLog(conn).add(kind, "paused", 1_200, 1_300)  # type: ignore[arg-type]  # parametrized kind
    assert valid(conn, ids, 2_000) == shown


def test_a_zero_length_all_gap_after_the_frame_does_not_invalidate_it(
    conn: Connection, ids: dict[str, int], shown: int
) -> None:
    conn.execute(
        "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) "
        "VALUES ('all', 'paused', 1200, 1200)"
    )
    assert valid(conn, ids, 2_000) == shown


# --- retention -------------------------------------------------------------------------------


def ago(days: float) -> int:
    return NOW - int(days * DAY_MS)


def seed_history(conn: Connection) -> dict[str, int]:
    """Old and new runs for monitor L; R has one old run that is its current one."""
    conn.execute("INSERT INTO monitors (key, name) VALUES ('L', 'Left'), ('R', 'Right')")
    ids = {str(key): int(mid) for key, mid in rows(conn, "SELECT key, id FROM monitors")}  # type: ignore[arg-type]
    for start, layout in ((50, "old"), (35, "mid"), (5, "new")):
        layout_id = conn.execute(
            "INSERT INTO monitor_layouts (start_ms) VALUES (?) RETURNING id", (ago(start),)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO layout_monitors VALUES (?, ?, 0, 0, 1, 1, 1.0, 1)",
            (layout_id, ids["L"]),
        )
        assert layout
    sql = "INSERT INTO monitor_states (monitor_id, state, start_ms, checked_ms) VALUES (?, ?, ?, ?)"
    conn.execute(sql, (ids["L"], "shown", ago(60), ago(50)))
    conn.execute(sql, (ids["L"], "hidden", ago(50), ago(40)))
    conn.execute(sql, (ids["L"], "shown", ago(40), ago(5)))
    conn.execute(sql, (ids["L"], "hidden", ago(5), ago(1)))
    conn.execute(sql, (ids["R"], "shown", ago(90), ago(80)))
    return ids


def test_old_runs_go_but_each_monitors_current_run_and_latest_layout_stay(
    conn: Connection, media: MediaStore
) -> None:
    seed_history(conn)
    removed = apply_retention(conn, media, {"screenshots": 30, "frame_text": 30}, NOW)
    assert removed["monitor_history"] == 2 + 1
    assert rows(conn, "SELECT state, checked_ms FROM monitor_states ORDER BY id") == [
        ("shown", ago(5)),
        ("hidden", ago(1)),
        ("shown", ago(80)),
    ]
    assert rows(conn, "SELECT start_ms FROM monitor_layouts ORDER BY start_ms") == [
        (ago(35),),
        (ago(5),),
    ]
    assert count(conn, "layout_monitors") == 2
    assert count(conn, "monitors") == 2


def test_the_cutoff_is_the_longer_of_the_two_limits(conn: Connection, media: MediaStore) -> None:
    seed_history(conn)
    removed = apply_retention(conn, media, {"screenshots": 30, "frame_text": 45}, NOW)
    assert removed["monitor_history"] == 1
    assert count(conn, "monitor_states") == 4
    assert count(conn, "monitor_layouts") == 3


def test_the_latest_layout_survives_even_when_everything_is_old(
    conn: Connection, media: MediaStore
) -> None:
    seed_history(conn)
    conn.execute("UPDATE monitor_layouts SET start_ms = start_ms - ?", (100 * DAY_MS,))
    apply_retention(conn, media, {"screenshots": 30, "frame_text": 30}, NOW)
    assert count(conn, "monitor_layouts") == 1


@pytest.mark.parametrize("limits", [{"screenshots": 30}, {"frame_text": 30}, {}])
def test_monitor_history_is_kept_when_either_limit_is_forever(
    conn: Connection, media: MediaStore, limits: dict[str, int | None]
) -> None:
    seed_history(conn)
    removed = apply_retention(conn, media, limits, NOW)
    assert "monitor_history" not in removed
    assert count(conn, "monitor_states") == 5
    assert count(conn, "monitor_layouts") == 3


def test_deleting_in_batches_handles_more_rows_than_one_batch(
    conn: Connection, media: MediaStore
) -> None:
    conn.execute("INSERT INTO monitors (key, name) VALUES ('L', 'Left')")
    monitor = conn.execute("SELECT id FROM monitors").fetchone()[0]
    conn.executemany(
        "INSERT INTO monitor_states (monitor_id, state, start_ms, checked_ms) VALUES (?, 'shown',"
        " ?, ?)",
        [(monitor, ago(100) + i * 2, ago(100) + i * 2) for i in range(1_500)],
    )
    removed = apply_retention(conn, media, {"screenshots": 30, "frame_text": 30}, NOW)
    assert removed["monitor_history"] == 1_499
    assert count(conn, "monitor_states") == 1


# --- Delete all ------------------------------------------------------------------------------


def test_delete_all_empties_the_monitor_tables(conn: Connection, media: MediaStore) -> None:
    ids = seed_history(conn)
    frame(conn, ids["L"], ago(1), 1, 2)
    delete_all_recorded_data(conn, media)
    for table in (
        "monitors",
        "monitor_layouts",
        "layout_monitors",
        "monitor_states",
        "frame_windows",
        "frames",
    ):
        assert count(conn, table) == 0
