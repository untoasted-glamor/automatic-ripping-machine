"""Make `.srt` files from PGS sidecars by hand: `python -m arm_transcode.subtitle_ocr <sup or folder>...`.

A folder means every `*.sup` in it and below. An existing `.srt` is kept unless
`--overwrite`. Prints one JSON line of stats per file.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from arm_transcode.subtitle_ocr import engine, run


def sidecars(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        found.extend(sorted(path.rglob("*.sup")) if path.is_dir() else [path])
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m arm_transcode.subtitle_ocr")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--overwrite", action="store_true", help="replace an existing .srt")
    parser.add_argument("--threads", type=int, default=1, help="ONNX Runtime threads (default 1)")
    args = parser.parse_args(argv)

    reader = engine.OnnxLineReader(threads=args.threads)
    failed = 0
    for sidecar in sidecars(args.paths):
        target = run.srt_path_for(sidecar)
        if target.exists() and not args.overwrite:
            print(json.dumps({"sidecar": str(sidecar), "skipped": "srt exists"}), flush=True)
            continue
        try:
            written, stats = run.convert(sidecar, reader)
        except Exception as exc:  # report and go on with the next file
            failed += 1
            print(json.dumps({"sidecar": str(sidecar), "error": f"{type(exc).__name__}: {exc}"}), flush=True)
            continue
        print(json.dumps({"sidecar": str(sidecar), "srt": str(written), **dataclasses.asdict(stats)}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
