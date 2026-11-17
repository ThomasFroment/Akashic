"""Line novelty after a scroll: OCR two overlapping views of one chat log (Windows OCR fr-FR, 2x),
mark lines of the second view as new when no line of the first view matches. Numbers only."""
import json, sys, os, re, unicodedata, difflib, random, ctypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_spike import ocr, FR, EN, OcrEngine, Language
from PIL import Image, ImageDraw, ImageFont
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
random.seed(7)
names = ["Alex", "Kev", "Sarah", "Tom", "Élodie"]
log = [f"{random.choice(names)}  {random.randint(10,23)}:{random.randint(10,59)}  {random.choice(FR + EN)}" for _ in range(160)]
font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 13)
lh = 22
page = Image.new("RGB", (1100, lh * len(log) + 40), (49, 51, 56)); d = ImageDraw.Draw(page)
for i, l in enumerate(log): d.text((16, 10 + i * lh), l, font=font, fill=(219, 222, 225))
eng = OcrEngine.try_create_from_language(Language("fr-FR"))
def norm(s):
    s = unicodedata.normalize("NFKD", s.casefold()); s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", s))
def view(top, h=900, scale=2):
    v = page.crop((0, top, 1100, top + h)); return [norm(x) for x in ocr(eng, v.resize((v.width*scale, v.height*scale), Image.LANCZOS)) if norm(x)]
res = {"exact": [], "fuzzy90": []}
trials = []
for t in range(20):
    top = random.randint(0, len(log) * lh - 1600); shift = random.randint(1, 12) * lh + random.randint(0, lh - 1)
    a, b = view(top), view(top + shift)
    # ground truth: lines of view b fully inside view a's pixel range are old
    for mode, thr in (("exact", 1.0), ("fuzzy90", 0.9)):
        seen = set(a)
        new = [l for l in b if not (l in seen if thr == 1.0 else any(difflib.SequenceMatcher(None, l, x).ratio() >= thr for x in a))]
        trials.append((mode, len(b), len(new), shift // lh))
for mode in ("exact", "fuzzy90"):
    rows = [x for x in trials if x[0] == mode]
    flagged = sum(r[2] for r in rows); truly_new = sum(min(r[3] + 1, r[1]) for r in rows); total = sum(r[1] for r in rows)
    res[mode] = {"lines_in_second_views": total, "flagged_new": flagged, "true_new_upper_bound": truly_new,
                 "flagged_over_true": round(flagged / truly_new, 2), "share_of_text_reindexed": round(flagged / total, 3)}
print(json.dumps(res, indent=1))
