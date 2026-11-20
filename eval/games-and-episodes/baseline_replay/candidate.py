"""Harness check, not a candidate: today's GameMode replayed on the simulated checks (ge.replay),
so replay-based candidates compare against the same simulation rather than the stored rows."""

import ge

NAME = "baseline_replay: today's GameMode on the simulated checks"


def game_rows(conn, ctx):
    return ge.replay(ctx)
