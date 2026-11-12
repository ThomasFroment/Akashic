"""llama-server lifecycle and a small client for it. The worker owns this; nothing else starts it.

The pinned ``llama-server.exe`` (``models.LLAMA_RUNTIME``) runs in router mode, with chat and
embeddings allowed to remain resident together (see docs/RETRIEVAL.md). It starts on first use
and stops again after ``idle_s`` without a request. Rules from
AGENTS.md, enforced here and covered by tests:

- listens on 127.0.0.1 only, on a free port picked at each start;
- a random API key per start, passed in the ``LLAMA_API_KEY`` environment variable, never in
  argv and never logged;
- ``--offline`` and ``--no-webui``; never ``-hf``, ``--model-url`` or anything that downloads;
- the program dies with the worker (the launcher puts it in a job object).

Requests only go to the address the server announced, which is checked to be loopback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import numpy as np

from akashic.storage.embeddings import unit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from pathlib import Path

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

log = logging.getLogger(__name__)

HOST: Final = "127.0.0.1"
START_TIMEOUT_S: Final = 90.0
REQUEST_TIMEOUT_S: Final = 120.0
LAST_ATTEMPT: Final = 2
EMBED_BATCH: Final = 32
MAX_EMBED_CHARS: Final = 2_000
# Qwen3-Embedding is trained so that its first dimensions stand on their own ("Matryoshka"
# truncation): 256 of its 1024 lose little and make vectors 4x smaller (8x as float16).
EMBED_DIMS: Final = 256
QUERY_INSTRUCTION: Final = (
    "Instruct: Given a question about what the user saw, heard or said on their computer, "
    "retrieve the passages that answer it\nQuery: "
)


EMBEDDING_FLAGS: Final = {
    "embeddings": "true",
    "pooling": "last",
    "batch-size": "2048",
    "ubatch-size": "2048",
}
# Chat models: everything on the GPU, room for the evidence of a question plus the answer, and
# no hidden "thinking" (it would only make answers slower; the planner and the answer need none).
CHAT_FLAGS: Final = {"ctx-size": "16384", "n-gpu-layers": "99", "reasoning": "off"}
type Presets = Mapping[str, Mapping[str, str]]


def render_presets(presets: Presets) -> str:
    """The INI text of ``--models-preset``: one section per model folder, flags without dashes
    in front. Flags are per model because ``--embeddings`` must not reach a chat model."""
    lines: list[str] = []
    for model, flags in presets.items():
        lines.append(f"[{model}]")
        lines.extend(f"{flag} = {value}" for flag, value in flags.items())
        lines.append("")
    return "\n".join(lines)


class LlamaError(Exception):
    """The server could not be started or answered badly. Messages never quote content."""


class Running(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


type Launcher = Callable[[Sequence[str], Mapping[str, str]], Running]
# (method, url, api key, JSON body or None) -> parsed JSON answer
type Transport = Callable[[str, str, str, dict[str, object] | None], object]
# (url, api key, JSON body) -> the JSON objects of a server-sent-events answer, one per event
type StreamTransport = Callable[[str, str, dict[str, object]], Iterator[object]]


def _open(method: str, url: str, key: str, body: dict[str, object] | None) -> Any:  # noqa: ANN401 - urllib's response object
    """Open ``url`` on the loopback server; refuses any other address before any I/O."""
    if not url.startswith(f"http://{HOST}:"):
        msg = "refusing to talk to anything but the local server"
        raise LlamaError(msg)
    request = urllib.request.Request(  # noqa: S310 - loopback checked above
        url,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S)  # noqa: S310


def _failure(exc: Exception) -> LlamaError:
    """The error for a failed request; never quotes the content."""
    if isinstance(exc, urllib.error.HTTPError):
        return LlamaError(f"request failed: HTTP {exc.code}")
    return LlamaError(f"request failed: {type(exc).__name__}")


def http_transport(method: str, url: str, key: str, body: dict[str, object] | None) -> object:
    """Plain HTTP to the loopback server."""
    try:
        with _open(method, url, key, body) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _failure(exc) from exc


def http_stream(url: str, key: str, body: dict[str, object]) -> Iterator[object]:
    """POST ``body`` and yield the JSON of each server-sent event until ``[DONE]``."""
    try:
        with _open("POST", url, key, body) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    return
                yield json.loads(data)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _failure(exc) from exc


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


@dataclass(slots=True)
class _Live:
    process: Running
    base_url: str
    key: str


class LlamaServer:
    def __init__(  # noqa: PLR0913 - collaborators and tunables
        self,
        program: Path,
        models_dir: Path,
        launcher: Launcher,
        transport: Transport = http_transport,
        stream_transport: StreamTransport = http_stream,
        *,
        presets: Presets,
        preset_file: Path,
        models_max: int = 1,
        idle_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        port: Callable[[], int] = free_port,
    ) -> None:
        self._program = program
        self._models_dir = models_dir
        self._launcher = launcher
        self._transport = transport
        self._stream_transport = stream_transport
        self._presets = presets
        self._models_max = models_max
        self._preset_file = preset_file
        self._idle_s = idle_s
        self._clock = clock
        self._sleep = sleep
        self._port = port
        self._live: _Live | None = None
        self._last_used = 0.0

    def available(self) -> bool:
        return self._program.is_file()

    def command(self, port: int) -> list[str]:
        """The command line. No secrets in it: the key travels in the environment."""
        command = [
            str(self._program),
            "--host", HOST,
            "--port", str(port),
            "--no-webui",
            "--offline",
            "--models-dir", str(self._models_dir),
            "--models-max", str(self._models_max),
            "--sleep-idle-seconds", str(max(60, int(self._idle_s))),
        ]  # fmt: skip
        if self._presets:
            command += ["--models-preset", str(self._preset_file)]
        return command

    def _start(self) -> _Live:
        if not self.available():
            msg = "llama-server is not installed"
            raise LlamaError(msg)
        port, key = self._port(), secrets.token_urlsafe(32)
        if self._presets:
            self._preset_file.parent.mkdir(parents=True, exist_ok=True)
            self._preset_file.write_text(render_presets(self._presets), encoding="utf-8")
        process = self._launcher(self.command(port), {**os.environ, "LLAMA_API_KEY": key})
        base_url = f"http://{HOST}:{port}"
        deadline = self._clock() + START_TIMEOUT_S
        while True:
            if process.poll() is not None:
                msg = f"llama-server exited with code {process.poll()}"
                raise LlamaError(msg)
            try:
                self._transport("GET", f"{base_url}/health", key, None)
                break
            except LlamaError:
                if self._clock() > deadline:
                    process.terminate()
                    msg = "llama-server did not become ready"
                    raise LlamaError(msg) from None
                self._sleep(0.25)
        log.info("llama-server started")
        return _Live(process, base_url, key)

    def request(self, path: str, body: dict[str, object] | None = None) -> object:
        """POST ``body`` (GET when None) to ``path``, starting the server if needed. One restart
        is attempted when the server has died."""
        for attempt in (1, 2):
            if self._live is not None and self._live.process.poll() is not None:
                self._live = None  # crashed or killed: start a new one
            if self._live is None:
                self._live = self._start()
            live = self._live
            try:
                answer = self._transport(
                    "GET" if body is None else "POST", f"{live.base_url}{path}", live.key, body
                )
            except LlamaError:
                if attempt == LAST_ATTEMPT or live.process.poll() is None:
                    raise
                self._live = None
                continue
            self._last_used = self._clock()
            return answer
        msg = "unreachable"  # pragma: no cover
        raise LlamaError(msg)

    def stream(self, path: str, body: dict[str, object]) -> Iterator[object]:
        """POST ``body`` and yield the events of the streamed answer. The server is started if
        needed; a stream that breaks halfway is an error, not retried (the text so far is
        already with the caller)."""
        if self._live is not None and self._live.process.poll() is not None:
            self._live = None
        if self._live is None:
            self._live = self._start()
        live = self._live
        for event in self._stream_transport(f"{live.base_url}{path}", live.key, body):
            self._last_used = self._clock()
            yield event
        self._last_used = self._clock()

    def running(self) -> bool:
        return self._live is not None and self._live.process.poll() is None

    def stop(self) -> None:
        if self._live is not None:
            self._live.process.terminate()
            self._live = None
            log.info("llama-server stopped")

    def unload_if_idle(self, *, force: bool = False) -> None:
        if self._live is not None and (force or self._clock() - self._last_used >= self._idle_s):
            self.stop()


class LlamaEmbedder:
    """Text embeddings through the server. Vectors come back unit length."""

    def __init__(self, server: LlamaServer, model: str, *, dimensions: int = EMBED_DIMS) -> None:
        self._server = server
        self.model = model
        self._dimensions = dimensions

    def available(self) -> bool:
        return self._server.available()

    def _embed(self, texts: Sequence[str]) -> list[Vector]:
        vectors: list[Vector] = []
        for start in range(0, len(texts), EMBED_BATCH):
            batch = [t[:MAX_EMBED_CHARS] or " " for t in texts[start : start + EMBED_BATCH]]
            answer = self._server.request("/v1/embeddings", {"model": self.model, "input": batch})
            vectors.extend(
                unit(v[: self._dimensions]) for v in _parse_embeddings(answer, len(batch))
            )
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return self._embed(texts)

    def embed_query(self, text: str) -> Vector:
        return self._embed([QUERY_INSTRUCTION + text])[0]

    def unload_if_idle(self, *, force: bool = False) -> None:
        self._server.unload_if_idle(force=force)


def _parse_embeddings(answer: object, expected: int) -> list[Vector]:
    items = answer.get("data") if isinstance(answer, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(items, list) or len(items) != expected:  # pyright: ignore[reportUnknownArgumentType]
        msg = "unexpected embeddings answer"
        raise LlamaError(msg)
    found: dict[int, Vector] = {}
    for position, item in enumerate(items):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        if not isinstance(item, dict):
            msg = "unexpected embeddings answer"
            raise LlamaError(msg)
        raw = item.get("embedding")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        index = item.get("index", position)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(raw, list) or not isinstance(index, int):
            msg = "unexpected embeddings answer"
            raise LlamaError(msg)
        numbers = [float(x) for x in cast("list[float]", raw)]
        found[index] = unit(np.asarray(numbers, dtype=np.float32))
    if sorted(found) != list(range(expected)):
        msg = "unexpected embeddings answer"
        raise LlamaError(msg)
    return [found[i] for i in range(expected)]


_THINKING = re.compile(r"<think>.*?(?:</think>|$)", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove a ``<think>…</think>`` block some models emit despite being told not to."""
    return _THINKING.sub("", text).strip()


class LlamaChat:
    """Chat completions through the server: one JSON-constrained call, and a streamed one."""

    def __init__(self, server: LlamaServer, model: str) -> None:
        self._server = server
        self.model = model

    def available(self) -> bool:
        return self._server.available()

    def _body(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def complete_json(
        self, system: str, user: str, schema: Mapping[str, object], max_tokens: int = 400
    ) -> str:
        """One answer that must fit ``schema`` (the server turns it into a grammar)."""
        body = self._body(system, user, max_tokens, 0.0)
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": schema, "strict": True},
        }
        answer = self._server.request("/v1/chat/completions", body)
        return strip_thinking(_message_text(answer))

    def stream(self, system: str, user: str, max_tokens: int = 900) -> Iterator[str]:
        """The answer, piece by piece as the model writes it."""
        body = self._body(system, user, max_tokens, 0.2)
        body["stream"] = True
        for event in self._server.stream("/v1/chat/completions", body):
            piece = _delta_text(event)
            if piece:
                yield piece

    def unload_if_idle(self, *, force: bool = False) -> None:
        self._server.unload_if_idle(force=force)


def _message_text(answer: object) -> str:
    choices = answer.get("choices") if isinstance(answer, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    first = choices[0] if isinstance(choices, list) and choices else None  # pyright: ignore[reportUnknownVariableType]
    message = first.get("message") if isinstance(first, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    content = message.get("content") if isinstance(message, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(content, str):
        msg = "unexpected chat answer"
        raise LlamaError(msg)
    return content


def _delta_text(event: object) -> str:
    choices = event.get("choices") if isinstance(event, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    first = choices[0] if isinstance(choices, list) and choices else None  # pyright: ignore[reportUnknownVariableType]
    delta = first.get("delta") if isinstance(first, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    content = delta.get("content") if isinstance(delta, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return content if isinstance(content, str) else ""
