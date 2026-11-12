"""The pinned model registry and the one-time download (network access is always faked)."""

from __future__ import annotations

import hashlib
import io
import re
import urllib.error
import urllib.request
import zipfile
from dataclasses import replace
from typing import TYPE_CHECKING, Self

import pytest

from akashic.api.models import Downloads
from akashic.config import DEFAULT_EMBEDDING_MODEL, Config
from akashic.pipeline import models
from akashic.pipeline.models import ModelDownloadError, ModelFile, ModelSpec

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DATA = {"a.json": b"{}" * 10, "big.bin": bytes(range(256)) * 40}


def _spec(files: dict[str, bytes] | None = None) -> ModelSpec:
    files = files if files is not None else DATA
    return ModelSpec(
        key="tiny",
        description="test model",
        license="MIT",
        base_url="https://huggingface.co/someone/tiny/resolve/abc123",
        files=tuple(
            ModelFile(name, hashlib.sha256(body).hexdigest(), len(body))
            for name, body in files.items()
        ),
    )


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._io = io.BytesIO(body)

    def read(self, size: int, /) -> bytes:
        return self._io.read(size)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeNet:
    def __init__(self, served: dict[str, bytes]) -> None:
        self.served = served
        self.requested: list[str] = []

    def __call__(self, url: str) -> models._Response:
        self.requested.append(url)
        name = url.rsplit("/", 1)[1]
        if name not in self.served:
            raise urllib.error.URLError("not found")
        return FakeResponse(self.served[name])


def test_registry_pins_everything() -> None:
    assert models.REGISTRY
    for key, spec in models.REGISTRY.items():
        assert key == spec.key
        assert re.search(r"/resolve/[0-9a-f]{40}$", spec.base_url), "pin an exact commit"
        assert spec.base_url.startswith("https://huggingface.co/")
        assert spec.total_size == sum(f.size for f in spec.files)
        for file in spec.files:
            assert re.fullmatch(r"[0-9a-f]{64}", file.sha256)
            assert file.size > 0
    assert {f.name for f in models.WHISPER_LARGE_V3_TURBO.files} >= {
        "model.bin",
        "config.json",
        "tokenizer.json",
    }


def test_download_fetches_verifies_and_reports_progress(tmp_path: Path) -> None:
    net = FakeNet(DATA)
    seen: list[tuple[str, int, int]] = []
    fetched = models.download(
        tmp_path, _spec(), net, lambda name, done, total: seen.append((name, done, total))
    )
    assert sorted(fetched) == sorted(DATA)
    for name, body in DATA.items():
        assert (tmp_path / "tiny" / name).read_bytes() == body
    assert not list((tmp_path / "tiny").glob("*.part"))
    assert seen[-1][1] == seen[-1][2]
    assert net.requested[0].startswith("https://huggingface.co/someone/tiny/resolve/abc123/")


def test_files_already_in_place_cost_no_network(tmp_path: Path) -> None:
    models.download(tmp_path, _spec(), FakeNet(DATA))
    again = FakeNet({})  # would raise if anything were requested
    assert models.download(tmp_path, _spec(), again) == []
    assert again.requested == []
    assert models.verify(tmp_path, _spec()) == []
    assert models.is_complete(tmp_path, _spec())


def test_a_corrupt_file_is_fetched_again(tmp_path: Path) -> None:
    models.download(tmp_path, _spec(), FakeNet(DATA))
    (tmp_path / "tiny" / "a.json").write_bytes(b"?" * len(DATA["a.json"]))  # same size, wrong bytes
    assert models.verify(tmp_path, _spec()) == ["a.json"]
    net = FakeNet(DATA)
    assert models.download(tmp_path, _spec(), net) == ["a.json"]
    assert models.verify(tmp_path, _spec()) == []


def test_verify_reports_missing_files(tmp_path: Path) -> None:
    assert sorted(models.verify(tmp_path, _spec())) == sorted(DATA)
    assert not models.is_complete(tmp_path, _spec())


def test_checksum_mismatch_is_refused_and_leaves_nothing(tmp_path: Path) -> None:
    tampered = {**DATA, "a.json": b"X" * len(DATA["a.json"])}
    with pytest.raises(ModelDownloadError, match="checksum"):
        models.download(tmp_path, _spec(), FakeNet(tampered))
    assert not (tmp_path / "tiny" / "a.json").exists()
    assert not list((tmp_path / "tiny").glob("*.part"))


def test_a_truncated_or_oversized_download_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ModelDownloadError, match="checksum"):
        models.download(tmp_path, _spec(), FakeNet({**DATA, "a.json": b"{}"}))
    with pytest.raises(ModelDownloadError, match="larger"):
        models.download(tmp_path, _spec(), FakeNet({**DATA, "a.json": DATA["a.json"] + b"extra"}))
    assert not list((tmp_path / "tiny").glob("*.part"))


def test_network_errors_are_wrapped_without_leaking_details(tmp_path: Path) -> None:
    with pytest.raises(ModelDownloadError, match="URLError"):
        models.download(tmp_path, _spec(), FakeNet({}))


def test_other_failures_still_clean_up_the_partial_file(tmp_path: Path) -> None:
    class Interrupted(FakeResponse):
        def read(self, size: int, /) -> bytes:
            del size
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        models.download(tmp_path, _spec(), lambda _url: Interrupted(b""))
    assert not list((tmp_path / "tiny").glob("*.part"))


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/x",
        "https://evil.example.com/x",
        "https://huggingface.co.evil.com/x",
        "ftp://huggingface.co/x",
        "https://nothf.co/x",
    ],
)
def test_only_https_hugging_face_hosts_are_allowed(url: str) -> None:
    with pytest.raises(ModelDownloadError, match="unexpected host"):
        models.default_opener(url)  # refuses before any connection is attempted


@pytest.mark.parametrize(
    "url",
    [
        "https://huggingface.co/a/b",
        "https://cdn-lfs.huggingface.co/x",
        "https://cas-bridge.xethub.hf.co/x",
        "https://hf.co/x",
    ],
)
def test_hugging_face_cdn_hosts_are_allowed(url: str) -> None:
    models._check_url(url)


def test_redirects_to_other_hosts_are_refused() -> None:
    handler = models._HostLimitedRedirects()
    request = urllib.request.Request("https://huggingface.co/a/b")
    with pytest.raises(ModelDownloadError):
        handler.redirect_request(
            request, io.BytesIO(), 302, "Found", {}, "https://evil.example.com/x"
        )  # type: ignore[arg-type]
    allowed = handler.redirect_request(
        request,
        io.BytesIO(),
        302,
        "Found",
        {},
        "https://cas-bridge.xethub.hf.co/x",  # type: ignore[arg-type]
    )
    assert allowed is not None
    assert allowed.full_url == "https://cas-bridge.xethub.hf.co/x"


# Pins the unused WHISPER_MODELS: rewrite against REGISTRY with #93.
def test_both_whisper_models_are_registered_and_offered() -> None:
    from akashic.config import WHISPER_MODELS  # noqa: PLC0415 - only this test needs it

    assert set(WHISPER_MODELS) <= set(models.REGISTRY)


def test_a_file_in_a_repository_subfolder_lands_in_a_matching_folder(tmp_path: Path) -> None:
    body = b"onnx" * 10
    spec = _spec({"onnx/model.onnx": body})
    net = FakeNet({"model.onnx": body})
    assert models.download(tmp_path, spec, net) == ["onnx/model.onnx"]
    assert (tmp_path / "tiny" / "onnx" / "model.onnx").read_bytes() == body
    assert net.requested == ["https://huggingface.co/someone/tiny/resolve/abc123/onnx/model.onnx"]
    assert models.is_complete(tmp_path, spec)


# --- the llama.cpp runtime and the embedding model -----------------------------------------


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _runtime(archives: dict[str, bytes], program: str = "server.exe") -> models.RuntimeSpec:
    return models.RuntimeSpec(
        key="runtime-1",
        description="test runtime",
        license="MIT",
        base_url="https://github.com/someone/project/releases/download/v1",
        archives=tuple(
            ModelFile(name, hashlib.sha256(body).hexdigest(), len(body))
            for name, body in archives.items()
        ),
        program=program,
    )


ZIPS = {
    "main.zip": _zip({"server.exe": b"MZ-server", "ggml.dll": b"ggml"}),
    "cuda.zip": _zip({"cudart.dll": b"cuda", "sub/cublas.dll": b"blas"}),
}


def test_the_runtime_is_downloaded_unpacked_together_and_cleaned_up(tmp_path: Path) -> None:
    spec = _runtime(ZIPS)
    net = FakeNet(ZIPS)
    models.download_runtime(tmp_path, spec, net)
    home = tmp_path / "runtime-1"
    assert (home / "server.exe").read_bytes() == b"MZ-server"
    assert (home / "cudart.dll").read_bytes() == b"cuda"
    assert (home / "sub" / "cublas.dll").read_bytes() == b"blas"
    assert models.runtime_ready(tmp_path, spec)
    assert models.runtime_program(tmp_path, spec) == home / "server.exe"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["runtime-1"]  # no zips, no scratch
    assert net.requested == [f"{spec.base_url}/main.zip", f"{spec.base_url}/cuda.zip"]
    again = FakeNet({})
    models.download_runtime(tmp_path, spec, again)
    assert again.requested == []


def test_a_runtime_with_a_wrong_checksum_is_refused_and_leaves_nothing_usable(
    tmp_path: Path,
) -> None:
    spec = _runtime(ZIPS)
    tampered = {**ZIPS, "main.zip": ZIPS["main.zip"][:-1] + b"X"}
    with pytest.raises(ModelDownloadError, match="checksum"):
        models.download_runtime(tmp_path, spec, FakeNet(tampered))
    assert not models.runtime_ready(tmp_path, spec)
    assert not (tmp_path / "runtime-1").exists()
    assert not (tmp_path / "runtime-1.part").exists()


def test_an_interrupted_runtime_download_keeps_the_archives_it_already_has(tmp_path: Path) -> None:
    spec = _runtime(ZIPS)
    with pytest.raises(ModelDownloadError):
        models.download_runtime(tmp_path, spec, FakeNet({"main.zip": ZIPS["main.zip"]}))
    assert not models.runtime_ready(tmp_path, spec)
    net = FakeNet(ZIPS)
    models.download_runtime(tmp_path, spec, net)
    assert net.requested == [f"{spec.base_url}/cuda.zip"]  # main.zip was kept and verified
    assert models.runtime_ready(tmp_path, spec)


def test_an_archive_that_would_write_outside_its_folder_is_refused(tmp_path: Path) -> None:
    evil = {"evil.zip": _zip({"server.exe": b"x", "../escaped.txt": b"gotcha"})}
    with pytest.raises(ModelDownloadError, match="escape"):
        models.download_runtime(tmp_path / "runtime", _runtime(evil), FakeNet(evil))
    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "runtime" / "runtime-1").exists()


def test_a_runtime_without_its_program_or_with_a_broken_archive_is_refused(tmp_path: Path) -> None:
    no_program = {"a.zip": _zip({"other.dll": b"x"})}
    with pytest.raises(ModelDownloadError, match=r"server\.exe missing"):
        models.download_runtime(tmp_path, _runtime(no_program), FakeNet(no_program))
    broken = {"b.zip": b"this is not a zip file"}
    with pytest.raises(ModelDownloadError, match="not a valid archive"):
        models.download_runtime(tmp_path, _runtime(broken), FakeNet(broken))
    assert not (tmp_path / "runtime-1").exists()


def test_the_runtime_and_embedding_pins_are_exact() -> None:
    runtime = models.LLAMA_RUNTIME
    assert runtime.base_url == "https://github.com/ggml-org/llama.cpp/releases/download/b12209"
    assert runtime.program == "llama-server.exe"
    assert runtime.total_size == sum(a.size for a in runtime.archives)
    for archive in runtime.archives:
        assert re.fullmatch(r"[0-9a-f]{64}", archive.sha256)
        assert archive.name.endswith("win-cuda-13.5-x64.zip")
    embedding = models.REGISTRY[models.EMBEDDING_TEXT_MODEL]
    assert models.EMBEDDING_TEXT_MODEL == DEFAULT_EMBEDDING_MODEL
    assert [f.name for f in embedding.files] == [models.EMBEDDING_TEXT_FILE]
    assert embedding.license == "Apache-2.0"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/ggml-org/llama.cpp/releases/download/b12209/x.zip",
        "https://release-assets.githubusercontent.com/github-production-release-asset/x",
        "https://objects.githubusercontent.com/x",
    ],
)
def test_release_downloads_may_come_from_github_and_its_asset_hosts(url: str) -> None:
    models._check_url(url)


@pytest.mark.parametrize(
    "url",
    ["http://github.com/x", "https://github.com.evil.com/x", "https://evilgithubusercontent.com/x"],
)
def test_lookalike_hosts_are_still_refused(url: str) -> None:
    with pytest.raises(ModelDownloadError, match="unexpected host"):
        models._check_url(url)


# --- Settings > Models -------------------------------------------------------------------------


def test_the_required_set_follows_the_settings() -> None:
    config = Config()
    keys = [n.spec.key for n in models.required(config)]
    assert keys == [
        config.audio.whisper_model,
        models.SEGMENTATION_MODEL,
        models.EMBEDDING_MODEL,
        DEFAULT_EMBEDDING_MODEL,
        models.LLAMA_RUNTIME.key,
        config.search.chat_model,
    ]
    custom = replace(
        config,
        audio=replace(config.audio, whisper_model="faster-whisper-large-v3-turbo"),
        search=replace(config.search, chat_model="my-own-gguf", embedding_model="mine"),
    )
    keys = [n.spec.key for n in models.required(custom)]
    assert keys[0] == "faster-whisper-large-v3-turbo"
    assert "my-own-gguf" not in keys  # copied in by hand: nothing to download
    assert "mine" not in keys


def _downloads(tmp_path: Path, net: FakeNet, specs: list[models.Pinned]) -> Downloads:
    wanted = [models.Needed(s, "Testing") for s in specs]
    return Downloads(tmp_path / "models", tmp_path / "rt", lambda: wanted, net, lambda work: work())


def test_downloads_report_state_and_fetch_only_what_is_missing(tmp_path: Path) -> None:
    spec, runtime = _spec(), _runtime(ZIPS)
    net = FakeNet({**DATA, **ZIPS})
    downloads = _downloads(tmp_path, net, [spec, runtime])
    rows = downloads.rows()
    assert [(r["key"], r["status"], r["needed_for"]) for r in rows] == [
        ("tiny", "missing", "Testing"),
        ("runtime-1", "missing", "Testing"),
    ]
    assert downloads.missing() == ["tiny", "runtime-1"]
    assert downloads.start(downloads.missing())
    assert [r["status"] for r in downloads.rows()] == ["ready", "ready"]
    assert models.is_complete(tmp_path / "models", spec)
    requested = len(net.requested)
    assert downloads.start(["tiny"])  # already there: no network
    assert len(net.requested) == requested


def test_a_failed_download_says_so_and_can_be_retried(tmp_path: Path) -> None:
    spec = _spec()
    downloads = _downloads(tmp_path, FakeNet({}), [spec])
    assert downloads.start(["tiny"])
    (row,) = downloads.rows()
    assert row["status"] == "failed"
    assert "a.json" in str(row["error"])


def test_only_one_download_runs_at_a_time(tmp_path: Path) -> None:
    pending: list[Callable[[], None]] = []
    wanted = [models.Needed(_spec(), "Testing")]
    downloads = Downloads(
        tmp_path / "models", tmp_path / "rt", lambda: wanted, FakeNet(DATA), pending.append
    )
    assert downloads.start(["tiny"])
    assert downloads.rows()[0]["status"] == "queued"
    assert not downloads.start(["tiny"])  # busy
    pending.pop()()
    assert downloads.rows()[0]["status"] == "ready"
    assert downloads.start(["tiny"])  # free again
