"""Labelled measurement contracts; no production filtering or access to personal history.

Labels here are invented regression cases. Public study caches remain regression evidence,
never independently corrected local calls or a newly unseen generalization set.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def source_hashes(root: Path) -> dict[str, str]:
    """Freeze candidate, fixture and public-study provenance before scoring."""
    paths = [
        *(
            p
            for p in root.joinpath("src/akashic").rglob("*")
            if p.suffix in {".py", ".sql", ".js", ".html", ".css"}
        ),
        *root.joinpath("scripts").glob("eval_*.py"),
        *root.joinpath("tests").rglob("*.py"),
        *root.joinpath("tests/data/planner").glob("*"),
    ]
    for study in (
        "thread-follow-up-field-inheritance",
        "conversational-time-windows",
        "segment-speaker-attribution",
        "transcript-hallucination-rejection",
    ):
        folder = root / "eval" / study
        paths.extend(
            p
            for p in folder.rglob("*")
            if p.is_file()
            and (
                p.name
                in {
                    "README.md",
                    "frozen.sha256",
                    "make_fixture.py",
                    "build_fixture.py",
                    "build_pairs.py",
                    "index.json",
                }
                or (p.suffix == ".jsonl" and "data" not in p.parts)
            )
        )
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(set(paths))
        if p.is_file()
    }


def attribution_counts(
    labels: dict[str, str | None], predictions: dict[str, str | None]
) -> dict[str, int]:
    """Same labelled clips, correct/wrong/unknown separately (mixed gold has no single author)."""
    if labels.keys() != predictions.keys():
        msg = "reference and candidate must cover identical labelled clip IDs"
        raise ValueError(msg)
    counts: dict[str, int] = dict.fromkeys(("correct", "wrong", "unknown", "total"), 0)
    for clip, gold in labels.items():
        heard = predictions[clip]
        key = "unknown" if heard is None else "correct" if heard == gold else "wrong"
        counts[key] += 1
        counts["total"] += 1
    return counts


@dataclass(frozen=True)
class Evidence:
    origin: str  # source recording identity; copies retain the same origin
    fact: str
    clear: bool
    author: str | None = None


@dataclass(frozen=True)
class EvidenceCase:
    key: str
    sources: tuple[Evidence, ...]
    fact: str = "the patch arrives Thursday"
    author: str | None = None


D9_CASES = (
    EvidenceCase(
        "flagged_only", (Evidence("recording-a", "the patch arrives Thursday", clear=False),)
    ),
    EvidenceCase(
        "clear_support", (Evidence("recording-a", "the patch arrives Thursday", clear=True),)
    ),
    EvidenceCase(
        "independent_corroboration",
        (
            Evidence("recording-a", "the patch arrives Thursday", clear=False),
            Evidence("recording-b", "the patch arrives Thursday", clear=True),
        ),
    ),
    EvidenceCase(
        "conflict",
        (
            Evidence("recording-a", "the patch arrives Thursday", clear=False),
            Evidence("recording-b", "the patch arrives Friday", clear=True),
        ),
    ),
    EvidenceCase(
        "duplicate_copies",
        (
            Evidence("recording-a", "the patch arrives Thursday", clear=False),
            Evidence("recording-a", "the patch arrives Thursday", clear=False),
        ),
    ),
    EvidenceCase(
        "uncertain_author",
        (Evidence("recording-a", "the patch arrives Thursday", clear=True),),
        author="Minlo",
    ),
)


def evidence_score(  # noqa: PLR0913 - independently labelled answer dimensions
    case: EvidenceCase,
    *,
    factual_claim: bool,
    possible_match: bool,
    recording_link: bool,
    cannot_answer: bool,
    conflict_disclosed: bool = False,
) -> dict[str, bool | int]:
    """Score labelled answer protection independently from STT detection/rejection.

    An evaluator labels ``factual_claim`` from the output, including hedged assertions. This
    intentionally does not pretend citation validity or a phrase regex proves entailment.
    """
    support = {
        s.origin
        for s in case.sources
        if s.clear and s.fact == case.fact and (case.author is None or s.author == case.author)
    }
    conflicts = any(s.fact != case.fact for s in case.sources)
    unsupported = factual_claim and not support
    protected = (
        not unsupported
        and (bool(support) or (possible_match and recording_link and cannot_answer))
        and (not conflicts or conflict_disclosed)
    )
    return {
        "unsupported_claims": int(unsupported),
        "answer_protected": protected,
        "independent_clear_support": len(support),
    }


def hallucination_counts(labels: dict[str, bool], outcomes: dict[str, str]) -> dict[str, int]:
    """Detection (unclear/reject), rejection and real-speech false rejections are distinct."""
    if labels.keys() != outcomes.keys() or set(outcomes.values()) - {"keep", "unclear", "reject"}:
        msg = "outcomes must cover the same labelled clips"
        raise ValueError(msg)
    return {
        "hallucinations": sum(labels.values()),
        "detected": sum(labels[k] and v != "keep" for k, v in outcomes.items()),
        "rejected": sum(labels[k] and v == "reject" for k, v in outcomes.items()),
        "real_rejected": sum(not labels[k] and v == "reject" for k, v in outcomes.items()),
        "real_unclear": sum(not labels[k] and v == "unclear" for k, v in outcomes.items()),
    }
