"""The `menu_pack` task: the job's disc menu as a pack the Jellyfin Disc Menu plugin plays (design v5, A3).

A `menu_pack` row (`TranscodeTaskKind.MENU_PACK`) runs in the transcode image under the same
register/claim/heartbeat/complete/fail protocol as an encode, in the dispatcher's non-encode slots, with `/raw`
mounted read-only (`arm_backend.task_kinds`). Its source track is the job's `disc_menu` track. At `/register` the
backend works out what to build (`plan_for`, a `MenuPackPlan`): the encodes of the task's session application,
which disc title each one is, and where the pack goes. The transcoder builds it into a temp folder beside
`<show folder>/.discmenus/<disc>/` and swaps it in (`arm_transcode.menus.task`), and `/complete` records the
folder it wrote as the task's `output_path`.

**Which encodes.** The job's newest session application with a finished encode; per track its newest finished
encode. **Which disc title.** A Blu-ray title of one clip maps by that clip (so a button that plays the play-all
from an episode's mark finds the episode), a title of several clips by the playlist MakeMKV named it after, and a
DVD title by its title number (`TINFO 24`). **Which library item.** An episode by season and number, a special by
season 0 and its number, a trailer, OP/ED or other bonus by its path under the show folder once it is in its
bonus folder (`arm_backend.disc_menus.rename`), and a movie disc's titles by file. A title that is none of these
is left out, with a note. **Where.** The deepest folder all the encodes share, up one level from a season or bonus
folder; the pack is `<that>/.discmenus/<bd|dvd>-d<disc number>` (or the job id's last 6 characters with no disc
number). One job owns a pack folder: a folder another job's pack holds is left alone and the build writes
`<id>-2`, and so on.

**When.** The dispatcher's sweep (`sweep_menu_packs`) queues one for a job whose menu track is copied, whose rip
is complete, and whose application's encodes are all finished (at least one done), when no pack task is live and
none was queued since its newest encode finished. A last encode that failed, passthrough moves (no `/complete`)
and two encodes finishing at once are all covered by that. A job whose encodes have all left the media folder
(`arm-to-jellyfin.sh` in move mode) gets none: its pack can't be built any more. The job page's Build asks for one by hand
(`queue_menu_pack`), and so does renaming an encode from the menu (`rebuild_after_rename`).

**Job delete** removes the pack folders the job's builds recorded, each only while its `menu.json` still names
the job (`remove_job_packs`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.disc_menus.preview_tasks import menu_dir_of
from arm_backend.disc_menus.proposals import scan_titles
from arm_backend.disc_menus.rename import BONUS_FOLDERS
from arm_backend.ws import WSHub
from arm_common import (
    DiscType,
    Job,
    MediaType,
    SessionApplication,
    Track,
    TrackKind,
    TrackRole,
    TrackStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)
from arm_common.schemas import MenuPackItem, MenuPackPlan

logger = logging.getLogger("arm_backend.disc_menus.packs")

PACKS_FOLDER = ".discmenus"
PACK_FILE = "menu.json"
_LIVE = (TranscodeTaskStatus.QUEUED, TranscodeTaskStatus.IN_PROGRESS)
_TERMINAL = (TranscodeTaskStatus.DONE, TranscodeTaskStatus.FAILED)
# A folder one level under the show: a season, or a bonus folder.
_SHOW_SUBFOLDER = re.compile(r"(?:Season \d+|Specials|trailers|other|extras)", re.I)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PackRefused(Exception):
    pass


@dataclass(frozen=True)
class _Encodes:
    application: SessionApplication
    # Every encode task of the application, and per track its newest finished one.
    tasks: list[TranscodeTask]
    done: dict[str, TranscodeTask]

    @property
    def finished(self) -> bool:
        return all(t.status in _TERMINAL for t in self.tasks)

    @property
    def newest_finish(self) -> datetime:
        return max((_aware(t.updated_at) for t in self.tasks), default=_EPOCH)


def _media_root() -> Path:
    from arm_backend.config import settings

    return Path(settings.MEDIA_ROOT)


def _encodes_moved(encodes: _Encodes) -> bool:
    """Has every finished encode left the media folder?"""
    root = _media_root()
    return not any((root / (t.output_path or "")).is_file() for t in encodes.done.values())


MOVED = "the job's encodes have left ARM's media folder; a pack is built before they're moved"


def _aware(at: datetime | None) -> datetime:
    if at is None:
        return _EPOCH
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


async def menu_track_of(db: AsyncSession, job_id: str) -> Track | None:
    return (
        await db.execute(select(Track).where(col(Track.job_id) == job_id).where(col(Track.kind) == TrackKind.DISC_MENU))
    ).scalar_one_or_none()


async def pack_tasks_of(db: AsyncSession, track_id: str) -> list[TranscodeTask]:
    """The menu track's pack tasks, oldest first."""
    rows = (
        (
            await db.execute(
                select(TranscodeTask)
                .where(col(TranscodeTask.source_track_id) == track_id)
                .where(col(TranscodeTask.kind) == TranscodeTaskKind.MENU_PACK)
            )
        )
        .scalars()
        .all()
    )
    return sorted(rows, key=lambda t: t.id)


async def _application_encodes(db: AsyncSession, application: SessionApplication) -> _Encodes:
    tasks = (
        (
            await db.execute(
                select(TranscodeTask)
                .where(col(TranscodeTask.session_application_id) == application.id)
                .where(col(TranscodeTask.kind) == TranscodeTaskKind.ENCODE)
            )
        )
        .scalars()
        .all()
    )
    done: dict[str, TranscodeTask] = {}
    for t in sorted(tasks, key=lambda t: t.id):
        if t.status == TranscodeTaskStatus.DONE and t.output_path:
            done[t.source_track_id] = t
    return _Encodes(application, list(tasks), done)


async def _pack_encodes(db: AsyncSession, job_id: str) -> _Encodes | None:
    """The job's newest session application with a finished encode, and its encodes."""
    applications = (
        (await db.execute(select(SessionApplication).where(col(SessionApplication.job_id) == job_id))).scalars().all()
    )
    # ULID ids sort by creation.
    for application in sorted(applications, key=lambda a: a.id, reverse=True):
        encodes = await _application_encodes(db, application)
        if encodes.done:
            return encodes
    return None


def show_folder(paths: list[str]) -> PurePosixPath | None:
    """The deepest folder every encode shares, up one level from a season or bonus folder; None when they share
    no folder under the media root."""
    if not paths:
        return None
    common = PurePosixPath(os.path.commonpath([str(PurePosixPath(p).parent) for p in paths]))
    if _SHOW_SUBFOLDER.fullmatch(common.name):
        common = common.parent
    return None if str(common) in ("", ".", "/") else common


def disc_id(job: Job) -> str:
    kind = "bd" if job.disc_type == DiscType.BLURAY else "dvd"
    return f"{kind}-d{job.disc_number}" if job.disc_number else f"{kind}-{job.id[-6:].lower()}"


def disc_name(job: Job) -> str:
    show = job.title or "Disc menu"
    return f"{show}, Disc {job.disc_number}" if job.disc_number else show


def _clips(value: object) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", value)] if isinstance(value, str) else []


def _where(job: Job, title: dict[str, Any]) -> dict[str, Any] | None:
    """The disc title a track is, as the plan's `bd_clip`, `bd_playlist` or `dvd_title`."""
    if job.disc_type == DiscType.DVD:
        n = title.get("original_title_id")
        return {"dvd_title": int(n)} if n is not None else None
    clips = _clips(title.get("segment_map"))
    if len(clips) == 1:
        return {"bd_clip": f"{clips[0]:05d}"}
    playlist = str(title.get("source_playlist") or "")
    if clips and playlist.lower().endswith(".mpls"):
        return {"bd_playlist": playlist[:-5]}
    return None


def _what(job: Job, track: Track, rel: PurePosixPath) -> tuple[dict[str, Any] | None, str | None]:
    """The library item a track's encode is (`rel`: under the show folder), or why the pack leaves it out."""
    role = track.role
    if job.media_type != MediaType.TV:
        if role is None or role == TrackRole.MAIN:
            return {}, None
        return None, f"a {role.value} on a movie disc isn't in Jellyfin's extras layout"
    if role == TrackRole.EPISODE:
        if track.episode_number is None:
            return None, "an episode with no episode number"
        return {"episode": track.episode_number, "season": track.season}, None
    if role == TrackRole.SPECIAL:
        if track.episode_number is None:
            return None, "a special with no season 0 number"
        return {"episode": track.episode_number, "season": 0}, None
    if role in BONUS_FOLDERS:
        if len(rel.parts) < 2 or rel.parts[0] != BONUS_FOLDERS[role]:
            return None, f"a {role.value} that isn't in {BONUS_FOLDERS[role]}/ yet (name it from the menu)"
        return {"extra": str(rel)}, None
    return None, f"a title with role {role.value if role else 'none'}"


async def plan_for(db: AsyncSession, task: TranscodeTask) -> tuple[MenuPackPlan | None, str | None]:
    """What a `menu_pack` task builds, or why it can't run. Read at `/register`, so a rename since it was queued
    is seen."""
    track = (await db.execute(select(Track).where(col(Track.id) == task.source_track_id))).scalar_one_or_none()
    if track is None:
        return None, "its menu track is gone"
    if menu_dir_of(track) is None:
        return None, "the job's disc menu hasn't been copied"
    job = (await db.execute(select(Job).where(col(Job.id) == track.job_id))).scalar_one_or_none()
    application = (
        await db.execute(select(SessionApplication).where(col(SessionApplication.id) == task.session_application_id))
    ).scalar_one_or_none()
    if job is None or application is None:
        return None, "its job or session application is gone"
    encodes = await _application_encodes(db, application)
    if not encodes.done:
        return None, "the job has no finished encodes"
    if _encodes_moved(encodes):
        return None, MOVED
    show = show_folder([t.output_path or "" for t in encodes.done.values()])
    if show is None:
        return None, "the job's encodes share no show folder"
    tracks = {t.id: t for t in (await db.execute(select(Track).where(col(Track.job_id) == job.id))).scalars().all()}
    titles = {t.get("index"): t for t in scan_titles(job)}
    items: list[MenuPackItem] = []
    notes: list[str] = []
    for track_id, encode in sorted(encodes.done.items(), key=lambda kv: tracks[kv[0]].index if kv[0] in tracks else 0):
        title_track = tracks.get(track_id)
        if title_track is None or title_track.kind != TrackKind.VIDEO_TITLE or title_track.role == TrackRole.PLAY_ALL:
            continue
        output = PurePosixPath(encode.output_path or "")
        label = f"title {title_track.source_ref} ({output.name})"
        try:
            title = titles.get(int(title_track.source_ref))
        except ValueError:
            title = None
        where = _where(job, title) if title is not None else None
        if where is None:
            notes.append(f"{label}: the disc scan doesn't say which disc title it is")
            continue
        # The show folder is every encode's common folder, so each one is under it.
        what, why = _what(job, title_track, output.relative_to(show))
        if what is None:
            notes.append(f"{label}: left out, {why}")
            continue
        items.append(MenuPackItem(file=str(output), **what, **where))
    if not items:
        return None, "none of the job's encodes is a disc title the pack can play" + (
            f" ({'; '.join(notes)})" if notes else ""
        )
    plan = MenuPackPlan(
        pack_dir=str(show / PACKS_FOLDER / disc_id(job)),
        name=disc_name(job),
        season=job.season if job.media_type == MediaType.TV else None,
        job_id=job.id,
        items=items,
        notes=notes,
    )
    return plan, None


async def menu_pack_inputs(
    db: AsyncSession, task: TranscodeTask
) -> tuple[Path | None, MenuPackPlan | None, str | None]:
    """The menu folder a `menu_pack` task reads and what it builds, or why it can never run."""
    plan, reason = await plan_for(db, task)
    if plan is None:
        return None, None, reason
    track = (await db.execute(select(Track).where(col(Track.id) == task.source_track_id))).scalar_one()
    return menu_dir_of(track), plan, None


async def _queue(db: AsyncSession, job: Job, track: Track, encodes: _Encodes, hub: WSHub | None) -> TranscodeTask:
    show = show_folder([t.output_path or "" for t in encodes.done.values()])
    task = TranscodeTask(
        kind=TranscodeTaskKind.MENU_PACK,
        session_application_id=encodes.application.id,
        source_track_id=track.id,
        # Where it means to write; /complete records the folder it did write (another job's pack may hold this).
        output_path=str(show / PACKS_FOLDER / disc_id(job)) if show is not None else None,
        status=TranscodeTaskStatus.QUEUED,
        attempts=0,
        progress_pct=0,
    )
    db.add(task)
    await db.flush()
    logger.info("menu pack queued job_id=%s task_id=%s", job.id, task.id)
    if hub is not None:
        await hub.emit(
            topic="transcode.events",
            event_type="task.queued",
            payload={"task_id": task.id, "kind": task.kind.value, "session_application_id": encodes.application.id},
            job_id=job.id,
            track_id=track.id,
            session=db,
        )
    return task


async def _refusal(
    db: AsyncSession, job: Job, track: Track | None
) -> tuple[str | None, _Encodes | None, list[TranscodeTask]]:
    """Why the job's pack can't be queued now (None when it can), its encodes, and its pack tasks."""
    if track is None or menu_dir_of(track) is None:
        return "the job's disc menu hasn't been copied", None, []
    tasks = await pack_tasks_of(db, track.id)
    if job.ripped_at is None:
        return "the job's rip isn't complete", None, tasks
    if any(t.status in _LIVE for t in tasks):
        return "a pack build is already queued or running", None, tasks
    encodes = await _pack_encodes(db, job.id)
    if encodes is None:
        return "the job has no finished encodes yet", None, tasks
    if not encodes.finished:
        return "the job's encodes are still running", encodes, tasks
    if _encodes_moved(encodes):
        return MOVED, encodes, tasks
    return None, encodes, tasks


async def pack_refusal(db: AsyncSession, job: Job, track: Track) -> str | None:
    return (await _refusal(db, job, track))[0]


async def sweep_menu_packs(db: AsyncSession, hub: WSHub | None = None) -> int:
    """Queue a pack build for every job whose encodes finished since its last one. Flushes; the caller commits.
    Returns how many it queued."""
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
        job = (await db.execute(select(Job).where(col(Job.id) == track.job_id))).scalar_one_or_none()
        if job is None:
            continue
        refusal, encodes, tasks = await _refusal(db, job, track)
        if refusal is not None or encodes is None:
            continue
        # One build per batch of encodes: none since the newest one finished (a failed build waits for Build).
        if any(_aware(t.created_at) >= encodes.newest_finish for t in tasks):
            continue
        await _queue(db, job, track, encodes, hub)
        queued += 1
    return queued


async def queue_menu_pack(db: AsyncSession, job: Job, hub: WSHub | None) -> TranscodeTask:
    """Build the job's pack again, by hand (the job page). Flushes; the caller commits. Raises PackRefused."""
    track = await menu_track_of(db, job.id)
    refusal, encodes, _ = await _refusal(db, job, track)
    if refusal is not None:
        raise PackRefused(refusal)
    assert track is not None and encodes is not None
    return await _queue(db, job, track, encodes, hub)


async def rebuild_after_rename(db: AsyncSession, job: Job, hub: WSHub | None) -> TranscodeTask | None:
    """An encode was renamed from the menu: rebuild a pack already built, so its cells name the new file. A job
    with no pack yet gets its first from the sweep. Never raises."""
    track = await menu_track_of(db, job.id)
    refusal, encodes, tasks = await _refusal(db, job, track)
    if refusal is not None or encodes is None or track is None or not tasks:
        return None
    return await _queue(db, job, track, encodes, hub)


def pack_owner(folder: Path) -> str | None:
    """The job a pack folder's `menu.json` names, or None."""
    try:
        data = json.loads((folder / PACK_FILE).read_text())
    except OSError, ValueError:
        return None
    disc = data.get("disc") if isinstance(data, dict) else None
    job = disc.get("job") if isinstance(disc, dict) else None
    return job if isinstance(job, str) else None


def owned_pack_folder(media_root: Path, relative: str | None, job_id: str) -> Path | None:
    """A pack folder a build of `job_id` recorded, while it is still there, under a `.discmenus/` folder inside the
    media root, and still names the job; else None."""
    if not relative:
        return None
    root = media_root.resolve()
    folder = (media_root / relative).resolve()
    if root not in folder.parents or folder.parent.name != PACKS_FOLDER or not folder.is_dir():
        return None
    return folder if pack_owner(folder) == job_id else None


async def job_pack_folders(db: AsyncSession, job_id: str, media_root: Path) -> list[Path]:
    """The pack folders the job's finished builds wrote and still own."""
    track = await menu_track_of(db, job_id)
    if track is None:
        return []
    folders = [
        owned_pack_folder(media_root, t.output_path, job_id)
        for t in await pack_tasks_of(db, track.id)
        if t.status == TranscodeTaskStatus.DONE
    ]
    return list(dict.fromkeys(f for f in folders if f is not None))


def remove_pack_folder(folder: Path) -> None:
    """Remove a pack folder, and its `.discmenus/` when that's left empty."""
    shutil.rmtree(folder)
    try:
        folder.parent.rmdir()
    except OSError:
        pass
    logger.info("menu pack removed path=%s", folder)


async def remove_job_packs(db: AsyncSession, job_id: str, media_root: Path) -> int:
    """Job delete's hook: remove the job's own pack folders. Never raises; returns how many it removed."""
    removed = 0
    try:
        for folder in await job_pack_folders(db, job_id, media_root):
            try:
                remove_pack_folder(folder)
                removed += 1
            except OSError as e:
                logger.warning("menu pack delete failed path=%s: %s", folder, e)
    except Exception:
        logger.exception("menu pack delete failed job_id=%s", job_id)
    return removed


def folder_size(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.iterdir() if p.is_file())
