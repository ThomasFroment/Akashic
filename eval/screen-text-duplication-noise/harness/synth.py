"""Synthetic window-text sequences with exact truth for near-duplicate and chrome decisions.

    uv run python eval/screen-text-duplication-noise/harness/synth.py   -> synthetic/windows.jsonl

Every rendered line carries its truth record [iid, kind, clean] with kind:
  content   a content line (static once shown; scrolled documents, chat messages, code, table rows)
  vcontent  volatile content: each state of a changing value or an edited line is its own instance
            (a price that moved, a line being typed); all states were seen by the user
  chrome    UI chrome: menus, toolbars, tabs, sidebars, member lists (same text across frames/windows)
  vchrome   volatile chrome: clocks, counters, HUD values, player time, cursor position
  garbage   OCR fragments of icons and textures (no clean text)
OCR noise is applied per frame and per line independently, so the same line is stored with varying
characters. Rates (assumed, from the standard run's harness lines: 1.1 % char errors and 79 % exact
lines live, 0.6 % / 89 % deferred, spaces kept 99.8 %; the deferred and HUD profiles are harsher on
purpose): see NOISE. Sentences come from the harness corpus (eval/quality/assets/sentences.json).
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import shared as S  # noqa: E402

OUT = HERE / "synthetic" / "windows.jsonl"
CONFUSE = {"l": "I", "I": "l", "1": "l", "O": "0", "0": "O", "e": "c", "c": "e", "m": "rn", "rn": "m", "a": "o", "o": "a",
           "é": "e", "è": "e", "à": "a", "S": "5", "5": "S", "B": "8", "t": "f", "i": "í", "u": "v", "n": "m"}
GARBAGE = ["@", "Q", "©", "ñ", "|", "II", "o", "®", "»", "«", "Il", "îî", "~", "#", "ô", "%", "0", "Ø", "=", "ii", "nn", "jI"]
NOISE = {  # per char error, space merge, line miss, garbage line per frame, garbage token in a chrome line
    "uia": (0.0, 0.0, 0.0, 0.0, 0.0),
    "ocr": (0.011, 0.002, 0.02, 0.6, 0.15),
    "ocr_deferred": (0.02, 0.004, 0.08, 1.0, 0.25),
    "hud": (0.05, 0.01, 0.10, 2.0, 0.4),
}
RESAMPLE = 0.3  # chance a moved line is read differently
SHORT_REPLIES = ["ok", "oui", "mdr", "gg", "+1", "merci", "yes", "lol", "ah ok", "non", "d'accord", "same", "😂", "go", "thx"]
AUTHORS = ["Minlo", "Kestrel", "Aube", "Tomas", "Lys"]


class Gen:
    def __init__(self, seed: int = 20261120) -> None:
        self.rng = random.Random(seed)
        self.sent = json.loads((S.QUALITY / "assets" / "sentences.json").read_text(encoding="utf-8"))
        self.fid = 1_000_000
        self.wid = 5_000_000
        self.iid = 0
        self.t0 = 1_795_200_000_000

    def new_iid(self, prefix: str) -> str:
        self.iid += 1
        return f"{prefix}{self.iid}"

    def sentence(self, lang: str) -> str:
        return self.rng.choice(self.sent[lang])

    def noisy(self, text: str, prof: str) -> str:
        p_char, p_merge, _, _, _ = NOISE[prof]
        if p_char == 0:
            return text
        out = []
        i = 0
        while i < len(text):
            c = text[i]
            r = self.rng.random()
            if c == " " and r < p_merge:
                i += 1
                continue
            if r < p_char:
                k = self.rng.random()
                if k < 0.7:
                    out.append(CONFUSE.get(c, self.rng.choice("aeilnorstu.,'")))
                elif k < 0.85:
                    pass  # deletion
                else:
                    out.append(c + self.rng.choice("'.,il"))
            else:
                out.append(c)
            i += 1
        return "".join(out)

    def render(self, shown: list[tuple[str, str, str]], prof: str, state: dict) -> tuple[str, list]:
        """shown: [(iid, kind, clean)] in reading order -> (text, aligned truth records).

        OCR is deterministic for unchanged pixels: a line keeps its noisy rendering (and a missed line
        stays missed) while it stays at the same position; when it moves, it is re-read with
        probability RESAMPLE. Garbage lines stay until the layout changes."""
        _, _, p_miss, g_lines, g_tok = NOISE[prof]
        lines, recs = [], []
        layout = tuple(x[0] for x in shown)
        moved_any = layout != state.get("layout")
        for pos, (iid, kind, clean) in enumerate(shown):
            prev = state["lines"].get(iid)
            if prev is not None and (prev[0] == pos or self.rng.random() >= RESAMPLE) and prev[2] == clean:
                txt = prev[1]
            else:
                txt = None if self.rng.random() < p_miss else self.noisy(clean, prof)
                if txt is not None and kind in ("chrome", "vchrome") and self.rng.random() < g_tok:
                    txt = f"{self.rng.choice(GARBAGE)} {txt}"
            state["lines"][iid] = (pos, txt, clean)
            if txt is not None and txt.strip():
                lines.append(txt)
                recs.append([iid, kind, clean])
        if moved_any or "garbage" not in state:
            if "garbage" not in state or self.rng.random() < 0.5:
                n_g = int(g_lines) + (self.rng.random() < g_lines - int(g_lines))
                state["garbage"] = [(self.rng.random(), " ".join(self.rng.choice(GARBAGE) for _ in range(self.rng.randint(1, 3)))) for _ in range(n_g)]
        state["layout"] = layout
        for frac, g in state["garbage"]:
            pos = int(frac * (len(lines) + 1))
            lines.insert(pos, g)
            recs.insert(pos, [None, "garbage", ""])
        return "\n".join(lines), recs

    def window(self, scenario: str, process: str, source: str, monitor: str, lang: str, frames: list[dict], prof: str | None = None) -> dict:
        self.wid += 1
        out = []
        state: dict = {"lines": {}}
        for f in frames:
            self.fid += 1
            text, recs = self.render(f["shown"], prof or source, state)
            out.append({"ts": f["ts"], "fid": self.fid, "wrow": self.fid * 10 + 1, "title": f.get("title"), "url": f.get("url"),
                        "text": text, "recs": recs})
        return {"key": f"syn-{scenario}-{self.wid}", "hwnd": self.wid, "scenario": scenario, "process": process, "app": S.app_type(process),
                "source": source, "monitor": monitor, "lang": lang, "frames": out}

    def times(self, n: int, start_min: float, period_ms: int = 2000) -> list[int]:
        t = self.t0 + int(start_min * 60_000)
        out = []
        for _ in range(n):
            t += int(period_ms * self.rng.uniform(0.5, 2.5))
            out.append(t)
        return out

    # --- scenarios ---

    def article(self, lang: str, source: str, start: float, browser_chrome: list, n_lines: int = 50, view: int = 16) -> dict:
        body = [(self.new_iid("c"), "content", self.sentence(lang)) for _ in range(n_lines)]
        title = f"{body[0][2][:40]} - Article"
        url = f"https://news.example/{self.rng.randint(1000, 9999)}"
        aside = [("aside0", "chrome", "Related"), *[(f"aside{i}", "chrome", self.sent["en"][i][:60]) for i in range(1, 5)]]
        frames, off = [], 0
        for ts in self.times(70, start):
            r = self.rng.random()
            off = max(0, min(n_lines - view, off + (0 if r < 0.4 else 1 if r < 0.75 else 3 if r < 0.92 else -4)))
            shown = [*browser_chrome, (f"tab-{url}", "chrome", title), ("url", "chrome", url), *body[off:off + view], *aside]
            frames.append({"ts": ts, "shown": shown, "title": title, "url": url})
        return self.window("article", "chrome.exe", source, "primary", lang, frames)

    def chat(self, source: str, start: float, chrome: list) -> dict:
        msgs: list[tuple[str, str, str]] = []
        frames = []
        unread = 0
        for k, ts in enumerate(self.times(90, start, 3000)):
            if self.rng.random() < 0.45:
                who = self.rng.choice(AUTHORS)
                hhmm = f"{(ts // 3_600_000) % 24:02d}:{(ts // 60_000) % 60:02d}"
                msgs.append((self.new_iid("c"), "content", f"{who}  {hhmm}"))
                body = self.rng.choice(SHORT_REPLIES) if self.rng.random() < 0.45 else self.sentence(self.rng.choice(["en", "fr", "mixed"]))
                msgs.append((self.new_iid("c"), "content", body))
                unread += 1
            if self.rng.random() < 0.2:
                unread = 0
            vis = msgs[-14:]
            badge = [(self.new_iid("v"), "vchrome", f"{unread} new messages")] if unread else []
            typing = [(self.new_iid("v"), "vchrome", f"{self.rng.choice(AUTHORS)} is typing...")] if self.rng.random() < 0.3 else []
            frames.append({"ts": ts, "shown": [*chrome, *badge, *vis, *typing, ("input", "chrome", "Message #general")],
                           "title": "#general | Server - Discord"})
        return self.window("chat", "discord.exe", source, "left", "mixed", frames)

    def code(self, start: float, menu: list) -> dict:
        src = []
        for i in range(70):
            r = self.rng.random()
            if r < 0.15:
                src.append("}")
            elif r < 0.25:
                src.append("    return None")
            elif r < 0.35:
                src.append("")
            else:
                name = self.rng.choice(["frame", "window", "rows", "lines", "text", "store", "index"])
                src.append(f"    let {name}_{i} = {self.rng.choice(['parse', 'load', 'read', 'merge'])}_{name}(&{name}, {i});")
        body = [(self.new_iid("c"), "content", ln) for ln in src if ln]
        frames, off = [], 0
        for ts in self.times(60, start):
            off = max(0, min(len(body) - 24, off + self.rng.choice([0, 0, 1, 2, -1])))
            ln, col = self.rng.randint(1, 70), self.rng.randint(1, 40)
            shown = [*menu, ("tabs", "chrome", "main.rs   lib.rs   Cargo.toml"), *[("tree" + x, "chrome", x) for x in ("src", "main.rs", "lib.rs", "Cargo.toml")],
                     *body[off:off + 24], (self.new_iid("v"), "vchrome", f"Ln {ln}, Col {col}   Spaces: 4   UTF-8   Rust")]
            frames.append({"ts": ts, "shown": shown, "title": "main.rs - quilvane - Visual Studio Code"})
        return self.window("code", "code.exe", "ocr", "right", "en", frames)

    def table(self, start: float) -> dict:
        rows = []
        for i in range(30):
            rows.append([self.new_iid("c"), f"Ticket #{1020 + i} {self.rng.choice(['open', 'closed', 'pending'])}", round(self.rng.uniform(5, 90), 2)])
        frames, off = [], 0
        header = ("hdr", "content", "Ticket   Status   Price")
        for ts in self.times(60, start):
            for r in rows:
                if self.rng.random() < 0.05:  # a price moves: the new state is a new instance
                    r[0], r[2] = self.new_iid("vc"), round(r[2] * self.rng.uniform(0.9, 1.1), 2)
            off = max(0, min(30 - 18, off + self.rng.choice([0, 0, 1, -1, 2])))
            shown = [("menu", "chrome", "Store   Library   Community"), header,
                     *[(r[0], "vcontent" if r[0].startswith("vc") else "content", f"{r[1]}   {r[2]:.2f} €") for r in rows[off:off + 18]]]
            frames.append({"ts": ts, "shown": shown, "title": "Tickets"})
        return self.window("table", "steamwebhelper.exe", "ocr", "primary", "en", frames)

    def hud(self, start: float) -> dict:
        frames = []
        hp, ammo, objective, feed = 100, 120, (self.new_iid("c"), "content", "Objective: reach the quiet harbour"), []
        for k, ts in enumerate(self.times(80, start, 5000)):
            hp = max(1, min(100, hp + self.rng.randint(-12, 8)))
            ammo = max(0, ammo - self.rng.randint(0, 6))
            if k % 20 == 19:
                objective = (self.new_iid("c"), "content", f"Objectif : {self.sentence('fr')[:50]}")
            if self.rng.random() < 0.25:
                feed.append((self.new_iid("c"), "content", f"{self.rng.choice(AUTHORS)} eliminated {self.rng.choice(AUTHORS)}"))
            secs = (ts - self.t0) // 1000
            shown = [(self.new_iid("v"), "vchrome", f"HP {hp}/100"), (self.new_iid("v"), "vchrome", f"Ammo {ammo}/120"),
                     (self.new_iid("v"), "vchrome", f"{secs // 60:02d}:{secs % 60:02d}"), (self.new_iid("v"), "vchrome", f"FPS {self.rng.randint(110, 165)}"),
                     objective, *feed[-4:]]
            frames.append({"ts": ts, "shown": shown, "title": "Outer Wilds"})
        return self.window("hud", "outerwilds.exe", "ocr_deferred", "primary", "mixed", frames, prof="hud")

    def terminal(self, start: float) -> dict:
        lines, frames, n = [], [], 0
        pool = [self.sentence(self.rng.choice(["en", "fr", "mixed"])) for _ in range(25)]  # repeats on purpose
        for ts in self.times(80, start, 4000):
            n += 1
            lines.append((self.new_iid("c"), "content", f"[{n:04d}] {self.rng.choice(pool)}"))
            frames.append({"ts": ts, "shown": [("ttab", "chrome", "quality termfeed"), *lines[-30:], ("prompt", "chrome", "PS C:\\Users\\me>")],
                           "title": "quality termfeed"})
        return self.window("terminal", "windowsterminal.exe", "ocr", "left", "mixed", frames)

    def files(self, start: float, nav: list, folder: str) -> dict:
        names = [f"{self.rng.choice(['rapport', 'report', 'notes', 'facture', 'photo', 'budget'])}_{2020 + i % 7}_{i:02d}.{self.rng.choice(['pdf', 'docx', 'png', 'txt'])}" for i in range(25)]
        rows = [(self.new_iid("c"), "content", f"{n}   {self.rng.randint(1, 28):02d}/11/2026   {self.rng.randint(1, 900)} KB") for n in names]
        frames, off = [], 0
        for ts in self.times(40, start):
            off = max(0, min(25 - 15, off + self.rng.choice([0, 1, -1])))
            shown = [*nav, ("addr", "chrome", f"Ce PC > Documents > {folder}"), *rows[off:off + 15], (self.new_iid("v"), "vchrome", f"{len(rows)} éléments")]
            frames.append({"ts": ts, "shown": shown, "title": folder})
        return self.window("files", "explorer.exe", "uia", "right", "fr", frames)

    def video(self, start: float, browser_chrome: list) -> dict:
        title = f"Mornhollow S01E0{self.rng.randint(1, 9)} - {self.sentence('en')[:30]}"
        desc = [(self.new_iid("c"), "content", self.sentence("en")) for _ in range(3)]
        comments = [(self.new_iid("c"), "content", f"{self.rng.choice(AUTHORS)}: {self.sentence('fr')}") for _ in range(4)]
        total = 645
        frames = []
        for ts in self.times(70, start, 3000):
            pos = (ts - self.t0 - int(start * 60_000)) // 1000
            shown = [*browser_chrome, ("vt", "content", title), (self.new_iid("v"), "vchrome", f"{pos // 60}:{pos % 60:02d} / {total // 60}:{total % 60:02d}"),
                     *desc, *comments, ("subs", "chrome", "Subscribe   Share   Save")]
            frames.append({"ts": ts, "shown": shown, "title": f"{title} - YouTube", "url": "https://video.example/watch?v=abc"})
        return self.window("video", "msedge.exe", "ocr", "left", "en", frames)

    def editing(self, start: float) -> dict:
        """UIA document being typed: every intermediate state of the edited line is vcontent."""
        done = [(self.new_iid("c"), "content", self.sentence("fr")) for _ in range(6)]
        frames = []
        target = self.sentence("mixed")
        typed = 0
        for ts in self.times(50, start, 1500):
            typed = min(len(target), typed + self.rng.randint(3, 12))
            cur = (self.new_iid("vc"), "vcontent", target[:typed])
            frames.append({"ts": ts, "shown": [("np", "chrome", "Fichier   Modifier   Affichage"), *done, cur, (self.new_iid("v"), "vchrome", f"Ln 7, Col {typed}")],
                           "title": "notes.txt - Bloc-notes"})
            if typed == len(target):
                done.append((cur[0], "content", target))
                target, typed = self.sentence("en"), 0
        return self.window("editing", "notepad.exe", "uia", "right", "mixed", frames)


def main() -> None:
    S.init_corpus()
    g = Gen()
    bchrome = [("bm", "chrome", "Bookmarks   Gmail   News   Maps"), ("nav", "chrome", "Back   Forward   Reload")]
    dchrome = [("srv", "chrome", "Akashic Friends"), *[(f"ch{i}", "chrome", c) for i, c in enumerate(["# general", "# jeux", "# musique", "🔊 Salon vocal"])],
               *[(f"mem{i}", "chrome", a) for i, a in enumerate(AUTHORS)]]
    menu = [("menu", "chrome", "File   Edit   Selection   View   Go   Run   Terminal   Help")]
    nav = [(f"nav{i}", "chrome", x) for i, x in enumerate(["Accueil", "Galerie", "Bureau", "Téléchargements", "Documents", "Images", "Ce PC", "Réseau"])]
    wins = [
        g.article("en", "ocr", 0, bchrome), g.article("fr", "ocr", 5, bchrome), g.article("mixed", "ocr_deferred", 10, bchrome),
        g.article("fr", "uia", 15, bchrome), g.chat("ocr", 3, dchrome), g.chat("uia", 20, dchrome), g.code(8, menu), g.code(30, menu),
        g.table(12), g.hud(40), g.terminal(25), g.files(18, nav, "Projets"), g.files(33, nav, "Factures"), g.video(22, bchrome),
        g.editing(28),
    ]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for w in wins:
            f.write(json.dumps(w, ensure_ascii=False) + "\n")
    n_frames = sum(len(w["frames"]) for w in wins)
    kinds: dict[str, int] = {}
    for w in wins:
        for fr in w["frames"]:
            for r in fr["recs"]:
                kinds[r[1]] = kinds.get(r[1], 0) + 1
    print(f"windows={len(wins)} frames={n_frames} line_records={kinds} -> {OUT}")


if __name__ == "__main__":
    main()
