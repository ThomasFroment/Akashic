"""Reference: today's app-run building, copied verbatim from src/akashic/pipeline/episodes.py
(lines 39-47, 57-67, 378-471 in the pre-audit code). Only the SQL is removed: ``rows`` are the
``app_sessions`` rows (id, process, window_title, url, start_ms, end_ms) ordered by
(start_ms, id), ``games`` the ``game_sessions`` (start_ms, end_ms) rows the SQL returned, and
``_add_episode(conn, **kw)`` is replaced by the injected ``add_episode(**kw)`` recorder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CLOSE_GAP_MS: Final = 5 * 60_000  # silence that ends a conversation / a browsing run
MIN_BROWSING_MS: Final = 3 * 60_000
MIN_WORK_MS: Final = 5 * 60_000
MAX_TITLES: Final = 8
BROWSER_PROCESSES: Final = frozenset(
    {"chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "zen.exe", "opera.exe", "vivaldi.exe"}
)


def app_label(process: str | None) -> str:
    """``discord.exe`` -> ``Discord``."""
    stem = (process or "").removesuffix(".exe")
    return stem[:1].upper() + stem[1:]


@dataclass(slots=True)
class _Visit:
    id: int
    process: str
    title: str
    url: str | None
    start_ms: int
    end_ms: int


def _run_kind(process: str) -> str:
    return "browsing" if process in BROWSER_PROCESSES else "work"


def _runs(visits: list[_Visit]) -> list[list[_Visit]]:
    runs: list[list[_Visit]] = []
    for visit in visits:
        if (
            runs
            and _run_kind(runs[-1][-1].process) == _run_kind(visit.process)
            and visit.start_ms - max(v.end_ms for v in runs[-1]) <= CLOSE_GAP_MS
        ):
            runs[-1].append(visit)
        else:
            runs.append([visit])
    return runs


def _top_titles(run: list[_Visit]) -> list[tuple[str, int]]:
    spent: dict[str, int] = {}
    for v in run:
        if v.title:
            spent[v.title] = spent.get(v.title, 0) + max(v.end_ms - v.start_ms, 1_000)
    return sorted(spent.items(), key=lambda kv: kv[1], reverse=True)[:MAX_TITLES]


def build_app_runs(rows, games, horizon, add_episode) -> int:
    # --- removed: watermark query, app_sessions SELECT ---
    if not rows:
        return 0
    # --- removed: game_sessions SELECT (``games`` is its result) ---
    visits = [
        _Visit(r[0], r[1], r[2] or "", r[3], r[4], r[5])
        for r in rows
        if not any(g[0] < r[5] and g[1] > r[4] for g in games)
    ]
    built = 0
    for run in _runs(visits):
        kind = _run_kind(run[0].process)
        start_ms, end_ms = run[0].start_ms, max(v.end_ms for v in run)
        if end_ms + CLOSE_GAP_MS > horizon:
            break  # may still grow; later runs are newer still
        if end_ms - start_ms < (MIN_BROWSING_MS if kind == "browsing" else MIN_WORK_MS):
            continue
        titles = _top_titles(run)
        process = max(
            {v.process for v in run},
            key=lambda p: sum(v.end_ms - v.start_ms for v in run if v.process == p),
        )
        url = next(
            (
                v.url
                for v in sorted(run, key=lambda v: v.end_ms - v.start_ms, reverse=True)
                if v.url
            ),
            None,
        )
        lead = titles[0][0] if titles else app_label(process)
        body = "\n".join(t for t, _ in titles)
        if kind == "work":
            body = f"{app_label(process)}\n{body}" if body else app_label(process)
        built += add_episode(
            kind=kind,
            ref=f"{kind}:{run[0].id}",
            start_ms=start_ms,
            end_ms=end_ms,
            title=lead,
            app=process,
            url=url,
            body=body,
            items=[("app_session", v.id) for v in run],
        )
    return built
