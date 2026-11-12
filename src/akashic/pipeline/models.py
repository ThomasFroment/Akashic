"""Pinned model files and the one-time, explicit download.

This is the only module allowed to touch the network, and only when the user asks: the
Download buttons of Settings > Models (``api/models.py``) and ``scripts/fetch_models.py``.
Every file is pinned to an exact commit of its repository and a SHA-256, downloaded over HTTPS
to a ``.part`` file, verified, and only then moved into place. Files already present and
verified cost no network at all. Nothing else in the app downloads anything; at runtime the
model libraries are told to stay offline (``akashic.offline``).

Adding a model means adding a ``ModelSpec`` here with hashes taken from the pinned commit; there
is no "latest".
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, Self
from urllib.parse import urlparse

from akashic.config import (
    DEFAULT_EMBEDDING_MODEL,
    WHISPER_LARGE_V3_MODEL,
    WHISPER_TURBO_MODEL,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from akashic.config import Config

CHUNK: Final = 1024 * 1024
# Where Hugging Face sends file downloads (CDN and Xet bridge hosts). Whatever the host, the
# SHA-256 below decides whether the bytes are accepted; this only keeps requests on known hosts.
# GitHub serves the pinned llama.cpp release assets (redirecting to its asset CDN).
ALLOWED_HOST_SUFFIXES: Final = (".huggingface.co", ".hf.co", ".githubusercontent.com")
ALLOWED_HOSTS: Final = ("huggingface.co", "hf.co", "github.com")
TIMEOUT_S: Final = 60


class ModelDownloadError(Exception):
    """A file could not be downloaded or did not match its pinned checksum."""


@dataclass(frozen=True, slots=True)
class ModelFile:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str  # folder name inside models/
    description: str
    license: str
    base_url: str  # ends with the pinned commit: files are at base_url + "/" + name
    files: tuple[ModelFile, ...]

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


# Whisper large-v3-turbo converted for CTranslate2 (OpenAI's weights, MIT). The repository moved
# from mobiuslabsgmbh/ to dropbox-dash/; the commit below is from 2025-11-05.
WHISPER_LARGE_V3_TURBO: Final = ModelSpec(
    key=WHISPER_TURBO_MODEL,
    description="Whisper large-v3-turbo (CTranslate2, float16), speech to text",
    license="MIT",
    base_url=(
        "https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo/resolve/"
        "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"
    ),
    files=(
        ModelFile(
            "config.json",
            "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e",
            2_263,
        ),
        ModelFile(
            "preprocessor_config.json",
            "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
            340,
        ),
        ModelFile(
            "tokenizer.json",
            "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd",
            2_710_337,
        ),
        ModelFile(
            "vocabulary.json",
            "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
            1_068_114,
        ),
        ModelFile(
            "model.bin",
            "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da",
            1_617_884_929,
        ),
    ),
)

# Whisper large-v3 converted for CTranslate2 by Systran (OpenAI's weights, MIT); commit pinned.
# Slower than the turbo model but slightly more accurate; chosen in Settings.
WHISPER_LARGE_V3: Final = ModelSpec(
    key=WHISPER_LARGE_V3_MODEL,
    description="Whisper large-v3 (CTranslate2, float16), speech to text",
    license="MIT",
    base_url=(
        "https://huggingface.co/Systran/faster-whisper-large-v3/resolve/"
        "edaa852ec7e145841d8ffdb056a99866b5f0a478"
    ),
    files=(
        ModelFile(
            "config.json",
            "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9",
            2_394,
        ),
        ModelFile(
            "preprocessor_config.json",
            "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711",
            340,
        ),
        ModelFile(
            "tokenizer.json",
            "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca",
            2_480_617,
        ),
        ModelFile(
            "vocabulary.json",
            "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1",
            1_068_114,
        ),
        ModelFile(
            "model.bin",
            "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
            3_087_284_237,
        ),
    ),
)

# Built-in Transformers speech models, downloaded explicitly and loaded from local files only.
QWEN3_ASR: Final = ModelSpec(
    key="qwen3-asr-1.7b",
    description="Qwen3-ASR 1.7B — French/English (experimental)",
    license="Apache-2.0",
    base_url="https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf/resolve/bcd2b5b7f32b480ab5790554cfa8347f246a14f3",
    files=(
        ModelFile(
            "chat_template.jinja",
            "f50e6b694fbf4a683206e37869990d68333fe95d285730f084c838a34b0d98c2",
            1434,
        ),
        ModelFile(
            "config.json",
            "117ac8e63e2af7cae3665e5a632d6eb03f5f384915519ceb6403c15ec6533f63",
            2399,
        ),
        ModelFile(
            "generation_config.json",
            "9939fc9388b79bd70757f938b87381e817173d6a6158f5af6506c0b73e775c3c",
            165,
        ),
        ModelFile(
            "model.safetensors",
            "2db53c7d81bd9b8cbc6a074e89be2c968a0d373fb4ee68bb1b1e14f7042dfee1",
            4_076_193_080,
        ),
        ModelFile(
            "processor_config.json",
            "bc0b230081b44e629dd5b9045b78495615c1831b4b9f4cffe97bd37e82a6156a",
            487,
        ),
        ModelFile(
            "tokenizer.json",
            "fe1fad59be22a41ee293363fcf95fdedbc7c93f3b49270b1d2e18bd1399a7a05",
            11429653,
        ),
        ModelFile(
            "tokenizer_config.json",
            "945e980986de2ca7768f3326bfdbb4fbea3406f972b8ae0be233089f2b253c11",
            998,
        ),
    ),
)

CANARY_V2: Final = ModelSpec(
    key="canary-1b-v2",
    description="Canary 1B v2 — French/English (experimental)",
    license="CC-BY-4.0",
    base_url="https://huggingface.co/nvidia/canary-1b-v2/resolve/d455706339a6b32e1aa40f82c713a482a0c938e2",
    files=(
        ModelFile(
            "chat_template.jinja",
            "5c06139c492bc95a9cf6ebe7e37aa4611af525485aeba32310dca560f8f1a78b",
            534,
        ),
        ModelFile(
            "config.json",
            "e6818410f7dd0a460a4f09324d5fb4ec76b64410416e1ce172e06658335b2aa2",
            1591,
        ),
        ModelFile(
            "generation_config.json",
            "e7c62fe7fbc09a036f7a2be62f02491c71720a5557c7d761f18262ed54191cb4",
            252,
        ),
        ModelFile(
            "model.safetensors",
            "4f6d381f6a939b95e9f8516212148b701e9b3805d26462c4c3e9e34f3d89b6f8",
            3_916_173_816,
        ),
        ModelFile(
            "processor_config.json",
            "07936c62f5d52898d05fc0c78b632121413cebc35e194432c7230196f3638e4a",
            362,
        ),
        ModelFile(
            "tokenizer.json",
            "286dd42a7b27e15332195df556c2df6d383bb4c4869774b055e1666a72e6bc64",
            2189980,
        ),
        ModelFile(
            "tokenizer_config.json",
            "82126b51f23a52cfe5a9be05cf15d9b30e454693a72d1ed6f0a43f9292975843",
            328,
        ),
    ),
)

SPEECH_CANDIDATES: Final[dict[str, ModelSpec]] = {
    spec.key: spec for spec in (WHISPER_LARGE_V3, WHISPER_LARGE_V3_TURBO, QWEN3_ASR, CANARY_V2)
}

# Speaker models (ONNX, small, run on the CPU). Who-spoke-when: pyannote segmentation-3.0 as
# converted by onnx-community (MIT).
SEGMENTATION_MODEL: Final = "pyannote-segmentation-3.0"
SEGMENTATION_FILE: Final = "onnx/model.onnx"
PYANNOTE_SEGMENTATION: Final = ModelSpec(
    key=SEGMENTATION_MODEL,
    description="pyannote segmentation-3.0 (ONNX), who speaks when",
    license="MIT",
    base_url=(
        "https://huggingface.co/onnx-community/pyannote-segmentation-3.0/resolve/"
        "733a93b6473d019a773298e08cefa686894b1854"
    ),
    files=(
        ModelFile(
            SEGMENTATION_FILE,
            "057ee564753071c0b09b5b611648b50ac188d50846bff5f01e9f7bbf1591ea25",
            5_986_908,
        ),
    ),
)

# Voice embedding: WeSpeaker ResNet34-LM trained on VoxCeleb (the model pyannote uses), 256
# numbers per voice. Licence CC-BY-4.0: credit WeSpeaker (https://github.com/wenet-e2e/wespeaker).
EMBEDDING_MODEL: Final = "wespeaker-voxceleb-resnet34-lm"
EMBEDDING_FILE: Final = "voxceleb_resnet34_LM.onnx"
WESPEAKER_RESNET34: Final = ModelSpec(
    key=EMBEDDING_MODEL,
    description="WeSpeaker ResNet34-LM (ONNX), voice embeddings",
    license="CC-BY-4.0",
    base_url=(
        "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/"
        "f0c48c298fd835726c27956a5d617bad7115627e"
    ),
    files=(
        ModelFile(
            EMBEDDING_FILE,
            "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068",
            26_530_309,
        ),
    ),
)

# Text embeddings for search: Qwen3-Embedding-0.6B (Apache-2.0), quantised to 8 bit, served by
# llama-server. 1024 numbers per text, French and English in one space. Chosen as the smaller of
# the two candidates in AGENTS.md; it can be swapped (the vectors record
# which model made them).
EMBEDDING_TEXT_MODEL: Final = DEFAULT_EMBEDDING_MODEL
EMBEDDING_TEXT_FILE: Final = "Qwen3-Embedding-0.6B-Q8_0.gguf"
QWEN3_EMBEDDING: Final = ModelSpec(
    key=EMBEDDING_TEXT_MODEL,
    description="Qwen3-Embedding-0.6B Q8_0 (GGUF), text embeddings",
    license="Apache-2.0",
    base_url=(
        "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/"
        "370f27d7550e0def9b39c1f16d3fbaa13aa67728"
    ),
    files=(
        ModelFile(
            EMBEDDING_TEXT_FILE,
            "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439",
            639_150_592,
        ),
    ),
)

# Chat candidates for answers and planning. They are downloaded only on explicit request.
# The default was chosen by the synthetic evaluation in ``scripts/eval_ask.py``; the newer
# alternatives remain experimental until compared on the same questions.
QWEN3_14B: Final = ModelSpec(
    key="qwen3-14b-q4km",
    description="Qwen3 14B Q4_K_M — larger multilingual model; high memory use",
    license="Apache-2.0",
    base_url=(
        "https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/530227a7d994db8eca5ab5ced2fb692b614357fd"
    ),
    files=(
        ModelFile(
            "Qwen3-14B-Q4_K_M.gguf",
            "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0",
            9_001_752_960,
        ),
    ),
)
GPT_OSS_20B: Final = ModelSpec(
    key="gpt-oss-20b-mxfp4",
    description="GPT-OSS 20B MXFP4 — largest option; very high memory use",
    license="Apache-2.0",
    base_url=(
        "https://huggingface.co/ggml-org/gpt-oss-20b-GGUF/resolve/"
        "15dcfc7e7c608c6161507211ed43c5b590801b4b"
    ),
    files=(
        ModelFile(
            "gpt-oss-20b-MXFP4.gguf",
            "27cd6c432c7672cb812a92f611cf3ba7bbc35928262bb1e1253ff4ee6ae35901",
            12_109_566_624,
        ),
    ),
)
GEMMA4_12B: Final = ModelSpec(
    key="gemma-4-12b-qat-q4",
    description="Gemma 4 12B QAT Q4_0 — current default; evaluated on Ask",
    license="Apache-2.0",
    base_url=(
        "https://huggingface.co/google/gemma-4-12B-it-qat-q4_0-gguf/resolve/"
        "29d097773436b69ff9feafd636ab4cf873786537"
    ),
    files=(
        ModelFile(
            "gemma-4-12b-it-qat-q4_0.gguf",
            "93567e57a8fe10b23569b9d9ec38cd005deedf71e29477c421a4b83f418a538b",
            6_975_879_296,
        ),
    ),
)

# Additional local chat models. Sizes and SHA-256s are the LFS metadata at each pinned commit.
QWEN35_4B: Final = ModelSpec(
    key="qwen3.5-4b-q4km",
    description="Qwen3.5 4B Q4_K_M — compact multilingual Ask model (experimental)",
    license="Apache-2.0",
    base_url="https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/e87f176479d0855a907a41277aca2f8ee7a09523",
    files=(
        ModelFile(
            "Qwen3.5-4B-Q4_K_M.gguf",
            "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4",
            2_740_937_888,
        ),
    ),
)
QWEN35_9B: Final = ModelSpec(
    key="qwen3.5-9b-q4km",
    description="Qwen3.5 9B Q4_K_M — larger multilingual alternative (experimental)",
    license="Apache-2.0",
    base_url="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/3885219b6810b007914f3a7950a8d1b469d598a5",
    files=(
        ModelFile(
            "Qwen3.5-9B-Q4_K_M.gguf",
            "03b74727a860a56338e042c4420bb3f04b2fec5734175f4cb9fa853daf52b7e8",
            5_680_522_464,
        ),
    ),
)
GEMMA4_E4B: Final = ModelSpec(
    key="gemma-4-e4b-q4",
    description="Gemma 4 E4B Q4_0 — smaller Gemma for Ask (experimental)",
    license="Apache-2.0",
    base_url="https://huggingface.co/ggml-org/gemma-4-E4B-it-GGUF/resolve/27cd6836f8e2a6e5e6b57ea0e77fa72490706985",
    files=(
        ModelFile(
            "gemma-4-E4B-it-Q4_0.gguf",
            "a555b900214b477d8880e7832e0b8925e139b0159640036b09fe472b6f2097f2",
            4_590_807_392,
        ),
    ),
)
QWEN3_4B: Final = ModelSpec(
    key="qwen3-4b-instruct-2507-q4km",
    description="Qwen3 4B Instruct Q4_K_M — compact, non-thinking text model (experimental)",
    license="Apache-2.0",
    base_url="https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/a06e946bb6b655725eafa393f4a9745d460374c9",
    files=(
        ModelFile(
            "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
            "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597",
            2_497_281_120,
        ),
    ),
)
MINISTRAL3_3B: Final = ModelSpec(
    key="ministral-3-3b-instruct-q4km",
    description="Ministral 3 3B Instruct Q4_K_M — small French/English model (experimental)",
    license="Apache-2.0",
    base_url="https://huggingface.co/mistralai/Ministral-3-3B-Instruct-2512-GGUF/resolve/eb599d408350ea2bb60452cb86be7c7b2fc28227",
    files=(
        ModelFile(
            "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
            "9ed150d4367e68df0ac8e1540f6ddc65b42d0ee26378329d1ecbca60f93fc5f8",
            2_147_023_008,
        ),
    ),
)
NEMOTRON_BOLT_E4B: Final = ModelSpec(
    key="nemotron-3.5-bolt-e4b-q4km",
    description="Nemotron 3.5 Bolt E4B Q4_K_M — instruction-focused model (experimental)",
    license="OpenMDW-1.1",
    base_url="https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB/resolve/1db54d5b12533ff1bcaa8e42cf4c667988a1f1be",
    files=(
        ModelFile(
            "Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf",
            "c8af2b8aea10a690a1d9491db744a9446660173cb38fc489612b556615a69612",
            6_180_446_208,
        ),
    ),
)
CHAT_CANDIDATES: Final[dict[str, ModelSpec]] = {
    spec.key: spec
    for spec in (
        GEMMA4_12B,
        QWEN35_4B,
        GEMMA4_E4B,
        QWEN35_9B,
        NEMOTRON_BOLT_E4B,
        QWEN3_4B,
        MINISTRAL3_3B,
        QWEN3_14B,
        GPT_OSS_20B,
    )
}

REGISTRY: Final[dict[str, ModelSpec]] = {
    spec.key: spec
    for spec in (
        WHISPER_LARGE_V3_TURBO,
        WHISPER_LARGE_V3,
        QWEN3_ASR,
        CANARY_V2,
        PYANNOTE_SEGMENTATION,
        WESPEAKER_RESNET34,
        QWEN3_EMBEDDING,
    )
}


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    """A pinned third-party program: zip archives unpacked together into ``runtime/<key>/``."""

    key: str  # the release name, also the folder
    description: str
    license: str
    base_url: str  # archives are at base_url + "/" + name
    archives: tuple[ModelFile, ...]
    program: str  # file that must exist once unpacked

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.archives)


# llama.cpp's own server, official ggml-org Windows CUDA 13.5 build (MIT). Upgrading means
# changing this pin on purpose; hashes are the digests GitHub publishes for each asset.
LLAMA_BUILD: Final = "b12209"
LLAMA_RUNTIME: Final = RuntimeSpec(
    key=f"llama-{LLAMA_BUILD}",
    description=f"llama.cpp {LLAMA_BUILD} llama-server (CUDA 13.5, Windows x64)",
    license="MIT",
    base_url=f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}",
    archives=(
        ModelFile(
            f"llama-{LLAMA_BUILD}-bin-win-cuda-13.5-x64.zip",
            "e627d12e080565eb07bb6853e511670b154f8c0c1f12ae40a58f27d9ec2a3584",
            170_946_929,
        ),
        ModelFile(
            "cudart-llama-bin-win-cuda-13.5-x64.zip",
            "c2bf4cc202bfbaaa7a0b75f5082457c3811326947bb216059b72839d844e9cba",
            426_314_199,
        ),
    ),
    program="llama-server.exe",
)


type Pinned = ModelSpec | RuntimeSpec


@dataclass(frozen=True, slots=True)
class Needed:
    """A pinned download the current settings need, and what for (shown in Settings > Models)."""

    spec: Pinned
    needed_for: str


def required(config: Config) -> list[Needed]:
    """What the app needs for the current ``config.toml``: the chosen speech model, the speaker
    models, the embedding model, the llama.cpp runtime and the chosen chat model. Choices that
    are not pinned here (a model copied in by hand) are left out: nothing to download."""
    wanted: list[Needed] = []
    speech = REGISTRY.get(config.audio.whisper_model)
    if speech is not None:
        wanted.append(Needed(speech, "Transcription"))
    wanted += [
        Needed(PYANNOTE_SEGMENTATION, "Speakers"),
        Needed(WESPEAKER_RESNET34, "Speakers"),
    ]
    embedding = REGISTRY.get(config.search.embedding_model)
    if embedding is not None:
        wanted.append(Needed(embedding, "Search by meaning"))
    wanted.append(Needed(LLAMA_RUNTIME, "Search by meaning and Ask"))
    chat = CHAT_CANDIDATES.get(config.search.chat_model)
    if chat is not None:
        wanted.append(Needed(chat, "Ask"))
    return wanted


def is_ready(models_dir: Path, runtime_dir: Path, spec: Pinned) -> bool:
    """Cheap presence check (sizes, or the unpacked program): no hashing, no network."""
    if isinstance(spec, RuntimeSpec):
        return runtime_ready(runtime_dir, spec)
    return is_complete(models_dir, spec)


def fetch(
    models_dir: Path,
    runtime_dir: Path,
    spec: Pinned,
    opener: Opener | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    """Download one pinned model or runtime (only what is missing). Raises ModelDownloadError."""
    if isinstance(spec, RuntimeSpec):
        download_runtime(runtime_dir, spec, opener or default_opener, progress)
    else:
        download(models_dir, spec, opener or default_opener, progress)


class _Response(Protocol):
    def read(self, size: int, /) -> bytes: ...

    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...


type Opener = Callable[[str], _Response]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def is_present(models_dir: Path, spec: ModelSpec, file: ModelFile) -> bool:
    """Cheap check (name and size), used at runtime; ``verify`` re-hashes."""
    path = models_dir / spec.key / file.name
    return path.is_file() and path.stat().st_size == file.size


def is_complete(models_dir: Path, spec: ModelSpec) -> bool:
    return all(is_present(models_dir, spec, f) for f in spec.files)


def verify(models_dir: Path, spec: ModelSpec) -> list[str]:
    """Names of files that are missing or whose SHA-256 differs from the pin."""
    return [
        f.name
        for f in spec.files
        if not is_present(models_dir, spec, f)
        or sha256_of(models_dir / spec.key / f.name) != f.sha256
    ]


class _HostLimitedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # noqa: PLR0913, PLR0917 - signature fixed by urllib
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]  # urllib's stub types are narrower than its runtime use


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host in ALLOWED_HOSTS or host.endswith(ALLOWED_HOST_SUFFIXES)
    ):
        msg = "refusing to download from an unexpected host"
        raise ModelDownloadError(msg)


def default_opener(url: str) -> _Response:
    _check_url(url)
    opener = urllib.request.build_opener(_HostLimitedRedirects)
    return opener.open(url, timeout=TIMEOUT_S)  # type: ignore[return-value]  # an HTTPResponse


def download(
    models_dir: Path,
    spec: ModelSpec,
    opener: Opener = default_opener,
    progress: Callable[[str, int, int], None] | None = None,
) -> list[str]:
    """Fetch every file of ``spec`` that is missing or fails its checksum.

    Returns the names downloaded (empty: everything was already in place and no network was
    used). Raises ModelDownloadError if a download does not match its pinned size and SHA-256.
    """
    target = models_dir / spec.key
    target.mkdir(parents=True, exist_ok=True)
    fetched: list[str] = []
    for file in spec.files:
        final = target / file.name
        if is_present(models_dir, spec, file) and sha256_of(final) == file.sha256:
            continue
        _fetch(f"{spec.base_url}/{file.name}", final, file, opener=opener, progress=progress)
        fetched.append(file.name)
    return fetched


def _fetch(
    url: str,
    final: Path,
    file: ModelFile,
    *,
    opener: Opener,
    progress: Callable[[str, int, int], None] | None,
) -> None:
    final.parent.mkdir(parents=True, exist_ok=True)  # a file name may carry a folder: "onnx/x"
    part = final.with_name(final.name + ".part")
    digest = hashlib.sha256()
    received = 0
    try:
        with opener(url) as response, part.open("wb") as out:
            while chunk := response.read(CHUNK):
                received += len(chunk)
                if received > file.size:
                    msg = f"{file.name} is larger than expected"
                    raise ModelDownloadError(msg)  # noqa: TRY301 - cleanup below
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(file.name, received, file.size)
        if received != file.size or digest.hexdigest() != file.sha256:
            msg = f"{file.name} does not match its pinned checksum"
            raise ModelDownloadError(msg)  # noqa: TRY301
        part.replace(final)
    except (OSError, urllib.error.URLError) as exc:
        part.unlink(missing_ok=True)
        msg = f"downloading {file.name} failed: {type(exc).__name__}"
        raise ModelDownloadError(msg) from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise


# --- pinned runtimes ------------------------------------------------------------------------


def runtime_program(runtime_dir: Path, spec: RuntimeSpec) -> Path:
    return runtime_dir / spec.key / spec.program


def runtime_ready(runtime_dir: Path, spec: RuntimeSpec) -> bool:
    """Cheap check used at runtime: the unpacked program exists."""
    return runtime_program(runtime_dir, spec).is_file()


def download_runtime(
    runtime_dir: Path,
    spec: RuntimeSpec,
    opener: Opener = default_opener,
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    """Fetch the archives (checked against their pinned SHA-256), unpack them together into a
    scratch folder and move it into place, so a half-unpacked runtime is never used."""
    if runtime_ready(runtime_dir, spec):
        return
    target = runtime_dir / spec.key
    scratch = runtime_dir / f"{spec.key}.part"
    downloads = runtime_dir / f"{spec.key}.zips"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    downloads.mkdir(parents=True, exist_ok=True)
    try:
        for archive in spec.archives:
            zip_path = downloads / archive.name
            if not (zip_path.is_file() and sha256_of(zip_path) == archive.sha256):
                url = f"{spec.base_url}/{archive.name}"
                _fetch(url, zip_path, archive, opener=opener, progress=progress)
            _unpack(zip_path, scratch)
        if not (scratch / spec.program).is_file():
            msg = f"{spec.program} missing from the unpacked archives"
            raise ModelDownloadError(msg)
        shutil.rmtree(target, ignore_errors=True)
        scratch.replace(target)
    except OSError as exc:
        msg = f"unpacking {spec.key} failed: {type(exc).__name__}"
        raise ModelDownloadError(msg) from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    shutil.rmtree(downloads, ignore_errors=True)


def _unpack(zip_path: Path, into: Path) -> None:
    """Extract an archive, refusing any member that would land outside ``into``."""
    root = into.resolve()
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if not (root / member.filename).resolve().is_relative_to(root):
                    msg = "archive member would escape the target folder"
                    raise ModelDownloadError(msg)
            archive.extractall(root)  # members checked above
    except zipfile.BadZipFile as exc:
        msg = f"{zip_path.name} is not a valid archive"
        raise ModelDownloadError(msg) from exc
