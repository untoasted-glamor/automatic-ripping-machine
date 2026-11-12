"""The `menu_preview` task: a job's disc menu as pages, labels and a walk.

A `menu_preview` row (`TranscodeTaskKind.MENU_PREVIEW`) runs in the menu
preview image under the same register/claim/heartbeat/complete/fail protocol
as an encode, in the dispatcher's non-encode slots, with `/raw` mounted
read-write (`arm_backend.task_kinds`). Its source track is the job's
`disc_menu` track; it reads `<job raw>/menu/` and writes
`<job raw>/menu/preview/` (`arm_transcode.menus.preview`). At /complete the
backend reads `preview.json` and makes the job's name proposals
(`arm_backend.disc_menus.proposals`).

Queued by the dispatcher's sweep (`sweep_menu_previews`), never from a rip
event: for a job whose menu track is `done`, whose rip is complete
(`ripped_at` set) and which has a session application (a task row needs one),
once, when no preview task exists for that track. A failed preview stays
failed until the job page asks for another (`queue_menu_preview`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.ws import WSHub
from arm_common import (
    Job,
    SessionApplication,
    Track,
    TrackKind,
    TrackStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)

logger = logging.getLogger("arm_backend.disc_menus.preview_tasks")

_LIVE = (TranscodeTaskStatus.QUEUED, TranscodeTaskStatus.IN_PROGRESS)


def menu_dir_of(track: Track) -> Path | None:
    """The menu copy a `disc_menu` track recorded, or None."""
    if track.kind != TrackKind.DISC_MENU or track.status != TrackStatus.DONE or not track.output_path:
        return None
    return Path(track.output_path)


async def _newest_application(db: AsyncSession, job_id: str) -> SessionApplication | None:
    rows = (
        (await db.execute(select(SessionApplication).where(col(SessionApplication.job_id) == job_id))).scalars().all()
    )
    # ULID ids sort by creation.
    return max(rows, key=lambda a: a.id, default=None)


async def preview_tasks_of(db: AsyncSession, track_id: str) -> list[TranscodeTask]:
    """The menu track's preview tasks, oldest first."""
    rows = (
        (
            await db.execute(
                select(TranscodeTask)
                .where(col(TranscodeTask.source_track_id) == track_id)
                .where(col(TranscodeTask.kind) == TranscodeTaskKind.MENU_PREVIEW)
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda t: t.id)


async def menu_preview_inputs(db: AsyncSession, task: TranscodeTask) -> tuple[Path | None, str | None]:
    """The menu folder a `menu_preview` task reads, or why it can never run."""
    track = (await db.execute(select(Track).where(col(Track.id) == task.source_track_id))).scalar_one_or_none()
    if track is None:
        return None, "its menu track is gone"
    menu_dir = menu_dir_of(track)
    if menu_dir is None:
        return None, "the job's disc menu hasn't been copied"
    return menu_dir, None


async def _queue(db: AsyncSession, job: Job, track: Track, hub: WSHub | None) -> TranscodeTask | None:
    application = await _newest_application(db, job.id)
    if application is None:
        return None
    task = TranscodeTask(
        kind=TranscodeTaskKind.MENU_PREVIEW,
        session_application_id=application.id,
        source_track_id=track.id,
        output_path=f"{track.output_path}/preview",
        status=TranscodeTaskStatus.QUEUED,
        attempts=0,
        progress_pct=0,
    )
    db.add(task)
    await db.flush()
    logger.info("menu preview queued job_id=%s task_id=%s", job.id, task.id)
    if hub is not None:
        await hub.emit(
            topic="transcode.events",
            event_type="task.queued",
            payload={"task_id": task.id, "kind": task.kind.value, "session_application_id": application.id},
            job_id=job.id,
            track_id=track.id,
            session=db,
        )
    return task


async def sweep_menu_previews(db: AsyncSession, hub: WSHub | None = None) -> int:
    """Queue a preview for every job whose copied menu has none yet. Flushes;
    the caller commits. Returns how many it queued."""
    tracks = (
        (
            await db.execute(
                select(Track).where(col(Track.kind) == TrackKind.DISC_MENU).where(col(Track.status) == TrackStatus.DONE)
            )
        )
        .scalars()
        .all()
    )
    queued = 0
    for track in tracks:
        if not track.output_path or await preview_tasks_of(db, track.id):
            continue
        job = (await db.execute(select(Job).where(col(Job.id) == track.job_id))).scalar_one_or_none()
        if job is None or job.ripped_at is None:
            continue
        queued += await _queue(db, job, track, hub) is not None
    return queued


class PreviewRefused(Exception):
    pass


async def queue_menu_preview(db: AsyncSession, job: Job, hub: WSHub | None) -> TranscodeTask:
    """Another preview of the job's menu, by hand (the job page). Flushes; the
    caller commits."""
    track = (
        await db.execute(select(Track).where(col(Track.job_id) == job.id).where(col(Track.kind) == TrackKind.DISC_MENU))
    ).scalar_one_or_none()
    if track is None or menu_dir_of(track) is None:
        raise PreviewRefused("the job's disc menu hasn't been copied")
    if any(t.status in _LIVE for t in await preview_tasks_of(db, track.id)):
        raise PreviewRefused("a menu preview is already queued or running")
    task = await _queue(db, job, track, hub)
    if task is None:
        raise PreviewRefused("the job has no session applied yet")
    return task
