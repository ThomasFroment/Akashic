"""Seeded fuzz and edge cases: conversation clustering and app runs against brute-force oracles.

Today's ``pipeline/episodes.py`` runs on rows in an encrypted test store and is compared with
``oracles.py`` (the documented semantics, ported from the Conversation Clustering Sweep and App
Run Single Pass studies). A faster rewrite (#102, #103) must keep every passing test here green.

Conversation layouts come in two families. *Separated* layouts never put microphone speech where
today's chaining (#36) or double attachment can show, so today must equal the contract exactly.
*Free* layouts put microphone speech anywhere; there only the voice clusters must match, and
today's clusters must contain at least the contract's members. The defects themselves are pinned
by small hand-built cases marked ``known_defect``.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING, Final

import pytest
from defects import known_defect
from oracles import (
    AppRun,
    Conversations,
    Game,
    Seg,
    Visit,
    app_runs,
    conversations,
    insert_games,
    insert_segments,
    insert_visits,
    voice_clusters,
)

from akashic.pipeline import episodes

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection

S: Final = 1_000
M: Final = 60_000
GAP: Final = episodes.CLOSE_GAP_MS
MARGIN: Final = episodes.MIC_MARGIN_MS
T0: Final = 1_794_000_000_000
GAME: Final = "eldenring.exe"
FAR: Final = T0 + 10**12  # a horizon after everything: every cluster and run is closed

VOICE_SOURCES: Final = (("discord.exe",), ("discord.exe", "chrome.exe"), ("teams.exe",), (GAME,))
NOISE_SOURCES: Final = ((), ("chrome.exe",), ("zen.exe",))
MIC_SOURCES: Final = ((), ("zen.exe",))

# --- running today's code ------------------------------------------------------------------


def _reset(conn: Connection) -> None:
    for table in ("episodes", "audio_segments", "game_sessions", "app_sessions", "state"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 - literal table names


def today_conversations(
    conn: Connection, segs: list[Seg], games: list[Game], horizon: int
) -> Conversations:
    """The one adapter to the private clustering step: a rewrite that renames or reshapes
    ``_conversation_clusters`` changes only this function (the replay test uses the public
    ``build_conversations``)."""
    _reset(conn)
    insert_segments(conn, segs)
    insert_games(conn, games)
    final, growing = episodes._conversation_clusters(conn, horizon, 0)
    return Conversations([[s.id for s in c.segments] for c in final], growing)


def today_app_runs(
    conn: Connection, visits: list[Visit], games: list[Game], horizon: int
) -> list[tuple[str, str, int, int, str, str | None, str | None, str, list[int]]]:
    """(kind, ref, start, end, title, app, url, body, item ids) of each episode built."""
    _reset(conn)
    insert_visits(conn, visits)
    insert_games(conn, games)
    built = episodes.build_app_runs(conn, horizon)
    rows = conn.execute(
        "SELECT id, kind, ref, start_ms, end_ms, title, app, url, body FROM episodes ORDER BY id"
    ).fetchall()
    assert built == len(rows)
    return [
        (
            r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
            [
                int(i[0])
                for i in conn.execute(
                    "SELECT item_id FROM episode_items WHERE episode_id = ?"
                    " AND item_kind = 'app_session' ORDER BY item_id",
                    (r[0],),
                )
            ],
        )
        for r in rows
    ]  # fmt: skip


def run_problems(
    got: list[tuple[str, str, int, int, str, str | None, str | None, str, list[int]]],
    want: list[AppRun],
) -> list[str]:
    if len(got) != len(want):
        return [f"{len(got)} episodes, oracle {len(want)}: {[g[1] for g in got]} vs {want}"]
    problems: list[str] = []
    for (kind, ref, start, end, title, app, url, body, items), run in zip(got, want, strict=True):
        if (kind, ref, start, end, url, items) != (
            run.kind, run.ref, run.start_ms, run.end_ms, run.url, sorted(run.items)
        ):  # fmt: skip
            problems.append(f"{ref}: {(kind, start, end, url, items)} vs {run}")
        elif app is None or app not in run.options:
            problems.append(f"{ref}: dominant {app!r} not in {sorted(run.options)}")
        elif (title, body) != run.options[app]:
            problems.append(f"{ref}: title/body {(title, body)!r} vs {run.options[app]!r}")
    return problems


# --- conversation layouts ------------------------------------------------------------------


class _Ids:
    """Unique ids, shuffled so that id order and time order disagree."""

    def __init__(self, rng: random.Random) -> None:
        self._pool = rng.sample(range(1, 100_000), 2_000)

    def __call__(self) -> int:
        return self._pool.pop()


def _calls(rng: random.Random, ids: _Ids) -> list[Seg]:
    """1-4 calls of voice segments: gaps up to exactly the close gap, overlaps, equal starts,
    voice microphone segments, in-game chat, non-voice system sound in between."""
    segs: list[Seg] = []
    t = T0 + rng.randrange(0, 10 * M)
    for _ in range(rng.randint(1, 4)):
        for _ in range(rng.randint(1, 8)):
            length = rng.choice((400, 2 * S, 5 * S, 30 * S))
            if rng.random() < 0.2:  # the user heard through the voice app
                segs.append(Seg(ids(), "mic", t, t + length, rng.choice(VOICE_SOURCES[:3])))
            else:
                segs.append(Seg(ids(), "system", t, t + length, rng.choice(VOICE_SOURCES)))
            if rng.random() < 0.25:
                segs.append(Seg(ids(), "system", t, t + rng.randint(1, 4) * S, NOISE_SOURCES[1]))
            t += length + rng.choice((-length, -1, 0, 1, 2 * S, M, GAP - 1, GAP))
        t += rng.choice((GAP + 1, GAP + 2 * S, 10 * M, 40 * M))
    return segs


def _games(rng: random.Random, segs: list[Seg]) -> list[Game]:
    """Game sessions over parts of the span. Each starts before the last segment start (segments
    added later start no earlier), so the ``_games`` query bound (first start, last-started
    segment's end) never hides one; that bound is pinned separately below."""
    first = min(s.start_ms for s in segs)
    last_start = max(s.start_ms for s in segs)
    games: list[Game] = []
    for _ in range(rng.randint(0, 2)):
        start = rng.randrange(first - M, last_start)
        games.append((GAME, start, start + rng.choice((0, S, 5 * M, 60 * M))))
    return games


def separated_layout(seed: int) -> tuple[list[Seg], list[Game], int]:
    """Microphone speech either ends inside a call window without passing the call's end, or
    stays outside every window: today's chaining and double attachment cannot show."""
    rng = random.Random(seed)  # noqa: S311 - seeded fuzz
    ids = _Ids(rng)
    segs = _calls(rng, ids)
    games = _games(rng, segs)
    bounds = [
        (min(s.start_ms for s in c), max(s.end_ms for s in c)) for c in voice_clusters(segs, games)
    ]
    for index, (start, end) in enumerate(bounds):
        for _ in range(rng.randint(0, 4)):  # inside: end in (start - 30 s, end]
            stop = rng.choice((start - MARGIN + 1, end, rng.randint(start - MARGIN + 1, end)))
            length = rng.choice((300, 5 * S, 30 * S))
            segs.append(Seg(ids(), "mic", stop - length, stop, rng.choice(MIC_SOURCES)))
        after = end + MARGIN  # outside: from exactly the window's end to the next window
        before_next = bounds[index + 1][0] - MARGIN if index + 1 < len(bounds) else after + 20 * M
        for _ in range(rng.randint(0, 3)):
            length = rng.choice((300, 5 * S, 30 * S))
            if before_next - after >= length:
                begin = rng.choice((after, before_next - length))
                segs.append(Seg(ids(), "mic", begin, begin + length, rng.choice(MIC_SOURCES)))
        if index == 0 and rng.random() < 0.5:  # before the first window, ending exactly at it
            segs.append(Seg(ids(), "mic", start - MARGIN - 5 * S, start - MARGIN))
    if games and rng.random() < 0.5:  # a game segment outside every session: not voice
        segs.append(Seg(ids(), "system", T0 - 20 * M, T0 - 19 * M, (GAME,)))
    ends = [e for _, e in bounds] or [T0]
    horizon = rng.choice(
        (FAR, rng.choice(ends) + GAP, rng.choice(ends) + GAP - 1, max(ends) + M, T0)
    )
    return segs, games, horizon


def free_layout(seed: int) -> tuple[list[Seg], list[Game]]:
    """Calls plus microphone speech anywhere, including chains of short pauses after a call."""
    rng = random.Random(seed)  # noqa: S311 - seeded fuzz
    ids = _Ids(rng)
    segs = _calls(rng, ids)
    games = _games(rng, segs)
    first = min(s.start_ms for s in segs)
    last = max(s.end_ms for s in segs)
    for _ in range(rng.randint(0, 30)):
        start = rng.randrange(first - 5 * M, last + 5 * M)
        segs.append(Seg(ids(), "mic", start, start + rng.choice((300, 5 * S, 30 * S))))
    if rng.random() < 0.5:  # a chain: every pause under the 30 s margin
        t = last + rng.choice((S, 20 * S))
        for _ in range(rng.randint(2, 12)):
            segs.append(Seg(ids(), "mic", t, t + 5 * S))
            t += 5 * S + rng.choice((S, 15 * S, 24 * S))
    return segs, games


# --- conversation fuzz -----------------------------------------------------------------------


def test_separated_layouts_equal_the_contract(conn: Connection) -> None:
    mismatches: list[str] = []
    finals = growing = 0
    for seed in range(300):
        segs, games, horizon = separated_layout(seed)
        got = today_conversations(conn, segs, games, horizon)
        want = conversations(segs, games, horizon)
        finals += bool(want.final)
        growing += want.growing_start is not None
        if got != want:
            mismatches.append(f"seed {seed}: {got} != {want}")
    assert not mismatches, mismatches[:3]
    assert finals >= 100  # the fuzz reaches both sides of the final/growing split
    assert growing >= 30


def test_free_layouts_keep_voice_clusters_and_contract_members(conn: Connection) -> None:
    """With chaining possible, today's clusters still hold exactly the oracle's voice segments,
    and every member the contract attaches; extra microphone members are the #36 defect."""
    mismatches: list[str] = []
    chained = 0
    for seed in range(200):
        segs, games = free_layout(seed)
        got = today_conversations(conn, segs, games, FAR)
        voice = voice_clusters(segs, games)
        voice_ids = {s.id for c in voice for s in c}
        want = conversations(segs, games, FAR)
        assert got.growing_start is None
        assert want.growing_start is None
        if [[i for i in c if i in voice_ids] for c in got.final] != [
            [s.id for s in c] for c in voice
        ]:
            mismatches.append(f"seed {seed}: voice clusters differ")
        elif any(not set(w) <= set(g) for g, w in zip(got.final, want.final, strict=True)):
            mismatches.append(f"seed {seed}: a contract member is missing")
        chained += got.final != want.final
    assert not mismatches, mismatches[:3]
    assert chained >= 10  # the family does reach today's chaining


def test_replayed_passes_build_the_one_shot_clusters(conn: Connection) -> None:
    """``build_conversations`` run every few minutes while speech arrives builds, in the end,
    exactly the contract's final clusters at the last horizon, each once; the watermark never
    loses a segment."""
    for seed in range(40):
        rng = random.Random(seed)  # noqa: S311 - seeded fuzz
        segs, games, _ = separated_layout(seed)
        _reset(conn)
        insert_games(conn, games)
        inserted: set[int] = set()
        now = min(s.start_ms for s in segs)
        last = max(s.end_ms for s in segs) + 3 * GAP
        while now < last:
            now += rng.choice((30 * S, 2 * M, 7 * M))
            arrived = [s for s in segs if s.end_ms <= now and s.id not in inserted]
            insert_segments(conn, arrived)
            inserted.update(s.id for s in arrived)
            episodes.build_conversations(conn, now - GAP)
        built = sorted(
            sorted(
                int(i[0])
                for i in conn.execute(
                    "SELECT item_id FROM episode_items WHERE episode_id = ?", (e[0],)
                )
            )
            for e in conn.execute("SELECT id FROM episodes WHERE kind = 'conversation'")
        )
        want = conversations(segs, games, now - GAP)
        assert built == sorted(sorted(c) for c in want.final), f"seed {seed}"


# --- conversation edge cases ----------------------------------------------------------------


def _call(start: int, count: int = 6, every: int = 10 * S, length: int = 5 * S) -> list[Seg]:
    """Discord voice segments every ``every`` ms from ``start``; ids from ``start``."""
    base = start // S
    return [
        Seg(base + n, "system", start + n * every, start + n * every + length, ("discord.exe",))
        for n in range(count)
    ]


def _both(
    conn: Connection, segs: list[Seg], games: list[Game] | None = None, horizon: int = FAR
) -> tuple[Conversations, Conversations]:
    games = games or []
    return today_conversations(conn, segs, games, horizon), conversations(segs, games, horizon)


@pytest.mark.parametrize(("gap", "clusters"), [(GAP, 1), (GAP + 1, 2)])
def test_a_pause_of_exactly_five_minutes_still_joins(
    conn: Connection, gap: int, clusters: int
) -> None:
    segs = [
        Seg(1, "system", T0, T0 + 5 * S, ("discord.exe",)),
        Seg(2, "system", T0 + 5 * S + gap, T0 + 10 * S + gap, ("discord.exe",)),
    ]
    got, want = _both(conn, segs)
    assert got == want
    assert len(got.final) == clusters


def test_the_pause_is_measured_from_the_latest_end_not_the_last_segment(conn: Connection) -> None:
    segs = [
        Seg(1, "system", T0, T0 + 30 * S, ("discord.exe",)),
        Seg(2, "system", T0 + S, T0 + 2 * S, ("discord.exe",)),
        Seg(3, "system", T0 + 30 * S + GAP, T0 + 35 * S + GAP, ("discord.exe",)),
        Seg(4, "system", T0 + 35 * S + 2 * GAP + 1, T0 + 36 * S + 2 * GAP, ("discord.exe",)),
    ]
    got, want = _both(conn, segs)
    assert got == want
    assert got.final == [[1, 2, 3], [4]]


@pytest.mark.parametrize(
    ("offset", "attached"),
    [(MARGIN, False), (MARGIN - 1, True)],
    ids=["at-end-plus-30s", "one-ms-earlier"],
)
def test_mic_window_after_a_call_is_half_open(
    conn: Connection, offset: int, *, attached: bool
) -> None:
    call = _call(T0)
    end = call[-1].end_ms
    mic = Seg(999, "mic", end + offset, end + offset + 5 * S)
    got, want = _both(conn, [*call, mic])
    assert got == want
    assert (999 in got.final[0]) is attached


@pytest.mark.parametrize(
    ("offset", "attached"),
    [(0, False), (1, True)],
    ids=["ends-at-start-minus-30s", "one-ms-later"],
)
def test_mic_window_before_a_call_is_open(conn: Connection, offset: int, *, attached: bool) -> None:
    call = _call(T0 + 2 * M)
    stop = T0 + 2 * M - MARGIN + offset
    got, want = _both(conn, [Seg(999, "mic", stop - 5 * S, stop), *call])
    assert got == want
    assert (999 in got.final[0]) is attached


def test_equal_starts_are_ordered_by_id(conn: Connection) -> None:
    segs = [
        Seg(50, "system", T0, T0 + 5 * S, ("discord.exe",)),
        Seg(7, "system", T0, T0 + 6 * S, ("discord.exe",)),
        Seg(30, "mic", T0, T0 + 4 * S),
        Seg(2, "mic", T0 + 2 * S, T0 + 3 * S),
        Seg(1, "system", T0 + 2 * S, T0 + 9 * S, ("discord.exe",)),
    ]
    got, want = _both(conn, segs)
    assert got == want
    assert got.final == [[7, 30, 50, 1, 2]]


def test_interleaved_mic_and_loopback(conn: Connection) -> None:
    """Microphone speech heard through the voice app is voice: it chains and moves the end;
    plain microphone speech only attaches within 30 s of the voice bounds."""
    call = _call(T0, count=3)
    end = call[-1].end_ms
    segs = [
        *call,
        Seg(100, "mic", T0 + 3 * S, T0 + 8 * S),  # between loopback lines
        Seg(101, "mic", end + 60 * S, end + 65 * S, ("discord.exe",)),  # voice: end + 65 s
        Seg(102, "mic", end + 90 * S, end + 95 * S),  # within 30 s of the new end
        Seg(103, "mic", end + 130 * S, end + 135 * S),  # 65 s past it
        Seg(104, "system", end + 10 * S, end + 15 * S, ("chrome.exe",)),  # not voice, not mic
    ]
    got, want = _both(conn, segs)
    assert got == want
    assert got.final == [[call[0].id, 100, call[1].id, call[2].id, 101, 102]]


def test_a_single_utterance_is_a_conversation(conn: Connection) -> None:
    seg = Seg(5, "system", T0, T0 + 2 * S, ("discord.exe",), "salut, tu joues ce soir ?")
    insert_segments(conn, [seg])
    assert episodes.build_conversations(conn, FAR) == 1
    row = conn.execute("SELECT ref, title, app, body, start_ms, end_ms FROM episodes").fetchone()
    assert row == (
        "conversation:5", "Discord conversation", "discord.exe", seg.text, T0, T0 + 2 * S
    )  # fmt: skip


def test_in_game_chat_is_voice_only_during_the_game_session(conn: Connection) -> None:
    g0 = T0 + M
    segs = [
        Seg(1, "system", T0, T0 + 5 * S, (GAME,)),
        Seg(2, "system", g0 + 10 * S, g0 + 15 * S, (GAME,)),
        Seg(3, "system", g0 + 40 * S, g0 + 50 * S, (GAME,)),
        Seg(4, "mic", g0 + 55 * S, g0 + 58 * S),
        Seg(5, "system", g0 + 20 * M, g0 + 20 * M + 5 * S, (GAME,)),
    ]
    got, want = _both(conn, segs, [(GAME, g0, g0 + 5 * M)])
    assert got == want
    assert got.final == [[2, 3, 4]]


@pytest.mark.parametrize(("slack", "final"), [(0, True), (-1, False)])
def test_finality_at_exactly_five_minutes_after_the_end(
    conn: Connection, slack: int, *, final: bool
) -> None:
    call = _call(T0)
    got, want = _both(conn, call, horizon=call[-1].end_ms + GAP + slack)
    assert got == want
    assert bool(got.final) is final
    assert got.growing_start == (None if final else T0)


def test_growing_start_includes_microphone_speech_before_the_call(conn: Connection) -> None:
    first = _call(T0)
    second = _call(T0 + 30 * M)
    early = Seg(999, "mic", T0 + 30 * M - 20 * S, T0 + 30 * M - 15 * S)
    got, want = _both(conn, [*first, early, *second], horizon=second[-1].end_ms + M)
    assert got == want
    assert got.growing_start == early.start_ms
    assert len(got.final) == 1


@known_defect(36, "microphone speech chains onto a call: each attached segment widens the window")
def test_mic_speech_does_not_chain_onto_a_call(conn: Connection) -> None:
    call = _call(T0)  # ends at T0 + 55 s
    end = call[-1].end_ms
    near = Seg(900, "mic", end + 10 * S, end + 15 * S)  # inside the 30 s margin
    chained = Seg(901, "mic", end + 40 * S, end + 45 * S)  # 40 s after the call
    got = today_conversations(conn, [*call, near, chained], [], FAR)
    assert got.final == [[*(s.id for s in call), 900]]


@known_defect(36, "a microphone chain keeps a finished call from ever becoming final")
def test_mic_chain_does_not_hold_back_finality(conn: Connection) -> None:
    call = _call(T0)
    end = call[-1].end_ms
    chain = [
        Seg(900 + n, "mic", end + 5 * S + n * 20 * S, end + 10 * S + n * 20 * S) for n in range(30)
    ]
    got = today_conversations(conn, [*call, *chain], [], end + 30 * S + GAP)
    assert got.final == [[*(s.id for s in call), 900, 901]]


@known_defect(36, "a microphone chain bridging two calls links the same speech to both")
def test_chained_speech_is_not_linked_to_two_calls(conn: Connection) -> None:
    first = _call(T0)
    end = first[-1].end_ms
    second = _call(end + 8 * M)
    chain = [
        Seg(900 + n, "mic", end + 5 * S + n * 20 * S, end + 10 * S + n * 20 * S) for n in range(30)
    ]
    got = today_conversations(conn, [*first, *chain, *second], [], FAR)
    want = conversations([*first, *chain, *second], [], FAR)
    assert got == want


@known_defect(102, "study finding: a microphone segment spanning two call windows joins both")
def test_one_mic_segment_joins_at_most_one_call(conn: Connection) -> None:
    first = _call(T0, count=2)
    second = _call(first[-1].end_ms + GAP + 1, count=2)
    long_mic = Seg(999, "mic", first[-1].end_ms - 5 * S, second[0].start_ms + 5 * S)
    got = today_conversations(conn, [*first, long_mic, *second], [], FAR)
    assert sum(999 in c for c in got.final) == 1


@pytest.mark.xfail(  # not an audit finding, so not known_defect
    strict=True,
    raises=AssertionError,
    reason="not in the audits: _games bounds by the last-started segment's end, so a game"
    " session starting after it is never read for an earlier, longer segment",
)
def test_a_game_session_starting_late_still_makes_earlier_game_sound_voice(
    conn: Connection,
) -> None:
    long_game_sound = Seg(1, "system", T0, T0 + 100 * S, (GAME,))
    short = Seg(2, "system", T0 + 10 * S, T0 + 20 * S, ("chrome.exe",))
    games: list[Game] = [(GAME, T0 + 50 * S, T0 + 10 * M)]
    got = today_conversations(conn, [long_game_sound, short], games, FAR)
    assert got.final == conversations([long_game_sound, short], games, FAR).final == [[1]]


# --- app runs ------------------------------------------------------------------------------

PROCESSES: Final = (
    "zen.exe", "chrome.exe", "msedge.exe", "firefox.exe", "Chrome.exe",
    "code.exe", "Code.exe", "windowsterminal.exe", "explorer.exe", "notepad.exe", None,
)  # fmt: skip
TITLES: Final = (None, "", "a", "b", "c", "A", "a b", "é")
URLS: Final = (None, "", "https://a.test", "https://b.test")
STEPS: Final = (0, 0, S, M, 2 * M, GAP - 1, GAP, GAP + 1, 400 * S)
LENGTHS: Final = (0, S, M, 200 * S, 400 * S, 2_000 * S)


def visits_layout(seed: int) -> tuple[list[Visit], list[Game], int]:
    rng = random.Random(seed)  # noqa: S311 - seeded fuzz
    count = rng.randrange(0, 60)
    processes = rng.sample(PROCESSES, rng.randint(1, 6))
    ids = rng.sample(range(1, 10 * count + 2), count)
    t = T0
    visits: list[Visit] = []
    for n in range(count):
        t += rng.choice(STEPS)
        visits.append(
            Visit(ids[n], rng.choice(processes), rng.choice(TITLES), rng.choice(URLS), t,
                  t + rng.choice(LENGTHS))
        )  # fmt: skip
    games: list[Game] = []
    if visits:
        visits.sort(key=lambda v: (v.start_ms, v.id))
        named = [v for v in visits if v.process is not None] or visits
        last_end = named[-1].end_ms  # today's game query bound (see the xfail below)
        for _ in range(rng.randint(0, 4)):
            anchor = rng.choice(visits)
            length = rng.choice((0, 1, S, GAP, 15 * M))
            start = rng.choice(
                (anchor.start_ms - length, anchor.end_ms, anchor.start_ms, anchor.start_ms + S)
            )
            if start < last_end:
                games.append((GAME, start, start + length))
    ends = [v.end_ms for v in visits] or [T0]
    pick = rng.choice(ends)
    horizon = rng.choice((FAR, pick + GAP, pick + GAP - 1, pick + GAP + 1, max(ends) + GAP, T0 - 1))
    return visits, games, horizon


def test_app_runs_match_the_oracle(conn: Connection) -> None:
    mismatches: list[str] = []
    built = ties = 0
    for seed in range(400):
        visits, games, horizon = visits_layout(seed)
        want = app_runs(visits, games, horizon)
        problems = run_problems(today_app_runs(conn, visits, games, horizon), want)
        built += len(want)
        ties += sum(len(run.options) > 1 for run in want)
        if problems:
            mismatches.append(f"seed {seed}: {problems}")
    assert not mismatches, mismatches[:3]
    assert built >= 300
    assert ties >= 10  # dominant-process ties are accepted as any tied process


def _visits(*spec: tuple[str | None, str | None, int, int]) -> list[Visit]:
    """(process, title, start, end) in minutes after T0; ids in order from 1."""
    return [
        Visit(n, process, title, None, T0 + start * M, T0 + end * M)
        for n, (process, title, start, end) in enumerate(spec, start=1)
    ]


def _check(
    conn: Connection, visits: list[Visit], games: list[Game] | None = None, horizon: int = FAR
) -> list[tuple[str, str, int, int, str, str | None, str | None, str, list[int]]]:
    games = games or []
    got = today_app_runs(conn, visits, games, horizon)
    assert not run_problems(got, app_runs(visits, games, horizon))
    return got


@pytest.mark.parametrize(("gap", "runs"), [(GAP, 1), (GAP + 1, 2)])
def test_an_idle_gap_of_exactly_five_minutes_keeps_the_run(
    conn: Connection, gap: int, runs: int
) -> None:
    visits = [
        Visit(1, "code.exe", "a", None, T0, T0 + 6 * M),
        Visit(2, "code.exe", "b", None, T0 + 6 * M + gap, T0 + 12 * M + gap),
    ]
    assert len(_check(conn, visits)) == runs


def test_the_run_gap_is_measured_from_the_latest_end(conn: Connection) -> None:
    got = _check(conn, _visits(("code.exe", "long", 0, 20), ("code.exe", "short", 1, 2),
                               ("code.exe", "late", 25, 30)))  # fmt: skip
    assert [g[8] for g in got] == [[1, 2, 3]]


def test_a_kind_switch_splits_a_run_without_a_gap(conn: Connection) -> None:
    got = _check(conn, _visits(("zen.exe", "page", 0, 4), ("code.exe", "file", 4, 10)))
    assert [(g[0], g[8]) for g in got] == [("browsing", [1]), ("work", [2])]


def test_process_names_are_compared_exactly(conn: Connection) -> None:
    """Capture writes lower-case executables; a different case is another program, and only the
    exact browser names count as browsing (characterises today, not a requirement)."""
    got = _check(
        conn,
        _visits(("Chrome.exe", "page", 0, 3), ("code.exe", "x", 3, 6), ("Code.exe", "y", 6, 10)),
    )
    assert [(g[0], g[5]) for g in got] == [("work", "Code.exe")]


def test_the_dominant_process_is_the_longest_not_the_most_frequent(conn: Connection) -> None:
    got = _check(
        conn,
        _visits(("code.exe", "a", 0, 1), ("code.exe", "b", 1, 2), ("code.exe", "c", 2, 3),
                ("notepad.exe", "n", 3, 10)),
    )  # fmt: skip
    assert got[0][5] == "notepad.exe"
    assert got[0][7] == "Notepad\nn\na\nb\nc"


def test_equal_visit_starts_are_ordered_by_id(conn: Connection) -> None:
    visits = [
        Visit(9, "code.exe", "nine", None, T0, T0 + 6 * M),
        Visit(3, "zen.exe", "three", "https://a.test", T0, T0 + 4 * M),
    ]
    got = _check(conn, visits)
    assert [(g[1], g[8]) for g in got] == [("browsing:3", [3]), ("work:9", [9])]


def test_the_longest_visit_with_a_url_gives_the_url(conn: Connection) -> None:
    visits = [
        Visit(1, "zen.exe", "a", "https://short.test", T0, T0 + M),
        Visit(2, "zen.exe", "b", "", T0 + M, T0 + 4 * M),
        Visit(3, "zen.exe", "c", "https://first.test", T0 + 4 * M, T0 + 6 * M),
        Visit(4, "zen.exe", "d", "https://tied.test", T0 + 6 * M, T0 + 8 * M),
    ]
    assert _check(conn, visits)[0][6] == "https://first.test"


@pytest.mark.parametrize(
    ("process", "minutes", "built"),
    [("zen.exe", 3, 1), ("zen.exe", 2.999, 0), ("code.exe", 5, 1), ("code.exe", 4.999, 0)],
)
def test_minimum_run_lengths_are_inclusive(
    conn: Connection, process: str, minutes: float, built: int
) -> None:
    visits = [Visit(1, process, "t", None, T0, T0 + round(minutes * M))]
    assert len(_check(conn, visits)) == built


def test_a_run_that_may_still_grow_stops_the_pass(conn: Connection) -> None:
    """Runs are emitted oldest first and the pass stops at the first open one, even when a
    later run of the other kind is already closed (it is built on a later pass)."""
    visits = _visits(("zen.exe", "open", 0, 60), ("code.exe", "inside", 10, 20))
    horizon = T0 + 20 * M + GAP
    assert _check(conn, visits, horizon=horizon) == []
    assert len(_check(conn, visits, horizon=T0 + 60 * M + GAP)) == 2


@pytest.mark.parametrize(
    ("game", "kept"),
    [
        ((T0 + 6 * M, T0 + 7 * M), [1]),  # touches the end of the visit: not an overlap
        ((T0 - M, T0), [1]),  # touches its start
        ((T0 + 3 * M, T0 + 3 * M), []),  # zero length inside still overlaps (strict on both)
        ((T0 + 3 * M, T0 + 4 * M), []),  # inside
        ((T0 + 4 * M, T0 + 2 * M), []),  # inverted, but start < end of visit and end > start
    ],
)
def test_time_in_a_game_is_left_out_with_strict_bounds(
    conn: Connection, game: tuple[int, int], kept: list[int]
) -> None:
    visits = [Visit(1, "code.exe", "a", None, T0, T0 + 6 * M)]
    got = _check(conn, visits, [(GAME, *game)])
    assert [g[8] for g in got] == ([kept] if kept else [])


@pytest.mark.xfail(  # not an audit finding, so not known_defect
    strict=True,
    raises=AssertionError,
    reason="not in the audits: the game_sessions query is bounded by the last visit's end, not"
    " the latest end, so a game during the tail of an earlier, longer visit is never read",
)
def test_a_game_during_an_earlier_longer_visit_removes_it(conn: Connection) -> None:
    visits = _visits(("code.exe", "long", 0, 20), ("code.exe", "short", 1, 2))
    games: list[Game] = [(GAME, T0 + 10 * M, T0 + 12 * M)]
    got = today_app_runs(conn, visits, games, FAR)
    assert not run_problems(got, app_runs(visits, games, FAR))


_TIE_SCRIPT = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from akashic.pipeline.episodes import build_app_runs
    from akashic.storage import db
    conn = db.connect(Path(sys.argv[1]), bytes(32))
    db.migrate(conn)
    for n, process in enumerate(("code.exe", "notepad.exe", "excel.exe", "obsidian.exe")):
        conn.execute(
            "INSERT INTO app_sessions (process, window_title, start_ms, end_ms, frame_count,"
            " last_frame_id, closed) VALUES (?, 't', ?, ?, 1, 0, 1)",
            (process, n * 120_000, (n + 1) * 120_000),
        )
    build_app_runs(conn, 10**15)
    print(conn.execute("SELECT app FROM episodes").fetchone()[0])
    """
)


@known_defect(103, "study finding: a dominant-process tie follows hash-randomized set order")
def test_a_dominant_process_tie_does_not_depend_on_the_hash_seed(tmp_path: Path) -> None:
    chosen: set[str] = set()
    for seed in range(6):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        result = subprocess.run(  # noqa: S603 - this interpreter, a literal script
            [sys.executable, "-c", _TIE_SCRIPT, str(tmp_path / f"tie{seed}.db")],
            capture_output=True, text=True, check=True, env=env, timeout=120,
        )  # fmt: skip
        chosen.add(result.stdout.strip())
    assert len(chosen) == 1, chosen
