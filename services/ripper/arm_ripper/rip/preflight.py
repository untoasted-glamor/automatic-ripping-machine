"""Disk-space check before a disc backup (track-select plan, S8).

A backup-then-remux rip needs room for the whole disc (the backup) plus the
kept titles (the remuxes), and a Blu-ray backup alone is ~25–50 GB. Running
out mid-backup wastes an hour of drive time and leaves `/raw` full, so the
ripper checks first and fails the kept tracks with a clear message instead.

The backup is about the disc's used size, which the scan doesn't state. The
titles' sizes overstate it whenever titles share clips: a Blu-ray play-all
plays the episodes' clips again (Hanako-kun: titles add up to 92.5 GB, the
backup was 46.5 GB). So on Blu-ray a title whose clips (TINFO 26) are all in
titles already counted adds nothing. DVD cell maps don't compare across
titles, so a DVD counts every title, capped at a dual-layer disc's size.

Free space is what the filesystem reports, so backups other jobs keep under
`/raw/<job_id>/.backup/` (a job whose titles didn't all remux keeps its
backup for a retry) are already accounted for; the message names them,
since deleting one is the quickest way to make room.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from arm_common import DiscType
from arm_common.schemas import ScanTitle

logger = logging.getLogger("arm_ripper.rip.preflight")

# The estimate is rough (filesystem overhead, MakeMKV's own files in a
# Blu-ray backup); leave a margin.
BACKUP_SIZE_MARGIN = 1.05
# A dual-layer DVD (DVD-9) holds at most this much.
DVD_MAX_BYTES = 8_547_991_552
_GB = 1_000_000_000


def _clips(title: ScanTitle) -> frozenset[int] | None:
    """A Blu-ray title's clips from its segment map (`14,15,16`), or None."""
    if not title.segment_map:
        return None
    try:
        return frozenset(int(part) for part in title.segment_map.split(","))
    except ValueError:
        return None


def estimate_backup_bytes(titles: list[ScanTitle], disc_type: DiscType) -> int:
    """About how much `makemkvcon backup` will write for this disc."""
    sized = sorted((t for t in titles if t.size_bytes), key=lambda t: t.size_bytes or 0, reverse=True)
    if not sized:
        return 0
    largest = sized[0].size_bytes or 0
    if disc_type != DiscType.BLURAY:
        return max(largest, min(sum(t.size_bytes or 0 for t in sized), DVD_MAX_BYTES))
    counted: set[int] = set()
    total = 0
    for title in sized:
        clips = _clips(title)
        if clips is not None and clips <= counted:
            continue  # e.g. an episode the play-all already holds
        total += title.size_bytes or 0
        if clips is not None:
            counted |= clips
    return max(largest, total)


def _tree_bytes(path: Path) -> int:
    total = 0
    if path.is_file():
        return path.stat().st_size
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def kept_backups(raw_root: Path, *, backup_dirname: str, exclude: Path | None = None) -> dict[str, int]:
    """Bytes of each other job's kept backup under `raw_root`, by job id."""
    found: dict[str, int] = {}
    try:
        job_dirs = [p for p in raw_root.iterdir() if p.is_dir() and p != exclude]
    except OSError:
        return found
    for job_dir in job_dirs:
        backup = job_dir / backup_dirname
        if backup.exists():
            found[job_dir.name] = _tree_bytes(backup)
    return found


def _gb(n: int) -> str:
    return f"{n / _GB:.1f} GB"


def check_backup_space(
    output_dir: Path,
    *,
    titles: list[ScanTitle],
    disc_type: DiscType,
    kept_bytes: int,
    backup_dirname: str,
) -> str | None:
    """None if `output_dir`'s filesystem has room for this disc's backup plus
    `kept_bytes` of remuxes, else the error to fail the kept tracks with."""
    backup_bytes = estimate_backup_bytes(titles, disc_type)
    need = int(backup_bytes * BACKUP_SIZE_MARGIN) + kept_bytes
    output_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output_dir).free
    raw_root = output_dir.parent
    if free >= need:
        logger.info("space for the backup: need ~%s, %s free on %s", _gb(need), _gb(free), raw_root)
        return None
    message = (
        f"not enough space on {raw_root}: need ~{_gb(need)} "
        f"(disc backup ~{_gb(backup_bytes)} + kept titles ~{_gb(kept_bytes)}), have {_gb(free)} free"
    )
    others = kept_backups(raw_root, backup_dirname=backup_dirname, exclude=output_dir)
    if others:
        listed = ", ".join(f"{job} ({_gb(size)})" for job, size in sorted(others.items(), key=lambda kv: -kv[1]))
        message += (
            f"; {len(others)} other job(s) keep a disc backup in <job>/{backup_dirname}/ "
            f"for retrying failed titles, ~{_gb(sum(others.values()))} in all: {listed}"
        )
    return message
