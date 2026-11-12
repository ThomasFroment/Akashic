"""Minimal parent/child messaging over the child's inherited stdin/stdout pipes.

Protocol, parent -> child (child's stdin):
  1. exactly 32 raw bytes: the data key (never in argv, environment or on disk);
  2. then JSON lines: {"type": "command", "name": "stop" | "pause" | "resume"}.
  EOF on stdin means the parent is gone: the child must stop.
Child -> parent (child's stdout): JSON lines {"type": "status", "state": "<state>"}.
Children must never print to stdout; it is the status channel.
"""

from __future__ import annotations

import json
from typing import IO, TYPE_CHECKING, Final, Literal, Protocol, cast, get_args

from akashic.jsonish import as_dict
from akashic.storage.crypto import KEY_BYTES, DataKey

if TYPE_CHECKING:
    from collections.abc import Iterator

type Command = Literal["stop", "pause", "resume"]
type State = Literal["starting", "ready", "gaming", "backlog", "paused", "stopping"]
COMMANDS: Final[tuple[str, ...]] = get_args(Command.__value__)
STATES: Final[tuple[str, ...]] = get_args(State.__value__)
MAX_LINE: Final = 4096


class KeyReader(Protocol):
    def readinto(self, buffer: memoryview, /) -> int | None: ...


class LineReader(Protocol):
    def readline(self, size: int = -1, /) -> bytes: ...


class ParentStream(KeyReader, LineReader, Protocol):
    """What a child reads from: its stdin (a BufferedReader at runtime)."""


class ProtocolError(Exception):
    """The peer sent something that isn't part of the protocol."""


def send_key(stream: IO[bytes], key: DataKey) -> None:
    stream.write(key.raw())
    stream.flush()


def read_key(stream: KeyReader) -> DataKey:
    buf = bytearray(KEY_BYTES)
    view = memoryview(buf)
    got = 0
    try:
        while got < KEY_BYTES:
            n = stream.readinto(view[got:])
            if not n:
                msg = "stdin closed before the data key arrived"
                raise ProtocolError(msg)
            got += n
        return DataKey(bytes(buf))
    finally:
        view.release()
        for i in range(KEY_BYTES):
            buf[i] = 0


def _write(stream: IO[bytes], message: dict[str, str]) -> None:
    stream.write(json.dumps(message).encode("utf-8") + b"\n")
    stream.flush()


def _read_lines(stream: LineReader) -> Iterator[dict[str, object]]:
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            msg = "message too long"
            raise ProtocolError(msg)
        try:
            message = as_dict(json.loads(line))
        except json.JSONDecodeError as exc:
            msg = "message is not JSON"
            raise ProtocolError(msg) from exc
        if message is None:
            msg = "message is not an object"
            raise ProtocolError(msg)
        yield message


def send_command(stream: IO[bytes], command: Command) -> None:
    _write(stream, {"type": "command", "name": command})


def read_commands(stream: LineReader) -> Iterator[Command]:
    """Yield commands until EOF. Raises ProtocolError on anything unexpected."""
    for message in _read_lines(stream):
        name = message.get("name")
        if message.get("type") != "command" or name not in COMMANDS:
            msg = "unexpected message from parent"
            raise ProtocolError(msg)
        yield cast("Command", name)


def send_status(stream: IO[bytes], state: State) -> None:
    _write(stream, {"type": "status", "state": state})


def read_statuses(stream: LineReader) -> Iterator[State]:
    for message in _read_lines(stream):
        state = message.get("state")
        if message.get("type") != "status" or state not in STATES:
            msg = "unexpected message from child"
            raise ProtocolError(msg)
        yield cast("State", state)
