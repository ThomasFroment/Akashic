"""Baseline of today's question planner on the labelled follow-up and time-window sets.

The sets and their discipline are described in ``tests/data/planner/README.md``; the scorer is
``scripts/eval_planner.py``. The floors below are today's measured counts (pre-audit
code), equal to the two method studies' "today" baselines: a regression fails, an
improvement passes. When a change improves a set, raise its floor to the new count.

The named ``known_defect`` cases assert the behaviour the audits and studies call correct and
fail today. Most compare two phrasings of the same question, so they isolate one defect from
the day conventions of #179.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast

import pytest
from defects import known_defect

from akashic.search.planner import follow_up, heuristic_plan
from akashic.search.timeparse import resolve

if TYPE_CHECKING:
    from types import ModuleType

    from akashic.search.planner import Plan

ROOT: Final = Path(__file__).resolve().parents[1]
DATA: Final = ROOT / "tests" / "data" / "planner"
TZ: Final = timezone(timedelta(hours=1))
NAMES: Final = ("Minlo", "Sacha", "Léa")

type Window = tuple[datetime, datetime]


class Tally(Protocol):
    passed: int
    total: int

    @property
    def ratio(self) -> float: ...


class SetScore(Protocol):
    """The part of ``eval_planner.SetScore`` these tests read."""

    counts: dict[str, int]

    def overall(self, metric: str) -> Tally: ...


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_planner", ROOT / "scripts/eval_planner.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while decorating
    spec.loader.exec_module(module)
    return module


evaluation: Final = _load_script()


@functools.cache
def _score(name: str) -> SetScore:
    return cast("SetScore", evaluation.score_set(name))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A local time in November 2026; the 10th is a Tuesday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def window_of(question: str, now: datetime, model_time_text: str | None = None) -> Window | None:
    return cast("Window | None", evaluation.window_of(question, now, model_time_text))


def passes(window: Window | None, gold: Window) -> bool:
    return cast("bool", evaluation.window_passes(window, gold))


def plan(question: str) -> Plan:
    return heuristic_plan(question, int(at(10, 14).timestamp() * 1000), NAMES, TZ)


# --- the sets are the ones the README describes ----------------------------------------------


def _readme_hashes() -> dict[str, tuple[int, str]]:
    text = (DATA / "README.md").read_text("utf-8")
    rows = re.findall(r"^\| `([\w.]+\.jsonl)` \| (\d+) \| `([0-9a-f]{64})` \|$", text, re.MULTILINE)
    return {file: (int(count), digest) for file, count, digest in rows}


def test_every_set_has_a_recorded_hash() -> None:
    files = {p.name for p in DATA.glob("*.jsonl")}
    assert files == set(_readme_hashes())
    assert len(files) == 6


@pytest.mark.parametrize("file", sorted(_readme_hashes()))
def test_set_is_unchanged(file: str) -> None:
    count, digest = _readme_hashes()[file]
    data = (DATA / file).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(data).hexdigest() == digest, (
        f"{file} changed: held-out sets must stay untouched (tests/data/planner/README.md)"
    )
    assert len([line for line in data.splitlines() if line.strip()]) == count


# --- ratchet floors: today's measured counts -------------------------------------------------

FLOORS: Final[dict[str, dict[str, int]]] = {
    "follow_ups_dev": {"people": 139, "kinds": 158, "keywords": 119, "time": 164, "pair": 99},
    "follow_ups_heldout2": {"people": 99, "kinds": 106, "keywords": 61, "time": 105, "pair": 51},
    "follow_ups_heldout3": {"people": 137, "kinds": 157, "keywords": 102, "time": 153, "pair": 64},
    "time_windows_dev": {"rules": 207, "model": 229},
    "time_windows_heldout2": {"rules": 142, "model": 163},
    "time_windows_heldout3": {"rules": 203, "model": 218},
}

# Counts where fewer is better: future windows and false filters (the budget is 0 of each).
CEILINGS: Final[dict[str, dict[str, int]]] = {
    "time_windows_dev": {"future_rules": 163, "future_model": 202, "false_filter_rules": 100,
                         "false_filter_model": 100, "errors_rules": 0, "errors_model": 0},
    "time_windows_heldout2": {"future_rules": 112, "future_model": 128, "false_filter_rules": 35,
                              "false_filter_model": 35, "errors_rules": 0, "errors_model": 0},
    "time_windows_heldout3": {"future_rules": 172, "future_model": 201, "false_filter_rules": 145,
                              "false_filter_model": 145, "errors_rules": 0, "errors_model": 0},
}  # fmt: skip


@pytest.mark.parametrize(
    ("name", "metric"), [(name, metric) for name, floors in FLOORS.items() for metric in floors]
)
def test_accuracy_does_not_regress(name: str, metric: str) -> None:
    tally = _score(name).overall(metric)
    floor = FLOORS[name][metric]
    assert tally.passed >= floor, (
        f"{name} {metric}: {tally.passed}/{tally.total} ({tally.ratio:.1%}) is below today's"
        f" measured {floor}/{tally.total} ({floor / tally.total:.1%})"
    )


@pytest.mark.parametrize(
    ("name", "count"), [(name, count) for name, limits in CEILINGS.items() for count in limits]
)
def test_bad_window_counts_do_not_grow(name: str, count: str) -> None:
    got = _score(name).counts[count]
    limit = CEILINGS[name][count]
    assert got <= limit, f"{name} {count}: {got} exceeds today's measured {limit}"


# --- the scorer's rules ------------------------------------------------------------------------


def test_field_rule_follows_the_study() -> None:
    correct = evaluation.field_correct
    alone, earlier = frozenset({"elaborate"}), frozenset({"game"})
    assert correct("inherit", earlier, alone, earlier)
    assert not correct("inherit", alone, alone, earlier)
    assert correct("own", alone, alone, earlier)
    assert correct("none", frozenset[str](), alone, earlier)
    assert correct("none", alone, alone, earlier)  # "plan as if alone" is accepted
    assert not correct("none", earlier, alone, earlier)


def test_window_rule_has_thirty_minutes_of_slack_and_a_length_bound() -> None:
    gold = (at(9, 12), at(9, 18))
    assert passes((at(9, 12, 30), at(9, 17, 30)), gold)
    assert not passes((at(9, 12, 31), at(9, 18)), gold)
    assert passes((at(9, 9), at(9, 21)), gold)  # twice as long
    assert not passes((at(9, 9), at(9, 21, 1)), gold)
    assert not passes(None, gold)
    assert cast("bool", evaluation.window_passes(None, None))
    assert not cast("bool", evaluation.window_passes(gold, None))


# --- known defects: follow-ups (#175) ---------------------------------------------------------

MINLO_QUESTION: Final = (
    "What did Minlo say during our conversation this morning, something about a game?"
)


@known_defect(175, "a self-contained question inherits the earlier person and topic")
def test_target_video_question_after_the_minlo_question_plans_alone() -> None:
    merged = follow_up(plan("What video did I watch yesterday afternoon?"), plan(MINLO_QUESTION))
    assert merged.people == ()
    assert "game" not in merged.keywords


@known_defect(175, "a self-contained question with no time inherits the earlier window")
def test_topic_switch_without_a_time_searches_all_history() -> None:
    merged = follow_up(
        plan("What did Sacha say about Elden Ring?"), plan("What video did I watch yesterday?")
    )
    assert merged.time is None


@known_defect(175, "inheritance is decided per empty field, so a leftover word blocks the topic")
def test_elaborate_continuation_keeps_the_earlier_topic() -> None:
    merged = follow_up(plan("Can you elaborate?"), plan("what did Minlo say about a game?"))
    assert merged.keywords == ("game",)


# --- known defects: time windows (#45, #179, #180, #181) --------------------------------------


@known_defect(45, '"il y a 2h" / "2h ago" are read as the clock time 02:00')
@pytest.mark.parametrize(
    ("compact", "spelled"),
    [
        ("Qu'est-ce que j'ai regardé il y a 2h ?", "Qu'est-ce que j'ai regardé il y a 2 heures ?"),
        ("What did I watch 2h ago?", "What did I watch 2 hours ago?"),
    ],
)
def test_compact_hours_ago_means_hours_before_now(compact: str, spelled: str) -> None:
    now = at(10, 14)
    assert window_of(spelled, now) == (at(10, 11, 30), at(10, 12, 30))
    assert window_of(compact, now) == window_of(spelled, now)


@known_defect(179, '"last night" starts at 23:00 and misses an evening call')
def test_last_night_covers_an_evening_call() -> None:
    window = window_of("What did Sacha say last night?", at(10, 14))
    assert window is not None
    assert window[0] <= at(9, 20, 30)  # the call ran 20:30-22:45
    assert passes(window, (at(9, 18), at(10, 6)))


@known_defect(179, '"hier soir" ends at 23:00 instead of running past midnight')
def test_yesterday_evening_runs_past_midnight() -> None:
    window = window_of("On a parlé de quoi hier soir avec Minlo ?", at(10, 9))
    assert passes(window, (at(9, 18), at(10, 2)))


@known_defect(179, 'at 00:40, "tonight" / "ce soir" is a future window')
@pytest.mark.parametrize("question", ["What did I watch tonight?", "J'ai regardé quoi ce soir ?"])
def test_tonight_after_midnight_is_the_evening_in_progress(question: str) -> None:
    now = at(10, 0, 40)
    window = window_of(question, now)
    assert window is not None
    assert window[0] < now
    assert passes(window, (at(9, 18), now))


@known_defect(179, 'at 00:40, "aujourd\'hui" covers only the 40 minutes since midnight')
def test_today_after_midnight_is_the_day_still_in_progress() -> None:
    now = at(10, 0, 40)
    assert passes(window_of("Qu'est-ce que j'ai fait aujourd'hui ?", now), (at(9, 5), now))


@known_defect(180, "informal or missing part-of-day words give no window or the whole day")
@pytest.mark.parametrize(
    ("informal", "formal"),
    [
        ("Qu'est-ce que j'ai regardé cet aprèm ?", "Qu'est-ce que j'ai regardé cet après-midi ?"),
        ("Qu'est-ce que j'ai regardé hier aprèm ?", "Qu'est-ce que j'ai regardé hier après-midi ?"),
    ],
)
def test_apocopated_afternoon_matches_the_full_word(informal: str, formal: str) -> None:
    now = at(10, 23, 30)
    assert window_of(formal, now) is not None
    assert window_of(informal, now) == window_of(formal, now)


@known_defect(180, "noon, midnight, earlier and weekend words resolve to no window")
@pytest.mark.parametrize(
    ("question", "now", "gold"),
    [
        ("On a parlé de quoi ce midi ?", at(10, 23, 30), (at(10, 12), at(10, 14))),
        ("What did Sacha say at noon?", at(10, 23, 30), (at(10, 11, 30), at(10, 12, 30))),
        ("J'ai écouté quoi vers minuit ?", at(10, 9), (at(9, 23, 30), at(10, 0, 30))),
        ("What did I watch around midnight?", at(10, 9), (at(9, 23, 30), at(10, 0, 30))),
        ("Qu'est-ce que Léa a dit tout à l'heure ?", at(10, 14), (at(10, 11), at(10, 14))),
        ("What did I read earlier?", at(10, 14), (at(10, 11), at(10, 14))),
        ("What games did I play this weekend?", at(10, 14), (at(7, 5), at(9, 5))),
        ("J'ai joué à quoi ce week-end ?", at(10, 14), (at(7, 5), at(9, 5))),
    ],
)
def test_missing_time_words_resolve(question: str, now: datetime, gold: Window) -> None:
    assert passes(window_of(question, now), gold)


@known_defect(180, "the model's time_text is discarded when the rules find no time")
def test_model_time_text_is_used_when_the_rules_find_none() -> None:
    now = at(10, 14)
    model_text = "il y a 2 jours"  # the model normalised the user's "y a 2 jours"
    read = resolve(model_text, int(now.timestamp() * 1000), TZ)
    assert read is not None
    window = window_of("j'ai regardé quoi y a 2 jours ?", now, model_text)
    assert window is not None
    assert (window[0].timestamp() * 1000, window[1].timestamp() * 1000) == (
        read.start_ms,
        read.end_ms,
    )


@known_defect(181, "day and part of day are picked from different clauses")
def test_two_anchor_question_keeps_the_main_clause_day_and_part() -> None:
    now = at(10, 14)
    main = window_of("What video did I watch yesterday afternoon?", now)
    assert main is not None
    two = "What video did I watch yesterday afternoon that Minlo told me about this morning?"
    assert window_of(two, now) == main


@known_defect(181, '"a 2h video" is read as the clock time 02:00')
def test_duration_before_a_noun_is_not_a_clock_time() -> None:
    now = at(10, 14)
    plain = window_of("Did I watch a video yesterday?", now)
    assert plain is not None
    assert window_of("Did I watch a 2h video yesterday?", now) == plain
