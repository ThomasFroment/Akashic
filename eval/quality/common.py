"""Shared helpers of the quality harness: paths, clocks, JSONL logs, hashing, DPAPI, junctions."""

from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from ctypes import wintypes
from pathlib import Path

QUALITY = Path(__file__).resolve().parent
REPO = QUALITY.parents[1]
LOCAL = QUALITY / "local"
ASSETS = QUALITY / "assets"
RAW = QUALITY / "data" / "raw"
RESULTS = QUALITY / "results"
MODELS_HOME = LOCAL / "models-home"
LENGTHS = ("smoke", "standard", "day", "attended")
CANARY_RGB = (250, 0, 250)  # pixel marker of the canary window and the vault page
CANARY_TEXT = "CANARY-VAULT-7f3a9c"
UA = {"User-Agent": "akashic-quality-harness"}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def mono_ns() -> int:
    return time.monotonic_ns()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def load_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True), encoding="utf-8", newline="\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


class JsonlWriter:
    """Append-only, thread-safe, flushed per line so a crashed run keeps its log."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def write(self, **fields: object) -> None:
        fields.setdefault("wall_ms", now_ms())
        fields.setdefault("mono_ns", mono_ns())
        line = json.dumps(fields, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()

    def close(self) -> None:
        self._f.close()


def run_id(length: str, seed: int, machine: str) -> str:
    return f"{dt.datetime.now().strftime('%Y%m%d')}-{length}-s{seed}-{machine}-{dt.datetime.now().strftime('%H%M')}"


def download_pinned(url: str, dest: Path, sha256: str) -> Path:
    """Fetch a public file, keep it only when its sha256 matches the pin."""
    if dest.is_file() and sha256_file(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as resp, part.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            h.update(chunk)
    if h.hexdigest() != sha256:
        part.unlink()
        raise RuntimeError(f"checksum mismatch for {url}")
    part.replace(dest)
    return dest


# --- DPAPI (user scope) ---------------------------------------------------------------------


class _Blob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))


def _blob(data: bytes) -> tuple[_Blob, object]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(fn_name: str, data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    src, keep = _blob(data)
    out = _Blob()
    ok = getattr(crypt32, fn_name)(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    del keep
    if not ok:
        raise OSError(ctypes.get_last_error(), fn_name)
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_wrap(data: bytes) -> bytes:
    return _dpapi("CryptProtectData", data)


def dpapi_unwrap(data: bytes) -> bytes:
    return _dpapi("CryptUnprotectData", data)


# --- files and processes --------------------------------------------------------------------


def make_junction(link: Path, target: Path) -> None:
    """Replace `link` (an empty folder or an old junction) with a junction to `target`."""
    if link.is_junction():
        os.rmdir(link)
    elif link.exists():
        os.rmdir(link)  # refuses a non-empty folder: never delete data
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def renamed_python(name: str, dest_dir: Path) -> tuple[Path, dict[str, str]]:
    """A copy of the base interpreter under another image name, plus the env it needs to start.

    Windows names an audio session after the exe, so `Discord.exe` plays as discord.exe.
    """
    base = Path(sys._base_executable).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    exe = dest_dir / name
    if not exe.is_file():
        shutil.copy2(base, exe)
    for dll in base.parent.glob("*.dll"):
        # Several renamed exes share one folder: a DLL already copied may be loaded by a running one.
        if not (dest_dir / dll.name).is_file():
            shutil.copy2(dll, dest_dir / dll.name)
    site = Path(sys.prefix) / "Lib" / "site-packages"
    env = {"PYTHONHOME": sys.base_prefix, "PYTHONPATH": os.pathsep.join([str(site), str(QUALITY)])}
    return exe, env


def free_loopback_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_local(name: str):
    """Import a sibling module that shares its name with a stdlib one (site.py)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"quality_{name}", QUALITY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
