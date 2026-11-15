"""Ripping a job's remaining titles after the rip (track-select plan S15).

Used by POST /api/jobs/{id}/retry-failed: which jobs can rip titles again,
which titles are left, and arming the retry. With a finished disc backup kept
in `/raw/<job_id>/.backup/`, the job goes to `remuxing` and the job's drive's
ripper is told (`rip.retry`) to remux them from it, with no disc, beside a rip
of whatever disc is in the drive; otherwise the retry starts when the disc goes
back in that drive.
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

# Stop 2 (`awaiting_titles`, fork only) starts here too: its ticked titles are remuxed from the backup.
RETRYABLE_JOB_STATUSES = frozenset(
    {JobStatus.RIPPED, JobStatus.RIPPED_PARTIAL, JobStatus.FAILED, JobStatus.AWAITING_TITLES}
)


class RetryRefused(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _has_complete_backup(job: Job) -> bool:
    backup = describe_backup(job.id)
    return backup is not None and backup.complete


async def check_retryable(db: AsyncSession, job: Job) -> None:
    """Raise RetryRefused (409) unless the job is ripped, ripped_partial or failed (or waits at stop 2) with a stored
    scan, ripped on a physical drive that is still enrolled. A retry that needs the disc (no finished backup kept)
    also needs the drive free of another disc; a remux from the backup runs beside one. Stop 2 only remuxes."""
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
    if _has_complete_backup(job):
        return
    if job.status == JobStatus.AWAITING_TITLES:
        raise RetryRefused(
            status.HTTP_409_CONFLICT,
            "the job's disc backup is gone; abandon it and rip the disc again",
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
    kept, set it `remuxing` and tell the drive's ripper to remux them from it. Doesn't commit. Returns whether it
    rips from the backup.

    Stop 2's start isn't armed as a retry: its titles have had no pass yet, so rip-start answers `retry=false`
    and the files land in the job's rip folder, not in `retry-N/`."""
    for track in remaining:
        track.status = TrackStatus.QUEUED
        track.last_error = None
        db.add(track)
    if job.status != JobStatus.AWAITING_TITLES:
        job.metadata_json = with_flags(job.metadata_json, retry_armed=True)
    from_backup = _has_complete_backup(job)
    if from_backup:
        # Off the drive: the drive's next disc gets its own job meanwhile.
        job.status = JobStatus.REMUXING
    db.add(job)
    if from_backup:
        await hub.emit(
            topic=f"ripper.commands.{job.drive_id}",
            event_type="rip.retry",
            payload={"job_id": job.id, "drive_id": job.drive_id},
            job_id=job.id,
            session=db,
        )
    return from_backup
