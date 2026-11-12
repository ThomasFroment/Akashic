"""Silero voice-activity detection on CPU, fed one 32 ms window at a time.

The model file ships inside the faster-whisper package (``silero_vad_v6.onnx``, MIT), so there is
nothing to download. It runs on onnxruntime's CPU provider only, which loads no CUDA library
(native check retained in docs/CAPTURE.md): the capture process stays CPU-only. One window costs
about 0.1 ms. The capture process must never import ``faster_whisper`` itself (that pulls in
CTranslate2), so the file is located without importing the package.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

WINDOW: Final = 512  # samples per VAD step at 16 kHz: 32 ms
CONTEXT: Final = 64  # samples of the previous window prepended to each step
_MODEL_NAME: Final = "silero_vad_v6.onnx"
_STATE_SHAPE: Final = (1, 1, 128)


class SpeechDetector(Protocol):
    def reset(self) -> None:
        """Forget the recurrent state (call when the stream restarts)."""
        ...

    def probability(self, window: NDArray[np.float32]) -> float:
        """Probability in [0, 1] that the next ``WINDOW`` samples contain speech."""
        ...


class _Session(Protocol):
    def run(
        self, output_names: None, input_feed: dict[str, NDArray[np.float32]]
    ) -> list[NDArray[np.float32]]: ...


def silero_model_path() -> Path:
    spec = importlib.util.find_spec("faster_whisper")
    if spec is None or spec.origin is None:
        msg = "faster-whisper is not installed, so the Silero VAD model file is missing"
        raise FileNotFoundError(msg)
    path = Path(spec.origin).parent / "assets" / _MODEL_NAME
    if not path.is_file():
        msg = (
            f"Silero VAD model not found at the expected place inside faster-whisper: {_MODEL_NAME}"
        )
        raise FileNotFoundError(msg)
    return path


class SileroVad:
    def __init__(self, model_path: Path | None = None) -> None:
        # onnxruntime ships no type stubs; imported here because it is heavy.
        ort: Any = importlib.import_module("onnxruntime")

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        options.enable_cpu_mem_arena = False
        options.log_severity_level = 4
        self._session = cast(
            "_Session",
            ort.InferenceSession(
                str(model_path or silero_model_path()),
                providers=["CPUExecutionProvider"],  # never CUDA: this runs in the capture process
                sess_options=options,
            ),
        )
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def reset(self) -> None:
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def probability(self, window: NDArray[np.float32]) -> float:
        if window.shape != (WINDOW,):
            msg = f"VAD window must be {WINDOW} samples"
            raise ValueError(msg)
        batch = np.concatenate([self._context, window])[None, :]
        probs, self._h, self._c = self._session.run(
            None, {"input": batch, "h": self._h, "c": self._c}
        )
        self._context = window[-CONTEXT:].copy()
        return float(probs[0])
