"""Profile C2's preprocessing on one corpus page (numbers only)."""
import cProfile, pstats, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness")); sys.path.insert(0, str(HERE))
from PIL import Image
import c2
from shared import CORPUS, Ctx
page = sys.argv[1] if len(sys.argv) > 1 else "mixed-dark-9"
sc = sys.argv[2] if len(sys.argv) > 2 else "s100"
img = Image.open(CORPUS / "chromium" / sc / f"{page}.png").convert("RGB")
ctx = Ctx("live", 1.0, None, None, {})
im, info = c2.mosaic(img, ctx)
print({k: (round(v, 1) if isinstance(v, float) else v) for k, v in info.items()}, im.size)
pr = cProfile.Profile(); pr.enable()
for _ in range(20): c2.mosaic(img, ctx)
pr.disable()
pstats.Stats(pr).sort_stats("tottime").print_stats(12)
