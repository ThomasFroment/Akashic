"""Audio player run under a renamed interpreter (Discord.exe, Spotify.exe, QualityVault.exe, ...).

    <Name>.exe player.py SCHEDULE.json --device NAME --log truth/player-X.jsonl [--canary X Y W H]

SCHEDULE.json: {"t0_wall_ms": ..., "end_ms": ..., "items": [{"id", "t_ms", "bank", "off", "n", "gain",
"stream", "speaker"}]}. Items are mixed (overlaps add) and played in 250 ms chunks on the waveOut
device whose name contains NAME. Each item's real start (now plus the samples still to play before it,
from the device position) is logged, so a sleep that pauses the device shifts the truth too.
Imports nothing from akashic.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

import assets
from common import CANARY_RGB, CANARY_TEXT, JsonlWriter, now_ms
from winapi import WavePlayer

CHUNK = assets.SR // 4
AHEAD = 3


def mix_chunk(items: list[dict], banks: dict[str, np.ndarray], index: int) -> np.ndarray:
    lo = index * CHUNK
    out = np.zeros(CHUNK, np.float32)
    for it in items:
        a = it["start"]
        b = a + it["n"]
        if b <= lo or a >= lo + CHUNK:
            continue
        s, e = max(a, lo), min(b, lo + CHUNK)
        src = banks[it["bank"]][it["off"] + (s - a):it["off"] + (e - a)]
        out[s - lo:e - lo] += src * it.get("gain", 1.0)
    return (np.clip(out, -1, 1) * 32767).astype(np.int16)


def play(schedule: dict, device: str, log: JsonlWriter) -> None:
    items = sorted(({**it, "start": round(it["t_ms"] * assets.SR / 1000)} for it in schedule["items"]), key=lambda i: i["start"])
    banks = {name: assets.load_bank(name) for name in {it["bank"] for it in items}}
    wait = (schedule["t0_wall_ms"] - now_ms()) / 1000
    if wait > 0:
        time.sleep(wait)
    player = WavePlayer(device, assets.SR)
    logged: set[str] = set()
    total = (schedule["end_ms"] * assets.SR // 1000) // CHUNK + 1
    for index in range(total):
        active = [it for it in items if it["start"] < (index + 1) * CHUNK and it["start"] + it["n"] > index * CHUNK]
        player.start(mix_chunk(active, banks, index).tobytes())
        played = player.played_samples()
        now = now_ms()
        for it in active:
            if it["id"] not in logged:
                logged.add(it["id"])
                log.write(type="start", id=it["id"], stream=it.get("stream"), speaker=it.get("speaker"), planned_ms=it["t_ms"],
                          start_wall_ms=now + (it["start"] - played) * 1000 // assets.SR, n=it["n"], rate=assets.SR)
        while len(player._live) >= AHEAD:
            player.wait_oldest()
    player.wait()
    player.close()


def canary_window(x: int, y: int, w: int, h: int, seconds: float) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title(f"{CANARY_TEXT} vault window")
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.configure(bg="white")
    tk.Label(root, text=CANARY_TEXT, font=("Segoe UI", 28), bg="white").pack(pady=10)
    tk.Frame(root, bg="#%02x%02x%02x" % CANARY_RGB, width=300, height=300).pack()
    root.after(int(seconds * 1000), root.destroy)
    root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("schedule", type=Path)
    ap.add_argument("--device", default="")
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--canary", nargs=4, type=int)
    args = ap.parse_args()
    schedule = json.loads(args.schedule.read_text(encoding="utf-8"))
    log = JsonlWriter(args.log)
    worker = threading.Thread(target=play, args=(schedule, args.device, log), daemon=True)
    worker.start()
    if args.canary:
        time.sleep(max(0, (schedule["t0_wall_ms"] - now_ms()) / 1000))
        canary_window(*args.canary, seconds=schedule["end_ms"] / 1000)
    worker.join()
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
