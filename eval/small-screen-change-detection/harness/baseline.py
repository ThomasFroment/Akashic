"""Today's test (akashic.capture.imaging.differs): equal digest -> False, else > 1e-4 of the
reduced 1/4 gray pixels moved > 16 levels. The digest compare is array equality here (same
verdict; in production the digest is computed by the signature, not by the compare)."""

import numpy as np

PIXEL_DELTA = 16
CHANGED_FRACTION = 1e-4


def make_state():
    return None


def decide(state, ref_gray, cur_gray, prev_gray, monitor):  # noqa: ARG001
    if ref_gray.shape != cur_gray.shape:
        return True
    if np.array_equal(ref_gray, cur_gray):
        return False
    moved = np.abs(ref_gray.astype(np.int16) - cur_gray.astype(np.int16)) > PIXEL_DELTA
    return float(moved.mean()) > CHANGED_FRACTION
