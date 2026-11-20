"""Numbers-only profile of c3.read on a large synthetic window (4 corpus pages tiled, ~2800x1440)."""
import cProfile, json, pstats, sys
from pathlib import Path
from PIL import Image
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness")); sys.path.insert(0, str(HERE))
import c3
from shared import CORPUS, Ctx, engines
fr, en = engines()
ims = [Image.open(CORPUS / "chromium" / "s100" / f"mixed-{t}-{px}.png").convert("RGB") for t, px in (("dark", 9), ("dark", 10), ("dark", 16), ("dark", 11))]
big = Image.new("RGB", (2800, 1440))
for i, im in enumerate(ims):
    big.paste(im, ((i % 2) * 1400, (i // 2) * 720))
ctx = Ctx(sys.argv[1] if len(sys.argv) > 1 else "replay", 1.0, fr, en)
c3.read(big, ctx)
pr = cProfile.Profile(); pr.enable()
for _ in range(3):
    c3.read(big, ctx)
pr.disable()
print({k: round(v, 1) if isinstance(v, float) else v for k, v in c3.STATS[-1].items()})
pstats.Stats(pr).sort_stats("tottime").print_stats(22)
