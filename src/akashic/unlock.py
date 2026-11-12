"""Unlock flow: first-run passphrase setup or unlocking an existing keyfile.

The dialog itself is in ``unlock_dialog.py`` (tkinter); this module holds the logic so it can
be tested without a display. Wrong passphrase: clear error, retry. No lockout, no hints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal, Protocol

from akashic.storage.crypto import (
    DEFAULT_ARGON2,
    MIN_PASSPHRASE_CHARS,
    Argon2Params,
    DataKey,
    WrongPassphraseError,
    unwrap_data_key,
    wrap_data_key,
    write_keyfile_atomic,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from akashic.config import Paths

type Outcome = Literal["quit", "lock"]

WRONG_PASSPHRASE: Final = "Wrong passphrase. Try again."  # noqa: S105 - a message, not a secret
MISMATCH: Final = "The two passphrases don't match."
TOO_SHORT: Final = f"Use at least {MIN_PASSPHRASE_CHARS} characters."


class UnlockError(Exception):
    """Unlocking can't proceed (e.g. the keyfile is missing but data exists)."""


class UnlockUI(Protocol):
    def ask_new_passphrase(self, error: str | None) -> tuple[str, str] | None:
        """First run: return (passphrase, confirmation), or None if the user cancels.
        Must warn that a lost passphrase makes all data unrecoverable."""
        ...

    def ask_passphrase(self, error: str | None) -> str | None:
        """Return the passphrase, or None if the user cancels."""
        ...


def unlock(paths: Paths, ui: UnlockUI, params: Argon2Params = DEFAULT_ARGON2) -> DataKey | None:
    """Return the data key, or None if the user cancelled."""
    if paths.keyfile.exists():
        contents = paths.keyfile.read_bytes()
        error: str | None = None
        while True:
            passphrase = ui.ask_passphrase(error)
            if passphrase is None:
                return None
            try:
                return unwrap_data_key(contents, passphrase)
            except WrongPassphraseError:
                error = WRONG_PASSPHRASE

    if paths.db_file.exists():
        # Creating a new key here would orphan the existing data. Never do that silently.
        msg = (
            f"The database exists but its keyfile is missing ({paths.keyfile}). "
            "Restore the keyfile from a backup; without it the data can't be decrypted."
        )
        raise UnlockError(msg)

    error = None
    while True:
        answer = ui.ask_new_passphrase(error)
        if answer is None:
            return None
        passphrase, confirmation = answer
        if passphrase != confirmation:
            error = MISMATCH
        elif len(passphrase) < MIN_PASSPHRASE_CHARS:
            error = TOO_SHORT
        else:
            key = DataKey.generate()
            write_keyfile_atomic(paths.keyfile, wrap_data_key(key, passphrase, params))
            return key


def run_until_quit(
    unlock_once: Callable[[], DataKey | None], run: Callable[[DataKey], Outcome]
) -> None:
    """Unlock, run, and after a Lock ask for the passphrase again; return on Quit or Cancel.
    The key is wiped after every run, whatever happened."""
    while True:
        key = unlock_once()
        if key is None:
            return
        try:
            outcome = run(key)
        finally:
            key.wipe()
        if outcome != "lock":
            return
