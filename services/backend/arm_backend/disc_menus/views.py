"""The job page's Disc menu panel: the newest preview, drawn page by page, the job's proposals, and its pack."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.disc_menus.packs import folder_size, owned_pack_folder, pack_refusal, pack_tasks_of
from arm_backend.disc_menus.preview_tasks import menu_dir_of, preview_tasks_of
from arm_backend.disc_menus.rip import rip_refusal
from arm_backend.disc_menus.proposals import (
    PLAY_ALL,
    scan_titles,
    clean_label,
    match_bd,
    match_dvd,
    plays_text,
    read_preview,
)
from arm_common import Job, MenuProposal, Track, TrackKind, TranscodeTaskStatus
from arm_common.schemas import (
    JobDiscMenuView,
    MenuPackView,
    MenuPreviewButtonView,
    MenuPreviewPageView,
    MenuPreviewView,
    MenuProposalView,
)

_LIVE = (TranscodeTaskStatus.QUEUED, TranscodeTaskStatus.IN_PROGRESS)
# The files of a preview the panel may fetch.
PREVIEW_FILE_DIRS = ("pages", "crops")


async def menu_track(db: AsyncSession, job_id: str) -> Track | None:
    return (
        await db.execute(select(Track).where(col(Track.job_id) == job_id).where(col(Track.kind) == TrackKind.DISC_MENU))
    ).scalar_one_or_none()


async def job_proposals(db: AsyncSession, job_id: str) -> list[MenuProposal]:
    rows = (await db.execute(select(MenuProposal).where(col(MenuProposal.job_id) == job_id))).scalars().all()
    return sorted(rows, key=lambda p: (p.page.zfill(4), p.button))


def _pages(
    preview: dict[str, Any], job: Job, tracks: list[Track], proposals: list[MenuProposal]
) -> list[MenuPreviewPageView]:
    by_at = {p["at"]: str(p["id"]) for p in preview.get("pages", [])}
    titles = scan_titles(job)
    by_ref = {t.source_ref: t for t in tracks if t.kind == TrackKind.VIDEO_TITLE}
    by_track = {p.track_id: p for p in proposals if p.track_id}
    by_button = {(p.page, p.button): p for p in proposals}
    playlists = preview.get("playlists") or {}
    pages: list[MenuPreviewPageView] = []
    for page in preview.get("pages", []):
        buttons: list[MenuPreviewButtonView] = []
        for b in page.get("buttons", []):
            o = b.get("outcome") or {}
            kind = o.get("kind")
            label = clean_label(str(b.get("label") or ""))
            view = MenuPreviewButtonView(
                id=int(b["id"]),
                box=[int(v) for v in b.get("box", [0, 0, 0, 0])],
                visible=bool(b.get("visible")),
                label=label,
                score=b.get("score"),
                crop=b.get("crop"),
                outcome="stopped",
            )
            if kind in ("plays", "plays_title"):
                view.outcome, view.plays = "plays", plays_text(o)
                index = match_dvd(titles, o) if kind == "plays_title" else match_bd(playlists, titles, o, label)
                track = by_ref.get(str(index)) if index is not None else None
                view.track_id = track.id if track is not None else None
                proposal = by_button.get((str(page["id"]), view.id))
                if proposal is None and track is not None and not PLAY_ALL.search(label):
                    proposal = by_track.get(track.id)
                view.proposal_id = proposal.id if proposal is not None else None
            elif kind == "menu":
                target = by_at.get(str(o.get("at")))
                view.outcome = "stays" if target == str(page["id"]) else "page"
                view.goes_to = target if target != str(page["id"]) else None
            buttons.append(view)
        pages.append(
            MenuPreviewPageView(
                id=str(page["id"]),
                at=str(page.get("at", "")),
                language=str(page.get("language", "")),
                image=str(page.get("image", "")),
                size=[int(v) for v in page.get("size", [0, 0])],
                buttons=buttons,
            )
        )
    return pages


def _media_root() -> Path:
    from arm_backend.config import settings

    return Path(settings.MEDIA_ROOT)


async def _pack_view(db: AsyncSession, job: Job, track: Track) -> MenuPackView | None:
    tasks = await pack_tasks_of(db, track.id)
    if not tasks:
        return None
    task = tasks[-1]
    view = MenuPackView(
        task_id=task.id,
        status=task.status,
        progress_pct=task.progress_pct,
        last_error=task.last_error,
        updated_at=task.updated_at,
    )
    done = [t for t in tasks if t.status == TranscodeTaskStatus.DONE]
    folder = owned_pack_folder(_media_root(), done[-1].output_path, job.id) if done else None
    if folder is not None:
        view.path = done[-1].output_path
        view.size_bytes = folder_size(folder)
    return view


async def job_disc_menu_view(db: AsyncSession, job: Job) -> JobDiscMenuView | None:
    """The panel, or None for a job with no disc menu track."""
    track = await menu_track(db, job.id)
    if track is None:
        return None
    proposals = await job_proposals(db, job.id)
    tasks = await preview_tasks_of(db, track.id)
    view = JobDiscMenuView(
        track_id=track.id,
        track_status=track.status,
        track_error=track.last_error,
        size_bytes=track.size_bytes,
        proposals=[MenuProposalView.model_validate(p) for p in proposals],
        can_preview=menu_dir_of(track) is not None and not any(t.status in _LIVE for t in tasks),
    )
    if any(p.track_id is None and p.scan_title is not None for p in proposals):
        view.rip_refusal = await rip_refusal(db, job)
    view.pack = await _pack_view(db, job, track)
    view.pack_refusal = await pack_refusal(db, job, track)
    if not tasks:
        return view
    task = tasks[-1]
    view.preview = MenuPreviewView(
        task_id=task.id,
        status=task.status,
        progress_pct=task.progress_pct,
        last_error=task.last_error,
        updated_at=task.updated_at,
    )
    if task.status == TranscodeTaskStatus.DONE and task.output_path:
        preview = read_preview(Path(task.output_path))
        if preview is not None:
            tracks = list((await db.execute(select(Track).where(col(Track.job_id) == job.id))).scalars().all())
            view.preview.disc = preview.get("disc")
            view.preview.notes = [str(n) for n in preview.get("notes", [])]
            view.preview.unsupported = [str(n) for n in preview.get("unsupported", [])]
            view.preview.truncated = bool(preview.get("truncated"))
            view.preview.pages = _pages(preview, job, tracks, proposals)
    return view


async def preview_file(db: AsyncSession, job: Job, relative: str) -> Path | None:
    """A page image or button crop of the job's newest finished preview, or None."""
    parts = Path(relative).parts
    if len(parts) != 2 or parts[0] not in PREVIEW_FILE_DIRS or not parts[1].endswith((".jpg", ".png")):
        return None
    track = await menu_track(db, job.id)
    if track is None:
        return None
    done = [t for t in await preview_tasks_of(db, track.id) if t.status == TranscodeTaskStatus.DONE and t.output_path]
    if not done:
        return None
    root = Path(done[-1].output_path or "").resolve()
    path = (root / parts[0] / parts[1]).resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path
