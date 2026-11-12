"""Ripping a title the disc menu plays but the job has no track for (fork only).

The rip's minimum length leaves short titles out of a job (a 92 s textless
opening at the default 120 s), but a menu button that plays one says it is
part of the disc. "Rip it" on the job page's Disc menu panel makes the track,
with the button's role and name and `any_length` set (the ripper remuxes it
from the kept disc backup whatever its length), and arms the job's retry
(`arm_backend.rip_retry`). The retry's fan-out encodes it straight into its
bonus folder (`compute_outputs` lays out a named bonus), and the proposal,
accepted, is marked applied at the encode's /complete
(`arm_backend.disc_menus.rename.apply_pending`).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.disc_menus.proposals import scan_titles
from arm_backend.disc_menus.rename import BONUS_ROLES, bonus_path, name_track
from arm_backend.raw_backup import describe_backup
from arm_backend.rip_retry import RetryRefused, arm_retry, check_retryable, remaining_tracks
from arm_backend.ws import WSHub
from arm_common import Job, MenuProposal, MenuProposalStatus, TrackKind, TrackRole, TrackStatus, TranscodeTaskStatus
from arm_common.models import Track, TranscodeTask

logger = logging.getLogger(__name__)


class RipRefused(Exception):
    pass


async def rip_refusal(db: AsyncSession, job: Job) -> str | None:
    """Why the job can't rip a title from its menu now, or None when it can."""
    try:
        await check_retryable(db, job)
    except RetryRefused as e:
        return e.detail
    backup = describe_backup(job.id)
    if backup is None or not backup.complete:
        return "the job's disc backup isn't kept, so ARM can't rip the title without the disc"
    return None


async def _reference_output(db: AsyncSession, tracks: list[Track]) -> str | None:
    """A finished encode of the job, to place the new title's file beside (its show folder)."""
    ids = [t.id for t in tracks]
    rows = (await db.execute(select(TranscodeTask).where(col(TranscodeTask.source_track_id).in_(ids)))).scalars().all()
    done = [t.output_path for t in rows if t.status == TranscodeTaskStatus.DONE and t.output_path]
    return done[0] if done else None


def _scan_title(job: Job, index: int) -> dict[str, Any] | None:
    return next((t for t in scan_titles(job) if t.get("index") == index), None)


async def rip_from_menu(
    db: AsyncSession, job: Job, proposal: MenuProposal, hub: WSHub, media_root: Path
) -> MenuProposal:
    """Make the track the proposal's button plays and arm the retry that rips it. Every check runs before any row
    changes. Flushes; the caller commits. Raises RipRefused with the reason."""
    if proposal.track_id is not None:
        raise RipRefused("the job already has a track for this title")
    if proposal.scan_title is None:
        raise RipRefused("the menu's walk didn't find this title in the disc scan")
    if proposal.role not in BONUS_ROLES or not proposal.name:
        raise RipRefused("give it a bonus role and a name first")
    if proposal.role == TrackRole.SPECIAL and proposal.episode_number is None:
        raise RipRefused("a special needs its season 0 number (TMDB's)")
    index = proposal.scan_title
    title = _scan_title(job, index)
    if title is None:
        raise RipRefused(f"title {index} isn't in the job's disc scan")
    tracks = list((await db.execute(select(Track).where(col(Track.job_id) == job.id))).scalars().all())
    if any(t.kind == TrackKind.VIDEO_TITLE and t.index == index for t in tracks):
        raise RipRefused(f"title {index} already has a track")
    reason = await rip_refusal(db, job)
    if reason is not None:
        raise RipRefused(reason)
    reference = await _reference_output(db, tracks)
    if reference is not None:
        target = bonus_path(reference, proposal.role, proposal.name, job.title or "", proposal.episode_number)
        if target is not None and (media_root / target).exists():
            raise RipRefused(f"{target} already exists")

    duration, size = title.get("duration_seconds"), title.get("size_bytes")
    track = Track(
        job_id=job.id,
        kind=TrackKind.VIDEO_TITLE,
        index=index,
        source_ref=str(index),
        status=TrackStatus.QUEUED,
        attempts=0,
        expected_duration_seconds=duration if isinstance(duration, int) else None,
        expected_size_bytes=size if isinstance(size, int) else None,
        any_length=True,
    )
    name_track(track, proposal)
    db.add(track)
    await db.flush()
    proposal.track_id = track.id
    proposal.status, proposal.last_error, proposal.note = MenuProposalStatus.ACCEPTED, None, None
    db.add(proposal)
    await arm_retry(db, job, hub, remaining_tracks([*tracks, track]))
    await db.flush()
    logger.info("ripping title %d from the disc menu job_id=%s proposal_id=%s", index, job.id, proposal.id)
    return proposal
