"""What an export keeps: per table, for the whole store, a range and each category."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from export_world import HIGH, LOW, World, exported, vec
from memory_data import MINUTE, NOW

from akashic.storage.export import CATEGORIES, ExportScope
from akashic.storage.retention import PURGERS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

pytestmark = pytest.mark.usefixtures("fast_keywrap")

RANGE = ExportScope(LOW, HIGH)
FTS_INDEXES = {
    "frame_windows_fts": "frame_windows",
    "media_sessions_fts": "media_sessions",
    "audio_segments_fts": "audio_segments",
    "episodes_fts": "episodes",
}
FILTERED = {
    "frames",
    "frame_windows",
    "window_passages",
    "media_sessions",
    "media_events",
    "app_sessions",
    "game_sessions",
    "audio_segments",
    "segment_passages",
    "jobs",
    "capture_gaps",
    "monitor_states",
    "speakers",
    "speaker_turns",
    "episodes",
    "episode_people",
    "episode_items",
    "embeddings",
    "state",
}
EMPTIED = {
    "questions",
    "question_segments",
    "question_windows",
    "stt_runs",
    "stt_results",
    "stt_samples",
    "process_info",
}
KEPT_WHOLE = {"monitors", "monitor_layouts", "layout_monitors", "sqlite_sequence"}
SHADOWS = ("data", "idx", "content", "docsize", "config")


def only(*names: str) -> ExportScope:
    return ExportScope(categories=frozenset(names))


def without(*names: str) -> ExportScope:
    return ExportScope(categories=CATEGORIES - set(names))


def column(conn: Connection, sql: str, *args: object) -> list[Any]:
    return [row[0] for row in conn.execute(sql, args).fetchall()]


def count(conn: Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed names


def export_files(target: Path) -> set[str]:
    return {p.relative_to(target / "media").as_posix() for p in (target / "media").rglob("*.enc")}


@pytest.fixture
def ranged(world: World, tmp_path: Path) -> Iterator[Connection]:
    with exported(world, tmp_path, RANGE) as conn:
        yield conn


# --- no range, no categories ---


def test_everything_keeps_every_captured_row(world: World, tmp_path: Path) -> None:
    kept = [
        *("frames", "frame_windows", "window_passages", "audio_segments", "segment_passages"),
        *("media_sessions", "media_events", "app_sessions", "game_sessions", "episodes"),
        *("episode_items", "episode_people", "speakers", "speaker_turns", "monitors"),
        *("monitor_layouts", "layout_monitors", "monitor_states"),
    ]
    with exported(world, tmp_path, ExportScope()) as conn:
        for table in kept:
            query = f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 - fixed names
            assert conn.execute(query).fetchall() == world.conn.execute(query).fetchall(), table
        query = "SELECT * FROM embeddings WHERE owner_kind != 'query' ORDER BY owner_kind, owner_id"
        assert conn.execute(query).fetchall() == world.conn.execute(query).fetchall()
        assert count(conn, "capture_gaps") == count(world.conn, "capture_gaps") + 1


def test_everything_still_empties_questions_comparisons_process_info_and_query_vectors(
    world: World, tmp_path: Path
) -> None:
    with exported(world, tmp_path, ExportScope()) as conn:
        for table in EMPTIED:
            assert count(conn, table) == 0, table
        assert count(conn, "embeddings") > 0
        assert column(conn, "SELECT count(*) FROM embeddings WHERE owner_kind = 'query'") == [0]


def test_everything_keeps_only_pending_jobs_and_resets_running_ones(
    world: World, tmp_path: Path
) -> None:
    with exported(world, tmp_path, ExportScope()) as conn:
        assert conn.execute("SELECT key, state FROM jobs ORDER BY key").fetchall() == [
            ("embed:run_in", "pending"),
            ("embed:run_out", "pending"),
            ("transcribe:pend_in", "pending"),
            ("transcribe:pend_out", "pending"),
        ]


# --- a range ---


def test_a_range_keeps_the_frames_inside_with_their_windows_and_passages(
    world: World, ranged: Connection
) -> None:
    ids = world.ids
    assert set(column(ranged, "SELECT id FROM frames")) == {ids["frame_in"], ids["frame_edgewin"]}
    assert set(column(ranged, "SELECT DISTINCT frame_id FROM frame_windows")) == {
        ids["frame_in"],
        ids["frame_edgewin"],
    }
    assert set(column(ranged, "SELECT window_id FROM window_passages")) == {
        ids["win_in"],
        ids["win_edgewin"],
    }


def test_a_range_keeps_the_audio_that_overlaps_it_and_drops_a_segment_ending_at_its_start(
    world: World, ranged: Connection
) -> None:
    ids = world.ids
    kept = {ids["seg_edge"], ids["seg_in"]}
    assert set(column(ranged, "SELECT id FROM audio_segments")) == kept
    assert set(column(ranged, "SELECT segment_id FROM segment_passages")) == kept
    assert (
        set(column(ranged, "SELECT owner_id FROM embeddings WHERE owner_kind = 'segment'")) == kept
    )
    assert ids["seg_touch"] not in kept


def test_a_range_keeps_overlapping_sessions_and_the_events_inside_it(
    world: World, ranged: Connection
) -> None:
    ids = world.ids
    assert set(column(ranged, "SELECT id FROM media_sessions")) == {
        ids["media_edge"],
        ids["media_in"],
    }
    assert ranged.execute(
        "SELECT media_session_id, count(*) FROM media_events GROUP BY 1 ORDER BY 1"
    ).fetchall() == [(ids["media_edge"], 1), (ids["media_in"], 2)]
    assert set(column(ranged, "SELECT id FROM app_sessions")) == {
        ids["visit_edge"],
        ids["visit_in"],
    }
    assert column(ranged, "SELECT id FROM game_sessions") == [ids["game_in"]]


def test_a_range_keeps_the_pending_and_running_jobs_inside_it(ranged: Connection) -> None:
    assert ranged.execute("SELECT key, state FROM jobs ORDER BY key").fetchall() == [
        ("embed:run_in", "pending"),
        ("transcribe:pend_in", "pending"),
    ]


def test_a_range_keeps_monitors_and_layouts_whole(world: World, ranged: Connection) -> None:
    for table in ("monitors", "monitor_layouts", "layout_monitors"):
        query = f"SELECT * FROM {table}"  # noqa: S608 - fixed names
        assert ranged.execute(query).fetchall() == world.conn.execute(query).fetchall()


def test_a_missing_bound_is_open(world: World, tmp_path: Path) -> None:
    ids = world.ids
    with exported(world, tmp_path, ExportScope(start_ms=LOW), "from") as conn:
        assert set(column(conn, "SELECT id FROM frames")) == {
            ids["frame_in"],
            ids["frame_edgewin"],
            ids["frame_after"],
            ids["frame_end"],
        }
        start = column(conn, "SELECT start_ms FROM capture_gaps WHERE end_ms IS NULL")[0]
        assert start == column(conn, "SELECT value FROM state")[0]
    with exported(world, tmp_path, ExportScope(end_ms=HIGH), "until") as conn:
        assert ids["frame_before"] in set(column(conn, "SELECT id FROM frames"))
        assert ids["frame_after"] not in set(column(conn, "SELECT id FROM frames"))


# --- episodes in a range ---


def test_episodes_outside_the_range_or_left_without_evidence_are_gone(
    world: World, ranged: Connection
) -> None:
    ids = world.ids
    kept = set(column(ranged, "SELECT id FROM episodes"))
    assert kept == {
        ids[f"ep_{name}"]
        for name in ("conv_edge", "conv_in", "video_in", "video_edge", "browse_edge", "browse_in")
    }
    assert column(
        ranged,
        "SELECT count(*) FROM episode_items i WHERE NOT EXISTS (SELECT 1"
        " FROM episodes e WHERE e.id = i.episode_id)",
    ) == [0]


def test_no_episode_item_points_at_a_deleted_row(ranged: Connection) -> None:
    tables = {
        "segment": "audio_segments",
        "media_session": "media_sessions",
        "app_session": "app_sessions",
        "game_session": "game_sessions",
    }
    for kind, table in tables.items():
        orphans = ranged.execute(
            "SELECT count(*) FROM episode_items WHERE item_kind = ?"  # noqa: S608
            f" AND item_id NOT IN (SELECT id FROM {table})",
            (kind,),
        ).fetchone()
        assert orphans == (0,), kind


def test_a_conversation_body_is_rebuilt_from_the_segments_left_and_loses_its_vector(
    world: World, ranged: Connection
) -> None:
    episode = world.ids["ep_conv_edge"]
    assert column(ranged, "SELECT body FROM episodes WHERE id = ?", episode) == ["wombatedge"]
    assert world.conn.execute("SELECT body FROM episodes WHERE id = ?", (episode,)).fetchone() == (
        "wombatpre wombatedge",
    )
    assert column(
        ranged,
        "SELECT count(*) FROM embeddings WHERE owner_kind = 'episode' AND owner_id = ?",
        episode,
    ) == [0]
    mismatches = ranged.execute(
        "SELECT count(*) FROM episodes e JOIN retained_episode_bodies r ON r.id = e.id"
        " WHERE e.body != r.body"
    ).fetchone()
    assert mismatches == (0,)


def test_episodes_whose_evidence_is_unchanged_keep_their_body_and_vector(
    world: World, ranged: Connection
) -> None:
    for name in ("conv_in", "video_in", "video_edge", "browse_in"):
        episode = world.ids[f"ep_{name}"]
        query = "SELECT body, url FROM episodes WHERE id = ?"
        assert (
            ranged.execute(query, (episode,)).fetchone()
            == world.conn.execute(query, (episode,)).fetchone()
        ), name
        vectors = "SELECT count(*) FROM embeddings WHERE owner_kind = 'episode' AND owner_id = ?"
        assert ranged.execute(vectors, (episode,)).fetchone() == (1,), name


def test_a_browsing_episode_across_the_edge_loses_body_url_and_vector_if_a_window_proves_it(
    world: World, ranged: Connection
) -> None:
    episode = world.ids["ep_browse_edge"]
    assert ranged.execute("SELECT body, url FROM episodes WHERE id = ?", (episode,)).fetchone() == (
        "",
        None,
    )
    assert column(
        ranged,
        "SELECT count(*) FROM embeddings WHERE owner_kind = 'episode' AND owner_id = ?",
        episode,
    ) == [0]


def test_a_browsing_episode_across_the_edge_with_no_window_in_range_is_deleted(
    world: World, ranged: Connection
) -> None:
    assert column(
        ranged, "SELECT count(*) FROM episodes WHERE id = ?", world.ids["ep_browse_lost"]
    ) == [0]


def test_no_embedding_is_left_without_its_owner(ranged: Connection) -> None:
    assert column(
        ranged,
        "SELECT count(*) FROM embeddings WHERE (owner_kind = 'segment' AND owner_id NOT IN"
        " (SELECT id FROM audio_segments)) OR (owner_kind = 'episode' AND owner_id NOT IN"
        " (SELECT id FROM episodes))",
    ) == [0]


# --- speakers, gaps, monitor states ---


def test_speakers_are_the_user_and_those_still_referenced_with_profiles_rebuilt(
    world: World, ranged: Connection
) -> None:
    ids = world.ids
    assert set(column(ranged, "SELECT id FROM speakers")) == {
        ids["spk_me"],
        ids["spk_a"],
        ids["spk_d"],
        ids["spk_e"],
    }
    profile = "SELECT vec_sum, vec_n FROM speakers WHERE id = ?"
    assert ranged.execute(profile, (ids["spk_a"],)).fetchone() == (vec(1, 0, 0, 0), 1)
    for name in ("me", "d", "e"):
        assert ranged.execute(profile, (ids[f"spk_{name}"],)).fetchone() == (None, 0)


def test_a_narrowed_export_drops_unreferenced_speakers_and_a_full_one_keeps_them(
    world: World, tmp_path: Path
) -> None:
    with exported(world, tmp_path, only("episodes")) as conn:
        assert world.ids["spk_c"] not in set(column(conn, "SELECT id FROM speakers"))
    with exported(world, tmp_path, ExportScope(), "all") as conn:
        assert count(conn, "speakers") == 6


def test_gaps_and_monitor_states_are_clipped_to_the_range(ranged: Connection) -> None:
    snapshot = column(ranged, "SELECT value FROM state")[0]
    assert snapshot > HIGH
    assert ranged.execute(
        "SELECT kind, reason, start_ms, end_ms FROM capture_gaps ORDER BY start_ms, id"
    ).fetchall() == [
        ("screen", "locked", LOW, NOW + 12 * MINUTE),
        ("system", "music", NOW + 13 * MINUTE, NOW + 14 * MINUTE),
        ("screen_text", "ocr_unavailable", NOW + 15 * MINUTE, HIGH),
        ("all", "ui_open", NOW + 18 * MINUTE, HIGH),
        ("all", "stopped", HIGH, None),
    ]
    assert ranged.execute(
        "SELECT state, start_ms, checked_ms FROM monitor_states ORDER BY start_ms"
    ).fetchall() == [
        ("shown", LOW, HIGH),
        ("dropped", NOW + 12 * MINUTE, NOW + 13 * MINUTE),
    ]


def test_a_row_ending_exactly_at_the_start_is_dropped_from_every_span_table(
    world: World, ranged: Connection
) -> None:
    assert world.ids["seg_touch"] not in set(column(ranged, "SELECT id FROM audio_segments"))
    assert column(ranged, "SELECT count(*) FROM capture_gaps WHERE reason = 'device_missing'") == [
        0
    ]
    assert column(ranged, "SELECT count(*) FROM monitor_states WHERE state = 'failed'") == [0]


# --- categories ---


def test_without_screenshots_no_image_or_capture_path_or_file_is_kept(
    world: World, tmp_path: Path
) -> None:
    with exported(world, tmp_path, without("screenshots")) as conn:
        assert column(conn, "SELECT count(*) FROM frames WHERE image_path IS NOT NULL") == [0]
        assert column(
            conn, "SELECT count(*) FROM frame_windows WHERE capture_path IS NOT NULL"
        ) == [0]
        assert count(conn, "frames") == count(world.conn, "frames")
    files = export_files(tmp_path / "out")
    assert not {f for f in files if f.startswith(("frames/", "windows/"))}
    assert any(f.startswith("audio/") for f in files)


def test_without_frame_text_no_window_text_or_source_is_kept(world: World, tmp_path: Path) -> None:
    with exported(world, tmp_path, without("frame_text")) as conn:
        assert column(
            conn,
            "SELECT count(*) FROM frame_windows WHERE text IS NOT NULL OR text_source IS NOT NULL",
        ) == [0]
        assert count(conn, "window_passages") == 0
        assert column(
            conn,
            "SELECT count(*) FROM frame_windows_fts WHERE frame_windows_fts MATCH 'quokkainside'",
        ) == [0]
        assert column(conn, "SELECT count(*) FROM frames WHERE image_path IS NOT NULL") == [5]


def test_without_screenshots_and_frame_text_no_frames_remain(world: World, tmp_path: Path) -> None:
    with exported(world, tmp_path, without("screenshots", "frame_text")) as conn:
        assert count(conn, "frames") == 0
        assert count(conn, "frame_windows") == 0
        assert count(conn, "window_passages") == 0
    assert not {f for f in export_files(tmp_path / "out") if f.startswith(("frames/", "windows/"))}


def test_without_raw_audio_no_audio_path_or_file_is_kept(world: World, tmp_path: Path) -> None:
    with exported(world, tmp_path, without("raw_audio")) as conn:
        assert column(conn, "SELECT count(*) FROM audio_segments WHERE audio_path IS NOT NULL") == [
            0
        ]
        assert column(conn, "SELECT count(*) FROM audio_segments WHERE transcript IS NOT NULL") == [
            6
        ]
    assert not {f for f in export_files(tmp_path / "out") if f.startswith("audio/")}


def test_without_transcripts_text_is_gone_and_what_follows_it_goes_too(
    world: World, tmp_path: Path
) -> None:
    with exported(world, tmp_path, without("transcripts")) as conn:
        assert column(conn, "SELECT count(*) FROM audio_segments WHERE transcript IS NOT NULL") == [
            0
        ]
        assert count(conn, "audio_segments") == 6
        assert count(conn, "segment_passages") == 0
        assert column(conn, "SELECT count(*) FROM embeddings WHERE owner_kind = 'segment'") == [0]
        assert column(conn, "SELECT body FROM episodes WHERE id = ?", world.ids["ep_conv_in"]) == [
            ""
        ]
        assert column(
            conn,
            "SELECT count(*) FROM audio_segments_fts WHERE audio_segments_fts MATCH 'wombatin'",
        ) == [0]


def test_without_raw_audio_and_transcripts_no_audio_segments_remain(
    world: World, tmp_path: Path
) -> None:
    with exported(world, tmp_path, without("raw_audio", "transcripts")) as conn:
        assert count(conn, "audio_segments") == 0
        assert count(conn, "speaker_turns") == 0
    assert not {f for f in export_files(tmp_path / "out") if f.startswith("audio/")}


def test_without_media_sessions_none_remain_and_neither_do_their_events(
    world: World, tmp_path: Path
) -> None:
    with exported(world, tmp_path, without("media_sessions")) as conn:
        assert count(conn, "media_sessions") == 0
        assert count(conn, "media_events") == 0


def test_without_app_sessions_no_app_or_game_session_remains(world: World, tmp_path: Path) -> None:
    with exported(world, tmp_path, without("app_sessions")) as conn:
        assert count(conn, "app_sessions") == 0
        assert count(conn, "game_sessions") == 0


def test_without_episodes_none_remain(world: World, tmp_path: Path) -> None:
    with exported(world, tmp_path, without("episodes")) as conn:
        assert count(conn, "episodes") == 0
        assert count(conn, "episode_items") == 0
        assert column(conn, "SELECT count(*) FROM embeddings WHERE owner_kind = 'episode'") == [0]
        assert count(conn, "audio_segments") == 6


def test_without_embeddings_no_vector_or_passage_remains(world: World, tmp_path: Path) -> None:
    with exported(world, tmp_path, without("embeddings")) as conn:
        assert count(conn, "embeddings") == 0
        assert count(conn, "segment_passages") == 0
        assert count(conn, "window_passages") == 0
        assert count(conn, "frame_windows") > 0


def test_only_the_chosen_categories_are_kept_whatever_the_pending_work(
    world: World, tmp_path: Path
) -> None:
    # Pending transcription jobs exist, yet an excluded category keeps nothing.
    with exported(world, tmp_path, only("episodes")) as conn:
        assert count(conn, "audio_segments") == 0
        assert column(conn, "SELECT count(*) FROM frames") == [0]


# --- full-text indexes ---


def test_a_phrase_found_only_in_out_of_scope_rows_matches_nothing(
    world: World, ranged: Connection
) -> None:
    searches = (
        ("frame_windows_fts", "quokkabefore"),
        ("frame_windows_fts", "quokkaafter"),
        ("audio_segments_fts", "wombatbefore"),
        ("audio_segments_fts", "wombattouch"),
        ("audio_segments_fts", "wombatpre"),
        ("audio_segments_fts", "wombatafter"),
        ("media_sessions_fts", "platypusbefore"),
        ("media_sessions_fts", "platypusafter"),
        ("episodes_fts", '"page after"'),
        ("episodes_fts", '"Lost page"'),
    )
    for index, phrase in searches:
        found = ranged.execute(
            f"SELECT count(*) FROM {index} WHERE {index} MATCH ?",  # noqa: S608 - fixed names
            (phrase,),
        ).fetchone()
        assert found == (0,), phrase
        live = world.conn.execute(
            f"SELECT count(*) FROM {index} WHERE {index} MATCH ?",  # noqa: S608 - fixed names
            (phrase,),
        ).fetchone()
        assert live == (1,), phrase
    inside = ranged.execute(
        "SELECT count(*) FROM frame_windows_fts WHERE frame_windows_fts MATCH 'quokkainside'"
    ).fetchone()
    assert inside == (1,)


def test_each_index_holds_exactly_the_rows_its_triggers_would_index(
    ranged: Connection,
) -> None:
    for index, content in FTS_INDEXES.items():
        where = " WHERE role = 'app'" if content == "frame_windows" else ""
        indexed = column(ranged, f"SELECT count(*) FROM {index}_docsize")  # noqa: S608
        rows = column(ranged, f"SELECT count(*) FROM {content}{where}")  # noqa: S608
        assert indexed == rows, index
        ranged.execute(f"INSERT INTO {index} ({index}) VALUES ('integrity-check')")  # noqa: S608


def test_the_rebuilt_window_index_equals_the_one_the_triggers_built(
    world: World, tmp_path: Path
) -> None:
    def indexed(conn: Connection) -> list[tuple[Any, ...]]:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT d.id, w.role, w.text, w.title, w.url FROM frame_windows_fts_docsize d"
                " JOIN frame_windows w ON w.id = d.id ORDER BY d.id"
            )
        ]

    live = indexed(world.conn)
    assert {role for _, role, *_ in live} == {"app"}
    assert count(world.conn, "frame_windows") > len(live)  # a shell row exists and is not indexed
    with exported(world, tmp_path, ExportScope()) as conn:
        assert indexed(conn) == live
        for term in ("quokkabefore", "quokkainside", "quokkaafter"):
            query = "SELECT rowid FROM frame_windows_fts WHERE frame_windows_fts MATCH ?"
            assert (
                conn.execute(query, (term,)).fetchall()
                == world.conn.execute(query, (term,)).fetchall()
            )


def test_every_retention_purger_has_an_export_category() -> None:
    assert frozenset(PURGERS) == CATEGORIES


def test_no_shadow_table_holds_a_term_of_a_removed_row(world: World, ranged: Connection) -> None:
    # Terms are stored prefix-compressed: the tail that differs between rows is what is searched.
    for index in FTS_INDEXES:
        for tail in (b"before", b"after", b"touch"):
            query = f"SELECT count(*) FROM {index}_data WHERE instr(block, ?) > 0"  # noqa: S608
            assert ranged.execute(query, (tail,)).fetchone() == (0,), (index, tail)
    live = world.conn.execute(
        "SELECT count(*) FROM audio_segments_fts_data WHERE instr(block, ?) > 0", (b"before",)
    ).fetchone()
    assert live[0] > 0


def test_the_database_is_vacuumed(ranged: Connection) -> None:
    assert ranged.execute("PRAGMA freelist_count").fetchone() == (0,)


# --- completeness ---


def unclassified(conn: Connection) -> set[str]:
    """Tables of the copy that are neither filtered, emptied, kept whole nor an index."""
    tables = set(column(conn, "SELECT name FROM sqlite_master WHERE type = 'table'"))
    indexes = set(
        column(
            conn,
            "SELECT name FROM sqlite_master WHERE type = 'table' AND sql LIKE '%USING fts5%'",
        )
    )
    shadows = {f"{index}_{suffix}" for index in indexes for suffix in SHADOWS}
    return tables - FILTERED - EMPTIED - KEPT_WHOLE - indexes - shadows


def test_every_table_of_the_copy_is_classified(ranged: Connection) -> None:
    assert unclassified(ranged) == set()
    ranged.execute("CREATE TABLE brand_new (x)")
    assert unclassified(ranged) == {"brand_new"}


def test_every_name_in_the_classification_is_a_table_of_the_copy(ranged: Connection) -> None:
    tables = set(column(ranged, "SELECT name FROM sqlite_master WHERE type = 'table'"))
    assert tables >= (FILTERED | EMPTIED | KEPT_WHOLE)


def test_emptied_tables_are_empty_and_kept_whole_tables_equal_the_live_ones(
    world: World, ranged: Connection
) -> None:
    for table in EMPTIED:
        assert count(ranged, table) == 0, table
    for table in KEPT_WHOLE - {"sqlite_sequence"}:
        query = f"SELECT * FROM {table}"  # noqa: S608 - fixed names
        assert ranged.execute(query).fetchall() == world.conn.execute(query).fetchall(), table
