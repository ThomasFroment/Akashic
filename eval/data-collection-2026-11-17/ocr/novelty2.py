"""Line novelty as recommended (NFKC, casefold, whitespace collapse, exact multiset), scored line
by line: each OCR line is mapped to its source chat line, so misses and false 'new' are counted."""
import json, sys, os, re, unicodedata, difflib, random, ctypes
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_spike import ocr, FR, EN, OcrEngine, Language
from PIL import Image, ImageDraw, ImageFont
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
random.seed(7)
names = ["Alex", "Kev", "Sarah", "Tom", "Élodie"]
log = [f"{random.choice(names)}  {random.randint(10,23)}:{random.randint(10,59)}  {random.choice(FR + EN)}" for _ in range(160)]
font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 13); lh = 22; top0 = 10
page = Image.new("RGB", (1100, lh * len(log) + 40), (49, 51, 56)); d = ImageDraw.Draw(page)
for i, l in enumerate(log): d.text((16, top0 + i * lh), l, font=font, fill=(219, 222, 225))
eng = OcrEngine.try_create_from_language(Language("fr-FR"))
norm = lambda s: " ".join(unicodedata.normalize("NFKC", s).casefold().split())
def view(top, h=900):
    v = page.crop((0, top, 1100, top + h)); return [x for x in ocr(eng, v.resize((v.width*2, v.height*2), Image.LANCZOS)) if x.strip()]
def source(line):  # index of the chat line this OCR line came from
    return max(range(len(log)), key=lambda i: difflib.SequenceMatcher(None, norm(line), norm(log[i])).ratio())
tot = dict(lines=0, flagged=0, truly_new=0, missed=0, false_new=0)
for t in range(20):
    top = random.randint(0, len(log) * lh - 1600); shift = random.randint(1, 12) * lh + random.randint(0, lh - 1)
    a, b = view(top), view(top + shift)
    src_a = {source(x) for x in a}
    prev = Counter(norm(x) for x in a); cur = Counter()
    for line in b:
        k = norm(line); cur[k] += 1
        new = cur[k] > prev[k]
        truly = source(line) not in src_a
        tot["lines"] += 1; tot["flagged"] += new; tot["truly_new"] += truly
        tot["missed"] += truly and not new; tot["false_new"] += new and not truly
tot["share_reindexed"] = round(tot["flagged"] / tot["lines"], 3)
print(json.dumps(tot))
