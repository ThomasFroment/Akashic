"""A pinned Whisper model with tiny files, so availability needs no gigabytes."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from akashic.pipeline import whisper
from akashic.pipeline.models import WHISPER_LARGE_V3, ModelFile

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from akashic.pipeline.models import ModelSpec

SMALL_FILES = (ModelFile("config.json", "0" * 64, 3), ModelFile("model.bin", "0" * 64, 5))


def small_whisper_spec(monkeypatch: pytest.MonkeyPatch) -> ModelSpec:
    """The real Whisper key with two tiny files, as the only speech model ``whisper`` knows."""
    spec = dataclasses.replace(WHISPER_LARGE_V3, files=SMALL_FILES)
    monkeypatch.setattr(whisper, "SPEECH_CANDIDATES", {spec.key: spec})
    return spec


def stage_whisper_model(monkeypatch: pytest.MonkeyPatch, models_dir: Path) -> Path:
    """Lay the tiny pinned files out under ``models_dir/<key>`` and return that folder."""
    spec = small_whisper_spec(monkeypatch)
    folder = models_dir / spec.key
    folder.mkdir()
    for file in spec.files:
        (folder / file.name).write_bytes(b"x" * file.size)
    return folder
