"""Encrypted media files: one AES-256-GCM file per screenshot.

Paths are relative to the media directory (``YYYY/MM/DD/<name>``) and bound into the ciphertext
as associated data, so a file moved or renamed fails to decrypt.
"""

from __future__ import annotations

import contextlib
import itertools
import secrets
import shutil
from pathlib import Path, PurePosixPath
from typing import Final

from akashic.storage.crypto import CryptoError, decrypt_blob, encrypt_blob

SUFFIX: Final = ".enc"


class MediaPathError(CryptoError):
    """A stored media path points outside the media directory."""


class MediaStore:
    def __init__(self, media_dir: Path, key: bytes) -> None:
        self._root = media_dir.resolve()
        self._key = key

    def _resolve(self, rel_path: str) -> Path:
        pure = PurePosixPath(rel_path)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts or not pure.name:
            msg = "media path must be relative and stay inside the media directory"
            raise MediaPathError(msg)
        path = (self._root / Path(*pure.parts)).resolve()
        if self._root not in path.parents:
            msg = "media path escapes the media directory"
            raise MediaPathError(msg)
        return path.with_name(path.name + SUFFIX)

    def write_unique(self, rel_path: str, data: bytes) -> str:
        """Encrypt and store ``data`` atomically (temp file, then rename) without ever replacing
        a file. A taken name gets ``_1``, ``_2``, ... before its extension. Returns the path
        actually written: keep that one."""
        pure = PurePosixPath(rel_path)
        candidate = rel_path
        for attempt in itertools.count(1):
            path = self._resolve(candidate)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
            try:
                tmp.write_bytes(encrypt_blob(self._key, data, (candidate + SUFFIX).encode()))
                tmp.rename(path)  # unlike replace, Windows refuses an existing target
            except FileExistsError:
                candidate = str(pure.with_name(f"{pure.stem}_{attempt}{pure.suffix}"))
            else:
                return candidate
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
        raise AssertionError  # pragma: no cover - itertools.count never ends

    def read(self, rel_path: str) -> bytes:
        path = self._resolve(rel_path)
        return decrypt_blob(self._key, path.read_bytes(), (rel_path + SUFFIX).encode())

    def delete(self, rel_path: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._resolve(rel_path).unlink()

    def delete_all(self) -> None:
        """Clear this media directory, including orphaned ciphertext and interrupted writes.

        Only with capture/worker stopped. Keep the root; never follow directory links outside
        it. shutil.rmtree also removes nested links/junctions without traversing their targets.
        Errors propagate so full-data deletion cannot claim success while files remain.
        """
        if not self._root.exists():
            return
        for path in self._root.iterdir():
            if path.is_symlink():
                path.unlink()
            elif path.is_junction():
                path.rmdir()
            else:
                if not path.resolve().is_relative_to(self._root):
                    msg = "media deletion target escapes the media directory"
                    raise MediaPathError(msg)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()

    def prune_empty_dirs(self, rel_path: str) -> None:
        """Remove the (now empty) date folders above a deleted file, up to the media root.
        Only for retention, which deletes old files: a folder in use today is never emptied."""
        parent = self._resolve(rel_path).parent
        while parent != self._root and self._root in parent.parents:
            try:
                parent.rmdir()  # fails while the folder still holds anything
            except OSError:
                return
            parent = parent.parent

    def is_inside(self, rel_path: str) -> bool:
        """Whether ``rel_path`` names a place inside the media directory."""
        try:
            self._resolve(rel_path)
        except MediaPathError:
            return False
        return True

    def exists(self, rel_path: str) -> bool:
        return self._resolve(rel_path).is_file()
