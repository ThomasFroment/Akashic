"""C6 variant (b): dual model. Turns under 4 s of speech scored with plain ResNet34 (S) against
profiles with n_S >= 3, longer ones with ResNet34-LM; both thresholds 0.5; every update embeds
with both. See C6_lib.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from C6_lib import make_dual  # noqa: E402

NAME = "C6_b_dual"
make_store, enroll, attribute, centroid = make_dual("resnet34", 0.5, 0.5, 4_000)
