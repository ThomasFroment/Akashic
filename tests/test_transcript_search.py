"""Transcript search and browse over stored speech."""

from __future__ import annotations

from typing import TYPE_CHECKING

from akashic.search import transcripts
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.crypto import DataKey
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Paths


def test_transcript_search_and_browse(conn: Connection, paths: Paths) -> None:
    audio = AudioStore(conn, MediaStore(paths.media_dir, DataKey.generate().subkey("media")))
    first = audio.add(NewSegment("mic", 1_000, 3_000, 1_500, ("discord.exe",)), b"x", 0)
    second = audio.add(NewSegment("system", 5_000, 7_000, 1_500), b"x", 0)
    pending = audio.add(NewSegment("mic", 9_000, 10_000, 500), b"x", 0)
    audio.set_transcript(first, "done", "fr", "On parle du café de Minlo")
    audio.set_transcript(second, "done", "en", "Minlo said something about a game")
    assert pending

    found = transcripts.search(conn, "minlo")
    assert [u.id for u in found] == [second, first]  # newest first
    assert found[1].sources == ("discord.exe",)
    assert "\x02Minlo\x03" in found[1].snippet
    assert [u.id for u in transcripts.search(conn, "cafe")] == [first]  # accents folded
    assert [u.id for u in transcripts.search(conn, "gam")] == [second]  # last word is a prefix
    assert transcripts.search(conn, "minlo", start_ms=4_000) == [found[0]]
    assert transcripts.search(conn, "minlo", limit=1, offset=1)[0].id == first
    assert transcripts.search(conn, "  ?! ") == []

    assert [u.id for u in transcripts.browse(conn)] == [second, first]
    assert [u.id for u in transcripts.browse(conn, newest_first=False)] == [first, second]
    assert [u.id for u in transcripts.browse(conn, 0, 4_000)] == [first]
