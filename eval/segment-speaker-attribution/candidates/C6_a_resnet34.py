"""C6 variant (a): reference pipeline, embedder swapped to plain WeSpeaker ResNet34 (non-LM),
best_match threshold unchanged at 0.5. See C6_lib.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from C6_lib import make_swap  # noqa: E402

NAME = "C6_a_resnet34"
make_store, enroll, attribute, centroid = make_swap("resnet34", 0.5)
