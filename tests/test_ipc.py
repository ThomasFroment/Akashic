from __future__ import annotations

import io

import pytest

from akashic import ipc
from akashic.storage.crypto import DataKey


class Trickle(io.RawIOBase):
    """Returns at most 5 bytes per read, like a pipe under load."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]  # narrower buffer type is fine here
        n = min(5, len(buffer), len(self._data))
        buffer[:n] = self._data[:n]
        self._data = self._data[n:]
        return n


def test_key_roundtrip() -> None:
    key = DataKey.generate()
    buf = io.BytesIO()
    ipc.send_key(buf, key)
    ipc.send_command(buf, "pause")
    buf.seek(0)
    assert ipc.read_key(buf).raw() == key.raw()
    assert list(ipc.read_commands(buf)) == ["pause"]


def test_key_read_in_pieces() -> None:
    key = DataKey.generate()
    stream = io.BufferedReader(Trickle(key.raw()), buffer_size=1)
    assert ipc.read_key(stream).raw() == key.raw()  # pyright: ignore[reportArgumentType]


def test_short_key() -> None:
    with pytest.raises(ipc.ProtocolError):
        ipc.read_key(io.BytesIO(b"x" * 10))


def test_commands_and_statuses() -> None:
    buf = io.BytesIO()
    for c in ("pause", "resume", "stop"):
        ipc.send_command(buf, c)
    buf.seek(0)
    assert list(ipc.read_commands(buf)) == ["pause", "resume", "stop"]

    buf = io.BytesIO()
    ipc.send_status(buf, "ready")
    ipc.send_status(buf, "paused")
    buf.seek(0)
    assert list(ipc.read_statuses(buf)) == ["ready", "paused"]


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b"[1]\n",
        b'{"type": "command", "name": "self-destruct"}\n',
        b'{"type": "status", "state": "ready"}\n',
        b'{"type": "command", "name": "' + b"x" * 5000 + b'"}\n',
    ],
)
def test_bad_commands(line: bytes) -> None:
    with pytest.raises(ipc.ProtocolError):
        list(ipc.read_commands(io.BytesIO(line)))


def test_bad_status() -> None:
    with pytest.raises(ipc.ProtocolError):
        list(ipc.read_statuses(io.BytesIO(b'{"type": "status", "state": "on fire"}\n')))
