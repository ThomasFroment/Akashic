"""Embed a prompt text file as the SYSTEM literal of candidate.py (static prompt)."""
import re, sys
from pathlib import Path
src = Path("candidate.py").read_text("utf-8")
text = Path(sys.argv[1]).read_text("utf-8").strip()
assert '"""' not in text and "\\" not in text
new = 'SYSTEM = """' + text + '"""'
src2 = re.sub(r'SYSTEM = (?:"PLACEHOLDER"|"""[\s\S]*?""")', lambda m: new, src, count=1)
assert src2 != src or new in src
Path("candidate.py").write_text(src2, "utf-8", newline="\n")
print(len(text), "chars")
