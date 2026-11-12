"""Applying a name from the disc menu: the bonus layout by role, and renaming a finished encode (design v5, M4).

**Layout.** Jellyfin files a show's bonuses by folder: specials in `Season 00/` matched by `S00Enn`, the rest in
the show's extras folders by type, matched by file name. So a TV title with a bonus role and a name goes to

- `special`: `<show folder>/Season 00/<Show> - S00Enn - <name>.<ext>` (needs its season 0 number);
- `trailer`: `<show folder>/trailers/<name>.<ext>`;
- `other` (the creditless OP and ED): `<show folder>/other/<name>.<ext>`;
- `extra`: `<show folder>/extras/<name>.<ext>`,

where the show folder is the encode's folder, or its parent when that is a season folder (`Season NN`,
`Specials`) or a bonus folder (renaming again). `compute_outputs` uses the same layout (`bonus_output_path`) for a track that already has a bonus
role and a name, so a re-apply writes where a rename put the file.

**Rename.** Accepting a proposal moves the track's newest finished encode and every file that records it: its
sidecars (`.sup`, `.mks`, `.srt`) and the rows that name them (the encode's `output_path`, `sidecar_paths` and
`replaced_sidecar_paths`; each `subtitle_ocr` task of it). Files move first; a failed move puts the moved ones
back, and the rows change only once every file has moved. An encode still queued or running leaves the proposal
`accepted`, and its /complete applies it. The track takes the role and the name either way.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.path_sanitize import sanitize_path_component
from arm_common import (
    Job,
    MediaType,
    MenuProposal,
    MenuProposalStatus,
    Track,
    TrackRole,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)

logger = logging.getLogger("arm_backend.disc_menus.rename")

BONUS_FOLDERS: dict[TrackRole, str] = {
    TrackRole.TRAILER: "trailers",
    TrackRole.OTHER: "other",
    TrackRole.EXTRA: "extras",
}
BONUS_ROLES = frozenset({TrackRole.SPECIAL, *BONUS_FOLDERS})
# A folder one level under the show: a season, or a bonus folder a rename made.
_SHOW_SUBFOLDER = re.compile(r"(?:Season \d+|Specials|trailers|other|extras)", re.I)
_LIVE = (TranscodeTaskStatus.QUEUED, TranscodeTaskStatus.IN_PROGRESS)


class RenameRefused(Exception):
    pass


def show_folder(output_path: str) -> PurePosixPath:
    folder = PurePosixPath(output_path).parent
    return folder.parent if _SHOW_SUBFOLDER.fullmatch(folder.name) else folder


def bonus_path(output_path: str, role: TrackRole, name: str, show: str, episode_number: int | None) -> str | None:
    """Where a bonus title's file goes, from where fan-out put it; None when the role has no bonus layout or a
    special has no season 0 number."""
    stem = sanitize_path_component(name)
    if role not in BONUS_ROLES or not stem:
        return None
    ext = PurePosixPath(output_path).suffix
    base = show_folder(output_path)
    if role == TrackRole.SPECIAL:
        if episode_number is None:
            return None
        show_part = sanitize_path_component(show)
        file = (
            f"{show_part} - S00E{episode_number:02d} - {stem}{ext}"
            if show_part
            else f"S00E{episode_number:02d} - {stem}{ext}"
        )
        return str(base / "Season 00" / file)
    return str(base / BONUS_FOLDERS[role] / f"{stem}{ext}")


def bonus_output_path(job: Job, track: Track, output_path: str) -> str:
    """`compute_outputs`' hook: a TV title that has a bonus role and a name goes in the bonus layout."""
    if job.media_type != MediaType.TV or track.role not in BONUS_ROLES or not track.custom_filename:
        return output_path
    return (
        bonus_path(output_path, track.role, track.custom_filename, job.title or "", track.episode_number) or output_path
    )


def _moved(path: str, old_video: str, new_video: str) -> str:
    """A sidecar's new path: the same suffix after the video's stem, next to the new video."""
    old, new = PurePosixPath(old_video), PurePosixPath(new_video)
    name = PurePosixPath(path).name
    tail = name[len(old.stem) :] if name.startswith(old.stem + ".") else name
    return str(new.parent / f"{new.stem}{tail}")


@dataclass
class _Plan:
    encode: TranscodeTask
    ocr: list[TranscodeTask]
    new_video: str
    moves: list[tuple[Path, Path]]


async def _encodes(db: AsyncSession, track_id: str) -> list[TranscodeTask]:
    rows = (
        (
            await db.execute(
                select(TranscodeTask)
                .where(col(TranscodeTask.source_track_id) == track_id)
                .where(col(TranscodeTask.kind) == TranscodeTaskKind.ENCODE)
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda t: t.id)


async def _plan(db: AsyncSession, media_root: Path, encode: TranscodeTask, new_video: str) -> _Plan:
    assert encode.output_path is not None
    old_video = encode.output_path
    ocr = (
        (
            await db.execute(
                select(TranscodeTask)
                .where(col(TranscodeTask.parent_task_id) == encode.id)
                .where(col(TranscodeTask.kind) == TranscodeTaskKind.SUBTITLE_OCR)
            )
        )
        .scalars()
        .all()
    )
    rel = [old_video, *(encode.sidecar_paths or []), *(p for t in ocr for p in (t.sidecar_paths or []))]
    moves: list[tuple[Path, Path]] = []
    for path in dict.fromkeys(rel):
        src = media_root / path
        dst = media_root / (new_video if path == old_video else _moved(path, old_video, new_video))
        if not src.exists():
            if path == old_video:
                raise RenameRefused(f"the encode isn't at {old_video} (moved out of ARM's media folder?)")
            continue
        if dst.exists():
            raise RenameRefused(f"{dst.relative_to(media_root)} already exists")
        moves.append((src, dst))
    return _Plan(encode, list(ocr), new_video, moves)


def _move_all(moves: list[tuple[Path, Path]]) -> None:
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
            done.append((src, dst))
    except OSError as e:
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError:  # pragma: no cover - logged; the rows still name the old files
                logger.exception("could not move %s back to %s", dst, src)
        raise RenameRefused(f"could not move {src.name}: {e}") from None


def _prune(folder: Path, media_root: Path) -> None:
    while folder != media_root and media_root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def name_track(track: Track, proposal: MenuProposal) -> None:
    """Give the track the proposal's role and name (and a special its season 0 number)."""
    track.role = proposal.role
    track.custom_filename = proposal.name
    if proposal.role == TrackRole.SPECIAL:
        track.season = 0
        track.episode_number = proposal.episode_number
        track.episode_number_end = None
    provenance = dict(track.identity_provenance or {})
    provenance["role"] = "disc_menu"
    track.identity_provenance = provenance


async def apply_proposal(db: AsyncSession, proposal: MenuProposal, media_root: Path) -> MenuProposal:
    """Give the track the proposal's role and name and rename its finished encode (or leave the rename for the
    encode's /complete). Flushes; the caller commits. Raises RenameRefused with the reason."""
    if proposal.track_id is None:
        raise RenameRefused("the job has no track for this title")
    if proposal.role not in BONUS_ROLES or not proposal.name:
        raise RenameRefused("only a bonus title with a name can be renamed")
    if proposal.role == TrackRole.SPECIAL and proposal.episode_number is None:
        raise RenameRefused("a special needs its season 0 number (TMDB's)")
    track = (await db.execute(select(Track).where(col(Track.id) == proposal.track_id))).scalar_one()
    job = (await db.execute(select(Job).where(col(Job.id) == track.job_id))).scalar_one()
    encodes = await _encodes(db, track.id)
    done = [t for t in encodes if t.status == TranscodeTaskStatus.DONE and t.output_path]
    if any(t.status in _LIVE for t in encodes):
        # Its encode is still running (or a re-encode is queued): the rename waits for /complete.
        name_track(track, proposal)
        proposal.status, proposal.last_error = MenuProposalStatus.ACCEPTED, None
        await db.flush()
        return proposal
    if not done:
        raise RenameRefused("the title has no finished encode to rename")
    encode = done[-1]
    assert encode.output_path is not None
    old_video = encode.output_path
    new_video = bonus_path(old_video, proposal.role, proposal.name, job.title or "", proposal.episode_number)
    assert new_video is not None
    if new_video != old_video:
        # Every check and every file move before any row changes: a refusal leaves the rows as they were.
        plan = await _plan(db, media_root, encode, new_video)
        _move_all(plan.moves)
        encode.output_path = new_video
        if encode.sidecar_paths:
            encode.sidecar_paths = [_moved(p, old_video, new_video) for p in encode.sidecar_paths]
        if encode.replaced_sidecar_paths:
            encode.replaced_sidecar_paths = [_moved(p, old_video, new_video) for p in encode.replaced_sidecar_paths]
        for task in plan.ocr:
            task.output_path = new_video
            if task.sidecar_paths:
                task.sidecar_paths = [_moved(p, old_video, new_video) for p in task.sidecar_paths]
        _prune((media_root / old_video).parent, media_root)
        logger.info("renamed encode task_id=%s %s -> %s (%d files)", encode.id, old_video, new_video, len(plan.moves))
    name_track(track, proposal)
    proposal.status, proposal.last_error = MenuProposalStatus.APPLIED, None
    proposal.applied_path = new_video
    await db.flush()
    return proposal


async def apply_pending(db: AsyncSession, encode: TranscodeTask, media_root: Path) -> int:
    """At an encode's /complete: apply the names accepted while it ran. A refusal is kept on the proposal."""
    rows = (
        (
            await db.execute(
                select(MenuProposal)
                .where(col(MenuProposal.track_id) == encode.source_track_id)
                .where(col(MenuProposal.status) == MenuProposalStatus.ACCEPTED)
            )
        )
        .scalars()
        .all()
    )
    applied = 0
    for proposal in rows:
        try:
            await apply_proposal(db, proposal, media_root)
            applied += proposal.status == MenuProposalStatus.APPLIED
        except RenameRefused as e:
            proposal.last_error = str(e)
            logger.warning("pending rename refused proposal_id=%s: %s", proposal.id, e)
    await db.flush()
    return applied
