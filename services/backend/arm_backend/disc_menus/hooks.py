"""The disc-menu hooks in shared code, one call each."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.disc_menus.proposals import make_proposals, read_preview
from arm_backend.disc_menus.rename import apply_pending
from arm_backend.ws import WSHub
from arm_common import Job, TranscodeTask, TranscodeTaskKind

logger = logging.getLogger("arm_backend.disc_menus.hooks")


def _media_root() -> Path:
    from arm_backend.config import settings

    return Path(settings.MEDIA_ROOT)


async def after_task_complete(db: AsyncSession, task: TranscodeTask, job_id: str, hub: WSHub | None) -> None:
    """At a task's /complete: a menu preview's proposals, a menu pack's event, or an encode's pending rename. Never
    raises: the task's own completion must stand."""
    try:
        if task.kind == TranscodeTaskKind.MENU_PREVIEW:
            preview = read_preview(Path(task.output_path or ""))
            job = (await db.execute(select(Job).where(col(Job.id) == job_id))).scalar_one_or_none()
            if preview is None or job is None:
                return
            await make_proposals(db, job, preview, task)
            if hub is not None:
                await hub.emit(
                    topic="transcode.events",
                    event_type="menu.proposals",
                    payload={"job_id": job_id, "task_id": task.id},
                    job_id=job_id,
                    session=db,
                )
        elif task.kind == TranscodeTaskKind.MENU_PACK:
            logger.info("menu pack built job_id=%s task_id=%s path=%s", job_id, task.id, task.output_path)
            if hub is not None:
                await hub.emit(
                    topic="transcode.events",
                    event_type="menu.pack",
                    payload={"job_id": job_id, "task_id": task.id, "path": task.output_path},
                    job_id=job_id,
                    session=db,
                )
        elif task.kind == TranscodeTaskKind.ENCODE:
            await apply_pending(db, task, _media_root())
    except Exception:
        logger.exception("disc menu step after task_id=%s failed", task.id)
