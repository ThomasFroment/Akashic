"""Baseline: today's `sessions.app_bands`, unchanged (a session ends at its last stored frame)."""

from core import ORIGINAL_APP_BANDS


def bands(conn, start_ms, end_ms, now_ms, ctx):
    return ORIGINAL_APP_BANDS(conn, start_ms, end_ms, None)
