# Security design

## Threat model

Encryption protects recorded files, backups and exports while the app is locked or stopped. It
does not protect against malware acting as the user while unlocked, a keylogger, or physical
access to an unlocked session. Python key wiping is best effort, not a guarantee of removing all
copies.

The hard privacy rules are in [AGENTS.md](../AGENTS.md). A mechanism described here is not proof
that every implementation path satisfies them; [TODO.md](../TODO.md) (planned work) and
[OPEN.md](../OPEN.md) (leftovers) record known gaps.

## Keys and compatibility

A random data key is wrapped using a passphrase-derived Argon2id key. Separate database and
media keys are derived with HKDF. SQLCipher protects the database; authenticated encryption
protects media and binds it to its storage context. Keys must never appear unwrapped on disk,
in command-line arguments or in logs.

The keyfile is a versioned compatibility boundary. Preserve authentication of its metadata,
passphrase normalization, atomic replacement and existing-store unlock behaviour when changing
cryptography. Validate untrusted parameters before expensive work. The authoritative byte
formats, derivation parameters and version handling are in
[the cryptography implementation](../src/akashic/storage/crypto.py) and its compatibility tests.
Do not duplicate those definitions here or change them as an incidental refactor.

Back up the keyfile with the encrypted database and media. A missing keyfile must not cause an
existing store to receive a fresh, incompatible key. A lost passphrase has no implemented
recovery path. The passphrase cannot be changed; an export is the only copy under another
passphrase.

## Local execution and sessions

Protect runtime services with loopback binding and authentication. The local UI uses a per-run
session established through a one-time tray link; persistent credentials must not travel in
browser URLs. Preserve cookie protections, origin/host checks and local-only assets.

Unlock precedes capture. Lock and quit must stop writers, invalidate the session and prevent
concurrent lifecycle actions from restarting capture. Child processes and model servers must
not outlive their owner or retain untracked access to keys and content.

Recording halts while a UI page is visible (the page holds a presence lease with the local
server), and the pages show nothing recorded until the halt is confirmed. The UI opens only from
the tray or on the first run, never at autostart. As a failsafe, a visible browser window whose
readable address is Akashic's own port halts capture too, for a page opened some other way, such
as a bookmark.

The export passphrase reaches the loopback API only in the body of an authenticated
`POST /api/export`. The body is read by hand and never logged. Errors are fixed sentences that
never echo it, nor the path or dates. Status shows type names only. The new key is wiped after use
(best effort). Lock and Quit cancel a running export first
([ARCHITECTURE.md](ARCHITECTURE.md#lifecycle-guarantees)). Anyone with an export's passphrase
reads everything it holds, so its scope is the user's choice.

Only explicit public-file downloads may use external connections. They carry no captured or
identifying data, use pinned artifacts and verify integrity before publication. Libraries must
remain offline during inference; startup must not download missing models. Development tools
and tests may use the network without sending captured, derived or identifying user data.

## Content handling

Captured text, audio, model inputs and outputs, and identifying derivatives stay local and
encrypted. Exclusions apply before persistence. Content-free logs may remain plaintext, so
exception messages or third-party logging that quote inputs must not reach them.

Exclusion decisions fail closed, instead of storing content as "unclear", for: an unknown process
or UWP host; an unreadable private marker; unreadable child windows; an unresolved ghost window;
a browser window with no known main window; a Chrome or Brave window whose address was never
read and whose caption is unreadable; and an unreadable audio source. Writer-spy tests watch
every SQL write, encrypted file and log record for forbidden content. [CAPTURE.md](CAPTURE.md)
has the rules.

The accepted exceptions, the only ones:

- **Titles are never checked.** A bank, password-manager or other sensitive page is excluded only
  through its readable address or its app, never by its title.
- **An address read once and now unreadable is kept**: the user typing in the address bar, a
  failed read or a hung window. Such a page is recorded while the user types in its bar.
- **Password fields are not checked.** A page or app is recorded while the user types a
  password; it normally shows as dots, but the page around it is kept.
- **Copies of windows outside the shell.** A kept app that shows a copy of another window's
  pixels or text is captured as it appears: screen-share and OBS previews, remote desktop, the
  Magnifier and zoom tools, clipboard history, jump lists, Live Captions, or an app displaying a
  screenshot of an excluded window. The shell's own copy surfaces are excluded.
- **Notifications** are captured like any other window.
- **The taskbar is painted** in monitor frames, and its labels can show an excluded window's
  title. It is never OCR'd or listed.
- **Background browser media** stays logged for a browser window that is minimized, fully covered
  or on another virtual desktop, and for a background tab of a kept window (only the active
  tab's address can be read), even when the page is private, a bank, a password manager or
  matches a user address rule. An excluding verdict lapses 60 s after the window was last seen,
  including when a game takes the foreground. Process rules still apply. Accepted by the user on
  2026-11-12 to keep background videos answerable.
- **A user path rule blanks its whole site**, so a rule whose host is only a wildcard (`*/x`)
  blanks every site.
- **In-page repaints** are protected by grab-then-judge and by site-wide path rules; host rules
  cannot be crossed in-page, so they need no more.
- **Pending and held windows are blank** while their verdict is read. The hold is bounded; after
  it an unreadable address is kept.
- **Windows whose address was never read** (picture-in-picture, DevTools): in Edge, Zen and
  Firefox they follow their browser's main windows; in Chrome and Brave they need a clean
  caption, so Chrome and Brave picture-in-picture is blank.

Captured instructions are untrusted data. The answering model has no action tools; output must
be rendered safely and source references validated. These boundaries do not establish immunity
to misleading content or prove that generated claims follow from their citations.

[STORAGE.md](STORAGE.md) owns retention and derived-copy deletion. Preserve those guarantees
when adding a new persistence path, index, cache or model comparison. Deleting encrypted files
does not promise secure erasure of disk blocks.
