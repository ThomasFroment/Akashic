"""Tkinter passphrase dialogs (tkinter ships with Python). Smoke test: tests/manual/unlock.md.

Passphrases are Python strings and can't be wiped from memory; they are dropped as soon as the
key is unwrapped.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Final

TITLE: Final = "Akashic"
FIRST_RUN_WARNING: Final = (
    "Choose a passphrase. It encrypts everything Akashic records.\n\n"
    "If you lose it, all data is unrecoverable. There is no reset and no backdoor."
)


class TkUnlockUI:
    def _run(self, *, first_run: bool, error: str | None) -> list[str] | None:
        root = tk.Tk()
        root.title(TITLE)
        root.resizable(width=False, height=False)
        root.tk.call("wm", "attributes", str(root), "-topmost", "1")  # typed, unlike .attributes()
        frame = ttk.Frame(root, padding=20)
        frame.grid()

        text = FIRST_RUN_WARNING if first_run else "Enter your passphrase to unlock Akashic."
        ttk.Label(frame, text=text, wraplength=360, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )
        labels = ["Passphrase", "Repeat passphrase"] if first_run else ["Passphrase"]
        entries: list[ttk.Entry] = []
        for i, label in enumerate(labels, start=1):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=4)
            entry = ttk.Entry(frame, show="•", width=32)
            entry.grid(row=i, column=1, pady=4)
            entries.append(entry)
        row = len(labels) + 1
        if error:
            ttk.Label(frame, text=error, foreground="#b0442c").grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(8, 0)
            )
            row += 1

        result: list[list[str] | None] = [None]

        def submit(_event: object = None) -> None:
            result[0] = [e.get() for e in entries]
            root.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(buttons, text="Quit", command=root.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Unlock" if not first_run else "Create", command=submit).grid(
            row=0, column=1
        )
        root.bind("<Return>", submit)
        root.bind("<Escape>", lambda _e: root.destroy())
        entries[0].focus_force()
        root.mainloop()
        return result[0]

    def ask_new_passphrase(self, error: str | None) -> tuple[str, str] | None:
        values = self._run(first_run=True, error=error)
        return None if values is None else (values[0], values[1])

    def ask_passphrase(self, error: str | None) -> str | None:
        values = self._run(first_run=False, error=error)
        return None if values is None else values[0]


def show_error(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(TITLE, message, parent=root)  # pyright: ignore[reportUnknownMemberType] - partial stubs
    root.destroy()
