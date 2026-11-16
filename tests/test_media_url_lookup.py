"""MediaLog.tab_frames reads the per-window rows, focused or not."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_data import add_frame, add_window

from akashic.storage.sessions import MediaLog

if TYPE_CHECKING:
    from sqlite3 import Connection

URL = "https://youtube.com/watch?v=abc"


def _lookup(conn: Connection, title: str = "Video A", since_ms: int = 0) -> list[tuple[str, str]]:
    return MediaLog(conn).tab_frames("zen.exe", title, since_ms)


def test_returns_title_and_address_of_an_unfocused_window(conn: Connection) -> None:
    add_frame(conn, 500, None, "Video A - YouTube", process="zen.exe", url=URL, focused=False)
    assert _lookup(conn) == [("Video A - YouTube", URL)]


def test_only_the_same_process(conn: Connection) -> None:
    add_frame(conn, 500, None, "Elden Ring - YouTube", process="chrome.exe", url=URL)
    assert _lookup(conn, "Elden Ring") == []


def test_rows_without_url_are_skipped(conn: Connection) -> None:
    add_frame(conn, 500, None, "Video A - YouTube", process="zen.exe", url=None)
    assert _lookup(conn) == []


def test_title_match_is_case_sensitive(conn: Connection) -> None:
    add_frame(conn, 500, None, "video a - YouTube", process="zen.exe", url=URL)
    assert _lookup(conn) == []


def test_frames_before_since_are_ignored(conn: Connection) -> None:
    add_frame(conn, 499, None, "Video A - YouTube", process="zen.exe", url=URL)
    add_frame(conn, 500, None, "Video A - YouTube", process="zen.exe", url=URL + "2")
    assert _lookup(conn, since_ms=500) == [("Video A - YouTube", URL + "2")]


def test_hidden_and_shell_rows_are_never_returned(conn: Connection) -> None:
    frame = add_frame(conn, 500, None, "Other", process="code.exe")
    add_window(conn, frame, 1, None, role="hidden")
    add_window(conn, frame, 2, None, role="shell")
    assert _lookup(conn, "") == []


def test_unfocused_browser_found_when_the_focused_window_is_another_app(conn: Connection) -> None:
    frame = add_frame(conn, 500, None, "Editor", process="code.exe")
    add_window(conn, frame, 1, None, process="zen.exe", title="Video A - YouTube", url=URL)
    assert _lookup(conn) == [("Video A - YouTube", URL)]


def test_unfocused_browser_found_when_no_window_is_focused(conn: Connection) -> None:
    frame = add_frame(conn, 500, None, "Editor", process="code.exe", focused=False)
    add_window(conn, frame, 1, None, process="zen.exe", title="Video A - YouTube", url=URL)
    assert _lookup(conn) == [("Video A - YouTube", URL)]


def test_newest_frame_first(conn: Connection) -> None:
    add_frame(conn, 900, None, "Video A - old", process="zen.exe", url=URL + "old")
    add_frame(conn, 1000, None, "Video A - new", process="zen.exe", url=URL + "new")
    assert [t for t, _ in _lookup(conn)] == ["Video A - new", "Video A - old"]


def test_in_a_tie_the_newest_window_row_comes_first(conn: Connection) -> None:
    frame = add_frame(conn, 500, None, "Video A - first", process="zen.exe", url=URL + "1")
    add_window(conn, frame, 1, None, process="zen.exe", title="Video A - second", url=URL + "2")
    assert [t for t, _ in _lookup(conn)] == ["Video A - second", "Video A - first"]
