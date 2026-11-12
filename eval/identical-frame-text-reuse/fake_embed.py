"""Deterministic fake embedder for the identical-frame-text-reuse harness.

text -> 256-dim unit vector (float32 in memory; the store keeps float16, like today):
the sum of seeded per-token Gaussian vectors (bag of words, ``\\w+`` lowercased), normalised.
Identical texts give the same clean vector; texts sharing most tokens are close, so the
semantic ranking behaves like a (crude) real one.

``embed_documents`` adds a small, call-order-dependent jitter (default sigma 1e-3 per dim, so
cos(clean, jittered) ~ 0.9999) to mimic llama.cpp batches not being bit-reproducible: a
candidate must not rely on bit-identical vectors for identical text. ``embed_query`` is clean.

Counters: ``calls`` (embed_documents calls), ``texts`` (texts sent), ``chars`` (characters
sent: the GPU-time proxy), ``query_calls``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

import numpy as np

DIM: Final = 256
CHARS: Final = 400  # passages.CHARS
STEP: Final = 300  # passages.STEP
_TOKEN = re.compile(r"\w+", re.UNICODE)
_TOKENS: dict[tuple[int, str], np.ndarray] = {}
_CLEAN: dict[tuple[int, str], np.ndarray] = {}
_CLEAN_CAP: Final = 400_000


def windows(text: str) -> list[tuple[int, int]]:
    """Copied from src/akashic/storage/passages.py windows()."""
    if not text:
        return []
    return [
        (start, min(start + CHARS, len(text)))
        for start in range(0, max(len(text) - CHARS, 0) + STEP, STEP)
    ]


def _token_vec(seed: int, token: str) -> np.ndarray:
    key = (seed, token)
    vec = _TOKENS.get(key)
    if vec is None:
        digest = hashlib.blake2b(token.encode(), digest_size=8, key=seed.to_bytes(8, "little"))
        rng = np.random.default_rng(int.from_bytes(digest.digest(), "little"))
        vec = rng.standard_normal(DIM, dtype=np.float32)
        _TOKENS[key] = vec
    return vec


def clean_vector(text: str, seed: int = 0) -> np.ndarray:
    """The jitter-free unit vector of ``text`` (float32)."""
    key = (seed, text)
    cached = _CLEAN.get(key)
    if cached is not None:
        return cached.astype(np.float32)
    tokens = _TOKEN.findall(text.lower()) or [text]
    acc = np.sum([_token_vec(seed, t) for t in tokens], axis=0, dtype=np.float32)
    norm = float(np.linalg.norm(acc))
    vec = acc / norm if norm > 0 else _token_vec(seed, "\x00empty")
    # Rounded through float16 always, so a cached and a fresh result are bit-identical.
    vec16 = (vec / np.linalg.norm(vec)).astype(np.float16)
    if len(_CLEAN) < _CLEAN_CAP:
        _CLEAN[key] = vec16
    return vec16.astype(np.float32)


class FakeEmbedder:
    """Implements the pipeline/embed.py ``Embedder`` protocol."""

    def __init__(self, model: str = "fake-qwen3-256", seed: int = 0, jitter: float = 1e-3) -> None:
        self.model = model
        self.seed = seed
        self.jitter = jitter
        self.calls = 0
        self.texts = 0
        self.chars = 0
        self.query_calls = 0
        self.sent: list[str] | None = None  # set to [] to log every text sent

    def available(self) -> bool:
        return True

    def unload_if_idle(self, *, force: bool = False) -> None:
        return None

    def embed_documents(self, texts: list[str]) -> list[np.ndarray]:
        self.calls += 1
        self.texts += len(texts)
        self.chars += sum(len(t) for t in texts)
        if self.sent is not None:
            self.sent.extend(texts)
        if not texts:
            return []
        clean = np.vstack([clean_vector(t, self.seed) for t in texts])
        if self.jitter:
            rng = np.random.default_rng((self.seed, self.calls))
            clean = clean + rng.normal(0.0, self.jitter, clean.shape).astype(np.float32)
            clean /= np.linalg.norm(clean, axis=1, keepdims=True)
        return [row.astype(np.float32) for row in clean]

    def embed_query(self, text: str) -> np.ndarray:
        self.query_calls += 1
        return clean_vector(text, self.seed)
