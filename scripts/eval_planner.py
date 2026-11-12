"""Score today's question planner on the labelled follow-up and time-window question sets.

uv run python scripts/eval_planner.py [--set NAME ...] [--failures] [--output report.json]

The six sets live in ``tests/data/planner/`` (its README gives their provenance, labelling
conventions and split discipline). No model, database or user data is used: every set is scored
through the repository's rule path, exactly as the two method studies scored "today".

- Follow-up sets: for each pair, ``heuristic_plan`` of the earlier question (E) and of the new
  question alone (S), then ``planner.follow_up(S, E)`` (C). Per field (people, kinds, keywords,
  time), gold ``inherit`` needs C == E, ``own`` needs C == S, and ``none`` needs C empty or
  C == S. ``pair`` counts pairs with all four fields right; the study's headline is the lowest
  field accuracy.
- Time-window sets: each question at each labelled "now", in two modes. ``rules`` is
  ``heuristic_plan(question).time``. ``model`` is ``plan_from_model`` with a best-case model
  reply whose ``time_text`` is the record's ``main_time_text`` (``""`` when the question names
  no time); it is deterministic and needs no model. A case passes when the window covers the
  gold span with 30 minutes of tolerance on each edge and is at most twice as long; for a null
  gold, when there is no window. ``future`` counts windows that start at or after now,
  ``false_filter`` counts windows given for a null gold.

Every labelled date (2026-11-08 to 2026-11-10) is in Europe/Paris winter time, UTC+1. The
study harnesses used that offset (the time-window labels carry it); a fixed offset is used here
because the environment ships no tz database.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from akashic.search import planner

if TYPE_CHECKING:
    from collections.abc import Callable

    from akashic.search.planner import Plan

DATA: Final = Path(__file__).resolve().parents[1] / "tests" / "data" / "planner"
TZ: Final = timezone(timedelta(hours=1), "CET")
NOW: Final = datetime(2026, 11, 10, 14, 0, tzinfo=TZ)  # the follow-up study's fixed clock
NOW_MS: Final = int(NOW.timestamp() * 1000)
DEFAULT_NAMES: Final = ("Minlo", "Sacha", "Léa")
FIELDS: Final = ("people", "kinds", "keywords", "time")
FOLLOW_UP_METRICS: Final = (*FIELDS, "pair")
WINDOW_METRICS: Final = ("rules", "model")
TOLERANCE: Final = timedelta(minutes=30)

SETS: Final[dict[str, str]] = {
    "follow_ups_dev": "follow_ups",
    "follow_ups_heldout2": "follow_ups",
    "follow_ups_heldout3": "follow_ups",
    "time_windows_dev": "time_windows",
    "time_windows_heldout2": "time_windows",
    "time_windows_heldout3": "time_windows",
}

type Record = dict[str, object]
type FieldValue = frozenset[str] | tuple[int, int] | None
type Window = tuple[datetime, datetime]


@dataclass(slots=True)
class Tally:
    passed: int = 0
    total: int = 0

    def add(self, ok: bool) -> None:  # noqa: FBT001 - a tally of outcomes
        self.passed += ok
        self.total += 1

    @property
    def ratio(self) -> float:
        return self.passed / self.total if self.total else float("nan")


@dataclass(slots=True)
class SetScore:
    """Scores of one set. ``groups`` maps "all", "split:<s>", "lang:<l>", "category:<c>" (and
    "now:<hh:mm>" for time windows) to a tally per metric; ``failing`` lists, per metric, the
    ids of the failed pairs or cases (``<id>@<date> <hh:mm>`` for time windows)."""

    name: str
    family: str
    metrics: tuple[str, ...]
    groups: dict[str, dict[str, Tally]] = field(default_factory=dict[str, dict[str, Tally]])
    counts: dict[str, int] = field(default_factory=dict[str, int])
    failing: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    def record(self, keys: list[str], metric: str, ok: bool, item: str) -> None:  # noqa: FBT001
        for key in keys:
            group = self.groups.setdefault(key, {m: Tally() for m in self.metrics})
            group[metric].add(ok)
        if not ok:
            self.failing.setdefault(metric, []).append(item)

    def overall(self, metric: str) -> Tally:
        return self.groups["all"][metric]

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "family": self.family,
            "accuracy": {
                key: {
                    m: {"passed": t.passed, "total": t.total, "accuracy": round(t.ratio, 4)}
                    for m, t in tallies.items()
                }
                for key, tallies in self.groups.items()
            },
            "counts": self.counts,
            "failing": {m: self.failing.get(m, []) for m in self.metrics},
        }


def load(name: str) -> list[Record]:
    lines = (DATA / f"{name}.jsonl").read_text("utf-8").splitlines()
    return [cast("Record", json.loads(line)) for line in lines if line.strip()]


def _text(record: Record, key: str) -> str:
    return cast("str", record[key])


def _group_keys(record: Record, *extra: str) -> list[str]:
    keys = ["all"]
    keys += [f"{k}:{record[k]}" for k in ("split", "lang", "category")]
    return keys + list(extra)


# --- follow-ups -----------------------------------------------------------------------------


def _names(record: Record) -> tuple[str, ...]:
    return tuple(cast("list[str]", record.get("names", DEFAULT_NAMES)))


def _value(plan: Plan, name: str) -> FieldValue:
    if name == "time":
        return None if plan.time is None else (plan.time.start_ms, plan.time.end_ms)
    return frozenset(cast("tuple[str, ...]", getattr(plan, name)))


def _empty(value: FieldValue) -> bool:
    return value is None or value == frozenset()


def field_correct(gold: str, got: FieldValue, alone: FieldValue, earlier: FieldValue) -> bool:
    """The follow-up study's rule for one field (see the module docstring)."""
    if gold == "inherit":
        return got == earlier
    if gold == "own":
        return got == alone
    return _empty(got) or got == alone


def follow_up_fields(record: Record) -> dict[str, bool]:
    """Which of the four fields today's ``follow_up`` gets right for one labelled pair."""
    names = _names(record)
    earlier = planner.heuristic_plan(_text(record, "prev_question"), NOW_MS, names, TZ)
    alone = planner.heuristic_plan(_text(record, "question"), NOW_MS, names, TZ)
    merged = planner.follow_up(alone, earlier)
    gold = cast("dict[str, str]", record["gold"])
    return {
        f: field_correct(gold[f], _value(merged, f), _value(alone, f), _value(earlier, f))
        for f in FIELDS
    }


def score_follow_ups(name: str) -> SetScore:
    score = SetScore(name, "follow_ups", FOLLOW_UP_METRICS)
    for record in load(name):
        keys, item = _group_keys(record), _text(record, "id")
        fields = follow_up_fields(record)
        for f, ok in fields.items():
            score.record(keys, f, ok, item)
        score.record(keys, "pair", all(fields.values()), item)
    return score


# --- time windows ---------------------------------------------------------------------------


def window_of(question: str, now: datetime, model_time_text: str | None) -> Window | None:
    """Today's window for a question: the rule path, or the model path with a given time_text."""
    now_ms = int(now.timestamp() * 1000)
    if model_time_text is None:
        found = planner.heuristic_plan(question, now_ms, DEFAULT_NAMES, now.tzinfo).time
    else:
        raw = json.dumps(
            {
                "time_text": model_time_text,
                "people": [],
                "kinds": [],
                "keywords": [],
                "semantic_query": "",
            }
        )
        plan = planner.plan_from_model(raw, question, now_ms, DEFAULT_NAMES, now.tzinfo)
        found = plan.time
    if found is None:
        return None
    return (
        datetime.fromtimestamp(found.start_ms / 1000, UTC).astimezone(now.tzinfo),
        datetime.fromtimestamp(found.end_ms / 1000, UTC).astimezone(now.tzinfo),
    )


def window_passes(window: Window | None, gold: Window | None) -> bool:
    """Covers the gold span within 30 minutes on each edge and is at most twice as long."""
    if gold is None or window is None:
        return gold is None and window is None
    covers = window[0] <= gold[0] + TOLERANCE and window[1] >= gold[1] - TOLERANCE
    return covers and window[1] - window[0] <= 2 * (gold[1] - gold[0])


def score_time_windows(name: str) -> SetScore:
    score = SetScore(name, "time_windows", WINDOW_METRICS)
    for metric in WINDOW_METRICS:
        for key in ("future", "false_filter", "errors"):
            score.counts[f"{key}_{metric}"] = 0
    for record in load(name):
        question = _text(record, "question")
        model_text = cast("str | None", record["main_time_text"]) or ""
        for label in cast("list[dict[str, object]]", record["labels"]):
            now = datetime.fromisoformat(_text(label, "now"))
            raw_gold = cast("list[str] | None", label["gold"])
            gold = None if raw_gold is None else _window(raw_gold)
            keys = _group_keys(record, f"now:{label['now_label']}")
            item = f"{record['id']}@{label['ref_date']} {label['now_label']}"
            for metric in WINDOW_METRICS:
                try:
                    window = window_of(question, now, None if metric == "rules" else model_text)
                except Exception:  # noqa: BLE001 - a crash is a failed case, counted
                    window = None
                    score.counts[f"errors_{metric}"] += 1
                score.counts[f"future_{metric}"] += window is not None and window[0] >= now
                score.counts[f"false_filter_{metric}"] += window is not None and gold is None
                score.record(keys, metric, window_passes(window, gold), item)
    return score


def _window(span: list[str]) -> Window:
    return datetime.fromisoformat(span[0]), datetime.fromisoformat(span[1])


# --- report ---------------------------------------------------------------------------------

SCORERS: Final[dict[str, Callable[[str], SetScore]]] = {
    "follow_ups": score_follow_ups,
    "time_windows": score_time_windows,
}


def score_set(name: str) -> SetScore:
    return SCORERS[SETS[name]](name)


def _line(label: str, tallies: dict[str, Tally]) -> str:
    cells = "  ".join(f"{m} {t.ratio:6.1%}" for m, t in tallies.items())
    return f"  {label:28} n={next(iter(tallies.values())).total:<5} {cells}"


def report(score: SetScore, *, failures: bool) -> str:
    all_ = score.groups["all"]
    head = "  ".join(f"{m}={t.passed}/{t.total} ({t.ratio:.1%})" for m, t in all_.items())
    lines = [f"{score.name}: {head}"]
    if score.family == "follow_ups":
        lowest = min(all_[f].ratio for f in FIELDS)
        lines.append(f"  lowest field accuracy (the study's headline): {lowest:.3f}")
    if score.counts:
        lines.append("  " + " ".join(f"{k}={v}" for k, v in score.counts.items()))
    for prefix in ("split", "lang", "now", "category"):
        lines += [_line(k, t) for k, t in score.groups.items() if k.startswith(prefix + ":")]
    if failures:
        for metric in score.metrics:
            ids = score.failing.get(metric, [])
            lines.append(f"  failing {metric} ({len(ids)}): {' '.join(ids)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    parser.add_argument("--set", dest="sets", action="append", choices=sorted(SETS))
    parser.add_argument("--failures", action="store_true", help="print the failing ids")
    parser.add_argument("--output", type=Path, help="write the full result as JSON")
    args = parser.parse_args()
    names = cast("list[str] | None", args.sets) or list(SETS)
    scores = [score_set(name) for name in names]
    for score in scores:
        print(report(score, failures=cast("bool", args.failures)))
    output = cast("Path | None", args.output)
    if output is not None:
        payload = {"now": NOW.isoformat(), "sets": [s.to_json() for s in scores]}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        print(f"-> {output}")


if __name__ == "__main__":
    main()
