"""Ogg/Opus encoding and decoding of mono 16 kHz audio, through PyAV.

PyAV's type stubs are too loose for strict checking (unknown overloads, packet generics), so its
objects are handled as ``Any`` here and nowhere else. Opus decodes at 48 kHz whatever it was
encoded at; decoding resamples back to the 16 kHz Whisper and the voice detector use.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any, Final

import av
import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

SAMPLE_RATE: Final = 16_000
BITRATE: Final = 24_000  # plenty for speech; about 10 MB per hour of pure speech


def encode_opus(samples: NDArray[np.float32]) -> bytes:
    """Mono 16 kHz float32 in [-1, 1] to Ogg/Opus bytes."""
    if len(samples) == 0:
        msg = "no audio to encode"
        raise ValueError(msg)
    codec: Any = av
    buffer = io.BytesIO()
    with codec.open(buffer, "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=SAMPLE_RATE)
        stream.bit_rate = BITRATE
        stream.layout = "mono"
        frame = codec.AudioFrame.from_ndarray(
            np.ascontiguousarray(samples, dtype=np.float32)[None, :], format="flt", layout="mono"
        )
        frame.sample_rate = SAMPLE_RATE
        container.mux(stream.encode(frame))
        container.mux(stream.encode(None))
    return buffer.getvalue()


def decode_opus(data: bytes) -> NDArray[np.float32]:
    """Ogg/Opus bytes to mono 16 kHz float32."""
    codec: Any = av
    resampler = codec.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    parts: list[NDArray[np.float32]] = []
    with codec.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            parts.extend(
                np.asarray(out.to_ndarray(), dtype=np.float32).reshape(-1)
                for out in resampler.resample(frame)
            )
    parts.extend(
        np.asarray(out.to_ndarray(), dtype=np.float32).reshape(-1)
        for out in resampler.resample(None)
    )
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)
