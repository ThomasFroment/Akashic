"""Synthetic timelines for what the harness runs lack: long static screens, idle, lock, sleep, pause,
end of recording, video and controller play without input, a call while away.

Each scenario is a plain in-memory SQLite store built from the real schema (all migrations), filled
the way capture would fill it, with `app_sessions` derived by the real
`sessions.derive_app_sessions`. The simulated records (`sim_fg`, `sim_idle`) are filled from the
scenario's own truth, so on these timelines the input-idle record is real, not empty.

Capture model (simplified from capture/screen.py and capture/gaps.py):
- 3 monitors, monitor 1 focused; one 'shown' state run per monitor whose checked_ms is the last
  check (the run is kept in memory across lock and pause, so it is one row, as on the real runs).
- A frame is stored only when the picture changed: on a focus switch (+0.3 s debounce), during
  active use (every ACTIVE_EVERY_S on average), while a video plays (every 5 s check), in game
  mode every 60 s. A static screen stores nothing.
- Lock: a `screen/locked` gap (1 s gap tick). Sleep: an `all/stalled` gap from the last tick to
  the wake. Pause: `all/paused`. Stop: `all/stopped`, left open.
- Input: active use and reading have input; away, video and controller stretches have none.

Truth: a list of (t0, t1, label, process) with label 'present' (the user uses `process`: must be
counted for it), 'away' (at the PC's screen but not using it: must not be counted) or 'off' (not
recorded: lock, sleep, pause, after stop: must not be counted).
"""

from __future__ import annotations

import json
import random
import sqlite3

from core import IDLE_RECORD_MIN_MS, install_sim

from akashic.pipeline import sessions
from akashic.storage import db

T0 = 1_767_225_600_000  # 2026-01-01 00:00 UTC, arbitrary
S = 1000
EDITOR, BROWSER, GAME, DISCORD, LOCKAPP = "Code.exe", "zen.exe", "Game.exe", "Discord.exe", "LockApp.exe"
HWND = {EDITOR: 101, BROWSER: 102, GAME: 103, DISCORD: 104, LOCKAPP: 105}
ACTIVE_EVERY_S = 12


class Syn:
    def __init__(self, name: str, seed: int) -> None:
        self.name = name
        self.rng = random.Random(seed)
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        for m in db.load_migrations():
            self.conn.executescript(m.sql)
        c = self.conn
        lid = c.execute("INSERT INTO monitor_layouts (start_ms) VALUES (?) RETURNING id", (T0,)).fetchone()[0]
        for mid, x0 in ((1, 0), (2, 2560), (3, -2560)):
            c.execute("INSERT INTO monitors (id, key, name) VALUES (?, ?, ?)", (mid, f"m{mid}", f"m{mid}"))
            c.execute("INSERT INTO layout_monitors VALUES (?, ?, ?, 0, ?, 1440, 1.0, ?)", (lid, mid, x0, x0 + 2560, int(mid == 1)))
        for mid in (2, 3):  # side monitors: one picture each, never focused
            self._frame(T0, mid, None, "window", hwnd=200 + mid)
        self.truth: list[tuple[int, int, str, str | None]] = []
        self.fg: list[tuple[int, int | None, str | None, str]] = []
        self.no_input: list[tuple[int, int]] = []  # stretches without keyboard/mouse input
        self.end_ms = T0
        self.stop_ms: int | None = None

    # --- capture-side writers ---------------------------------------------------------------
    def _frame(self, ts: int, monitor: int, process: str | None, trigger: str, hwnd: int | None = None) -> None:
        fid = self.conn.execute("INSERT INTO frames (ts_ms, monitor_id, trigger, image_path) VALUES (?, ?, ?, 'x')"
                                " RETURNING id", (ts, monitor, trigger)).fetchone()[0]
        proc = process or "Side.exe"
        self.conn.execute("INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1, process, title, focused, painted)"
                          " VALUES (?, 'app', 0, ?, 0, 0, 100, 100, ?, 'syn', ?, 1)",
                          (fid, hwnd or HWND.get(proc, 199), proc, int(process is not None)))

    def gap(self, kind: str, reason: str, a: int, b: int | None) -> None:
        self.conn.execute("INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, ?, ?, ?)", (kind, reason, a, b))

    def switch(self, t: int, process: str) -> None:
        self.fg.append((t, HWND[process], process, "app"))
        self._frame(t + 300, 1, process, "focus")

    def active(self, a: int, b: int, process: str, switch: bool = True) -> None:
        """The user works in `process`: input all along, a changed picture every ~ACTIVE_EVERY_S."""
        if switch:
            self.switch(a, process)
        t = a + 300 + self.rng.randint(3, 2 * ACTIVE_EVERY_S) * S
        while t < b:
            self._frame(t, 1, process, "input")
            t += self.rng.randint(3, 2 * ACTIVE_EVERY_S) * S
        self.truth.append((a, b, "present", process))
        self.end_ms = max(self.end_ms, b)

    def finish(self, now_ms: int | None = None) -> tuple[sqlite3.Connection, int, int, int]:
        now = now_ms or self.end_ms
        last_check = self.stop_ms or now
        for mid in (1, 2, 3):
            self.conn.execute("INSERT INTO monitor_states (monitor_id, state, start_ms, checked_ms) VALUES (?, 'shown', ?, ?)",
                              (mid, T0, last_check))
        sessions.derive_app_sessions(self.conn, now)
        idle = [(a, b) for a, b in self.no_input if b - a >= IDLE_RECORD_MIN_MS]
        install_sim(self.conn, self.fg, idle)
        return self.conn, T0, self.end_ms, now


def at(s: float) -> int:
    return T0 + int(s * S)


# --- scenarios -------------------------------------------------------------------------------


def static_read() -> Syn:
    """60 min reading one page: no new frame, the monitor confirmed unchanged every 5 s, mouse input."""
    x = Syn("static_read", 1)
    x.active(at(0), at(300), EDITOR)
    x.switch(at(300), BROWSER)
    x.truth.append((at(300), at(3900), "present", BROWSER))
    t = 300
    while t < 3900:  # small mouse moves every 20-90 s: the cursor is not in the picture
        step = x.rng.randint(20, 90)
        x.no_input.append((at(t), at(min(t + step, 3900))))
        t += step
    x.active(at(3900), at(4200), EDITOR)
    return x


def lock() -> Syn:
    """Lock 10 min: Windows shows LockApp, capture writes screen/locked."""
    x = Syn("lock", 2)
    x.active(at(0), at(300), EDITOR)
    x.fg.append((at(300), HWND[LOCKAPP], LOCKAPP, "app"))
    x.gap("screen", "locked", at(300.6), at(900.4))
    x.truth.append((at(300), at(900), "off", None))
    x.no_input.append((at(300), at(900)))
    x.fg.append((at(900), HWND[EDITOR], EDITOR, "app"))
    x._frame(at(901), 1, EDITOR, "idle")
    x.active(at(900), at(1200), EDITOR, switch=False)
    return x


def sleep() -> Syn:
    """Sleep 20 min: the gap thread sees a > 30 s wall-clock jump and writes all/stalled."""
    x = Syn("sleep", 3)
    x.active(at(0), at(300), EDITOR)
    x.gap("all", "stalled", at(300.2), at(1500.5))
    x.truth.append((at(300), at(1500), "off", None))
    x.no_input.append((at(299), at(1501)))
    x._frame(at(1501), 1, EDITOR, "idle")
    x.active(at(1500), at(1800), EDITOR, switch=False)
    return x


def pause() -> Syn:
    """Pause 10 min while the user keeps working (and switches apps): nothing is recorded."""
    x = Syn("pause", 4)
    x.active(at(0), at(300), EDITOR)
    x.gap("all", "paused", at(300.5), at(900.5))
    x.truth.append((at(300), at(900), "off", None))
    x.fg.append((at(900.5), HWND[EDITOR], EDITOR, "app"))  # the capture restart repeats the foreground
    x._frame(at(901), 1, EDITOR, "idle")
    x.active(at(900), at(1200), EDITOR, switch=False)
    return x


def end_of_recording() -> Syn:
    """Stop at 10 min; the user goes on working for 10 min unrecorded."""
    x = Syn("end_of_recording", 5)
    x.active(at(0), at(600), EDITOR)
    x.gap("all", "stopped", at(600), None)
    x.stop_ms = at(600)
    x.truth.append((at(600), at(1200), "off", None))
    x.end_ms = at(1200)
    return x


def away_static() -> Syn:
    """20 min away from the keyboard, the screen unchanged, no input."""
    x = Syn("away_static", 6)
    x.active(at(0), at(300), BROWSER)
    x.truth.append((at(300), at(1500), "away", None))
    x.no_input.append((at(300), at(1500)))
    x._frame(at(1501), 1, BROWSER, "input")
    x.active(at(1500), at(1800), BROWSER, switch=False)
    return x


def video_noinput() -> Syn:
    """A 30 min video in the browser, no input; the picture changes at every 5 s check."""
    x = Syn("video_noinput", 7)
    x.active(at(0), at(120), BROWSER)
    x.truth.append((at(120), at(1920), "present", BROWSER))
    for t in range(125, 1920, 5):
        x._frame(at(t), 1, BROWSER, "idle")
    x.no_input.append((at(120), at(1920)))
    mid = x.conn.execute("INSERT INTO media_sessions (app_id, app, title, start_ms, end_ms, played_ms, closed)"
                         " VALUES ('syn', 'Zen', 'syn', ?, ?, ?, 1) RETURNING id", (at(120), at(1920), 1800 * S)).fetchone()[0]
    x.conn.executemany("INSERT INTO media_events (media_session_id, ts_ms, state) VALUES (?, ?, ?)",
                       [(mid, at(120), "playing"), (mid, at(1920), "paused")])
    for t in range(130, 1900, 30):
        x.conn.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status) VALUES ('system', ?, ?, 15000, ?, 'done')",
                       (at(t), at(t + 20), json.dumps([BROWSER.lower()])))
    x.active(at(1920), at(2100), BROWSER, switch=False)
    return x


def controller_game() -> Syn:
    """30 min of a game played with a controller: game mode, a frame every 60 s, no keyboard/mouse input."""
    x = Syn("controller_game", 8)
    x.active(at(0), at(120), EDITOR)
    x.switch(at(120), GAME)
    x.conn.execute("INSERT INTO game_sessions (process, window_title, start_ms, end_ms, closed) VALUES (?, 'syn', ?, ?, 1)",
                   (GAME, at(121), at(1918)))
    for t in range(180, 1920, 60):
        x._frame(at(t), 1, GAME, "game")
    x.truth.append((at(120), at(1920), "present", GAME))
    x.no_input.append((at(120), at(1920)))
    x.active(at(1920), at(2100), EDITOR)
    return x


def voice_call_away() -> Syn:
    """A 20 min Discord call on the headset, away from the PC: the browser stays in front, no input."""
    x = Syn("voice_call_away", 9)
    x.active(at(0), at(300), BROWSER)
    x.truth.append((at(300), at(1500), "away", None))
    x.no_input.append((at(300), at(1500)))
    for t in range(305, 1490, 25):
        stream, src = ("mic", []) if (t // 25) % 2 else ("system", [DISCORD.lower()])
        x.conn.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status) VALUES (?, ?, ?, 12000, ?, 'done')",
                       (stream, at(t), at(t + 15), json.dumps(src)))
    x._frame(at(1501), 1, BROWSER, "input")
    x.active(at(1500), at(1800), BROWSER, switch=False)
    return x


SCENARIOS = (static_read, lock, sleep, pause, end_of_recording, away_static, video_noinput, controller_game, voice_call_away)


def build_all():
    out = []
    for make in SCENARIOS:
        x = make()
        conn, a, b, now = x.finish()
        out.append({"name": x.name, "conn": conn, "start": a, "end": b, "now": now, "truth": x.truth})
    return out
