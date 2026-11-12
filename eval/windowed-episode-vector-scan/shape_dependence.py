"""Does a row's float32 score depend on the matrix it is computed in?

Question: is (M[:n] @ q)[i] bitwise equal to (M[i:i+1] @ q)[0] for n in 1..4000 (all i < n)?
M is built exactly like embeddings.top builds a chunk (np.vstack of float16 rows, .astype
float32, C-contiguous), from the first 4,000 valid stored vectors of the cached DB. Also checks
position dependence (the same row at different offsets of a 4,000-row matrix, as when the
filtered rows of a chunk change) and run-to-run determinism. Writes shape.txt next to this file.

Run: python shape_dependence.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_db  # noqa: E402

N = 4000


def ulps(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """|a-b| in float32 ULPs via the ordered integer representation."""
    def ordered(x):
        i = x.astype(np.float32).view(np.int32).astype(np.int64)
        return np.where(i < 0, -(i & 0x7FFFFFFF), i)

    return np.abs(ordered(a) - ordered(b))


def main() -> None:
    build_db.ensure()
    conn = build_db.connect()
    rows = conn.execute(
        "SELECT owner_id, vec FROM embeddings WHERE owner_kind = 'episode' AND model = ?"
        " ORDER BY start_ms, rowid",
        (build_db.MODEL,),
    ).fetchall()
    rows = [r for r in rows if len(r[1]) == build_db.DIMS * 2][: N + 64]
    m_all = np.vstack([np.frombuffer(bytes(r[1]), dtype=np.float16) for r in rows]).astype(
        np.float32
    )
    m = np.ascontiguousarray(m_all[:N])
    rng = np.random.default_rng(3)
    out = [f"numpy {np.__version__}; BLAS: "
           f"{np.show_config(mode='dicts')['Build Dependencies']['blas']['openblas configuration']}"]  # fmt: skip
    for qname, q in (
        ("random", rng.standard_normal(build_db.DIMS)),
        ("stored+noise", m[17] + 0.05 * rng.standard_normal(build_db.DIMS)),
    ):
        q = np.ascontiguousarray((q / np.linalg.norm(q)).astype(np.float32))
        single = np.array([(m[i : i + 1] @ q)[0] for i in range(N)], dtype=np.float32)
        pairs = mism = 0
        n_with = []
        max_ulp = 0
        max_abs = 0.0
        hist: dict[int, int] = {}
        for n in range(1, N + 1):
            s = m[:n] @ q
            bad = s.view(np.uint32) != single[:n].view(np.uint32)
            pairs += n
            k = int(bad.sum())
            if k:
                mism += k
                n_with.append(n)
                u = ulps(s[bad], single[:n][bad])
                max_ulp = max(max_ulp, int(u.max()))
                max_abs = max(max_abs, float(np.abs(s[bad] - single[:n][bad]).max()))
                for v in u.tolist():
                    hist[v] = hist.get(v, 0) + 1
        out.append(
            f"[{qname}] (M[:n] @ q)[i] vs (M[i:i+1] @ q)[0], n=1..{N}, all i<n: "
            f"{mism:,} of {pairs:,} pairs differ bitwise ({mism / pairs:.2%}); "
            f"n with any difference: {len(n_with)} of {N} (first: {n_with[:12]}); "
            f"max ulp {max_ulp}, max |diff| {max_abs:.3e}; ulp histogram {dict(sorted(hist.items()))}"
        )
        # Distinct values a row takes across all n >= i+1 (shape dependence, not vs single).
        distinct = np.ones(N, dtype=np.int64)
        seen = [{int(single[i].view(np.uint32))} for i in range(N)]
        for n in range(1, N + 1):
            s = (m[:n] @ q).view(np.uint32)
            for i in np.flatnonzero(s != single[:n].view(np.uint32)):
                seen[i].add(int(s[i]))
        distinct = np.array([len(x) for x in seen])
        out.append(f"[{qname}] distinct float32 values per row over all shapes (incl. 1-row): "
                   f"max {distinct.max()}, rows with >1 value: {(distinct > 1).sum()} of {N}")  # fmt: skip
        # Position dependence: the same rows shifted by j inside a 4000-row matrix.
        full = m @ q
        shifted_bad = 0
        shifted_max = 0
        for j in range(1, 33):
            s = np.ascontiguousarray(m_all[j : j + N]) @ q
            a, b = s[: N - j], full[j:]
            bad = a.view(np.uint32) != b.view(np.uint32)
            shifted_bad += int(bad.sum())
            if bad.any():
                shifted_max = max(shifted_max, int(ulps(a[bad], b[bad]).max()))
        out.append(f"[{qname}] same row at offsets shifted by 1..32 in a {N}-row matrix: "
                   f"{shifted_bad:,} of {sum(N - j for j in range(1, 33)):,} differ, "
                   f"max ulp {shifted_max}")  # fmt: skip
        # Determinism: same call 20 times.
        reps = {(m @ q).tobytes() for _ in range(20)}
        out.append(f"[{qname}] M[:4000] @ q repeated 20x: {len(reps)} distinct result(s)")
        # float64 reference error of the float32 products, for scale.
        ref = m.astype(np.float64) @ q.astype(np.float64)
        out.append(f"[{qname}] |float32 4000-row score - float64| max {np.abs(full - ref).max():.3e}")
    text = "\n".join(out) + "\n"
    (HERE / "shape.txt").write_text(text, "utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
