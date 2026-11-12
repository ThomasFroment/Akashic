from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from conftest import FAST_ARGON2

from akashic import unlock as unlock_mod
from akashic.storage.crypto import DataKey, KeyfileError, unwrap_data_key
from akashic.unlock import UnlockError, run_until_quit, unlock

if TYPE_CHECKING:
    from akashic.config import Paths
    from akashic.unlock import Outcome


class ScriptedUI:
    def __init__(
        self,
        new: list[tuple[str, str] | None] | None = None,
        existing: list[str | None] | None = None,
    ) -> None:
        self.new = list(new or [])
        self.existing = list(existing or [])
        self.errors: list[str | None] = []

    def ask_new_passphrase(self, error: str | None) -> tuple[str, str] | None:
        self.errors.append(error)
        return self.new.pop(0)

    def ask_passphrase(self, error: str | None) -> str | None:
        self.errors.append(error)
        return self.existing.pop(0)


def test_first_run_creates_keyfile(paths: Paths) -> None:
    ui = ScriptedUI(
        new=[("short", "short"), ("long enough", "long enougj"), ("long enough", "long enough")]
    )
    key = unlock(paths, ui, FAST_ARGON2)
    assert key is not None
    assert ui.errors == [None, unlock_mod.TOO_SHORT, unlock_mod.MISMATCH]
    assert unwrap_data_key(paths.keyfile.read_bytes(), "long enough").raw() == key.raw()


def test_first_run_cancel(paths: Paths) -> None:
    assert unlock(paths, ScriptedUI(new=[None]), FAST_ARGON2) is None
    assert not paths.keyfile.exists()


def test_unlock_retries_on_wrong_passphrase(paths: Paths) -> None:
    created = unlock(paths, ScriptedUI(new=[("my passphrase", "my passphrase")]), FAST_ARGON2)
    assert created is not None
    ui = ScriptedUI(existing=["wrong", "also wrong", "my passphrase"])
    key = unlock(paths, ui, FAST_ARGON2)
    assert key is not None
    assert key.raw() == created.raw()
    assert ui.errors == [None, unlock_mod.WRONG_PASSPHRASE, unlock_mod.WRONG_PASSPHRASE]


def test_unlock_cancel(paths: Paths) -> None:
    unlock(paths, ScriptedUI(new=[("my passphrase", "my passphrase")]), FAST_ARGON2)
    assert unlock(paths, ScriptedUI(existing=[None]), FAST_ARGON2) is None


def test_missing_keyfile_with_existing_db_refuses(paths: Paths) -> None:
    paths.db_file.write_bytes(b"encrypted data")
    with pytest.raises(UnlockError, match="keyfile is missing"):
        unlock(paths, ScriptedUI(new=[("my passphrase", "my passphrase")]), FAST_ARGON2)
    assert not paths.keyfile.exists()


def test_corrupt_keyfile_raises(paths: Paths) -> None:
    paths.keyfile.write_bytes(b"garbage")
    with pytest.raises(KeyfileError):
        unlock(paths, ScriptedUI(existing=["anything"]), FAST_ARGON2)


def test_lock_asks_for_the_passphrase_again_and_quit_ends() -> None:
    keys = [DataKey.generate(), DataKey.generate()]
    unlocks = iter([keys[0], keys[1]])
    outcomes = iter(["lock", "quit"])
    ran: list[DataKey] = []

    def run(key: DataKey) -> Outcome:
        ran.append(key)
        return next(outcomes)  # type: ignore[return-value]

    run_until_quit(lambda: next(unlocks), run)
    assert ran == keys
    assert all(k.wiped for k in keys)  # each run's key is forgotten afterwards


def test_cancelling_the_unlock_after_a_lock_ends_the_app() -> None:
    first = DataKey.generate()
    unlocks = iter([first, None])
    calls: list[int] = []

    def run(key: DataKey) -> Outcome:
        del key
        calls.append(1)
        return "lock"

    run_until_quit(lambda: next(unlocks), run)
    assert calls == [1]
    assert first.wiped


def test_the_key_is_wiped_even_when_a_run_fails() -> None:
    key = DataKey.generate()

    def run(_key: DataKey) -> Outcome:
        raise RuntimeError

    with pytest.raises(RuntimeError):
        run_until_quit(lambda: key, run)
    assert key.wiped
