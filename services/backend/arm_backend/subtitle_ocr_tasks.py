"""Subtitle OCR tasks: text subtitles (`.srt`) from an encode's image subtitles.

A `subtitle_ocr` task is a `transcode_tasks` row of kind
`TranscodeTaskKind.SUBTITLE_OCR` run by the same dispatcher, transcoder image
and register/claim/heartbeat/complete/fail protocol as an encode. Its parent
is the encode task (`parent_task_id`) whose image subtitle sidecars it reads;
it shares that task's session application, source track and `output_path`
(the video), and records the `.srt` files it writes as its own
`sidecar_paths`. It never changes its session application's status: the
application is the encode's, and an OCR failure shows on the OCR task only.

It must be created only once its encode task is DONE, since the `.sup`
files exist only then (never queue a task before its input exists). Two
paths create one:

- `queue_after_encode`, when an encode completes: only while text subtitles
  are on in Settings and the job's choice (`jobs.subtitle_ocr`, NULL taking
  `config.subtitle_ocr_default_on`) is on.
- `make_text_subtitles`, the job page's action, for every finished encode of
  a job; it leaves an encode whose `.srt` files exist unless told to
  overwrite.

The transcoder always writes with overwrite, so the existing-file check
happens here, before a task is created.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.seeders import CONFIG_SINGLETON_ID
from arm_backend.sidecars import TEXT_SUBTITLE_SUFFIX, image_subtitle_sidecars, sidecar_files
from arm_backend.ws import WSHub
from arm_common import (
    Config,
    Job,
    SessionApplication,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)
from arm_common.schemas import JobSubtitleOcrChoice, JobSubtitleOcrView, SubtitleOcrModelView
from arm_common.subtitle_ocr import BUNDLED_MODEL

logger = logging.getLogger("arm_backend.subtitle_ocr_tasks")

_LIVE = (TranscodeTaskStatus.QUEUED, TranscodeTaskStatus.IN_PROGRESS)


def is_subtitle_ocr(task: TranscodeTask) -> bool:
    return task.kind == TranscodeTaskKind.SUBTITLE_OCR


async def subtitle_ocr_inputs(db: AsyncSession, task: TranscodeTask) -> tuple[list[str], str | None]:
    """The image subtitle sidecars a `subtitle_ocr` task reads, relative to
    the media root, or why it can never run: `([], reason)`."""
    if task.parent_task_id is None:
        return [], "its encode task is gone"
    parent = (
        await db.execute(select(TranscodeTask).where(col(TranscodeTask.id) == task.parent_task_id))
    ).scalar_one_or_none()
    if parent is None:
        return [], "its encode task is gone"
    if parent.status != TranscodeTaskStatus.DONE:
        return [], f"its encode task is {parent.status.value}, not done"
    inputs = image_subtitle_sidecars(parent.sidecar_paths)
    if not inputs:
        return [], "its encode task wrote no image subtitles"
    return inputs, None


def bundled_model_view() -> SubtitleOcrModelView:
    """The model built into the transcode image, for Settings."""
    return SubtitleOcrModelView(
        name=BUNDLED_MODEL.name,
        version=BUNDLED_MODEL.version,
        size=BUNDLED_MODEL.size,
        sha256=BUNDLED_MODEL.sha256,
        licence=BUNDLED_MODEL.licence,
        source=BUNDLED_MODEL.source,
    )


async def _config(db: AsyncSession) -> Config | None:
    return (await db.execute(select(Config).where(col(Config.id) == CONFIG_SINGLETON_ID))).scalar_one_or_none()


def _enabled(cfg: Config | None) -> bool:
    return cfg is not None and cfg.subtitle_ocr_enabled is True


def _choice(cfg: Config, job: Job) -> JobSubtitleOcrChoice:
    # None reads as the column default (a bare in-memory Config carries it).
    default = cfg.subtitle_ocr_default_on is not False
    return JobSubtitleOcrChoice(
        override=job.subtitle_ocr,
        default=default,
        effective=default if job.subtitle_ocr is None else job.subtitle_ocr,
    )


async def subtitle_ocr_choice(db: AsyncSession, job: Job) -> JobSubtitleOcrChoice | None:
    """The job's text subtitles choice, or None while they are off in Settings."""
    cfg = await _config(db)
    if not _enabled(cfg):
        return None
    assert cfg is not None
    return _choice(cfg, job)


def text_subtitle_paths(sidecar_paths: list[str] | None) -> list[str]:
    """The `.srt` a subtitle OCR task writes for an encode's image sidecars
    (`….eng.2.sup` gives `….eng.2.srt`), media-root-relative."""
    return [str(PurePosixPath(p).with_suffix(TEXT_SUBTITLE_SUFFIX)) for p in image_subtitle_sidecars(sidecar_paths)]


def _has_text_subtitles(media_root: Path, encode: TranscodeTask) -> bool:
    return any(path.exists() for path in sidecar_files(media_root, text_subtitle_paths(encode.sidecar_paths)))


async def _job_encodes(db: AsyncSession, job_id: str) -> list[TranscodeTask]:
    """The job's finished encodes that wrote image subtitles, oldest first
    (ULID ids are monotonic)."""
    applications = (
        (await db.execute(select(SessionApplication).where(col(SessionApplication.job_id) == job_id))).scalars().all()
    )
    if not applications:
        return []
    rows = (
        (
            await db.execute(
                select(TranscodeTask)
                .where(col(TranscodeTask.session_application_id).in_([a.id for a in applications]))
                .where(col(TranscodeTask.kind) == TranscodeTaskKind.ENCODE)
                .where(col(TranscodeTask.status) == TranscodeTaskStatus.DONE)
            )
        )
        .scalars()
        .all()
    )
    return sorted((t for t in rows if t.output_path and image_subtitle_sidecars(t.sidecar_paths)), key=lambda t: t.id)


async def job_subtitle_ocr_view(db: AsyncSession, job: Job, media_root: Path) -> JobSubtitleOcrView | None:
    """The job page's text subtitles, or None while they are off in Settings."""
    cfg = await _config(db)
    if not _enabled(cfg):
        return None
    assert cfg is not None
    encodes = await _job_encodes(db, job.id)
    return JobSubtitleOcrView(
        **_choice(cfg, job).model_dump(),
        eligible_encodes=len(encodes),
        with_text_subtitles=sum(1 for e in encodes if _has_text_subtitles(media_root, e)),
    )


def _new_task(encode: TranscodeTask, replaced: list[str] | None = None) -> TranscodeTask:
    return TranscodeTask(
        kind=TranscodeTaskKind.SUBTITLE_OCR,
        parent_task_id=encode.id,
        session_application_id=encode.session_application_id,
        source_track_id=encode.source_track_id,
        output_path=encode.output_path,
        status=TranscodeTaskStatus.QUEUED,
        replaced_sidecar_paths=replaced or None,
        attempts=0,
        progress_pct=0,
    )


async def _emit_queued(hub: WSHub | None, db: AsyncSession, job_id: str, tasks: list[TranscodeTask]) -> None:
    if hub is None:
        return
    for task in tasks:
        await hub.emit(
            topic="transcode.events",
            event_type="task.queued",
            payload={
                "task_id": task.id,
                "kind": task.kind.value,
                "parent_task_id": task.parent_task_id,
                "session_application_id": task.session_application_id,
                "output_path": task.output_path,
            },
            job_id=job_id,
            track_id=task.source_track_id,
            session=db,
        )


async def queue_after_encode(
    db: AsyncSession, encode: TranscodeTask, job_id: str, hub: WSHub | None
) -> TranscodeTask | None:
    """Queue the text subtitles task of an encode that has just completed,
    when it wrote image subtitles and the job's choice is on. Flushes the new
    row; the caller commits. Returns it, or None when none is due."""
    if encode.kind != TranscodeTaskKind.ENCODE or encode.status != TranscodeTaskStatus.DONE:
        return None
    if not encode.output_path or not image_subtitle_sidecars(encode.sidecar_paths):
        return None
    cfg = await _config(db)
    if not _enabled(cfg):
        return None
    assert cfg is not None
    job = (await db.execute(select(Job).where(col(Job.id) == job_id))).scalar_one_or_none()
    if job is None or not _choice(cfg, job).effective:
        return None
    task = _new_task(encode)
    db.add(task)
    await db.flush()
    await _emit_queued(hub, db, job_id, [task])
    logger.info("subtitle ocr queued task_id=%s encode_task_id=%s job_id=%s", task.id, encode.id, job_id)
    return task


class SubtitleOcrOff(Exception):
    """Text subtitles are off in Settings."""


@dataclass
class MakeTextSubtitlesOutcome:
    tasks: list[TranscodeTask] = field(default_factory=list)
    # (encode task, reason) for each finished encode left alone.
    skipped: list[tuple[TranscodeTask, Literal["running", "exists"]]] = field(default_factory=list)


async def make_text_subtitles(
    db: AsyncSession, job: Job, *, overwrite: bool, media_root: Path, hub: WSHub | None
) -> MakeTextSubtitlesOutcome:
    """Queue a text subtitles task for each of the job's finished encodes
    that wrote image subtitles (the job page's action). An encode with a
    queued or running one is skipped; so is one whose `.srt` files exist,
    unless `overwrite`. With `overwrite`, the encode's earlier finished OCR
    rows are dropped and the `.srt` they recorded handed to the new task
    (`replaced_sidecar_paths`), which removes any it doesn't write again once
    it completes. Flushes; the caller commits. Raises SubtitleOcrOff while
    text subtitles are off in Settings."""
    if not _enabled(await _config(db)):
        raise SubtitleOcrOff
    outcome = MakeTextSubtitlesOutcome()
    encodes = await _job_encodes(db, job.id)
    children: dict[str, list[TranscodeTask]] = {}
    if encodes:
        rows = (
            (
                await db.execute(
                    select(TranscodeTask)
                    .where(col(TranscodeTask.kind) == TranscodeTaskKind.SUBTITLE_OCR)
                    .where(col(TranscodeTask.parent_task_id).in_([e.id for e in encodes]))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            assert row.parent_task_id is not None
            children.setdefault(row.parent_task_id, []).append(row)
    for encode in encodes:
        own = children.get(encode.id, [])
        if any(c.status in _LIVE for c in own):
            outcome.skipped.append((encode, "running"))
            continue
        if not overwrite and _has_text_subtitles(media_root, encode):
            outcome.skipped.append((encode, "exists"))
            continue
        replaced: list[str] = []
        for old in own:
            for path in [*(old.replaced_sidecar_paths or []), *(old.sidecar_paths or [])]:
                if path not in replaced:
                    replaced.append(path)
            await db.delete(old)
        task = _new_task(encode, replaced)
        db.add(task)
        outcome.tasks.append(task)
    await db.flush()
    await _emit_queued(hub, db, job.id, outcome.tasks)
    if outcome.tasks:
        logger.info(
            "subtitle ocr requested job_id=%s queued=%d skipped=%d overwrite=%s",
            job.id,
            len(outcome.tasks),
            len(outcome.skipped),
            overwrite,
        )
    return outcome
