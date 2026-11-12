"""Baseline candidate: today's repo path, unchanged.

Rule path: ``planner.heuristic_plan(question, now_ms, names, tz).time`` (which calls
``timeparse.resolve`` on the whole question). Model path: ``planner.plan_from_model`` with a JSON
reply whose ``time_text`` is ``model_time_text``; as at planner.py:275-280, the model's time text
is only used when the rules already found a time, and it then replaces the rule window.

No dependency needs stubbing: planner.py imports only the standard library and timeparse.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from akashic.search.planner import heuristic_plan, plan_from_model  # noqa: E402

NAMES = ("Minlo", "Sacha", "Léa")


def _span(plan_time, tz):  # noqa: ANN001, ANN202
    if plan_time is None:
        return None
    return (
        datetime.fromtimestamp(plan_time.start_ms / 1000, tz),
        datetime.fromtimestamp(plan_time.end_ms / 1000, tz),
    )


def resolve_question(question: str, now: datetime, model_time_text: str | None):  # noqa: ANN201
    now_ms = int(now.timestamp() * 1000)
    if model_time_text is None:
        return _span(heuristic_plan(question, now_ms, NAMES, now.tzinfo).time, now.tzinfo)
    raw = json.dumps(
        {"time_text": model_time_text, "people": [], "kinds": [], "keywords": [],
         "semantic_query": ""}
    )  # fmt: skip
    return _span(plan_from_model(raw, question, now_ms, NAMES, now.tzinfo).time, now.tzinfo)
