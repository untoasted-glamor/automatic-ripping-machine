"""The job page's Disc menu panel (fork only): the menu preview and naming titles from it.

`GET` the panel; fetch a page image or button crop; ask for another preview; edit, accept or reject a proposed
name, accept every proposal at once, or name a title by hand. Accepting renames the title's finished encode
(`arm_backend.disc_menus.rename`).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.auth import require_jwt, require_writer
from arm_backend.config import settings
from arm_backend.db import get_session
from arm_backend.disc_menus.preview_tasks import PreviewRefused, queue_menu_preview
from arm_backend.disc_menus.rename import RenameRefused, apply_proposal
from arm_backend.disc_menus.views import job_disc_menu_view, preview_file
from arm_backend.routers._params import JobIdParam
from arm_backend.ws import WSHub
from arm_common import Job, MenuProposal, MenuProposalStatus, Track, User
from arm_common.schemas import (
    JobDiscMenuView,
    MenuProposalAcceptAllResponse,
    MenuProposalCreate,
    MenuProposalUpdate,
    MenuProposalView,
)

logger = logging.getLogger("arm_backend.routers.disc_menus")

router = APIRouter(prefix="/api/jobs", tags=["disc-menus"])


def _get_hub(request: Request) -> WSHub:
    hub: WSHub = request.app.state.ws_hub
    return hub


async def _job(db: AsyncSession, job_id: str) -> Job:
    job = (await db.execute(select(Job).where(col(Job.id) == job_id))).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown job_id: {job_id}")
    return job


async def _view(db: AsyncSession, job: Job) -> JobDiscMenuView:
    view = await job_disc_menu_view(db, job)
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="this job has no disc menu")
    return view


async def _proposal(db: AsyncSession, job_id: str, proposal_id: str) -> MenuProposal:
    row = (await db.execute(select(MenuProposal).where(col(MenuProposal.id) == proposal_id))).scalar_one_or_none()
    if row is None or row.job_id != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown proposal: {proposal_id}")
    return row


def _media_root() -> Path:
    return Path(settings.MEDIA_ROOT)


@router.get("/{job_id}/disc-menu", response_model=JobDiscMenuView)
async def get_disc_menu(
    job_id: JobIdParam,
    _: User = Depends(require_jwt),
    db: AsyncSession = Depends(get_session),
) -> JobDiscMenuView:
    """The job's disc menu: its track, the newest preview (pages and buttons) and the proposed names."""
    return await _view(db, await _job(db, job_id))


@router.get("/{job_id}/disc-menu/files/{folder}/{name}", response_class=FileResponse)
async def get_disc_menu_file(
    job_id: JobIdParam,
    folder: str,
    name: str,
    _: User = Depends(require_jwt),
    db: AsyncSession = Depends(get_session),
) -> FileResponse:
    """A page image (`pages/<n>.jpg`) or button crop (`crops/<n>.b<id>.png`) of the newest preview."""
    path = await preview_file(db, await _job(db, job_id), f"{folder}/{name}")
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such preview file")
    return FileResponse(path, headers={"Cache-Control": "private, max-age=300"})


@router.post("/{job_id}/disc-menu/preview", response_model=JobDiscMenuView)
async def request_disc_menu_preview(
    job_id: JobIdParam,
    _: User = Depends(require_writer),
    db: AsyncSession = Depends(get_session),
    hub: WSHub = Depends(_get_hub),
) -> JobDiscMenuView:
    """Preview the menu again (after a failed preview, or to read it with newer rules). 409 when it can't run."""
    job = await _job(db, job_id)
    try:
        await queue_menu_preview(db, job, hub)
    except PreviewRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    return await _view(db, job)


async def _apply(db: AsyncSession, proposal: MenuProposal) -> None:
    try:
        await apply_proposal(db, proposal, _media_root())
    except RenameRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch("/{job_id}/disc-menu/proposals/{proposal_id}", response_model=MenuProposalView)
async def update_proposal(
    job_id: JobIdParam,
    proposal_id: str,
    req: MenuProposalUpdate,
    _: User = Depends(require_writer),
    db: AsyncSession = Depends(get_session),
) -> MenuProposal:
    """Edit a proposal's role, name or season 0 number, and accept, reject or reopen it. Accepting an applied
    proposal again with a new name renames the encode again. 409 when it can't be applied (with why)."""
    row = await _proposal(db, job_id, proposal_id)
    for field in ("role", "name", "episode_number"):
        if field in req.model_fields_set:
            setattr(row, field, getattr(req, field))
    if req.action == "reject":
        row.status = MenuProposalStatus.REJECTED
    elif req.action == "reopen":
        row.status = MenuProposalStatus.PROPOSED
    elif req.action == "accept":
        await _apply(db, row)
    await db.commit()
    await db.refresh(row)
    return row


@router.post("/{job_id}/disc-menu/proposals", response_model=MenuProposalView, status_code=status.HTTP_201_CREATED)
async def create_proposal(
    job_id: JobIdParam,
    req: MenuProposalCreate,
    _: User = Depends(require_writer),
    db: AsyncSession = Depends(get_session),
) -> MenuProposal:
    """Name one of the job's titles by hand, and apply it."""
    await _job(db, job_id)
    track = (await db.execute(select(Track).where(col(Track.id) == req.track_id))).scalar_one_or_none()
    if track is None or track.job_id != job_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown track: {req.track_id}")
    row = MenuProposal(
        job_id=job_id,
        track_id=track.id,
        page=req.page,
        button=req.button,
        label="",
        plays="set by hand",
        role=req.role,
        name=req.name,
        episode_number=req.episode_number,
        status=MenuProposalStatus.PROPOSED,
    )
    db.add(row)
    await db.flush()
    await _apply(db, row)
    await db.commit()
    await db.refresh(row)
    return row


@router.post("/{job_id}/disc-menu/proposals/accept-all", response_model=MenuProposalAcceptAllResponse)
async def accept_all_proposals(
    job_id: JobIdParam,
    _: User = Depends(require_writer),
    db: AsyncSession = Depends(get_session),
) -> MenuProposalAcceptAllResponse:
    """Accept every proposed name. One that can't be applied stays proposed, with why."""
    await _job(db, job_id)
    rows = (
        (
            await db.execute(
                select(MenuProposal)
                .where(col(MenuProposal.job_id) == job_id)
                .where(col(MenuProposal.status) == MenuProposalStatus.PROPOSED)
            )
        )
        .scalars()
        .all()
    )
    refused: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: r.id):
        try:
            await apply_proposal(db, row, _media_root())
        except RenameRefused as exc:
            refused[row.id] = str(exc)
            row.last_error = str(exc)
    await db.commit()
    for row in rows:
        await db.refresh(row)
    return MenuProposalAcceptAllResponse(proposals=[MenuProposalView.model_validate(r) for r in rows], refused=refused)
