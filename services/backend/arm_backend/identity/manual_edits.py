"""Applying an operator's manual job and track edits, with identity fields routed through the resolver."""

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.identity.pipeline import resolve_job
from arm_backend.identity.proposals import (
    record_manual_job,
    record_manual_track,
    revert_manual_track,
)
from arm_backend.identity.resolve_service import identity_snapshot
from arm_backend.identity.stage_runner import EpisodeStageRunner
from arm_backend.ws import WSHub
from arm_common import Job, TrackStatus
from arm_common.enums import RIPPER_BUSY_JOB_STATUSES
from arm_common.tracks import is_title
from arm_common.models import Track
from arm_common.schemas import JobUpdateRequest, TrackEditRequest
from arm_common.schemas.identity import JOB_CLAIM_FIELDS, TRACK_CLAIM_FIELDS

# Resolver-owned attributes on TrackEditRequest / JobUpdateRequest: an edit to
# one of these becomes a `manual` identity proposal (with provenance) instead
# of a direct column write, so it goes through the same tiered resolver as
# every other identity source. Everything else is still a plain `setattr`.
# Derived from the claim-field maps so the resolver, the recorders and this
# module cannot drift apart. JobUpdateRequest has no `season` (it is set
# through /resolve), so the job set is the maps' attrs the request can carry.
_IDENTITY_TRACK_ATTRS = frozenset(TRACK_CLAIM_FIELDS.values())
_IDENTITY_JOB_ATTRS = frozenset(JOB_CLAIM_FIELDS.values()).intersection(JobUpdateRequest.model_fields)


def _apply_job_fields(job: Job, req: JobUpdateRequest) -> bool:
    job_fields = req.model_dump(exclude_unset=True, exclude={"tracks"})
    job_identity = {k: v for k, v in job_fields.items() if k in _IDENTITY_JOB_ATTRS}
    for key, value in job_fields.items():
        if key not in _IDENTITY_JOB_ATTRS:
            setattr(job, key, value)
    return record_manual_job(job, job_identity) if job_identity else False


def _apply_track_edit(job: Job, track: Track, edit: TrackEditRequest) -> bool:
    identity_touched = False
    data = edit.model_dump(exclude_unset=True, exclude={"track_id", "revert_fields"})
    identity = {k: v for k, v in data.items() if k in _IDENTITY_TRACK_ATTRS}
    for key, value in data.items():
        if key not in _IDENTITY_TRACK_ATTRS:
            setattr(track, key, value)
    if identity and record_manual_track(job, track, identity):
        identity_touched = True
    if edit.revert_fields:
        revert_manual_track(job, track.source_ref, edit.revert_fields)
        identity_touched = True
    return identity_touched


def _track_name(track: Track) -> str:
    return f"track {track.index}" if is_title(track) else "the disc menu"


def _check_menu_edit(edit: TrackEditRequest, track: Track) -> None:
    """The disc menu row takes only keep/drop (it has no identity, role or
    file name), and a skipped one can't be ticked."""
    other = edit.model_dump(exclude_unset=True, exclude={"track_id", "excluded"})
    if other:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"the disc menu takes only excluded, not {', '.join(sorted(other))}",
        )
    if edit.excluded is False and track.status == TrackStatus.SKIPPED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"the disc menu can't be ripped: {track.last_error or 'skipped'}",
        )


def _check_track_edit(job: Job, track: Track, edit: TrackEditRequest) -> None:
    # Re-including a track with no file is refused while the disc rips:
    # the ripper took its keep/drop set at rip-start, so it wouldn't get
    # one. After the rip it is allowed and means "rip it later": the
    # track stays queued with no file (never fanned out to a session)
    # until POST /retry-failed rips it, from the kept disc backup or
    # when the disc goes back in. Excluding is allowed in every status
    # ("skip at transcode").
    if not is_title(track):
        _check_menu_edit(edit, track)
    if edit.excluded is False and track.excluded and job.status in RIPPER_BUSY_JOB_STATUSES and not track.output_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{_track_name(track)} has no ripped file; keep it once the rip ends to rip it then",
        )


async def _validate_track_edits(db: AsyncSession, job: Job, edits: list[TrackEditRequest]) -> dict[str, Track]:
    """Check every track edit before anything changes, so a rejected request
    leaves the job and all its tracks as they were. Returns the job's tracks by id."""
    rows = list((await db.execute(select(Track).where(col(Track.job_id) == job.id))).scalars().all())
    by_id = {t.id: t for t in rows}
    for edit in edits:
        track = by_id.get(edit.track_id)
        if track is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown track_id {edit.track_id} for job {job.id}",
            )
        _check_track_edit(job, track, edit)
    return by_id


def _apply_track_edits(
    db: AsyncSession, job: Job, by_id: dict[str, Track], edits: list[TrackEditRequest]
) -> tuple[list[str], bool]:
    identity_touched = False
    edited_track_ids: list[str] = []
    # Duplicate track_ids in req.tracks → last-wins (idempotent setattr);
    # track.updated is emitted once per track (de-duplicated below).
    # Callers needn't deduplicate.
    for edit in edits:
        track = by_id[edit.track_id]
        if _apply_track_edit(job, track, edit):
            identity_touched = True
        db.add(track)
        edited_track_ids.append(track.id)
    return edited_track_ids, identity_touched


async def _emit_track_updates(
    db: AsyncSession, hub: WSHub, job_id: str, edited_track_ids: list[str], resolver_track_ids: frozenset[str]
) -> None:
    # De-duplicated, stable order: the tracks the request itself edited,
    # followed by any sibling the resolver also changed (e.g. a claim that
    # was never applied until this PATCH ran the resolver) -- fixing PR 1's
    # parked finding that resolver-only changes never got a track.updated.
    for tid in list(dict.fromkeys([*edited_track_ids, *sorted(resolver_track_ids)])):
        await hub.emit(
            topic="ripper.events",
            event_type="track.updated",
            payload={"track_id": tid, "job_id": job_id},
            job_id=job_id,
            track_id=tid,
            session=db,
        )


async def apply_manual_edits(
    db: AsyncSession,
    job: Job,
    req: JobUpdateRequest,
    *,
    hub: WSHub,
    stage_runner: EpisodeStageRunner | None,
) -> None:
    before_identity = identity_snapshot(job)
    by_id = await _validate_track_edits(db, job, req.tracks) if req.tracks is not None else {}

    job_identity_touched = _apply_job_fields(job, req)
    db.add(job)

    edited_track_ids: list[str] = []
    track_identity_touched = False
    if req.tracks is not None:
        edited_track_ids, track_identity_touched = _apply_track_edits(db, job, by_id, req.tracks)

    resolver_track_ids: frozenset[str] = frozenset()
    if job_identity_touched or track_identity_touched:
        outcome = await resolve_job(db, job)
        resolver_track_ids = outcome.track_ids
    await db.flush()
    await _emit_track_updates(db, hub, job.id, edited_track_ids, resolver_track_ids)
    await db.commit()
    await db.refresh(job)
    if stage_runner is not None and identity_snapshot(job) != before_identity:
        stage_runner.schedule(job.id)
