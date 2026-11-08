"""A `subtitle_ocr` transcode task: one `.srt` beside each image subtitle sidecar.

Each sidecar is read in its own `python -m arm_transcode.subtitle_ocr`
process, so a cancel (or a revoked claim) can kill the reader at once, as the
encoders kill HandBrake or ffmpeg. The reader writes `<name>.srt` through
`*.arm-inprogress` and prints one JSON line of stats or of its error.

A failed sidecar fails the task: the `.srt` files already written are kept
and reported (`SubtitleOcrFailed.written`). A cancel removes them, since the
task's row goes with it and nothing would record them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from arm_common.fileops import INPROGRESS_SUFFIX

from arm_transcode.subtitle_ocr.run import srt_path_for

logger = logging.getLogger("arm_transcode.subtitle_ocr.task")

# ONNX Runtime threads per reader; the accuracy test ran on one core, and the
# dispatcher's ARM_SUBTITLE_OCR_MAX_PARALLEL assumes one core per task.
THREADS = 1
_ERROR_TAIL_LINES = 20


class SubtitleOcrFailed(Exception):
    """One sidecar could not be read. `written` are the `.srt` files finished
    before it, left in place."""

    def __init__(self, message: str, written: list[Path]) -> None:
        super().__init__(message)
        self.written = written


def reader_command(sidecar: Path, threads: int = THREADS) -> list[str]:
    return [sys.executable, "-m", "arm_transcode.subtitle_ocr", "--overwrite", "--threads", str(threads), str(sidecar)]


def _inprogress(path: Path) -> Path:
    return path.with_name(path.name + INPROGRESS_SUFFIX)


def _report(stdout: bytes) -> dict[str, object]:
    """The reader's last JSON line, or {} when it printed none."""
    for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
        with contextlib.suppress(ValueError):
            report = json.loads(line)
            if isinstance(report, dict):
                return report
    return {}


async def read_sidecar(sidecar: Path) -> Path:
    """Write the `.srt` of one sidecar; return its path. Raises RuntimeError
    with the reader's error when it fails."""
    proc = await asyncio.create_subprocess_exec(
        *reader_command(sidecar),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        _inprogress(srt_path_for(sidecar)).unlink(missing_ok=True)
        raise
    report = _report(stdout)
    if proc.returncode != 0 or "error" in report or "srt" not in report:
        detail = report.get("error") or "\n".join(stderr.decode("utf-8", "replace").splitlines()[-_ERROR_TAIL_LINES:])
        raise RuntimeError(f"{sidecar.name}: subtitle OCR failed (rc={proc.returncode}): {detail}")
    logger.info("subtitle OCR done %s", json.dumps(report))
    return Path(str(report["srt"]))


async def make_text_subtitles(
    sidecars: Sequence[Path],
    *,
    on_progress: Callable[[int], None],
    reader: Callable[[Path], Awaitable[Path]] = read_sidecar,
) -> list[Path]:
    """Read every sidecar in turn and return the `.srt` files written.

    `reader` is `read_sidecar` but for tests."""
    written: list[Path] = []
    try:
        for done, sidecar in enumerate(sidecars):
            on_progress(100 * done // len(sidecars))
            try:
                written.append(await reader(sidecar))
            except Exception as exc:  # a cancel is a BaseException and passes through
                raise SubtitleOcrFailed(str(exc), written) from exc
    except asyncio.CancelledError:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    on_progress(100)
    return written
