"""Key derivation, key wrapping and AES-GCM helpers.

Design (documented in full in docs/SECURITY.md):

- A random 256-bit data key is the root secret. It is stored only wrapped, in ``db/keyfile``.
- The wrapping key (KEK) is derived from the passphrase with Argon2id. KDF parameters and salt
  are stored in the keyfile next to the wrapped key and bound to it as AES-GCM associated data.
- Subkeys for the database and media files are derived from the data key with HKDF-SHA256,
  so each purpose uses an independent key.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from akashic.jsonish import as_dict

if TYPE_CHECKING:
    from pathlib import Path

KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
SALT_BYTES: Final = 16

KEYFILE_FORMAT: Final = "akashic-keyfile"
KEYFILE_VERSION: Final = 1
MEDIA_MAGIC: Final = b"AKM1"
ARGON2_VERSION: Final = 19  # 0x13, the current Argon2 version

MIN_PASSPHRASE_CHARS: Final = 8


class CryptoError(Exception):
    """Base class for key and encryption errors."""


class WrongPassphraseError(CryptoError):
    """The passphrase does not unwrap the data key."""


class KeyfileError(CryptoError):
    """The keyfile is missing, malformed or of an unsupported version."""


class DecryptionError(CryptoError):
    """Ciphertext failed authentication (wrong key, wrong context or tampered data)."""


@dataclass(frozen=True, slots=True)
class Argon2Params:
    time_cost: int
    memory_kib: int
    parallelism: int


# RFC 9106 "second recommended option" (t=3, p=4) with 256 MiB instead of 64 MiB:
# about one second on the target machine, paid once per unlock.
DEFAULT_ARGON2: Final = Argon2Params(time_cost=3, memory_kib=256 * 1024, parallelism=4)

# Floor enforced when reading a keyfile, so a tampered file can't make us accept weak params
# silently. (Tampered params also fail authentication, but this gives a clearer error.)
_MIN_ARGON2: Final = Argon2Params(time_cost=1, memory_kib=8 * 1024, parallelism=1)

# Ceiling enforced before any derivation, so a tampered keyfile can't make the unlock run out of
# memory or time: each is 4x the default memory, over 3x its passes, 4x its lanes.
_MAX_ARGON2: Final = Argon2Params(time_cost=10, memory_kib=1_024 * 1024, parallelism=16)


class DataKey:
    """The in-memory data key. Best-effort wiping only: Python may keep copies we can't reach."""

    __slots__ = ("_buf",)

    def __init__(self, raw: bytes) -> None:
        if len(raw) != KEY_BYTES:
            msg = f"data key must be {KEY_BYTES} bytes"
            raise ValueError(msg)
        self._buf = bytearray(raw)

    @classmethod
    def generate(cls) -> DataKey:
        return cls(secrets.token_bytes(KEY_BYTES))

    def raw(self) -> bytes:
        if self.wiped:
            msg = "data key has been wiped"
            raise CryptoError(msg)
        return bytes(self._buf)

    @property
    def wiped(self) -> bool:
        return len(self._buf) == 0

    def wipe(self) -> None:
        for i in range(len(self._buf)):
            self._buf[i] = 0
        self._buf.clear()

    def subkey(self, purpose: str) -> bytes:
        """Derive an independent 256-bit key for one purpose (e.g. "db", "media")."""
        return HKDF(
            algorithm=hashes.SHA256(),
            length=KEY_BYTES,
            salt=None,
            info=f"akashic/{purpose}/v1".encode(),
        ).derive(self.raw())

    def __repr__(self) -> str:
        return "DataKey(<redacted>)"


def _encode_passphrase(passphrase: str) -> bytes:
    # NFC so that "é" typed as one code point or as e + combining accent derives the same key.
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def derive_kek(passphrase: str, salt: bytes, params: Argon2Params) -> bytes:
    return hash_secret_raw(
        secret=_encode_passphrase(passphrase),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_kib,
        parallelism=params.parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: object, field: str) -> bytes:
    if not isinstance(text, str):
        msg = f"keyfile field {field!r} must be a string"
        raise KeyfileError(msg)
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error as exc:
        msg = f"keyfile field {field!r} is not valid base64"
        raise KeyfileError(msg) from exc


def _header_aad(kdf: dict[str, object]) -> bytes:
    header = {"format": KEYFILE_FORMAT, "version": KEYFILE_VERSION, "kdf": kdf}
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def wrap_data_key(key: DataKey, passphrase: str, params: Argon2Params = DEFAULT_ARGON2) -> bytes:
    """Return keyfile contents holding ``key`` encrypted under a passphrase-derived KEK."""
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    kdf: dict[str, object] = {
        "name": "argon2id",
        "argon2_version": ARGON2_VERSION,
        "time_cost": params.time_cost,
        "memory_kib": params.memory_kib,
        "parallelism": params.parallelism,
        "salt": _b64(salt),
    }
    kek = derive_kek(passphrase, salt, params)
    wrapped = AESGCM(kek).encrypt(nonce, key.raw(), _header_aad(kdf))
    doc = {
        "format": KEYFILE_FORMAT,
        "version": KEYFILE_VERSION,
        "kdf": kdf,
        "wrap": {"alg": "AES-256-GCM", "nonce": _b64(nonce), "ciphertext": _b64(wrapped)},
    }
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _int_field(kdf: dict[str, object], name: str, minimum: int, maximum: int) -> int:
    value = kdf.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        msg = f"keyfile kdf.{name} must be an integer from {minimum} to {maximum}"
        raise KeyfileError(msg)
    return value


def _dict_field(doc: dict[str, object], name: str) -> dict[str, object]:
    value = as_dict(doc.get(name))
    if value is None:
        msg = f"keyfile field {name!r} must be an object"
        raise KeyfileError(msg)
    return value


def unwrap_data_key(keyfile: bytes, passphrase: str) -> DataKey:
    """Decrypt the data key. Raises WrongPassphraseError or KeyfileError."""
    try:
        doc = as_dict(json.loads(keyfile.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = "keyfile is not valid JSON"
        raise KeyfileError(msg) from exc
    if doc is None:
        msg = "keyfile must be a JSON object"
        raise KeyfileError(msg)
    if doc.get("format") != KEYFILE_FORMAT:
        msg = "not an Akashic keyfile"
        raise KeyfileError(msg)
    if doc.get("version") != KEYFILE_VERSION:
        msg = f"unsupported keyfile version {doc.get('version')!r}"
        raise KeyfileError(msg)

    kdf = _dict_field(doc, "kdf")
    wrap = _dict_field(doc, "wrap")
    if kdf.get("name") != "argon2id" or kdf.get("argon2_version") != ARGON2_VERSION:
        msg = "unsupported key derivation function"
        raise KeyfileError(msg)
    if wrap.get("alg") != "AES-256-GCM":
        msg = "unsupported key wrapping algorithm"
        raise KeyfileError(msg)
    params = Argon2Params(
        time_cost=_int_field(kdf, "time_cost", _MIN_ARGON2.time_cost, _MAX_ARGON2.time_cost),
        memory_kib=_int_field(kdf, "memory_kib", _MIN_ARGON2.memory_kib, _MAX_ARGON2.memory_kib),
        parallelism=_int_field(
            kdf, "parallelism", _MIN_ARGON2.parallelism, _MAX_ARGON2.parallelism
        ),
    )
    salt = _unb64(kdf.get("salt"), "kdf.salt")
    nonce = _unb64(wrap.get("nonce"), "wrap.nonce")
    ciphertext = _unb64(wrap.get("ciphertext"), "wrap.ciphertext")
    if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES:
        msg = "keyfile salt or nonce has the wrong length"
        raise KeyfileError(msg)

    kek = derive_kek(passphrase, salt, params)
    try:
        raw = AESGCM(kek).decrypt(nonce, ciphertext, _header_aad(kdf))
    except InvalidTag as exc:
        raise WrongPassphraseError from exc
    return DataKey(raw)


def write_keyfile_atomic(path: Path, contents: bytes) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written keyfile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as f:
        f.write(contents)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def change_passphrase(
    path: Path, old: str, new: str, params: Argon2Params = DEFAULT_ARGON2
) -> None:
    """Re-wrap the data key under a new passphrase. No data is re-encrypted."""
    key = unwrap_data_key(path.read_bytes(), old)
    try:
        write_keyfile_atomic(path, wrap_data_key(key, new, params))
    finally:
        key.wipe()


def encrypt_blob(key: bytes, plaintext: bytes, context: bytes) -> bytes:
    """AES-256-GCM. ``context`` (e.g. the file's relative path) is bound as associated data,
    so a ciphertext moved to another file fails to decrypt."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    return MEDIA_MAGIC + nonce + AESGCM(key).encrypt(nonce, plaintext, MEDIA_MAGIC + context)


def decrypt_blob(key: bytes, blob: bytes, context: bytes) -> bytes:
    header = len(MEDIA_MAGIC) + NONCE_BYTES
    if len(blob) < header or not blob.startswith(MEDIA_MAGIC):
        msg = "not an Akashic encrypted blob"
        raise DecryptionError(msg)
    nonce = blob[len(MEDIA_MAGIC) : header]
    try:
        return AESGCM(key).decrypt(nonce, blob[header:], MEDIA_MAGIC + context)
    except InvalidTag as exc:
        raise DecryptionError from exc
