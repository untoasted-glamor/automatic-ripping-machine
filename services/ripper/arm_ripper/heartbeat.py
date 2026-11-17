"""The ripper's heartbeat: report the drive's media status to the backend every HEARTBEAT_INTERVAL_SECONDS and pick
up a job the backend holds for this drive."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from arm_common import DriveMediaStatus, JobStatus
from arm_ripper.backend_client import BackendClient, JobView
from arm_ripper.drive_handle import DriveHandle
from arm_ripper.drive_status import probe_drive_media
from arm_ripper.job_controller import JobController
from arm_ripper.source import is_file_source

logger = logging.getLogger("arm_ripper")

# Heartbeat carries the current CDROM_DRIVE_STATUS reading to the
# backend so the manual-trigger endpoint can refuse clicks made
# against an empty / open tray. 30s gives a click-time check that's
# at most ~30s stale; a stale heartbeat (older than the backend's
# freshness window) falls back to "unknown" and the request is
# allowed through to identify (which will fail visibly).
HEARTBEAT_INTERVAL_SECONDS = 30.0


# AWAITING_REVIEW is intentionally excluded: recovery for review-gated discs is
# owned by the boot probe + the review-countdown auto-start path. Picking up an
# AWAITING_REVIEW job here would call controller.pickup → _run_rip → rip_start,
# which transitions straight to RIPPING and bypasses the countdown, manual_pause,
# and global ripping_paused. Only re-acquire IDENTIFIED (the resolve-after-timeout
# seated disc — Defect-1's target) and RIPPING (harmless restart race).
_RIP_READY = frozenset({JobStatus.IDENTIFIED, JobStatus.RIPPING})


async def maybe_reacquire_current_job(
    controller: JobController,
    *,
    get_current_job: Callable[[str], Awaitable[JobView | None]],
    drive_id: str,
    device_path: str,
    seated: bool,
) -> None:
    """Idle re-probe: if the ripper is idle with a disc seated, ask the backend
    for the drive's current non-terminal job. If it's rip-ready (operator
    resolved it after our in-memory wait timed out), pick it up. Pull-based, so
    it survives a backend restart and the 30-min ceiling."""
    if not seated or not controller.is_idle():
        return
    try:
        job = await get_current_job(drive_id)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("current-job reprobe failed: %s", exc)
        return
    if job is None or job.status not in _RIP_READY:
        return
    logger.info("reacquiring current job %s status=%s via heartbeat reprobe", job.id, job.status.value)
    await controller.pickup(job, device_path)


async def maybe_pickup_remux(
    controller: JobController,
    *,
    get_remux_job: Callable[[str], Awaitable[JobView | None]],
    drive_id: str,
) -> None:
    """Idle re-probe for the remux lane: while no remux runs, ask the backend
    for a job on this drive still `remuxing` from its kept disc backup, and
    start it in the background. With or without a disc seated: a remux never
    reads the drive. Covers a missed `rip.retry`, a ripper restart mid-remux,
    and a retry started while another remux ran."""
    if not controller.remux_idle:
        return
    try:
        job = await get_remux_job(drive_id)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("remux-job reprobe failed: %s", exc)
        return
    if job is None or not controller.remux_idle:
        return
    logger.info("picking up job %s remuxing from its disc backup via heartbeat reprobe", job.id)
    controller.start_remux(job.id)


async def send_heartbeat(
    client: BackendClient, drive_id: str, handle: DriveHandle, controller: JobController
) -> tuple[DriveMediaStatus, str | None]:
    """Probe the media status and post one heartbeat carrying it plus the
    controller's scan activity (None when not scanning). Returns the status
    and the node it was read from. Raises on transport errors."""
    device_path = handle.current
    status = _media_status(device_path)
    await client.heartbeat(drive_id=drive_id, media_status=status, scan=controller.scan_activity)
    return status, device_path


async def push_scan_activity(
    client: BackendClient, drive_id: str, handle: DriveHandle, controller: JobController, lock: asyncio.Lock
) -> None:
    """An immediate heartbeat when a scan starts, retries or ends, so the UI's
    "scanning" doesn't lag a heartbeat interval. Serialized so a slow "started"
    post can't land after the "ended" one; each reads the state at send time."""
    async with lock:
        await send_heartbeat(client, drive_id, handle, controller)


def _media_status(device_path: str | None) -> DriveMediaStatus:
    """The media status a heartbeat reports: DETACHED while the drive is absent, LOADED for a file source (an ISO
    is always present), otherwise what the drive's ioctl probe says.
    """
    if device_path is None:
        return DriveMediaStatus.DETACHED
    if is_file_source(device_path):
        return DriveMediaStatus.LOADED
    status, _ = probe_drive_media(device_path)
    return status


async def heartbeat_loop(client: BackendClient, drive_id: str, handle: DriveHandle, controller: JobController) -> None:
    """Post the current media status to the backend every
    HEARTBEAT_INTERVAL_SECONDS. Errors are logged + swallowed —
    the heartbeat is best-effort; a row that goes stale makes the
    manual-trigger pre-check refuse clicks until the next beat lands.

    Reads the shared DriveHandle every beat, so a drive that moved to a
    new srN is probed at its new node with no restart. While the drive is
    absent the beat carries DETACHED — and keeps going, so the backend can
    derive OFFLINE while the row stays visible.

    For ISO sources we skip the SCSI ioctl (it fails on regular files)
    and report `loaded` unconditionally — the source is always present
    by construction in manual-trigger mode.

    After each successful heartbeat, maybe_reacquire_current_job checks
    whether the idle ripper should re-acquire a rip-ready job from the
    backend (handles the case where the in-memory wait timed out or the
    backend restarted while a disc was seated), and maybe_pickup_remux
    whether its remux lane should start a job remuxing from its backup.

    Each beat also carries the controller's scan activity; the controller
    pushes one at once on every change too (`push_scan_activity`).
    """
    while True:
        try:
            status, device_path = await send_heartbeat(client, drive_id, handle, controller)
            await maybe_pickup_remux(controller, get_remux_job=client.get_remux_job, drive_id=drive_id)
            if device_path is not None:
                await maybe_reacquire_current_job(
                    controller,
                    get_current_job=client.get_current_job,
                    drive_id=drive_id,
                    device_path=device_path,
                    seated=(status == DriveMediaStatus.LOADED),
                )
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
