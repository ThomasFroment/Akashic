# Security design

## Threat model

Encryption protects recorded files and backups while the app is locked or stopped. It does not
protect against malware acting as the user while unlocked, a keylogger, or physical access to
an unlocked session. Python key wiping is best effort, not a guarantee of removing all copies.

The hard privacy rules are in [AGENTS.md](../AGENTS.md). A mechanism described here is not proof
that every implementation path satisfies them; the [audits](audits/) record known gaps.

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
recovery path.

## Local execution and sessions

Protect runtime services with loopback binding and authentication. The local UI uses a per-run
session established through a one-time tray link; persistent credentials must not travel in
browser URLs. Preserve cookie protections, origin/host checks and local-only assets.

Unlock precedes capture. Lock and quit must stop writers, invalidate the session and prevent
concurrent lifecycle actions from restarting capture. Child processes and model servers must
not outlive their owner or retain untracked access to keys and content.

Only explicit public-file downloads may use external connections. They carry no captured or
identifying data, use pinned artifacts and verify integrity before publication. Libraries must
remain offline during inference; startup must not download missing models. Development tools
and tests may use the network without sending captured, derived or identifying user data.

## Content handling

Captured text, audio, model inputs and outputs, and identifying derivatives stay local and
encrypted. Exclusions apply before persistence. Content-free logs may remain plaintext, so
exception messages or third-party logging that quote inputs must not reach them.

Exclusion decisions fail closed for an unreadable window, process, audio source, focus state or
Chromium private caption, instead of storing content as "unclear". Writer-spy tests watch every
SQL write, encrypted file and log record for forbidden content. [CAPTURE.md](CAPTURE.md) has the
rules.

The accepted exceptions, the only ones:

- A browser whose address cannot be read is kept when the user has no address rules and its
  title names no built-in host. Its title cannot prove anything either way.
- Some background private-browser media cannot be identified as private: a playing tab in a
  browser window the screen loop has not examined has no verdict. An excluding verdict lapses
  60 s after the window was last seen, including when a game takes the foreground (gaming mode
  publishes no verdicts). This does not waive URL, finance or other exclusions.
- Firefox and Zen layouts and Brave's caption are unverified on this PC.

Captured instructions are untrusted data. The answering model has no action tools; output must
be rendered safely and source references validated. These boundaries do not establish immunity
to misleading content or prove that generated claims follow from their citations.

[STORAGE.md](STORAGE.md) owns retention and derived-copy deletion. Preserve those guarantees
when adding a new persistence path, index, cache or model comparison. Deleting encrypted files
does not promise secure erasure of disk blocks.
