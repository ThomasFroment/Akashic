"""Keyword search over frames: query building, ranking, filters, browsing, neighbors."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from akashic.search import keyword
from akashic.search.keyword import Filters
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore, NewFrame
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths


@pytest.fixture
def conn(paths: Paths) -> Iterator[Connection]:
    key = DataKey.generate()
    c = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(c)
    store = FrameStore(c, MediaStore(paths.media_dir, key.subkey("media")))
    rows = [
        (1_000, "chrome.exe", "Un café à Paris", "Recette du café crème et du croissant", None),
        (2_000, "code.exe", "main.py", "def search(): return the cafe menu", None),
        (3_000, "chrome.exe", "Minlo stream", "Minlo plays a game about trains", b"img"),
        (4_000, "discord.exe", "General", None, None),
    ]
    for ts, proc, title, text, image in rows:
        store.add(
            NewFrame(ts, 0, "idle", process=proc, window_title=title, text=text, image_webp=image)
        )
    yield c
    c.close()


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
    assert {h.id for h in keyword.search(conn, "CAFE")} == {1, 2}
    assert {h.id for h in keyword.search(conn, "café")} == {1, 2}


def test_prefix_match_on_last_word(conn: Connection) -> None:
    assert [h.id for h in keyword.search(conn, "minlo pla")] == [3]
    assert keyword.search(conn, "pla minlo") == []


def test_snippet_marks_matches_with_control_characters(conn: Connection) -> None:
    hit = keyword.search(conn, "croissant")[0]
    assert f"{keyword.HIGHLIGHT_START}croissant{keyword.HIGHLIGHT_END}" in hit.snippet
    assert not hit.has_image
    assert keyword.search(conn, "trains")[0].has_image


def test_filters_by_time_and_process(conn: Connection) -> None:
    assert [h.id for h in keyword.search(conn, "cafe", Filters(start_ms=1_500))] == [2]
    assert [h.id for h in keyword.search(conn, "cafe", Filters(end_ms=1_500))] == [1]
    assert [h.id for h in keyword.search(conn, "cafe", Filters(process="CODE.EXE"))] == [2]


def test_browse_orders_by_time(conn: Connection) -> None:
    assert [h.id for h in keyword.browse(conn)] == [4, 3, 2, 1]
    assert [h.id for h in keyword.browse(conn, newest_first=False)] == [1, 2, 3, 4]
    assert [h.id for h in keyword.browse(conn, Filters(process="chrome.exe"))] == [3, 1]
    assert keyword.browse(conn, limit=2, offset=1)[0].id == 3


def test_neighbors_and_processes(conn: Connection) -> None:
    assert keyword.neighbors(conn, 1) == (None, 2)
    assert keyword.neighbors(conn, 3) == (2, 4)
    assert keyword.neighbors(conn, 4) == (3, None)
    assert keyword.neighbors(conn, 999) == (None, None)
    assert keyword.processes(conn) == ["chrome.exe", "code.exe", "discord.exe"]
