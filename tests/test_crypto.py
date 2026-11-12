from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, cast

import pytest
from conftest import FAST_ARGON2

from akashic.storage import crypto
from akashic.storage.crypto import (
    DataKey,
    DecryptionError,
    KeyfileError,
    WrongPassphraseError,
    change_passphrase,
    decrypt_blob,
    encrypt_blob,
    unwrap_data_key,
    wrap_data_key,
    write_keyfile_atomic,
)

if TYPE_CHECKING:
    from pathlib import Path


def _doc(contents: bytes) -> dict[str, object]:
    return json.loads(contents)


def _section(doc: dict[str, object], name: str) -> dict[str, str]:
    value = doc[name]
    assert isinstance(value, dict)
    return cast("dict[str, str]", value)


def test_wrap_unwrap_roundtrip() -> None:
    key = DataKey.generate()
    keyfile = wrap_data_key(key, "correct horse", FAST_ARGON2)
    assert unwrap_data_key(keyfile, "correct horse").raw() == key.raw()


def test_wrong_passphrase() -> None:
    keyfile = wrap_data_key(DataKey.generate(), "correct horse", FAST_ARGON2)
    with pytest.raises(WrongPassphraseError):
        unwrap_data_key(keyfile, "correct hors")


def test_passphrase_unicode_normalization() -> None:
    composed, decomposed = "café crème", "café crème"
    keyfile = wrap_data_key(DataKey.generate(), composed, FAST_ARGON2)
    unwrap_data_key(keyfile, decomposed)  # must not raise


def test_keyfile_contains_no_key_material() -> None:
    key = DataKey.generate()
    keyfile = wrap_data_key(key, "pass phrase", FAST_ARGON2)
    assert key.raw() not in keyfile
    assert key.raw().hex().encode() not in keyfile
    assert base64.b64encode(key.raw()) not in keyfile


def test_keyfile_format() -> None:
    doc = _doc(wrap_data_key(DataKey.generate(), "pass phrase", crypto.DEFAULT_ARGON2))
    assert doc["format"] == "akashic-keyfile"
    assert doc["version"] == 1
    assert doc["kdf"] == {
        "name": "argon2id",
        "argon2_version": 19,
        "time_cost": 3,
        "memory_kib": 262144,
        "parallelism": 4,
        "salt": _section(doc, "kdf")["salt"],
    }
    assert len(base64.b64decode(_section(doc, "kdf")["salt"])) == 16
    wrap = _section(doc, "wrap")
    assert wrap["alg"] == "AES-256-GCM"
    assert len(base64.b64decode(wrap["nonce"])) == 12
    assert len(base64.b64decode(wrap["ciphertext"])) == 32 + 16


def test_salt_and_nonce_are_random() -> None:
    key = DataKey.generate()
    a = _doc(wrap_data_key(key, "pass phrase", FAST_ARGON2))
    b = _doc(wrap_data_key(key, "pass phrase", FAST_ARGON2))
    assert a["kdf"] != b["kdf"]
    assert a["wrap"] != b["wrap"]


def _tamper(contents: bytes, section: str, field: str, value: object) -> bytes:
    doc = _doc(contents)
    target = doc[section]
    assert isinstance(target, dict)
    target[field] = value
    return json.dumps(doc).encode()


def test_tampered_kdf_params_fail_authentication() -> None:
    contents = wrap_data_key(DataKey.generate(), "pass phrase", FAST_ARGON2)
    tampered = _tamper(contents, "kdf", "time_cost", 2)
    with pytest.raises(WrongPassphraseError):
        unwrap_data_key(tampered, "pass phrase")


def test_tampered_ciphertext_fails_authentication() -> None:
    contents = wrap_data_key(DataKey.generate(), "pass phrase", FAST_ARGON2)
    wrap = _section(_doc(contents), "wrap")
    raw = bytearray(base64.b64decode(wrap["ciphertext"]))
    raw[0] ^= 1
    tampered = _tamper(contents, "wrap", "ciphertext", base64.b64encode(raw).decode())
    with pytest.raises(WrongPassphraseError):
        unwrap_data_key(tampered, "pass phrase")


@pytest.mark.parametrize(
    "contents",
    [
        b"\xff\xfe",
        b"not json",
        b"[]",
        b'{"format": "something-else", "version": 1}',
        b'{"format": "akashic-keyfile", "version": 2}',
        b'{"format": "akashic-keyfile", "version": 1, "kdf": 3}',
    ],
)
def test_malformed_keyfiles(contents: bytes) -> None:
    with pytest.raises(KeyfileError):
        unwrap_data_key(contents, "pass phrase")


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("kdf", "name", "scrypt"),
        ("kdf", "argon2_version", 16),
        ("kdf", "time_cost", 0),
        ("kdf", "memory_kib", 1024),
        ("kdf", "parallelism", True),
        ("kdf", "salt", "!!!"),
        ("kdf", "salt", 5),
        ("kdf", "salt", base64.b64encode(b"short").decode()),
        ("wrap", "alg", "AES-128-CBC"),
        ("wrap", "nonce", base64.b64encode(b"x" * 8).decode()),
    ],
)
def test_invalid_keyfile_fields(section: str, field: str, value: object) -> None:
    contents = wrap_data_key(DataKey.generate(), "pass phrase", FAST_ARGON2)
    with pytest.raises(KeyfileError):
        unwrap_data_key(_tamper(contents, section, field, value), "pass phrase")


def test_missing_wrap_section() -> None:
    doc = _doc(wrap_data_key(DataKey.generate(), "pass phrase", FAST_ARGON2))
    del doc["wrap"]
    with pytest.raises(KeyfileError):
        unwrap_data_key(json.dumps(doc).encode(), "pass phrase")


# Pins change_passphrase: delete or rewrite with M14.4.
def test_change_passphrase_keeps_data_key(tmp_path: Path) -> None:
    path = tmp_path / "db" / "keyfile"
    key = DataKey.generate()
    write_keyfile_atomic(path, wrap_data_key(key, "old passphrase", FAST_ARGON2))
    change_passphrase(path, "old passphrase", "new passphrase", FAST_ARGON2)
    assert unwrap_data_key(path.read_bytes(), "new passphrase").raw() == key.raw()
    with pytest.raises(WrongPassphraseError):
        unwrap_data_key(path.read_bytes(), "old passphrase")
    assert not path.with_name("keyfile.tmp").exists()


def test_change_passphrase_with_wrong_old_leaves_file(tmp_path: Path) -> None:
    path = tmp_path / "keyfile"
    write_keyfile_atomic(path, wrap_data_key(DataKey.generate(), "old passphrase", FAST_ARGON2))
    before = path.read_bytes()
    with pytest.raises(WrongPassphraseError):
        change_passphrase(path, "nope nope", "new passphrase", FAST_ARGON2)
    assert path.read_bytes() == before


def test_data_key_wipe_and_repr() -> None:
    key = DataKey.generate()
    assert "redacted" in repr(key)
    assert key.raw().hex() not in repr(key)
    key.wipe()
    assert key.wiped
    with pytest.raises(crypto.CryptoError):
        key.raw()


def test_data_key_length() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        DataKey(b"short")


def test_subkeys_are_independent_and_stable() -> None:
    key = DataKey.generate()
    assert key.subkey("db") != key.subkey("media")
    assert key.subkey("db") != key.raw()
    assert key.subkey("db") == DataKey(key.raw()).subkey("db")
    assert len(key.subkey("media")) == 32


def test_blob_roundtrip_and_context_binding() -> None:
    key = DataKey.generate().subkey("media")
    blob = encrypt_blob(key, b"webp bytes", b"2026/11/08/1.webp")
    assert b"webp bytes" not in blob
    assert decrypt_blob(key, blob, b"2026/11/08/1.webp") == b"webp bytes"
    with pytest.raises(DecryptionError):
        decrypt_blob(key, blob, b"2026/11/08/2.webp")
    with pytest.raises(DecryptionError):
        decrypt_blob(DataKey.generate().subkey("media"), blob, b"2026/11/08/1.webp")


@pytest.mark.parametrize("blob", [b"", b"AKM1short", b"XXXX" + b"\0" * 40])
def test_blob_rejects_garbage(blob: bytes) -> None:
    with pytest.raises(DecryptionError):
        decrypt_blob(b"\0" * 32, blob, b"ctx")


# --- #55: Argon2 parameters are bounded before any derivation ----------------------------
# Ceilings and floors from the plan: memory 8 MiB..1 GiB, time 1..10, parallelism 1..16.


def _spy_derivation(monkeypatch: pytest.MonkeyPatch) -> list[crypto.Argon2Params]:
    derived: list[crypto.Argon2Params] = []

    def record(passphrase: str, salt: bytes, params: crypto.Argon2Params) -> bytes:
        del passphrase, salt
        derived.append(params)  # never run the real Argon2 at a ceiling
        return bytes(32)

    monkeypatch.setattr(crypto, "derive_kek", record)
    return derived


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_kib", 1_048_577),
        ("memory_kib", 2**64),
        ("time_cost", 11),
        ("time_cost", 2**64),
        ("parallelism", 17),
        ("parallelism", 2**64),
    ],
)
def test_parameters_above_a_ceiling_are_refused_without_derivation(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    contents = wrap_data_key(DataKey.generate(), "pass phrase", FAST_ARGON2)
    tampered = _tamper(contents, "kdf", field, value)
    derived = _spy_derivation(monkeypatch)
    with pytest.raises(KeyfileError):
        unwrap_data_key(tampered, "pass phrase")
    assert derived == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_kib", 1_048_576),
        ("memory_kib", 8 * 1024),
        ("time_cost", 10),
        ("time_cost", 1),
        ("parallelism", 16),
        ("parallelism", 1),
    ],
)
def test_parameters_exactly_at_a_bound_reach_derivation(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    contents = wrap_data_key(DataKey.generate(), "pass phrase", FAST_ARGON2)
    tampered = _tamper(contents, "kdf", field, value)
    derived = _spy_derivation(monkeypatch)
    with pytest.raises(WrongPassphraseError):  # the fake KEK fails authentication
        unwrap_data_key(tampered, "pass phrase")
    assert len(derived) == 1
    assert getattr(derived[0], field) == value


@pytest.mark.parametrize("params", [crypto.DEFAULT_ARGON2, FAST_ARGON2], ids=["default", "fast"])
def test_default_and_fast_keyfiles_unlock(params: crypto.Argon2Params) -> None:
    key = DataKey.generate()
    keyfile = wrap_data_key(key, "pass phrase", params)
    assert unwrap_data_key(keyfile, "pass phrase").raw() == key.raw()
    with pytest.raises(WrongPassphraseError):
        unwrap_data_key(keyfile, "other phrase")


def test_in_range_tampered_parameters_fail_authentication_not_validation() -> None:
    contents = wrap_data_key(DataKey.generate(), "pass phrase", FAST_ARGON2)
    for field, value in (("memory_kib", 16 * 1024), ("parallelism", 2)):
        with pytest.raises(WrongPassphraseError):
            unwrap_data_key(_tamper(contents, "kdf", field, value), "pass phrase")
