"""Trace invented early/late facts through real episode construction and local embeddings.

Never opens the user's database. Only installed model/runtime files are read. Results measure
retrieval and source text availability, not generated answer correctness or real ASR quality.
Run with --keywords-only to omit model inference.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from eval_ask import disposable_connection, embedding_server, index_memory

from akashic.config import DEFAULT_EMBEDDING_MODEL, Paths, default_data_dir
from akashic.pipeline.embed import EmbedHandler
from akashic.pipeline.episodes import build_episodes
from akashic.pipeline.jobs import Job
from akashic.pipeline.llm import LlamaEmbedder
from akashic.search.answer import MAX_LINES, build_sources
from akashic.search.hybrid import Results, episode_lines, match_any, search_memory
from akashic.search.planner import Plan
from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

MINUTE = 60_000
START = 1_794_139_200_000
PASSAGE_CHARS = 400
PASSAGE_STEP = 300
FULL_DIMS = 1024


@dataclass(frozen=True)
class Fact:
    key: str
    segment: int
    text: str
    keyword: str
    question: str


def add_segment(conn: Connection, start: int, text: str) -> int:
    row = conn.execute(
        "INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, status,"
        " transcript, speaker_id) VALUES ('system', ?, ?, 4000, '[\"discord.exe\"]', 'done', ?, 1)",
        (start, start + 4000, text),
    )
    if row.lastrowid is None:
        raise RuntimeError
    conn.execute(
        "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms)"
        " VALUES (?, 1, 0, 4000, 4000)",
        (row.lastrowid,),
    )
    return row.lastrowid


def seed(conn: Connection) -> list[Fact]:
    conn.execute("INSERT INTO speakers (id, name, created_ms) VALUES (1, 'Minlo', 0)")
    gold = {
        0: (
            "early",
            "Mon train pour Lyon part à huit heures quinze.",
            "Lyon",
            "À quelle heure est le départ ferroviaire vers la capitale des Gaules ?",
        ),
        35: (
            "middle",
            "My veterinary appointment is Tuesday at three in the afternoon.",
            "veterinary",
            "When will the doctor examine your pet?",
        ),
        71: (
            "late",
            "Ashfall Frontier sort samedi, avec quatre joueurs en coop.",
            "Ashfall",
            "When can we play that survival title together and how many people can join?",
        ),
    }
    facts: list[Fact] = []
    for i in range(72):
        filler = (
            f"Tour {i}. Nous vérifions les paramètres audio, le niveau du microphone et la "
            "connexion. The sound is clear now; let us continue checking the volume settings."
        )
        text = gold[i][1] if i in gold else filler
        segment = add_segment(conn, START + i * MINUTE, text)
        if i in gold:
            key, text, keyword, question = gold[i]
            facts.append(Fact(key, segment, text, keyword, question))
    # Identical first 2,000 characters; only the late topic differs. Keep whole transcripts
    # to distinguish embedding input loss from answer excerpt loss.
    prefix = "Nous réglons encore le volume. We are checking the microphone. " * 50
    for i, (key, text, keyword, question) in enumerate(
        (
            (
                "long_rail",
                "The sleeper train to Oslo departs at midnight.",
                "Oslo",
                "When does the overnight railway service leave for the Norwegian capital?",
            ),
            (
                "long_pet",
                "Le vétérinaire examinera le chat jeudi à neuf heures.",
                "veterinaire",
                "Quand le médecin pour animaux doit-il voir le félin ?",
            ),
        )
    ):
        segment = add_segment(conn, START + (180 + i * 60) * MINUTE, prefix + text)
        facts.append(Fact(key, segment, text, keyword, question))
    return facts


def evaluate(conn: Connection, facts: list[Fact], model: LlamaEmbedder | None) -> None:
    print(
        json.dumps(
            {
                "model": model.model if model else None,
                "episode_body_chars": [
                    len(str(r[0])) for r in conn.execute("SELECT body FROM episodes ORDER BY id")
                ],
                "linked_segments": int(
                    conn.execute(
                        "SELECT count(*) FROM episode_items WHERE item_kind = 'segment'"
                    ).fetchone()[0]
                ),
            }
        ),
        flush=True,
    )
    for fact in facts:
        episode, body = conn.execute(
            "SELECT e.id, e.body FROM episodes e JOIN episode_items i ON i.episode_id = e.id"
            " WHERE i.item_kind = 'segment' AND i.item_id = ?",
            (fact.segment,),
        ).fetchone()
        indexed = bool(
            conn.execute(
                "SELECT rowid FROM episodes_fts WHERE rowid = ? AND episodes_fts MATCH ?",
                (episode, match_any((fact.keyword,))),
            ).fetchone()
        )
        for mode in ("keywords", "semantic", "hybrid") if model else ("keywords",):
            query = model.embed_query(fact.question) if model and mode != "keywords" else None
            found = search_memory(
                conn,
                Plan(people=("Minlo",), keywords=() if mode == "semantic" else (fact.keyword,)),
                query,
                DEFAULT_EMBEDDING_MODEL,
            )
            rank = next(
                (n for n, line in enumerate(found.lines, 1) if line.segment_id == fact.segment),
                None,
            )
            sources = build_sources(found)
            print(
                json.dumps(
                    {
                        "fact": fact.key,
                        "mode": mode,
                        "digest_contains": fact.text in body,
                        "episode_keyword_indexed": indexed,
                        "line_rank": rank,
                        "gold_line_source": any(
                            s.kind == "line" and s.ref_id == fact.segment and fact.text in s.text
                            for s in sources
                        ),
                        "gold_any_source": any(fact.text in s.text for s in sources),
                        "gold_episode_source": any(
                            s.kind == "episode" and s.ref_id == episode and fact.text in s.text
                            for s in sources
                        ),
                    }
                ),
                flush=True,
            )


def passage_probe(conn: Connection, facts: list[Fact], model: LlamaEmbedder) -> None:
    """In-memory spike, not production search: all fixture speakers/times are eligible.

    Compare per-segment average vectors with max-passage ranking on exactly the same windows.
    Carry the winning window into the existing source builder to isolate excerpt loss too.
    """
    passages: list[tuple[int, str]] = []
    for segment, transcript in conn.execute(
        "SELECT id, transcript FROM audio_segments ORDER BY id"
    ):
        text = str(transcript)
        passages.extend(
            (int(segment), text[start : start + PASSAGE_CHARS])
            for start in range(0, len(text), PASSAGE_STEP)
        )
    began = time.perf_counter()
    # Match the production float16 storage precision, then score in float32.
    vectors = np.asarray(model.embed_documents([text for _, text in passages]), dtype=np.float16)
    matrix = vectors.astype(np.float32)
    print(
        json.dumps(
            {
                "prototype": "passages",
                "passage_count": len(passages),
                "segment_count": int(
                    conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0]
                ),
                "chars": PASSAGE_CHARS,
                "step": PASSAGE_STEP,
                "vector_bytes": vectors.nbytes,
                "embedding_seconds": round(time.perf_counter() - began, 3),
            }
        ),
        flush=True,
    )
    lines = {
        line.segment_id: line
        for (episode,) in conn.execute("SELECT id FROM episodes")
        for line in episode_lines(conn, int(episode), 1000)
    }
    groups: dict[int, list[int]] = {}
    for i, (segment, _) in enumerate(passages):
        groups.setdefault(segment, []).append(i)
    means = {segment: np.mean(matrix[indices], axis=0) for segment, indices in groups.items()}
    means = {
        segment: vector / max(float(np.linalg.norm(vector)), 1e-12)
        for segment, vector in means.items()
    }
    for fact in facts:
        query = model.embed_query(fact.question)
        scores = matrix @ query
        best = {
            segment: max(indices, key=lambda i: float(scores[i]))
            for segment, indices in groups.items()
        }
        for method in ("mean", "max"):
            ordered = sorted(
                groups,
                key=lambda segment: (
                    -float(np.dot(means[segment], query))
                    if method == "mean"
                    else -float(scores[best[segment]]),
                    segment,
                ),
            )
            hits = [
                replace(lines[segment], text=passages[best[segment]][1]) for segment in ordered[:10]
            ]
            sources = build_sources(Results(lines=hits, semantic=True))
            prefix_sources = build_sources(
                Results(
                    lines=[lines[segment] for segment in ordered[:10]],
                    semantic=True,
                )
            )
            print(
                json.dumps(
                    {
                        "fact": fact.key,
                        "mode": f"passage_{method}",
                        "line_rank": ordered.index(fact.segment) + 1,
                        "gold_line_source": any(
                            s.ref_id == fact.segment and fact.text in s.text for s in sources
                        ),
                        "gold_prefix_source": any(
                            s.ref_id == fact.segment and fact.text in s.text for s in prefix_sources
                        ),
                    }
                ),
                flush=True,
            )


def dimension_probe(conn: Connection, facts: list[Fact], full: LlamaEmbedder) -> None:
    """Compare dimensions/precision from identical full outputs, holding passages constant."""
    windows = [
        (int(segment), str(text)[start : start + PASSAGE_CHARS])
        for segment, text in conn.execute("SELECT id, transcript FROM audio_segments ORDER BY id")
        for start in range(0, len(str(text)), PASSAGE_STEP)
    ]
    raw = np.asarray(full.embed_documents([text for _, text in windows]), dtype=np.float32)
    if raw.shape[1] != FULL_DIMS:
        msg = "Expected 1024-dimensional model output for comparison"
        raise ValueError(msg)
    for fact in facts:
        query_full = full.embed_query(fact.question)
        for dims, precision in ((256, "float16"), (1024, "float16"), (1024, "float32")):
            matrix = raw[:, :dims].copy()
            matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
            stored = matrix.astype(precision)
            query = query_full[:dims].copy()
            query /= max(float(np.linalg.norm(query)), 1e-12)
            scores = stored.astype(np.float32) @ query
            best: dict[int, int] = {}
            for i, (segment, _) in enumerate(windows):
                if segment not in best or scores[i] > scores[best[segment]]:
                    best[segment] = i
            ordered = sorted(best, key=lambda segment: (-float(scores[best[segment]]), segment))
            rank = ordered.index(fact.segment) + 1
            print(
                json.dumps(
                    {
                        "fact": fact.key,
                        "mode": "dimension_comparison",
                        "dimensions": dims,
                        "precision": precision,
                        "line_rank": rank,
                        "gold_in_top6_passage": rank <= MAX_LINES
                        and fact.text in windows[best[fact.segment]][1],
                        "vector_bytes": stored.nbytes,
                    }
                ),
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keywords-only", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    paths = Paths(args.data_dir or default_data_dir())
    with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
        work = Path(tmp)
        conn = stack.enter_context(closing(disposable_connection(work / "eval.db")))
        with db.transaction(conn):
            facts = seed(conn)
        build_episodes(conn, START + 360 * MINUTE)
        if args.keywords_only:
            evaluate(conn, facts, None)
            return
        server, model = stack.enter_context(embedding_server(paths, work))
        index_memory(conn, model, START + 360 * MINUTE)
        evaluate(conn, facts, model)
        passage_probe(conn, facts, model)
        dimension_probe(
            conn, facts, LlamaEmbedder(server, DEFAULT_EMBEDDING_MODEL, dimensions=1024)
        )
        print(json.dumps({"stage": "production_persisted_passages"}), flush=True)
        ids = [int(r[0]) for r in conn.execute("SELECT id FROM audio_segments")]
        EmbedHandler(conn, model, lambda: True).run(
            Job(
                1,
                "embed",
                "evaluation",
                {"kind": "segment", "ids": ids, "model": DEFAULT_EMBEDDING_MODEL},
                START,
                1,
            )
        )
        evaluate(conn, facts, model)


if __name__ == "__main__":
    main()
