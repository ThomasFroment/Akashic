"""Explicit model download from the command line; Settings > Models downloads the same models.

    uv run python scripts\\fetch_models.py            # every pinned model
    uv run python scripts\\fetch_models.py --verify   # re-hash what is on disk, no network
    uv run python scripts\\fetch_models.py --data-dir D:\\akashic-data

Files are pinned to an exact repository commit and SHA-256 (src/akashic/pipeline/models.py),
downloaded over HTTPS, verified, and only then moved into place. Anything already present and
correct is skipped without touching the network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from akashic.config import Paths, default_data_dir
from akashic.pipeline import models

if TYPE_CHECKING:
    from collections.abc import Callable

MIB = 1024 * 1024


def _progress() -> Callable[[str, int, int], None]:
    last = {"name": "", "pct": -1}

    def show(name: str, done: int, total: int) -> None:
        pct = done * 100 // total
        if name != last["name"] or pct != last["pct"]:
            last.update(name=name, pct=pct)
            end = "\n" if done == total else "\r"
            print(f"  {name}: {pct:3d}%  ({done // MIB} / {total // MIB} MiB)", end=end, flush=True)

    return show


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--verify", action="store_true", help="check files on disk; no network")
    parser.add_argument("keys", nargs="*", help="model folder names (default: all)")
    args = parser.parse_args()
    paths = Paths(args.data_dir or default_data_dir())
    paths.ensure()
    known = {**models.REGISTRY, **models.CHAT_CANDIDATES}  # candidates only by explicit name
    specs = [known[k] for k in args.keys] if args.keys else list(models.REGISTRY.values())
    status = 0
    if not args.keys and not args.verify:  # the llama.cpp runtime comes with "everything"
        runtime = models.LLAMA_RUNTIME
        print(f"{runtime.key}: {runtime.description} ({runtime.total_size // MIB} MiB)")
        try:
            models.download_runtime(paths.runtime_dir, runtime, progress=_progress())
            print("  ready")
        except models.ModelDownloadError as exc:
            print(f"  FAILED: {exc}")
            status = 1
    for spec in specs:
        print(f"{spec.key}: {spec.description} ({spec.total_size // MIB} MiB, {spec.license})")
        if args.verify:
            bad = models.verify(paths.models_dir, spec)
            print("  OK" if not bad else f"  MISSING OR CORRUPT: {', '.join(bad)}")
            status |= bool(bad)
            continue
        try:
            fetched = models.download(paths.models_dir, spec, progress=_progress())
        except models.ModelDownloadError as exc:
            print(f"  FAILED: {exc}")
            status = 1
            continue
        print(f"  downloaded {len(fetched)} file(s)" if fetched else "  already present, verified")
    return status


if __name__ == "__main__":
    sys.exit(main())
