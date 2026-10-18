"""A job's raw disc backup, `/raw/<job_id>/.backup/`.

When only some of a disc's titles are kept, the ripper reads the disc once
into this folder (an encrypted backup: one ISO image on DVD, a folder on
Blu-ray) and remuxes the kept titles from it (`arm_ripper.rip.dispatcher`).
It deletes the backup itself once every kept title has been remuxed. If one
failed, the backup stays so a retry needs no disc, until the job is deleted
(the whole job dir goes) or the operator deletes it here. Transcoding never
reads it: a transcode, or a retry of one, reads the remuxed `.mkv`.

The Backend mounts `/raw` read-write (as for job delete), so it can report
and remove the backup without the ripper.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from arm_backend.config import settings
from arm_common.schemas import RawBackupView

logger = logging.getLogger("arm_backend.raw_backup")

# Must match arm_ripper.rip.dispatcher.BACKUP_DIRNAME / BACKUP_COMPLETE_MARKER.
BACKUP_DIRNAME = ".backup"
COMPLETE_MARKER = ".complete"


def backup_dir(job_id: str) -> Path:
    # basename: a crafted id can never point outside the raw root.
    return Path(settings.RAW_ROOT) / os.path.basename(job_id) / BACKUP_DIRNAME


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def describe_backup(job_id: str) -> RawBackupView | None:
    """The job's backup if there is one: its size, and whether it finished
    (a backup still being written, or one cut short, has no marker)."""
    target = backup_dir(job_id)
    if not target.is_dir():
        return None
    return RawBackupView(size_bytes=_tree_bytes(target), complete=(target / COMPLETE_MARKER).is_file())


def delete_backup(job_id: str) -> bool:
    """Remove the job's backup. False if there was none; OSError if it can't be removed."""
    target = backup_dir(job_id)
    if not target.exists():
        return False
    shutil.rmtree(target)
    logger.info("deleted the raw backup of job_id=%s at %s", job_id, target)
    return True
