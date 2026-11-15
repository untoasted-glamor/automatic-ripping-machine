"""Stop 2: choose the titles once the disc is backed up (fork only).

A DVD or Blu-ray from a drive is backed up whole before any title is ripped
(`parks_after_backup`, told to the ripper at rip-start). After the backup, and
the menu copy, the ripper stops and calls `rip-parked`. The job is then
`awaiting_titles` and the drive takes the next disc. Its titles are remuxed
from the backup once they're chosen.

`sweep_choose_titles` runs each dispatcher tick, after the menu preview sweep:

- **Opening.** The stop opens once the job's menu preview has ended, or at
  once when there is none to wait for, so the menu's names (and its rows for
  short titles) are on the title rows first. Opening applies the default ticks
  (`default_ticks`) and sets `wait_start_time`, in one commit. A set
  `wait_start_time` is what "open" means.
- **Starting.** An open stop starts when `stop_may_start` says so (the rule
  stop 1 uses), through the same path as `POST /api/jobs/{id}/retry-failed`
  (`start_from_backup`): the ticked titles are remuxed from the backup.

At rip-complete the backend deletes the backup of a parked job once no kept
title is failed (`delete_parked_backup`): it is kept only for a retry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.disc_menus.preview_tasks import preview_settled
from arm_backend.raw_backup import delete_backup
from arm_backend.rip_retry import RetryRefused, arm_retry, check_retryable, remaining_tracks
from arm_backend.seeders import CONFIG_SINGLETON_ID
from arm_backend.ws import WSHub
from arm_common import Config, DiscType, Drive, Job, JobStatus, MediaType, TrackKind, TrackRole, TrackStatus
from arm_common.enums import MenuProposalStatus
from arm_common.models import Track
from arm_common.models.menu_proposal import MenuProposal
from arm_common.review_gate import stop_may_start
from arm_common.schemas.job_metadata import flag_is_set, with_flags
from arm_common.tracks import titles

logger = logging.getLogger("arm_backend.choose_titles")

# Ticked when stop 2 opens (owner decision 4): episodes, specials, OP/ED and trailers.
_TICKED_ROLES = frozenset({TrackRole.EPISODE, TrackRole.SPECIAL, TrackRole.OTHER, TrackRole.TRAILER})
# The metadata flag a parked job carries until it is deleted (`rip-parked` sets it).
PARKED_FLAG = "parked"


def backs_up_first(job: Job, drive: Drive | None, tracks: Sequence[Track]) -> bool:
    """A disc whose first pass is backed up whole and stops at stop 2: a DVD or Blu-ray from a physical drive,
    ripped as titles. The job page reads it before the rip (`JobDetailView.parks_after_backup`)."""
    return (
        job.disc_type in (DiscType.DVD, DiscType.BLURAY)
        and drive is not None
        and drive.source_kind is None
        and any(t.kind == TrackKind.VIDEO_TITLE for t in tracks)
    )


def parks_after_backup(job: Job, drive: Drive | None, tracks: Sequence[Track], *, retry: bool) -> bool:
    """At rip-start: whether the ripper stops at stop 2 after backing this disc up. Its first pass only (not a
    retry, nor a remux from a kept backup)."""
    return not retry and job.status == JobStatus.RIPPING and backs_up_first(job, drive, tracks)


async def drive_of(db: AsyncSession, job: Job) -> Drive | None:
    return (await db.execute(select(Drive).where(col(Drive.id) == job.drive_id))).scalar_one_or_none()


def default_ticks(tracks: Sequence[Track], proposals: Sequence[MenuProposal], *, movie: bool) -> list[Track]:
    """Stop 2's default keep/drop set, applied once when it opens. A title's roles are its own (identity) and those
    of its menu proposals (any but a rejected one). A play-all is left out; an episode, special, OP/ED (`other`) or
    trailer is kept, and so is a movie disc's main feature. Every other title keeps the rip preset's default (set at
    identify). Returns the tracks whose `excluded` changed."""
    menu_roles: dict[str, set[TrackRole]] = {}
    for p in proposals:
        if p.track_id is not None and p.role is not None and p.status != MenuProposalStatus.REJECTED:
            menu_roles.setdefault(p.track_id, set()).add(p.role)
    changed: list[Track] = []
    for track in titles(tracks):
        roles = menu_roles.get(track.id, set()) | ({track.role} if track.role is not None else set())
        if TrackRole.PLAY_ALL in roles:
            keep = False
        elif roles & _TICKED_ROLES or (movie and TrackRole.MAIN in roles):
            keep = True
        else:
            continue
        if track.excluded == keep:
            track.excluded = not keep
            changed.append(track)
    return changed


async def _job_tracks(db: AsyncSession, job: Job) -> list[Track]:
    return list((await db.execute(select(Track).where(col(Track.job_id) == job.id))).scalars().all())


async def open_stop(db: AsyncSession, job: Job, now: datetime) -> None:
    """Apply the default ticks and open the stop (`wait_start_time`). Flushes; the caller commits."""
    tracks = await _job_tracks(db, job)
    proposals = (await db.execute(select(MenuProposal).where(col(MenuProposal.job_id) == job.id))).scalars().all()
    changed = default_ticks(tracks, proposals, movie=job.media_type == MediaType.MOVIE)
    for track in changed:
        db.add(track)
    job.wait_start_time = now
    db.add(job)
    await db.flush()
    logger.info(
        "stop 2 open job_id=%s: %d of %d title(s) ticked (%d changed by the defaults)",
        job.id,
        sum(1 for t in titles(tracks) if not t.excluded),
        len(titles(tracks)),
        len(changed),
    )


async def start_from_backup(db: AsyncSession, job: Job, hub: WSHub) -> bool:
    """Remux the job's ticked titles from its backup, as retry-failed does. Doesn't commit. False (nothing done)
    when it can't start: no ticked title, or a refusal from `check_retryable`."""
    try:
        await check_retryable(db, job)
    except RetryRefused as e:
        logger.debug("stop 2 job_id=%s can't start: %s", job.id, e.detail)
        return False
    remaining = remaining_tracks(await _job_tracks(db, job))
    if not titles(remaining):
        return False
    await arm_retry(db, job, hub, remaining)
    logger.info("stop 2 job_id=%s: remuxing %d title(s) from the disc backup", job.id, len(titles(remaining)))
    return True


async def sweep_choose_titles(db: AsyncSession, hub: WSHub) -> int:
    """Open the stops whose menu preview has ended, and start the open ones the wait settings let go. Flushes; the
    caller commits. Returns how many it opened or started."""
    jobs = (await db.execute(select(Job).where(col(Job.status) == JobStatus.AWAITING_TITLES))).scalars().all()
    if not jobs:
        return 0
    cfg = (await db.execute(select(Config).where(col(Config.id) == CONFIG_SINGLETON_ID))).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    touched = 0
    for job in jobs:
        if job.wait_start_time is None:
            if not await preview_settled(db, job):
                continue
            await open_stop(db, job, now)
            touched += 1
        if cfg is not None and stop_may_start(cfg, job, now) and await start_from_backup(db, job, hub):
            touched += 1
    return touched


async def delete_parked_backup(job: Job, tracks: Sequence[Track]) -> None:
    """At rip-complete: a parked job's backup goes once no kept track (title or menu) is failed (owner decision 5).
    A failed one keeps it, so retry-failed needs no disc."""
    if not flag_is_set(job.metadata_json, PARKED_FLAG):
        return
    if any(not t.excluded and t.status == TrackStatus.FAILED for t in tracks):
        return
    try:
        # A Blu-ray backup is tens of GB: off the event loop.
        await asyncio.to_thread(delete_backup, job.id)
    except OSError as exc:
        logger.warning("could not delete the disc backup of job_id=%s: %s", job.id, exc)


def mark_parked(job: Job) -> None:
    """`rip-parked`: the ripper backed the disc up, copied its menu and stopped before the remux."""
    job.status = JobStatus.AWAITING_TITLES
    # Stop 1's countdown is spent; stop 2 opens (sets it again) once the menu has named the titles.
    job.wait_start_time = None
    job.metadata_json = with_flags(job.metadata_json, **{PARKED_FLAG: True})
