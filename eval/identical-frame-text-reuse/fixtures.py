"""Seeded frame fixtures for the identical-frame-text-reuse harness.

All timestamps are UTC ms, one frame every 5 s, unique across fixtures (so a query window
[ts, ts+1) holds exactly one frame). Texts are 1-20 KB, mixed French/English pseudo-text.

- ``video``:  1 h of one playing video, 720 frames. Constant player/page UI text; a dynamic slot
              (player time + subtitle line) changes every 6-14 frames (~10), so ~72 distinct
              texts. Per-video title and URL.
- ``static``: 10 min on one unchanged page, 120 identical frames.
- ``mixed``:  2,000 frames of browsing/coding/chat, mostly distinct (scrolling slices of long
              pages; ~25 % of frames repeat the previous frame exactly).
- ``twovid``: two videos, 60 frames each, with byte-identical window text but different titles
              and URLs (tests grouping by (text, title, url)).

``SENTINELS`` are strings that occur in fixture text; they must never appear in the raw
(encrypted) database or WAL bytes.
"""

from __future__ import annotations

from typing import Final

import numpy as np

STEP_MS: Final = 5_000
DAY0_MS: Final = 1_794_211_200_000  # 2026-11-09T08:00:00Z
OFFSETS_MS: Final = {
    "video": 0,
    "static": 65 * 60_000,
    "mixed": 80 * 60_000,
    "twovid": 255 * 60_000,
}
NAMES: Final = ("video", "static", "mixed", "twovid")
SENTINELS: Final = ("AKSENTINELQXZW", "sentinelle-privée-ZQ")

_REAL = (
    "the and video game minlo said about this that with from discord call voice chat "
    "le la les une des avec pour dans sur pas est sont jeu partie vidéo écran réunion "
    "café déjà très après matin soir hier aujourd'hui demain projet fichier code python "
    "build release patch notes boss niveau quête équipe serveur stream subtitles player "
    "comment reply share subscribe abonnez-vous partager commentaire recommandé playlist "
    "settings window title search results page article chapter scene music trailer review"
).split()


def _vocab(rng: np.random.Generator, n: int = 5_000) -> list[str]:
    cons = list("bcdfghjklmnprstvz") + ["ch", "qu", "tr", "br", "gr", "pl"]
    vow = list("aeiou") + ["é", "è", "à", "ou", "ai", "eau"]
    words = set()
    while len(words) < n:
        k = int(rng.integers(1, 4))
        words.add("".join(str(rng.choice(cons)) + str(rng.choice(vow)) for _ in range(k)))
    return sorted(words) + _REAL * 4


class _Gen:
    def __init__(self, seed: int) -> None:
        self.rng = np.random.default_rng(seed)
        self.vocab = _vocab(self.rng)

    def words(self, n: int) -> str:
        picked = self.rng.integers(0, len(self.vocab), n)
        marks = self.rng.random(n)
        out = [
            self.vocab[int(w)] + (".\n" if r < 0.04 else ", " if r < 0.10 else " ")
            for w, r in zip(picked, marks, strict=True)
        ]
        return "".join(out).rstrip(" ,.\n")

    def block(self, chars: int) -> str:
        text = self.words(chars // 5 + 16)
        while len(text) < chars:
            text += " " + self.words(64)
        return text[:chars].rsplit(" ", 1)[0]

    def ident(self, k: int = 11) -> str:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        return "".join(alphabet[int(i)] for i in self.rng.integers(0, len(alphabet), k))


def _frame(ts: int, text: str, title: str, url: str | None, process: str) -> dict[str, object]:
    return {
        "ts_ms": ts,
        "monitor": 1,
        "process": process,
        "window_title": title,
        "url": url,
        "text": text,
        "text_source": "uia",
        "trigger": "interval",
        "image_path": f"fixture/{ts}.webp",
        "focused": 1,
        "image_scope": "window",
    }


def _video(g: _Gen, base: int, n: int) -> list[dict[str, object]]:
    title = g.words(6).replace("\n", " ").replace(".", "") + " - YouTube - Zen"
    url = f"https://www.youtube.com/watch?v={g.ident()}"
    header = "YouTube FR Rechercher Accueil Shorts Abonnements Bibliothèque\n" + g.block(400)
    body = (
        "\n" + g.block(int(g.rng.integers(3_000, 9_000)))
        + "\nCommentaires\n" + g.block(int(g.rng.integers(1_000, 4_000)))
    )
    frames: list[dict[str, object]] = []
    i = 0
    while i < n:
        run = int(g.rng.integers(6, 15))
        secs = i * STEP_MS // 1000
        slot = f"\n{secs // 60:02d}:{secs % 60:02d} / 58:10\n{g.words(int(g.rng.integers(6, 16)))}\n"
        text = header + slot + body
        frames.extend(_frame(base + j * STEP_MS, text, title, url, "zen.exe") for j in range(i, min(i + run, n)))
        i += run
    return frames


def _static(g: _Gen, base: int, n: int) -> list[dict[str, object]]:
    text = (
        "Documentation Python 3.14 — Référence\n" + g.block(4_000) + f" {SENTINELS[0]} "
        + g.block(4_000) + f" {SENTINELS[1]}\n" + g.block(1_500)
    )
    title = "sqlite3 — DB-API 2.0 interface — Python 3.14 documentation - Zen"
    url = "https://docs.python.org/3.14/library/sqlite3.html"
    return [_frame(base + i * STEP_MS, text, title, url, "zen.exe") for i in range(n)]


def _mixed(g: _Gen, base: int, n: int) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    apps = (("zen.exe", True), ("code.exe", False), ("discord.exe", False), ("explorer.exe", False))
    while len(frames) < n:
        process, has_url = apps[int(g.rng.choice(4, p=[0.55, 0.25, 0.15, 0.05]))]
        article = g.block(int(g.rng.integers(10_000, 40_000)))
        title = g.words(5).replace("\n", " ").replace(".", "") + f" - {process[:-4]}"
        url = f"https://example.{g.ident(5).lower()}.org/{g.ident(8)}" if has_url else None
        dwell = int(g.rng.integers(1, 9))
        text = ""
        for _ in range(dwell):
            if len(frames) >= n:
                break
            if not text or g.rng.random() >= 0.25:  # scroll: a new viewport slice
                size = int(min(20_000, max(1_000, g.rng.lognormal(np.log(6_000), 0.6))))
                start = int(g.rng.integers(0, max(1, len(article) - size)))
                text = article[start : start + size]
                if len(text) < 1_000:
                    text = (text + "\n" + g.block(1_000))[:1_000]
            frames.append(_frame(base + len(frames) * STEP_MS, text, title, url, process))
    return frames


def _twovid(g: _Gen, base: int, n_each: int) -> list[dict[str, object]]:
    ui = (
        "YouTube FR Rechercher Accueil Shorts Abonnements\nLecture automatique\n"
        + g.block(int(g.rng.integers(4_000, 8_000)))
    )
    frames: list[dict[str, object]] = []
    for k in range(2):
        title = g.words(5).replace("\n", " ").replace(".", "") + f" (part {k + 1}) - YouTube - Zen"
        url = f"https://www.youtube.com/watch?v={g.ident()}"
        frames.extend(
            _frame(base + (k * n_each + i) * STEP_MS, ui, title, url, "zen.exe")
            for i in range(n_each)
        )
    return frames


def build(seed: int = 0) -> dict[str, list[dict[str, object]]]:
    """The four fixtures, in time order (each list sorted by ts_ms)."""
    g = _Gen(seed)
    return {
        "video": _video(g, DAY0_MS + OFFSETS_MS["video"], 720),
        "static": _static(g, DAY0_MS + OFFSETS_MS["static"], 120),
        "mixed": _mixed(g, DAY0_MS + OFFSETS_MS["mixed"], 2_000),
        "twovid": _twovid(g, DAY0_MS + OFFSETS_MS["twovid"], 60),
    }


def small(seed: int = 0) -> list[dict[str, object]]:
    """A ~420-frame subset for deletion/rollback tests (same timestamps as ``build``)."""
    f = build(seed)
    return f["video"][:120] + f["static"][:30] + f["mixed"][:150] + f["twovid"]


def write_stream(n: int = 10_000, seed: int = 0) -> list[dict[str, object]]:
    """``n`` frames for write-cost timing: the fixtures concatenated and repeated with shifted
    timestamps (so later cycles repeat earlier texts, like a long day of capture)."""
    f = build(seed)
    one = f["video"] + f["static"] + f["mixed"] + f["twovid"]
    span = one[-1]["ts_ms"] - one[0]["ts_ms"] + STEP_MS  # type: ignore[operator]
    out: list[dict[str, object]] = []
    cycle = 0
    while len(out) < n:
        for fr in one:
            if len(out) >= n:
                break
            out.append({**fr, "ts_ms": int(fr["ts_ms"]) + cycle * (span + 3_600_000)})  # type: ignore[arg-type]
        cycle += 1
    return out


if __name__ == "__main__":
    for name, frames in build().items():
        texts = [str(fr["text"]) for fr in frames]
        sizes = [len(t) for t in texts]
        groups = {(fr["text"], fr["window_title"], fr["url"]) for fr in frames}
        print(
            f"{name}: frames={len(frames)} distinct_texts={len(set(texts))} groups={len(groups)}"
            f" chars min={min(sizes)} median={int(np.median(sizes))} max={max(sizes)}"
        )
