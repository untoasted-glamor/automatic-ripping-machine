"""Ripping a job's remaining titles after the rip (track-select plan S15).

Shared by POST /api/jobs/{id}/retry-failed and the Disc menu panel's "Rip it"
(fork only, `arm_backend.disc_menus.rip`): which jobs can rip titles again,
which titles are left, and arming the retry. With a finished disc backup kept
in `/raw/<job_id>/.backup/`, the job's drive's ripper is told (`rip.retry`) to
remux them from it, with no disc; otherwise the retry starts when the disc
goes back in that drive.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.raw_backup import describe_backup
from arm_backend.ws import WSHub
from arm_common import Drive, Job, JobStatus, TrackStatus
from arm_common.enums import NON_TERMINAL_JOB_STATUSES
from arm_common.models import Track
from arm_common.models._columns import enum_value_str
from arm_common.schemas.job_metadata import with_flags

RETRYABLE_JOB_STATUSES = frozenset({JobStatus.RIPPED, JobStatus.RIPPED_PARTIAL, JobStatus.FAILED})


class RetryRefused(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def check_retryable(db: AsyncSession, job: Job) -> None:
    """Raise RetryRefused (409) unless the job is ripped, ripped_partial or failed with a stored scan, ripped on a
    physical drive that is still enrolled and isn't busy with another disc."""
    if job.status not in RETRYABLE_JOB_STATUSES:
        raise RetryRefused(
            status.HTTP_409_CONFLICT,
            f"only a ripped, ripped_partial or failed job can rip its remaining titles, got {enum_value_str(job.status)}",
        )
    if not (job.metadata_json or {}).get("scan_result"):
        raise RetryRefused(status.HTTP_409_CONFLICT, "job has no stored disc scan to retry from")
    drive = (await db.execute(select(Drive).where(col(Drive.id) == job.drive_id))).scalar_one_or_none()
    if drive is None or drive.source_kind is not None:
        raise RetryRefused(
            status.HTTP_409_CONFLICT,
            "the job wasn't ripped on a drive that is still enrolled; rip its source again instead",
        )
    active = (
        (
            await db.execute(
                select(Job)
                .where(col(Job.drive_id) == job.drive_id)
                .where(col(Job.status).in_(tuple(NON_TERMINAL_JOB_STATUSES)))
            )
        )
        .scalars()
        .all()
    )
    busy = next((j for j in active if j.id != job.id), None)
    if busy is not None:
        raise RetryRefused(
            status.HTTP_409_CONFLICT,
            f"the drive is busy with another disc (job {busy.id}); retry once it is done",
        )


def remaining_tracks(tracks: Sequence[Track]) -> list[Track]:
    """The kept tracks with no file: failed, or left queued. A skipped disc menu is finished and never comes back;
    a failed or newly ticked one is retried like a title, alone too."""
    return [
        t
        for t in tracks
        if not t.excluded and (t.status == TrackStatus.FAILED or (t.status == TrackStatus.QUEUED and not t.output_path))
    ]


async def arm_retry(db: AsyncSession, job: Job, hub: WSHub, remaining: Sequence[Track]) -> bool:
    """Queue `remaining` again (errors cleared), arm the job (`retry_armed`) and, with a finished disc backup
    kept, tell the drive's ripper to remux them from it. Doesn't commit. Returns whether it rips from the
    backup."""
    for track in remaining:
        track.status = TrackStatus.QUEUED
        track.last_error = None
        db.add(track)
    job.metadata_json = with_flags(job.metadata_json, retry_armed=True)
    db.add(job)
    backup = describe_backup(job.id)
    from_backup = backup is not None and backup.complete
    if from_backup:
        await hub.emit(
            topic=f"ripper.commands.{job.drive_id}",
            event_type="rip.retry",
            payload={"job_id": job.id, "drive_id": job.drive_id},
            job_id=job.id,
            session=db,
        )
    return from_backup
