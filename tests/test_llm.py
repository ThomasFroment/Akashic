"""llama-server lifecycle and the embeddings client, with a fake process and transport."""

from __future__ import annotations

import http.server
import json
import threading
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest

from akashic.pipeline import llm
from akashic.pipeline.llm import (
    EMBED_DIMS,
    HOST,
    LlamaEmbedder,
    LlamaError,
    LlamaServer,
    http_transport,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path


class FakeProcess:
    def __init__(self) -> None:
        self.code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 0


class Harness:
    """A launcher and transport standing in for the real program."""

    def __init__(self, *, healthy_after: int = 0) -> None:
        self.launches: list[tuple[list[str], dict[str, str]]] = []
        self.processes: list[FakeProcess] = []
        self.requests: list[tuple[str, str, str, dict[str, object] | None]] = []
        self.health_calls = 0
        self.healthy_after = healthy_after
        self.fail_next: int = 0
        self.dim = 3

    def launcher(self, args: Sequence[str], env: Mapping[str, str]) -> FakeProcess:
        self.launches.append((list(args), dict(env)))
        process = FakeProcess()
        self.processes.append(process)
        return process

    def transport(self, method: str, url: str, key: str, body: dict[str, object] | None) -> object:
        self.requests.append((method, url, key, body))
        if url.endswith("/health"):
            self.health_calls += 1
            if self.health_calls <= self.healthy_after:
                raise LlamaError
            return {"status": "ok"}
        if self.fail_next:
            self.fail_next -= 1
            raise LlamaError
        assert body is not None
        texts = body["input"]
        assert isinstance(texts, list)
        return {
            "data": [
                {"index": i, "embedding": [float(i + 1)] + [0.0] * (self.dim - 1)}
                for i in reversed(range(len(cast("list[str]", texts))))  # out of order on purpose
            ]
        }


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_server(  # noqa: PLR0913 - a fake server and the knobs the scenarios turn
    tmp_path: Path,
    harness: Harness,
    clock: Clock,
    *,
    idle_s: float = 600.0,
    transport: llm.Transport | None = None,
    launcher: llm.Launcher | None = None,
) -> LlamaServer:
    program = tmp_path / "llama-server.exe"
    program.write_bytes(b"")
    return LlamaServer(
        program,
        tmp_path / "models",
        launcher or harness.launcher,
        transport or harness.transport,
        idle_s=idle_s,
        clock=clock,
        sleep=clock.sleep,
        port=lambda: 4242,
        presets={},
        preset_file=tmp_path / "models.ini",
    )


def test_the_command_is_local_offline_and_carries_no_secret(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    server = make_server(tmp_path, harness, clock)
    server.request("/v1/embeddings", {"model": "m", "input": ["x"]})
    args, env = harness.launches[0]
    key = env["LLAMA_API_KEY"]
    assert len(key) >= 32
    assert not any(key in a for a in args)
    assert args[args.index("--host") + 1] == HOST == "127.0.0.1"
    assert args[args.index("--port") + 1] == "4242"
    assert "--offline" in args
    assert "--no-webui" in args
    assert args[args.index("--models-dir") + 1] == str(tmp_path / "models")
    assert args[args.index("--models-max") + 1] == "1"
    for forbidden in ("-hf", "--hf-repo", "--model-url", "--hf-file"):
        assert forbidden not in args
    assert "0.0.0.0" not in args  # noqa: S104 - asserting it is absent
    # Every request carries that key and goes to the announced loopback address.
    assert {k for _, _, k, _ in harness.requests} == {key}
    assert all(u.startswith("http://127.0.0.1:4242/") for _, u, _, _ in harness.requests)


def test_each_start_gets_a_new_key(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    server = make_server(tmp_path, harness, clock)
    server.request("/v1/embeddings", {"input": ["x"]})
    server.stop()
    server.request("/v1/embeddings", {"input": ["x"]})
    keys = {env["LLAMA_API_KEY"] for _, env in harness.launches}
    assert len(harness.launches) == len(keys) == 2


def test_it_waits_for_the_server_to_become_healthy(tmp_path: Path) -> None:
    harness, clock = Harness(healthy_after=3), Clock()
    server = make_server(tmp_path, harness, clock)
    server.request("/v1/embeddings", {"input": ["x"]})
    assert harness.health_calls == 4
    assert clock.now == pytest.approx(0.75)


def test_a_server_that_never_gets_ready_is_stopped_and_reported(tmp_path: Path) -> None:
    harness, clock = Harness(healthy_after=10**9), Clock()
    server = make_server(tmp_path, harness, clock)
    with pytest.raises(LlamaError, match="did not become ready"):
        server.request("/v1/embeddings", {"input": ["x"]})
    assert harness.processes[0].terminated
    assert not server.running()


def test_a_server_that_exits_while_starting_is_reported_with_its_code(tmp_path: Path) -> None:
    harness, clock = Harness(healthy_after=10**9), Clock()

    def dying(args: Sequence[str], env: Mapping[str, str]) -> FakeProcess:
        process = harness.launcher(args, env)
        process.code = 3
        return process

    server = make_server(tmp_path, harness, clock, launcher=dying)
    with pytest.raises(LlamaError, match="exited with code 3"):
        server.request("/v1/embeddings", {"input": ["x"]})


def test_missing_program_is_a_clear_error(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    server = LlamaServer(
        tmp_path / "nope.exe",
        tmp_path,
        harness.launcher,
        harness.transport,
        presets={},
        preset_file=tmp_path / "models.ini",
    )
    assert not server.available()
    with pytest.raises(LlamaError, match="not installed"):
        server.request("/v1/models")
    assert harness.launches == []
    del clock


def test_a_crashed_server_is_started_again(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    server = make_server(tmp_path, harness, clock)
    server.request("/v1/embeddings", {"input": ["x"]})
    harness.processes[0].code = 1  # it died between requests
    server.request("/v1/embeddings", {"input": ["x"]})
    assert len(harness.launches) == 2


def test_a_failed_request_to_a_dead_server_is_retried_once_on_a_new_one(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()

    def dies_during_first_request(
        method: str, url: str, key: str, body: dict[str, object] | None
    ) -> object:
        if body is not None and len(harness.processes) == 1:
            harness.processes[0].code = 1
            raise LlamaError
        return harness.transport(method, url, key, body)

    server = make_server(tmp_path, harness, clock, transport=dies_during_first_request)
    assert server.request("/v1/embeddings", {"input": ["x"]}) is not None
    assert len(harness.launches) == 2


def test_an_error_from_a_live_server_is_not_retried(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    server = make_server(tmp_path, harness, clock)
    server.request("/v1/embeddings", {"input": ["x"]})
    harness.fail_next = 1
    with pytest.raises(LlamaError):
        server.request("/v1/embeddings", {"input": ["x"]})
    assert len(harness.launches) == 1


def test_it_stops_after_sitting_idle(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    server = make_server(tmp_path, harness, clock, idle_s=60)
    server.request("/v1/embeddings", {"input": ["x"]})
    clock.now += 59
    server.unload_if_idle()
    assert server.running()
    clock.now += 2
    server.unload_if_idle()
    assert not server.running()
    assert harness.processes[0].terminated
    server.unload_if_idle()  # nothing running: nothing to do
    server.request("/v1/embeddings", {"input": ["x"]})
    server.unload_if_idle(force=True)
    assert not server.running()
    server.stop()


# --- embeddings ----------------------------------------------------------------------------


def test_documents_come_back_in_order_and_unit_length(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    embedder = LlamaEmbedder(make_server(tmp_path, harness, clock), "qwen")
    vectors = embedder.embed_documents(["a", "b", "c"])
    assert [float(v[0]) for v in vectors] == [1.0, 1.0, 1.0]  # each normalised from [i+1, 0, 0]
    assert all(np.linalg.norm(v) == pytest.approx(1.0) for v in vectors)
    assert embedder.available()


def test_documents_are_sent_in_batches_and_cut(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    embedder = LlamaEmbedder(make_server(tmp_path, harness, clock), "qwen")
    texts = ["x" * (llm.MAX_EMBED_CHARS + 500)] + [""] * (llm.EMBED_BATCH + 1)
    assert len(embedder.embed_documents(texts)) == len(texts)
    sent = [b for m, _, _, b in harness.requests if m == "POST" and b]
    assert [len(b["input"]) for b in sent] == [llm.EMBED_BATCH, 2]  # type: ignore[arg-type]
    assert len(sent[0]["input"][0]) == llm.MAX_EMBED_CHARS  # type: ignore[index]
    assert sent[0]["input"][1] == " "  # type: ignore[index]  # empty text still gets a vector
    assert sent[0]["model"] == "qwen"


def test_a_query_carries_the_instruction_and_a_document_does_not(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    embedder = LlamaEmbedder(make_server(tmp_path, harness, clock), "qwen")
    embedder.embed_query("what video")
    embedder.embed_documents(["a video"])
    bodies = [b for m, _, _, b in harness.requests if m == "POST" and b]
    assert str(bodies[0]["input"][0]).startswith("Instruct:")  # type: ignore[index]
    assert str(bodies[0]["input"][0]).endswith("Query: what video")  # type: ignore[index]
    assert bodies[1]["input"] == ["a video"]


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {"data": []},
        {"data": [{"embedding": "bad", "index": 0}, {"embedding": [1.0], "index": 1}]},
    ],
)
def answering(answer: object) -> llm.Transport:
    def transport(method: str, url: str, key: str, body: dict[str, object] | None) -> object:
        del method, key, body
        return {"status": "ok"} if url.endswith("/health") else answer

    return transport


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {},
        {"data": "nope"},
        {"data": []},
        {"data": [{"embedding": [1.0]}, "x"]},
        {"data": [{"embedding": "bad", "index": 0}, {"embedding": [1.0], "index": 1}]},
        {"data": [{"embedding": [1.0], "index": 0}, {"embedding": [1.0], "index": 0}]},
    ],
)
def test_odd_answers_are_errors(tmp_path: Path, answer: object) -> None:
    server = make_server(tmp_path, Harness(), Clock(), transport=answering(answer))
    with pytest.raises(LlamaError, match="unexpected embeddings answer"):
        LlamaEmbedder(server, "m").embed_documents(["a", "b"])


def test_a_zero_vector_stays_zero(tmp_path: Path) -> None:
    answer = {"data": [{"index": 0, "embedding": [0, 0]}]}
    server = make_server(tmp_path, Harness(), Clock(), transport=answering(answer))
    (vector,) = LlamaEmbedder(server, "m").embed_documents(["a"])
    assert not vector.any()


def test_vectors_keep_their_first_dimensions_and_unit_length(tmp_path: Path) -> None:
    full = [3.0, 4.0] + [100.0] * 1022  # what the model returns: 1024 numbers
    answer = {"data": [{"index": 0, "embedding": full}]}
    server = make_server(tmp_path, Harness(), Clock(), transport=answering(answer))
    (vector,) = LlamaEmbedder(server, "m").embed_documents(["a"])
    assert vector.shape == (EMBED_DIMS,)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-5)
    assert vector[0] / vector[1] == pytest.approx(3 / 4)  # the leading ones, not the tail


def test_full_dimensions_can_be_compared_without_changing_the_default(tmp_path: Path) -> None:
    answer = {"data": [{"index": 0, "embedding": [1.0] * 1024}]}
    server = make_server(tmp_path, Harness(), Clock(), transport=answering(answer))
    (full,) = LlamaEmbedder(server, "m", dimensions=1024).embed_documents(["a"])
    (small,) = LlamaEmbedder(server, "m").embed_documents(["a"])
    assert full.shape == (1024,)
    assert small.shape == (EMBED_DIMS,)
    assert float(np.linalg.norm(full)) == pytest.approx(1.0)


def test_the_embedder_unloads_with_its_server(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    server = make_server(tmp_path, harness, clock, idle_s=10)
    embedder = LlamaEmbedder(server, "m")
    embedder.embed_query("x")
    clock.now += 11
    embedder.unload_if_idle()
    assert not server.running()


# --- the real transport --------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen: list[tuple[str, str, bytes]] = []  # noqa: RUF012 - shared with the test

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _Handler.seen.append(
            (self.command, self.headers.get("Authorization", ""), self.rfile.read(length))
        )
        body = b"{not json" if self.path == "/bad" else json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer  # noqa: N815

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def test_the_real_transport_sends_the_key_and_parses_json() -> None:
    _Handler.seen = []
    httpd = http.server.HTTPServer((HOST, 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://{HOST}:{httpd.server_address[1]}"
        assert http_transport("POST", f"{base}/x", "k3y", {"a": 1}) == {"ok": True}
        assert http_transport("GET", f"{base}/y", "k3y", None) == {"ok": True}
        assert _Handler.seen == [
            ("POST", "Bearer k3y", b'{"a": 1}'),
            ("GET", "Bearer k3y", b""),
        ]
        with pytest.raises(LlamaError, match="JSONDecodeError"):
            http_transport("GET", f"{base}/bad", "k", None)
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize(
    "url",
    ["http://example.com/v1", "https://127.0.0.1:1/x", "http://localhost.evil.com:1/", "file:///x"],
)
def test_the_real_transport_refuses_anything_but_the_local_server(url: str) -> None:
    with pytest.raises(LlamaError, match="local server"):
        http_transport("GET", url, "k", None)


# --- presets, chat and streaming -----------------------------------------------------------


def streaming(events: list[object]) -> llm.StreamTransport:
    def transport(url: str, key: str, body: dict[str, object]) -> Iterator[object]:
        del url, key, body
        yield from events

    return transport


def test_presets_are_written_to_an_ini_and_passed_by_path(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    ini = tmp_path / "runtime" / "models.ini"
    program = tmp_path / "llama-server.exe"
    program.write_bytes(b"")
    server = LlamaServer(
        program,
        tmp_path / "models",
        harness.launcher,
        harness.transport,
        presets={"emb": llm.EMBEDDING_FLAGS, "chat": llm.CHAT_FLAGS},
        preset_file=ini,
        clock=clock,
        sleep=clock.sleep,
    )
    server.request("/v1/embeddings", {"model": "m", "input": ["x"]})
    args, _ = harness.launches[0]
    assert args[args.index("--models-preset") + 1] == str(ini)
    text = ini.read_text(encoding="utf-8")
    assert "[emb]\nembeddings = true\npooling = last" in text
    assert "[chat]\nctx-size = 16384\nn-gpu-layers = 99\nreasoning = off" in text
    # --embeddings must reach the embedder only, never the router as a whole.
    assert "--embeddings" not in args
    assert text.index("embeddings = true") < text.index("[chat]")


def test_without_presets_the_command_has_no_preset_flag(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    make_server(tmp_path, harness, clock).request("/v1/embeddings", {"input": ["x"]})
    assert "--models-preset" not in harness.launches[0][0]


def chat_with(tmp_path: Path, answer: object) -> tuple[llm.LlamaChat, Harness]:
    harness = Harness()
    server = make_server(tmp_path, harness, Clock(), transport=answering(answer))
    return llm.LlamaChat(server, "gemma"), harness


def test_a_json_answer_is_requested_with_a_schema_and_no_thinking(tmp_path: Path) -> None:
    sent: list[dict[str, object]] = []

    def transport(method: str, url: str, key: str, body: dict[str, object] | None) -> object:
        del method, key
        if url.endswith("/health"):
            return {"status": "ok"}
        assert body is not None
        sent.append(body)
        return {"choices": [{"message": {"content": '<think>x</think> {"a": 1}'}}]}

    chat = llm.LlamaChat(make_server(tmp_path, Harness(), Clock(), transport=transport), "gemma")
    schema = {"type": "object"}
    assert chat.complete_json("sys", "usr", schema) == '{"a": 1}'
    body = sent[0]
    assert body["model"] == "gemma"
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert body["temperature"] == 0.0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": schema, "strict": True},
    }
    assert chat.available()


ODD_CHAT_ANSWERS: list[object] = [
    None,
    {"choices": []},
    {"choices": [{"message": {"content": 3}}]},
]


@pytest.mark.parametrize("answer", ODD_CHAT_ANSWERS)
def test_odd_chat_answers_are_errors(tmp_path: Path, answer: object) -> None:
    chat, _ = chat_with(tmp_path, answer)
    with pytest.raises(LlamaError, match="unexpected chat answer"):
        chat.complete_json("s", "u", {})


def test_a_streamed_answer_yields_its_pieces(tmp_path: Path) -> None:
    events: list[object] = [
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"reasoning_content": "hidden"}}]},
        {"choices": []},
        "junk",
    ]
    harness, clock = Harness(), Clock()
    program = tmp_path / "llama-server.exe"
    program.write_bytes(b"")
    server = LlamaServer(
        program,
        tmp_path,
        harness.launcher,
        harness.transport,
        streaming(events),
        clock=clock,
        sleep=clock.sleep,
        presets={},
        preset_file=tmp_path / "models.ini",
    )
    chat = llm.LlamaChat(server, "gemma")
    assert list(chat.stream("s", "u")) == ["Hel", "lo"]
    assert server.running()
    chat.unload_if_idle()


def test_a_stream_starts_a_dead_server_again(tmp_path: Path) -> None:
    harness, clock = Harness(), Clock()
    program = tmp_path / "llama-server.exe"
    program.write_bytes(b"")
    server = LlamaServer(
        program,
        tmp_path,
        harness.launcher,
        harness.transport,
        streaming([]),
        clock=clock,
        sleep=clock.sleep,
        presets={},
        preset_file=tmp_path / "models.ini",
    )
    assert list(server.stream("/x", {})) == []
    harness.processes[0].code = 1
    assert list(server.stream("/x", {})) == []
    assert len(harness.launches) == 2


def test_thinking_blocks_are_removed_even_when_unfinished() -> None:
    assert llm.strip_thinking("<think>a\nb</think>\nHello") == "Hello"
    assert llm.strip_thinking("Hi <think>never closed") == "Hi"
    assert llm.strip_thinking("plain") == "plain"


class _Sse(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in (
            ": comment\n\n",
            'data: {"n": 1}\n\n',
            'data: {"n": 2}\n\n',
            "data: [DONE]\n\n",
            'data: {"n": 3}\n\n',
        ):
            self.wfile.write(line.encode())
        self.wfile.flush()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def test_the_real_stream_transport_reads_events_until_done() -> None:
    httpd = http.server.HTTPServer((HOST, 0), _Sse)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://{HOST}:{httpd.server_address[1]}/v1/chat/completions"
        assert list(llm.http_stream(url, "k", {"stream": True})) == [{"n": 1}, {"n": 2}]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_real_stream_transport_refuses_remote_hosts() -> None:
    with pytest.raises(LlamaError, match="local server"):
        list(llm.http_stream("http://example.com/x", "k", {}))
