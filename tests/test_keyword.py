"""Keyword search over the windows of frames: query building, ranking, filters, collapsing."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from memory_data import add_monitor, app_window, new_frame

from akashic.search import keyword
from akashic.search.keyword import Filters
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore, NewFrame, NewWindow
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths

MARKS = str.maketrans("", "", f"{keyword.HIGHLIGHT_START}{keyword.HIGHLIGHT_END}")


@pytest.fixture
def store(paths: Paths) -> Iterator[FrameStore]:
    key = DataKey.generate()
    c = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(c)
    add_monitor(c)
    frames = FrameStore(c, MediaStore(paths.media_dir, key.subkey("media")))
    rows = [
        (1_000, "chrome.exe", "Un café à Paris", "Recette du café crème et du croissant", None),
        (2_000, "code.exe", "main.py", "def search(): return the cafe menu", None),
        (3_000, "chrome.exe", "Minlo stream", "Minlo plays a game about trains", b"img"),
        (4_000, "discord.exe", "General", None, None),
    ]
    for ts, proc, title, text, image in rows:
        frames.add(new_frame(ts, process=proc, title=title, text=text, image=image))
    yield frames
    c.close()


@pytest.fixture
def conn(store: FrameStore) -> Connection:
    return store._conn


def win(  # noqa: PLR0913 - the fields the tests vary
    z: int,
    process: str,
    text: str | None,
    *,
    title: str = "T",
    url: str | None = None,
    hwnd: int | None = None,
) -> NewWindow:
    return app_window(
        process=process, title=title, url=url, text=text, z=z, hwnd=hwnd or z + 1, focused=z == 0
    )


def add(store: FrameStore, ts: int, *windows: NewWindow) -> int:
    return store.add(NewFrame(ts, 0, "idle", None, windows))


def plain(hit: keyword.Hit) -> str:
    return hit.snippet.translate(MARKS)


def test_build_match_quotes_words_and_prefixes_the_last() -> None:
    assert keyword.build_match("hello world") == '"hello" "world"*'
    assert keyword.build_match('a"b OR (c) NEAR') == '"a" "b" "OR" "c" "NEAR"*'
    assert keyword.build_match("  ***  ") is None
    assert keyword.build_match("") is None


def test_hostile_query_is_not_fts_syntax(conn: Connection) -> None:
    assert keyword.search(conn, 'cafe" OR text:*') == []
    assert keyword.search(conn, "NOT AND ( ) ^ :") == []
    assert keyword.search(conn, "***") == []


def test_diacritics_and_case_are_ignored(conn: Connection) -> None:
    assert {h.frame_id for h in keyword.search(conn, "CAFE")} == {1, 2}
    assert {h.frame_id for h in keyword.search(conn, "café")} == {1, 2}


def test_prefix_match_on_last_word(conn: Connection) -> None:
    assert [h.frame_id for h in keyword.search(conn, "minlo pla")] == [3]
    assert keyword.search(conn, "pla minlo") == []


def test_snippet_marks_matches_with_control_characters(conn: Connection) -> None:
    hit = keyword.search(conn, "croissant")[0]
    assert f"{keyword.HIGHLIGHT_START}croissant{keyword.HIGHLIGHT_END}" in hit.snippet
    assert not hit.has_image
    assert keyword.search(conn, "trains")[0].has_image


def test_filters_by_time_and_process(conn: Connection) -> None:
    assert [h.frame_id for h in keyword.search(conn, "cafe", Filters(start_ms=1_500))] == [2]
    assert [h.frame_id for h in keyword.search(conn, "cafe", Filters(end_ms=1_500))] == [1]
    assert [h.frame_id for h in keyword.search(conn, "cafe", Filters(process="CODE.EXE"))] == [2]


def test_browse_orders_by_time(conn: Connection) -> None:
    assert [h.frame_id for h in keyword.browse(conn)] == [4, 3, 2, 1]
    with pytest.raises(TypeError):
        keyword.browse(conn, newest_first=False)  # type: ignore[call-arg]
    assert [h.frame_id for h in keyword.browse(conn, Filters(process="chrome.exe"))] == [3, 1]
    assert keyword.browse(conn, limit=2, offset=1)[0].frame_id == 3


def test_neighbors_and_processes(conn: Connection) -> None:
    assert keyword.neighbors(conn, 1) == (None, 2)
    assert keyword.neighbors(conn, 3) == (2, 4)
    assert keyword.neighbors(conn, 4) == (3, None)
    assert keyword.neighbors(conn, 999) == (None, None)
    assert keyword.processes(conn) == ["chrome.exe", "code.exe", "discord.exe"]


def test_a_hit_is_one_window_with_its_own_fields(conn: Connection) -> None:
    hit = keyword.search(conn, "trains")[0]
    (window_id,) = conn.execute("SELECT id FROM frame_windows WHERE title = 'Minlo stream'")
    assert (hit.frame_id, hit.window_id, hit.ts_ms, hit.monitor_id) == (3, window_id[0], 3_000, 0)
    assert (hit.process, hit.title, hit.url, hit.has_image) == (
        "chrome.exe",
        "Minlo stream",
        None,
        True,
    )


# --- several windows on one frame ------------------------------------------------------------


def test_each_app_window_of_a_frame_is_its_own_hit_with_its_own_process(
    store: FrameStore, conn: Connection
) -> None:
    add(store, 10_000, win(0, "chrome.exe", "zebra alpha"), win(1, "code.exe", "zebra beta"))
    hits = keyword.search(conn, "zebra")
    assert {(h.process, plain(h)) for h in hits} == {
        ("chrome.exe", "zebra alpha"),
        ("code.exe", "zebra beta"),
    }
    assert len({h.window_id for h in hits}) == 2
    assert len({h.frame_id for h in hits}) == 1
    only_code = keyword.search(conn, "zebra", Filters(process="code.exe"))
    assert [plain(h) for h in only_code] == ["zebra beta"]
    browsed = keyword.browse(conn, Filters(start_ms=10_000, process="code.exe"))
    assert [h.process for h in browsed] == ["code.exe"]


def test_a_phrase_never_matches_across_two_windows(store: FrameStore, conn: Connection) -> None:
    add(store, 10_000, win(0, "chrome.exe", "pomme rouge"), win(1, "code.exe", "verte banane"))
    assert keyword.search(conn, "rouge verte") == []
    assert keyword.search(conn, "pomme rouge")
    assert keyword.search(conn, "verte banane")


def test_hidden_and_shell_rows_are_not_indexed(store: FrameStore, conn: Connection) -> None:
    frame = add(
        store,
        10_000,
        win(0, "chrome.exe", "visible words", title="Front"),
        NewWindow("hidden", 1, None, 0, 0, 10, 10),
        NewWindow("shell", 2, 5, 0, 0, 10, 10),
    )
    hits = keyword.browse(conn, Filters(start_ms=10_000))
    assert [(h.frame_id, h.title) for h in hits] == [(frame, "Front")]
    indexed = conn.execute(
        "SELECT count(*) FROM frame_windows w WHERE w.frame_id = ? AND w.role = 'app'", (frame,)
    ).fetchone()
    assert indexed == (1,)
    assert conn.execute("SELECT count(*) FROM frame_windows_fts_docsize").fetchone() == (5,)


def test_title_and_url_are_searchable(store: FrameStore, conn: Connection) -> None:
    add(store, 10_000, win(0, "chrome.exe", None, title="Quokka page", url="https://x.test/q"))
    assert len(keyword.search(conn, "quokka")) == 1
    assert len(keyword.search(conn, "x test")) == 1


def test_processes_lists_the_distinct_window_processes(store: FrameStore, conn: Connection) -> None:
    add(store, 10_000, win(0, "chrome.exe", "x"), win(1, "obs64.exe", "y"))
    assert keyword.processes(conn) == ["chrome.exe", "code.exe", "discord.exe", "obs64.exe"]


# --- consecutive identical hits collapse -------------------------------------------------------


def seq(store: FrameStore, texts: list[str], hwnd: int = 7, start: int = 20_000) -> None:
    for n, text in enumerate(texts):
        add(store, start + n, win(0, "chrome.exe", text, hwnd=hwnd))


def times(hits: list[keyword.Hit]) -> list[int]:
    return sorted(h.ts_ms for h in hits)


def test_a_run_of_identical_rows_of_one_hwnd_shows_its_first_row(
    store: FrameStore, conn: Connection
) -> None:
    seq(store, ["quux same", "quux same", "quux same"])
    assert times(keyword.search(conn, "quux")) == [20_000]


def test_identical_rows_separated_by_a_different_one_are_three_hits(
    store: FrameStore, conn: Connection
) -> None:
    seq(store, ["quux a", "quux b", "quux a"])
    assert times(keyword.search(conn, "quux")) == [20_000, 20_001, 20_002]


def test_identical_content_on_two_hwnds_is_two_hits(store: FrameStore, conn: Connection) -> None:
    add(store, 20_000, win(0, "chrome.exe", "quux same", hwnd=7))
    add(store, 20_001, win(0, "chrome.exe", "quux same", hwnd=8))
    assert len(keyword.search(conn, "quux")) == 2


def test_a_changed_title_ends_a_run(store: FrameStore, conn: Connection) -> None:
    add(store, 20_000, win(0, "chrome.exe", "quux same", title="One", hwnd=7))
    add(store, 20_001, win(0, "chrome.exe", "quux same", title="Two", hwnd=7))
    assert len(keyword.search(conn, "quux")) == 2


def test_limit_and_offset_apply_after_collapsing(store: FrameStore, conn: Connection) -> None:
    seq(store, ["quux a", "quux a", "quux a", "quux b", "quux b", "quux c"])
    assert len(keyword.search(conn, "quux")) == 3
    assert len(keyword.search(conn, "quux", limit=2)) == 2
    assert len(keyword.search(conn, "quux", limit=2, offset=2)) == 1


def test_browse_does_not_collapse(store: FrameStore, conn: Connection) -> None:
    seq(store, ["quux same", "quux same", "quux same"])
    assert len(keyword.browse(conn, Filters(start_ms=20_000))) == 3


def test_only_a_painted_window_of_a_stored_picture_has_an_image(
    store: FrameStore, conn: Connection
) -> None:
    shown = app_window(process="code.exe", text="lighthouse shown", z=0, hwnd=21, painted=True)
    pending = app_window(process="code.exe", text="lighthouse pending", z=1, hwnd=22, focused=False)
    store.add(NewFrame(9_000, 0, "idle", b"img", (shown, pending)))
    found = {plain(h): h.has_image for h in keyword.search(conn, "lighthouse")}
    assert found == {"lighthouse shown": True, "lighthouse pending": False}
    browsed = [h.has_image for h in keyword.browse(conn, Filters(start_ms=9_000, end_ms=9_001))]
    assert browsed == [True, False]  # ordered by z
