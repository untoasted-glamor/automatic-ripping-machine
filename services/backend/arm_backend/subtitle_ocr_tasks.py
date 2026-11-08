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
files exist only then (never queue a task before its input exists).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.sidecars import image_subtitle_sidecars
from arm_common import TranscodeTask, TranscodeTaskKind, TranscodeTaskStatus


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
