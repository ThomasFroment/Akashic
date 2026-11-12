"""C6 variant (a'): as C6_a_resnet34 with the threshold raised to 0.53, about the plain
model's offset over ResNet34-LM in EER threshold on 1-4 s pieces (step 1). See C6_lib.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from C6_lib import make_swap  # noqa: E402

NAME = "C6_a_resnet34_t53"
make_store, enroll, attribute, centroid = make_swap("resnet34", 0.53)
