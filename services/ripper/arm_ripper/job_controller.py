import asyncio
import contextlib
import functools
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from arm_common import DiscType, Job, JobStatus, TrackKind, TrackStatus, with_log_context
from arm_common.schemas import (
    JobView,
    RipperScanActivity,
    RipStartResponse,
    ScanResult,
    TrackView,
    WSEnvelope,
)
from arm_ripper.backend_client import BackendClient
from arm_ripper.background_tasks import spawn_tracked
from arm_ripper.backend_retry import BackendRetry
from arm_ripper.drive_handle import DriveHandle
from arm_ripper.eject import Ejector
from arm_ripper.makemkv_prep import MakeMKVPrep
from arm_ripper.menus.extract import MenuOutcome
from arm_ripper.menus.extract import failed as menu_failed
from arm_ripper.ripper_config import ripper_config_or_none
from arm_ripper.resolution_wait import IDENTIFY_WAIT, RESOLUTION_TIMEOUT_UNSET, REVIEW_WAIT, ResolutionWaiter, WaitSpec
from arm_ripper.rip import RipResult, rip_all
from arm_ripper.rip.dispatcher import DEFAULT_MIN_LENGTH_SECONDS
from arm_ripper.rip.optical_rip import BACKUP_COMPLETE_MARKER, SELECTION_BACKUP_DIRNAME
from arm_ripper.rip.track_callbacks import TransferStats
from arm_ripper.drive_poll import _ABSENT_ERRNOS, DriveState, read_drive_status
from arm_ripper.scan import ScanError, scan as scan_disc
from arm_ripper.source import is_file_source
from arm_ripper.ws_client import WSClient

logger = logging.getLogger("arm_ripper.job_controller")

EJECT_GRACE_SECONDS = 3.0
# A DVD/BD drive that drops off the bus mid-rip (a USB enclosure reset; it
# often re-enumerates under a new srN): how long the rip waits for it to come
# back with the disc readable before finishing the job as partial, how often
# it looks, and how many times one rip may resume.
DEVICE_RETURN_WAIT_SECONDS = 180.0
DEVICE_WATCH_INTERVAL_SECONDS = 1.0
MAX_DEVICE_RESUMES = 3
# The disc menu row's error when a rip ends without settling it (the drive
# dropped during the backup and didn't come back).
MENU_NOT_COPIED = "The rip ended before the disc menu was copied"


@dataclass
class _RipRun:
    """One running rip's own state. A disc rip and a remux from a kept disc
    backup can run at once (the remux lane), so it isn't kept on the
    controller. `drive_released`: the rip no longer uses the drive, from the
    start for a remux, or once backup-then-remux has ejected its disc. The
    drive is then the operator's: no drop detection, no second eject, and a
    disc inserted meanwhile is the next one."""

    drive_released: bool
RAW_ROOT = Path("/raw")
# A freshly-settled disc can report DISC_OK via the CDROM_DRIVE_STATUS ioctl
# before the medium is *data*-readable, so makemkvcon enumerates zero titles.
# The disc IS present (per ioctl), so a zero-title scan there means "not ready
# yet," not "genuinely empty" — retry, letting the drive finish spinning up /
# the TOC become readable. Generous budget (a quick 1-2s retry would miss a slow
# settle — the field report saw 4/4 discs fail); bounded so a genuinely
# unreadable disc resolves to a clear give-up in ~26s.
SCAN_NOT_READY_MAX_ATTEMPTS = 5
SCAN_NOT_READY_BACKOFFS = (2.0, 4.0, 8.0, 12.0)  # between attempts 1→2, 2→3, 3→4, 4→5


def _disconnect_reason(resumes: int) -> str:
    """Why the titles a lost drive left are failed: it did not come back in time, or it dropped more often than a
    rip may resume."""
    return (
        f"the drive disconnected mid-rip and did not come back within {DEVICE_RETURN_WAIT_SECONDS:.0f}s"
        if resumes < MAX_DEVICE_RESUMES
        else f"the drive disconnected mid-rip {resumes + 1} times; giving up"
    )


def _command_payload(envelope: WSEnvelope) -> dict[str, Any]:
    """A WS command's payload, or an empty dict when it isn't one."""
    return envelope.payload if isinstance(envelope.payload, dict) else {}


def _previously_ripped(response: httpx.Response) -> bool:
    """identify's 409 for a disc the backend refused because it was ripped
    before (on_previously_ripped=reject)."""
    if response.status_code != 409:
        return False
    try:
        detail = response.json().get("detail")
    except ValueError:
        return False
    return isinstance(detail, dict) and detail.get("reason") == "previously_ripped"


def _resumable(disc_type: DiscType, device_path: str, tracks: list[TrackView]) -> bool:
    """Whether a rip can wait out its drive dropping off the bus and resume on the drive's new node: a DVD or BD read
    from a real drive, unless every track is a full-disc backup or dump."""
    # A DVD/BD rip whose drive drops off the bus mid-rip stops makemkvcon
    # and resumes on the drive's new node instead of failing every
    # remaining title (D-133). ISO and folder sources have no bus to drop.
    return (
        disc_type in (DiscType.DVD, DiscType.BLURAY)
        and not is_file_source(device_path)
        and not all(t.kind in (TrackKind.DISC_BACKUP, TrackKind.DATA_DUMP) for t in tracks)
    )


def _done_fields(result: RipResult) -> dict[str, object]:
    """The track patch for a finished title: DONE, plus each of its output path, size, checksum and duration that the
    rip reported."""
    fields: dict[str, object] = {"status": TrackStatus.DONE}
    if result.output_path is not None:
        fields["output_path"] = str(result.output_path)
    if result.size_bytes is not None:
        fields["size_bytes"] = result.size_bytes
    if result.sha256 is not None:
        fields["sha256"] = result.sha256
    if result.duration_seconds is not None:
        fields["duration_seconds"] = result.duration_seconds
    return fields


def _progress_payload(track_id: str, fraction: float, stats: TransferStats | None) -> dict[str, object]:
    """The WS progress payload for a track: its percentage, plus the byte detail, rounded rate and phase of a copy
    when the rip reports them."""
    payload: dict[str, object] = {
        "track_id": track_id,
        "progress_pct": round(fraction * 100, 1),
    }
    if stats is not None:
        # Byte detail for a copy (the full-disc dump): the UI shows
        # "X of Y GB at R MB/s" and derives the ETA from the rate.
        payload["bytes_done"] = stats["bytes_done"]
        payload["bytes_total"] = stats["bytes_total"]
        payload["rate_bps"] = round(stats["rate_bps"]) if stats["rate_bps"] is not None else None
        if "phase" in stats:
            payload["phase"] = stats["phase"]
    return payload


class JobController:
    """Drives one disc through scan → identify → rip → eject."""

    def __init__(
        self,
        client: BackendClient,
        drive_id: str,
        *,
        ws: WSClient | None = None,
        device_path: str | DriveHandle | None = None,
        default_min_length_seconds: int = DEFAULT_MIN_LENGTH_SECONDS,
        backup_full_selections: bool = False,
        resolution_timeout: float | None = RESOLUTION_TIMEOUT_UNSET,  # type: ignore[assignment]
        on_scan_activity_change: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        # Disc scan in progress (None = not scanning), carried by every
        # heartbeat so the UI can show "scanning" before identify creates a
        # job. `on_scan_activity_change` (main.py: an immediate heartbeat)
        # fires on each change so the UI doesn't lag a heartbeat interval.
        self._scan_activity: RipperScanActivity | None = None
        self._on_scan_activity_change = on_scan_activity_change
        self._scan_notify_tasks: set[asyncio.Task[None]] = set()
        # The retried backend calls a rip makes (rip-start, rip-complete, track PATCH, identify).
        self._backend = BackendRetry(client, drive_id)
        # Getting MakeMKV ready for a disc: key refresh plus the background KEYDB/SDF refreshes.
        self._makemkv = MakeMKVPrep(client)
        self._wipe_tasks: set[asyncio.Task[None]] = set()
        self._drive_id = drive_id
        self._ws = ws
        # Each ripper container owns exactly one optical drive. The node it
        # occupies can change while we run (replug under a new srN), so we
        # hold the shared DriveHandle rather than a copy of the path — the
        # poll loop keeps it current and every read here sees the move. A
        # plain str (ISO source, tests) is wrapped as a fixed handle.
        if isinstance(device_path, DriveHandle):
            self._device = device_path
        elif device_path is None:
            self._device = DriveHandle()
        else:
            self._device = DriveHandle.fixed(device_path)
        # Umount/eject is its own job; it reads the same shared handle.
        self._ejector = Ejector(self._device)
        # Host-side baseline `--minlength` for `makemkvcon mkv all`. The
        # backend can override per-rip via `RipStartResponse.min_length_seconds`
        # (resolved from the Session's `overrides_json["min_length_seconds"]`);
        # when no override is sent we fall back to this value. Injected by
        # `main.py` from `ARM_MIN_LENGTH_SECONDS`; tests get the dispatcher
        # default (600).
        self._default_min_length_seconds = default_min_length_seconds
        # `ARM_BACKUP_FULL_SELECTIONS`: rip a disc whose every title is kept by
        # backup-then-remux too, instead of one `mkv all` (track-select Q6).
        self._backup_full_selections = backup_full_selections
        # Parks a held job until it leaves its gate. `resolution_timeout` is the ceiling on an
        # AWAITING_USER_ID / AWAITING_REVIEW park, in seconds; None means wait indefinitely — source
        # mode's one-shot container must not exit (and have the backend watchdog mark the job FAILED)
        # while the operator is still reviewing.
        self._waiter = ResolutionWaiter(client, resolution_timeout)
        # Single-flight gate: only one rip pipeline (insert-driven OR manual-
        # trigger) runs at a time per drive. Ensures the WS-triggered manual
        # path can't race with an in-progress disc-inserted task.
        self._active_lock = asyncio.Lock()
        # Backend abandons a job by emitting `job.abandoned` over WS; the
        # handler cancels this task to kill the running makemkvcon and free
        # the raw dir for cleanup. Set inside `_run_pipeline` only.
        self._active_task: asyncio.Task[None] | None = None
        self._active_job_id: str | None = None
        # Abandon-triggered eject runs fire-and-forget (the WS handler is
        # sync); kept here so overlapping abandon signals don't stack
        # umount/eject subprocess retries. Set inside `_spawn_eject` only.
        self._eject_task: asyncio.Task[None] | None = None
        # The disc lane's running rip (`_execute_rip`), for `drive_released`.
        self._disc_run: _RipRun | None = None
        # The remux lane (fork only): `rip.retry` remuxes a job's titles from
        # its kept disc backup beside a rip of the disc in the drive, one
        # remux at a time, with its own lock, task and job id.
        self._remux_lock = asyncio.Lock()
        self._remux_task: asyncio.Task[None] | None = None
        self._remux_job_id: str | None = None
        self._remux_spawned: set[asyncio.Task[None]] = set()

    @property
    def _device_path(self) -> str | None:
        """Read-only view of the shared DriveHandle. Only the poll loop writes
        the handle (spec §4) — a controller-side write would let a stale
        pickup path override the node the loop just resolved."""
        return self._device.current

    @property
    def is_active(self) -> bool:
        return self._active_lock.locked()

    @property
    def drive_released(self) -> bool:
        """The disc lane's running rip has ejected its disc and no longer uses the drive."""
        return self._disc_run is not None and self._disc_run.drive_released

    @property
    def remux_idle(self) -> bool:
        """No remux from a kept disc backup is running (the remux lane is free)."""
        return not self._remux_lock.locked()

    def start_remux(self, job_id: str) -> None:
        """Run `handle_retry` for `job_id` in the background (the WS `rip.retry`, and the heartbeat's idle pickup)."""
        task = asyncio.create_task(self.handle_retry(job_id))
        self._remux_spawned.add(task)
        task.add_done_callback(self._remux_spawned.discard)

    @property
    def scan_activity(self) -> RipperScanActivity | None:
        """The disc scan running on this drive right now, or None."""
        return self._scan_activity

    def _set_scan_activity(self, activity: RipperScanActivity | None) -> None:
        """Record scan progress and push it out without blocking the scan:
        the callback runs as a fire-and-forget task, so a slow or unreachable
        backend never delays or fails the scan (the periodic heartbeat
        re-sends the current state anyway)."""
        self._scan_activity = activity
        if self._on_scan_activity_change is None:
            return
        task = asyncio.create_task(self._notify_scan_activity_change(self._on_scan_activity_change))
        self._scan_notify_tasks.add(task)
        task.add_done_callback(self._scan_notify_tasks.discard)

    @staticmethod
    async def _notify_scan_activity_change(callback: Callable[[], Awaitable[None]]) -> None:
        try:
            await callback()
        except Exception as exc:  # noqa: BLE001 - best-effort, like the heartbeat
            logger.warning("scan-activity update failed: %s", exc)

    def is_idle(self) -> bool:
        """True when no rip pipeline is running (no active task claimed the lock).

        Used by the heartbeat idle re-probe to decide whether to attempt pickup.
        """
        return self._active_task is None

    @contextlib.asynccontextmanager
    async def _claim(self, job_id: str | None) -> AsyncIterator[None]:
        """Hold the single-flight gate for this task and job: is_idle() is False, and job.abandoned can cancel the
        task, until the block exits. A pipeline claims with None, before identify has made the job, and records the
        id itself once it has one."""
        async with self._active_lock:
            self._active_task = asyncio.current_task()
            self._active_job_id = job_id
            try:
                yield
            finally:
                self._active_task = None
                self._active_job_id = None

    async def pickup(self, job: JobView, device_path: str) -> None:
        """Re-acquire a rip-ready job from a heartbeat reprobe.

        Claims _active_task before starting so is_idle() returns False from the
        moment we enter, preventing a double-rip if heartbeat fires again while
        the pipeline is starting up. The actual rip runs via _run_rip, which
        follows the same path as a normal pipeline (rip-start → execute → complete
        → eject) but skips scan/identify since the job already exists.
        """
        if not self.is_idle():
            logger.debug("pickup ignored: controller already busy")
            return
        async with self._claim(job.id):
            with with_log_context(job_id=job.id):
                logger.info("pickup: re-acquiring job %s status=%s", job.id, job.status.value)
                await self._run_rip(job, device_path)

    async def on_ws_command(self, envelope: WSEnvelope) -> None:
        """Handler registered for `ripper.commands.{drive_id}` topic: runs the handler for the command's event
        type and ignores any other type."""
        handlers: dict[str, Callable[[WSEnvelope], None]] = {
            "identify.resolved": self._on_wake_command,
            "rip.start": self._on_wake_command,
            "review.pause": self._on_wake_command,
            "manual.trigger": self._on_manual_trigger,
            "rip.retry": self._on_rip_retry,
            "job.abandoned": self._on_job_abandoned,
        }
        handler = handlers.get(envelope.event_type)
        if handler is None:
            logger.debug("ws command ignored: type=%s", envelope.event_type)
            return
        handler(envelope)

    def _on_wake_command(self, envelope: WSEnvelope) -> None:
        """Wake the pipeline parked on the payload's job so it re-fetches the job and re-evaluates:
        identify.resolved -> identified, rip.start -> ripping, review.pause -> re-read
        manual_pause / wait_start_time so a per-job pause or resume applies promptly instead of
        on the next periodic poll."""
        job_id = _command_payload(envelope).get("job_id")
        if not isinstance(job_id, str):
            logger.warning("%s without job_id payload: %s", envelope.event_type, envelope.payload)
            return
        if self._waiter.wake(job_id):
            logger.info("ws %s received for job_id=%s", envelope.event_type, job_id)
        else:
            logger.debug("%s for job_id=%s but no waiter registered", envelope.event_type, job_id)

    def _on_manual_trigger(self, envelope: WSEnvelope) -> None:
        """Start a manual rip for the payload's session, or with no session when it doesn't name one as a string."""
        payload = _command_payload(envelope)
        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            logger.warning("manual.trigger with non-string session_id: %r", session_id)
            session_id = None
        asyncio.create_task(self.handle_manual_trigger(session_id))

    def _on_rip_retry(self, envelope: WSEnvelope) -> None:
        """Fork: remux the payload's job from its kept disc backup in the remux lane."""
        job_id = _command_payload(envelope).get("job_id")
        if not isinstance(job_id, str):
            logger.warning("rip.retry without job_id payload: %s", envelope.payload)
            return
        self.start_remux(job_id)

    def _on_job_abandoned(self, envelope: WSEnvelope) -> None:
        """Abandon the payload's job, then wake a pipeline parked on it so it sees the new status."""
        payload = _command_payload(envelope)
        job_id = payload.get("job_id")
        delete_raw = bool(payload.get("delete_raw"))
        if not isinstance(job_id, str):
            logger.warning("job.abandoned without job_id payload: %s", payload)
            return
        self._handle_abandon(job_id, delete_raw=delete_raw, off_drive=bool(payload.get("off_drive")))
        # Also wake any `ResolutionWaiter.await_resolution` waiter — abandoning a parked
        # job needs to unstick the pipeline the same way `identify.resolved`
        # does. The waiter polls and decides based on the new job status.
        self._waiter.wake(job_id)

    def _handle_abandon(self, job_id: str, *, delete_raw: bool, off_drive: bool = False) -> None:
        """Cancel the active pipeline if it owns this job (or the remux lane's
        run, if that does), optionally wipe `/raw/<job_id>/`, then give the
        disc back. Runs synchronously from the WS handler, so the slow parts
        (the raw-dir wipe and the eject) are spawned as tasks: deleting a
        50-100 GB rip can take seconds on NFS, and the WS receive loop must
        not stall behind it. `off_drive`: the job was remuxing from its disc
        backup, its disc long gone; whatever disc is in the drive now isn't
        its to eject.

        rmtree on Linux is safe even if makemkvcon still has file handles
        open: the directory entries are removed, existing fds keep working
        (writing to nowhere) until the cancelled subprocess dies.
        """
        active = self._active_task
        owns = self._active_job_id == job_id and active is not None and not active.done()
        if active is not None and owns:
            logger.info("job.abandoned: cancelling active task for job_id=%s", job_id)
            active.cancel()
        remux = self._remux_task
        if remux is not None and self._remux_job_id == job_id and not remux.done():
            logger.info("job.abandoned: cancelling the remux for job_id=%s", job_id)
            remux.cancel()
        if delete_raw:
            spawn_tracked(self._wipe_raw(job_id), self._wipe_tasks)
        # A rip that already gave its disc back may have a new disc in the
        # tray by now; that one isn't this job's to eject.
        if off_drive:
            return
        if self._abandon_ejects(owns):
            self._spawn_eject()

    def _abandon_ejects(self, owns: bool) -> bool:
        """Whether an abandon gives the disc back: "done with this disc, give it back".

        The lifecycle's post-rip eject (end of _run_rip) never runs on an abandon exit, so eject here: when this
        job owns the drive's pipeline (ripping or parked; Ejector.eject's EBUSY retries absorb the cancelled
        makemkvcon's teardown), or when the drive is idle with a disc still seated (pipeline already exited, e.g.
        after a rip-start abort). A stale abandon while a DIFFERENT job's rip owns the drive must not pop the
        tray mid-rip.
        """
        active = self._active_task
        idle = active is None or active.done()
        # Fork: a rip that gave its disc back (ejected after its backup) has none of its own left to eject.
        return (owns and not self.drive_released) or (idle and self._disc_seated())

    async def _wipe_raw(self, job_id: str) -> None:
        """Delete `/raw/<job_id>/` off the event loop."""
        target = RAW_ROOT / job_id
        try:
            await asyncio.to_thread(shutil.rmtree, target)
            logger.info("wiped raw dir job_id=%s path=%s", job_id, target)
        except FileNotFoundError:
            logger.info("raw dir already absent job_id=%s path=%s", job_id, target)
        except OSError as exc:
            logger.warning("raw-dir cleanup failed job_id=%s path=%s: %s", job_id, target, exc)

    def _disc_seated(self) -> bool:
        """True when the drive currently reports a seated disc (DISC_OK) —
        guards abandon-eject so abandoning an old job from history with an
        empty (or already-ejected) drive doesn't pop the tray."""
        if self._device_path is None or is_file_source(self._device_path):
            return False
        try:
            return read_drive_status(self._device_path) == DriveState.DISC_OK
        except OSError:
            return False

    def _spawn_eject(self) -> None:
        """Fire-and-forget eject for the sync abandon handler; deduped so
        overlapping abandon signals don't stack umount/eject retries."""
        if self._device_path is None:
            return
        if self._eject_task is not None and not self._eject_task.done():
            return
        self._eject_task = asyncio.create_task(self._ejector.eject(self._device_path))

    async def handle_manual_trigger(self, session_id: str | None) -> None:
        """Run the normal scan→identify→rip flow on demand, threading
        `session_id` (if any) into identify so it lands on the Job's
        metadata as `pending_session_id`. Caller is the WS dispatcher,
        which fires-and-forgets; we own all error handling.
        """
        if self.is_active:
            logger.info("manual.trigger ignored: drive busy with another rip")
            return
        if self._device_path is None:
            logger.warning("manual.trigger ignored: no device_path bound to controller")
            return
        await self._run_pipeline(self._device_path, pending_session_id=session_id)

    async def handle_retry(self, job_id: str) -> None:
        """`rip.retry`: remux a `remuxing` job's titles from the disc backup it
        kept (POST /api/jobs/{id}/retry-failed, track-select plan S15). No disc
        is read, so none has to be in the drive, and none is ejected. It runs
        in the remux lane, beside a rip of whatever disc is in the drive, one
        remux at a time. A busy lane ignores it: the job stays `remuxing`, and
        the heartbeat's idle pickup (`GET .../remux-job`) starts it later."""
        if not self.remux_idle:
            logger.info("rip.retry for job_id=%s waits: another remux is running", job_id)
            return
        if not (RAW_ROOT / job_id / SELECTION_BACKUP_DIRNAME / BACKUP_COMPLETE_MARKER).is_file():
            logger.warning("rip.retry for job_id=%s ignored: no finished disc backup", job_id)
            return
        async with self._remux_lock:
            self._remux_task = asyncio.current_task()
            self._remux_job_id = job_id
            try:
                with with_log_context(job_id=job_id):
                    await self._makemkv.refresh_key()
                    job = await self._client.get_job(job_id)
                    rip_start = await self._backend.rip_start(job_id)
                    logger.info("retrying %d title(s) of job_id=%s from its disc backup", len(rip_start.tracks), job_id)
                    await self._execute_rip(
                        job_id=job_id,
                        disc_type=job.disc_type,
                        device_path=self._device_path or "",
                        rip_start=rip_start,
                        drive_free=True,
                    )
            except httpx.HTTPStatusError as e:
                logger.error("rip.retry for job_id=%s refused by the backend (%s)", job_id, e.response.status_code)
            finally:
                self._remux_task = None
                self._remux_job_id = None

    async def handle_disc_inserted(self, device_path: str) -> None:
        if self.is_active:
            logger.debug("disc-inserted ignored: drive already busy")
            return
        # Auto-rip-on-insert is the global Config switch; default true so
        # this is a no-op on standard deployments.
        if not await self._auto_rip_enabled():
            logger.info("disc-inserted ignored: auto_rip_on_insert=false; trigger via UI to start a rip")
            return
        await self._run_pipeline(device_path, pending_session_id=None)

    async def _auto_rip_enabled(self) -> bool:
        """Whether a disc insert should start a rip: the auto_rip_on_insert switch, or True when the config can't be
        read (fail-open, so a flapping backend never silently halts ripping)."""
        cfg = await ripper_config_or_none(self._client, "auto-rip config lookup", "proceeding fail-open")
        return cfg is None or cfg.auto_rip_on_insert

    async def _run_pipeline(self, device_path: str, *, pending_session_id: str | None) -> None:
        async with self._claim(None):
            # Refresh the MakeMKV key before makemkvcon first touches
            # the disc (the scan probe runs it for every disc), so a
            # container up across a beta-key rotation doesn't scan/rip
            # protected discs with a stale key.
            await self._makemkv.prepare()
            scan_result = await self._scan_or_none(device_path)
            if scan_result is None:
                return

            job = await self._identify_or_none(scan_result, device_path, pending_session_id=pending_session_id)
            if job is None:
                return
            self._active_job_id = job.id

            # Phase 12 — every log line below carries job_id once identify lands.
            with with_log_context(job_id=job.id):
                if not await self._pass_gate(job, IDENTIFY_WAIT):
                    return

                # Timed review gate: the backend parks an identified disc here
                # when hold_for_review is on. Wait for Start (-> ripping) or the
                # auto-start path (-> identified); either proceeds to the rip.
                if not await self._pass_gate(job, REVIEW_WAIT):
                    return

                # Proceed for: identified (normal / operator Start landed),
                # ripping (operator Start already transitioned), or
                # awaiting_review (timed auto-start — rip-start transitions it).
                if job.status not in (JobStatus.IDENTIFIED, JobStatus.RIPPING, JobStatus.AWAITING_REVIEW):
                    logger.info("job %s in unexpected status %s; not ripping", job.id, job.status.value)
                    return

                await self._run_rip_guarded(job, device_path)

    async def _pass_gate(self, job: Job, spec: WaitSpec) -> bool:
        """Wait a job parked at `spec`'s gate until it leaves, recording the status it left in; True unless it left
        the gate some other way (abandoned). A job not parked there passes straight through."""
        if job.status != spec.parked:
            return True
        resolved = await self._waiter.await_resolution(job.id, spec)
        if resolved is None:
            return False
        job.status = resolved.status
        return True

    async def _run_rip_guarded(self, job: Job, device_path: str) -> None:
        """Run the rip. A backend rejection of a state transition is logged and ends the pipeline; a cancel (abandon)
        is logged and propagates."""
        try:
            await self._run_rip(job, device_path)
        except httpx.HTTPStatusError as e:
            # Backend rejected a state transition (e.g. rip-start
            # 422 because identify produced an empty scan_result).
            # Retrying won't fix it — log clearly and let the
            # pipeline exit so the operator can abandon the job.
            logger.error(
                "rip pipeline aborted: backend returned %s for %s — abandon job_id=%s manually",
                e.response.status_code,
                e.request.url,
                job.id,
            )
            return
        except asyncio.CancelledError:
            # Backend abandon → WS `job.abandoned` → task cancel.
            # makemkvcon (or the abcde/dd subprocess) gets killed
            # by its own finally-block subprocess cleanup; we just
            # log and let the cancellation propagate so the lock
            # releases without calling rip-complete.
            logger.info("rip cancelled job_id=%s", job.id)
            raise

    async def _scan_or_none(self, device_path: str) -> ScanResult | None:
        """Scan the disc, or None after a log line when the scan fails or the drive goes absent mid-scan (which ends
        the pipeline). Any other OSError propagates."""
        try:
            return await self._scan_with_ready_retry(device_path)
        except ScanError as e:
            logger.error("scan failed device=%s err=%s", device_path, e)
            return None
        except OSError as e:
            if e.errno not in _ABSENT_ERRNOS:
                raise
            logger.warning("drive went absent during scan device=%s — pipeline abandoned", device_path)
            return None

    async def _scan_with_ready_retry(self, device_path: str) -> ScanResult:
        """Scan the disc (`_scan_until_ready`), reporting it as scan activity
        from the first attempt until the scan returns or raises, including
        a cancellation."""
        started_at = datetime.now(timezone.utc)
        try:
            result = await self._scan_until_ready(device_path, started_at=started_at)
        finally:
            self._set_scan_activity(None)
        if result.disc_type in (DiscType.DVD, DiscType.BLURAY):
            # The MakeMKV scan lists every title; the backend gives Track rows
            # only to those at or above this baseline.
            result = result.model_copy(update={"min_length_seconds": self._default_min_length_seconds})
        return result

    async def _scan_until_ready(self, device_path: str, *, started_at: datetime) -> ScanResult:
        """Scan the disc, retrying when a DISC_OK drive yields zero titles.

        A `DISC_OK` drive that scans to zero titles is almost certainly not
        data-ready yet (the medium reports present via ioctl before the first
        SCSI read succeeds). Retry with backoff to wait out the settle. If the
        drive leaves DISC_OK (tray opened / disc pulled), stop. On exhaustion,
        return the last (empty) result — the caller handles the give-up.

        Note: `read_drive_status` (os.open + ioctl) may raise OSError if the
        device node vanishes mid-retry (USB autosuspend / passthrough teardown);
        that propagates to the pipeline task boundary by design — the task ends
        and the drive poller re-arms on the next disc-insert event.
        """
        last: ScanResult | None = None
        for attempt in range(1, SCAN_NOT_READY_MAX_ATTEMPTS + 1):
            self._set_scan_activity(
                RipperScanActivity(started_at=started_at, attempt=attempt, max_attempts=SCAN_NOT_READY_MAX_ATTEMPTS)
            )
            result = await scan_disc(device_path)
            last = result
            if result.titles:
                return result

            # An ISO file has no drive to settle: the CDROM_DRIVE_STATUS ioctl
            # below is invalid on a regular file (ENOTTY) and would crash the
            # one-shot source pipeline. Zero titles from a file is final.
            if self._zero_titles_are_final(device_path):
                return result

            if attempt < SCAN_NOT_READY_MAX_ATTEMPTS and not await self._back_off_before_rescan(device_path, attempt):
                return result

        logger.error(
            "DISC_UNREADABLE_AFTER_RETRIES device=%s drive_state=DISC_OK attempts=%d — "
            "disc present per ioctl but no readable titles after retries; check disc/drive "
            "readiness (dirty/damaged disc, drive spin-up, or container device passthrough); "
            "the 'makemkvcon found 0 titles' warnings above say what MakeMKV reported",
            device_path,
            SCAN_NOT_READY_MAX_ATTEMPTS,
        )
        assert last is not None
        return last

    @staticmethod
    def _zero_titles_are_final(device_path: str) -> bool:
        """Whether a zero-title scan is the answer rather than a drive still settling: an ISO source has nothing to
        settle, and a drive no longer DISC_OK has lost its disc."""
        if is_file_source(device_path):
            logger.warning("scan: 0 titles from ISO source %s — nothing to retry", device_path)
            return True
        state = read_drive_status(device_path)
        if state != DriveState.DISC_OK:
            logger.info(
                "scan: 0 titles and drive no longer DISC_OK (state=%s) device=%s — stopping",
                state.name,
                device_path,
            )
            return True
        return False

    @staticmethod
    async def _back_off_before_rescan(device_path: str, attempt: int) -> bool:
        """Wait out this attempt's back-off, then report whether the drive still holds its disc and is worth
        scanning again."""
        backoff = SCAN_NOT_READY_BACKOFFS[attempt - 1]
        logger.warning(
            "scan returned 0 titles while drive=DISC_OK (data not ready?) device=%s titles=0 retry=%d/%d backoff=%.0fs",
            device_path,
            attempt,
            SCAN_NOT_READY_MAX_ATTEMPTS,
            backoff,
        )
        await asyncio.sleep(backoff)
        # Re-check status after the sleep — disc may have been pulled
        # while we were waiting; stop before wasting another scan probe.
        post_sleep_state = read_drive_status(device_path)
        if post_sleep_state != DriveState.DISC_OK:
            logger.info(
                "scan: drive left DISC_OK during backoff (state=%s) device=%s — stopping",
                post_sleep_state.name,
                device_path,
            )
            return False
        return True

    async def _identify_or_none(
        self, scan_result: ScanResult, device_path: str, *, pending_session_id: str | None
    ) -> Job | None:
        """Identify the scanned disc, or None when the backend refuses it: a disc ripped before is ejected, and any
        other rejection parks the pipeline."""
        try:
            return await self._backend.identify(scan_result, pending_session_id=pending_session_id)
        except httpx.HTTPStatusError as e:
            if _previously_ripped(e.response):
                # on_previously_ripped=reject: the backend made no job
                # and told the operator; give the disc back.
                logger.info("disc already ripped (on_previously_ripped=reject); ejecting %s", device_path)
                await self._ejector.eject(device_path)
                return None
            # Non-retriable identify rejection (e.g. 409 ripping-paused). Park
            # the pipeline quietly; the caller's claim releases the drive.
            logger.info(
                "rip pipeline not started: identify rejected (%s) for drive %s",
                e.response.status_code,
                self._drive_id,
            )
            return None

    async def _run_rip(self, job: Job | JobView, device_path: str) -> None:
        rip_start = await self._backend.rip_start(job.id)
        logger.info(
            "rip-start job_id=%s preset=%s tracks=%d",
            job.id,
            rip_start.rip_preset_id,
            len(rip_start.tracks),
        )
        await self._execute_rip(
            job_id=job.id,
            disc_type=job.disc_type,
            device_path=device_path,
            rip_start=rip_start,
        )

    async def recover_held_job(self, job: JobView, device_path: str) -> None:
        """Reboot recovery for a disc that was PAUSED in the review gate
        (timed review gate §6.3). Re-park the review wait so the operator can
        still Start it; on Start (-> ripping) or auto-start (-> identified), run
        the rip. Unlike resume_inflight_job this does NOT wipe /raw or re-rip —
        the disc never ripped. A re-parked disc waits for an explicit Start; it
        does not auto-rip on boot even if its countdown had expired.
        """
        async with self._claim(job.id):
            with with_log_context(job_id=job.id):
                logger.info("reboot recovery: re-parking held job %s", job.id)
                resolved = await self._waiter.await_resolution(job.id, REVIEW_WAIT)
                if resolved is None:
                    return
                job = resolved
                if job.status not in (JobStatus.IDENTIFIED, JobStatus.RIPPING):
                    logger.info("recovered job %s in status %s; not ripping", job.id, job.status.value)
                    return
                await self._run_rip(job, device_path)

    async def resume_inflight_job(self, job: JobView, device_path: str) -> None:
        """Phase 9 — drive a crash-recovered rip from the boot probe.

        The backend's `/resume` endpoint resets tracks to QUEUED and
        sets `resumed_from_crash=True`; we then run the same rip-loop
        as a fresh disc would.

        Claims the single-flight gate exactly like `recover_held_job`, so a
        crash-resumed rip is abandonable via `job.abandoned` and makes
        `is_idle()` False (no double-rip from the heartbeat re-probe or from
        a reattach boot probe).
        """
        async with self._claim(job.id):
            with with_log_context(job_id=job.id):
                # Crash-resume skips the scan path, so refresh the key here too —
                # a rip resumed days after a crash must not run on a stale key.
                await self._makemkv.prepare()
                rip_start = await self._client.resume(job.id)
                logger.info("rip-resume job_id=%s tracks=%d", job.id, len(rip_start.tracks))
                await self._execute_rip(
                    job_id=job.id,
                    disc_type=job.disc_type,
                    device_path=device_path,
                    rip_start=rip_start,
                )

    async def _execute_rip(
        self,
        *,
        job_id: str,
        disc_type: DiscType,
        device_path: str,
        rip_start: RipStartResponse,
        drive_free: bool = False,
    ) -> None:
        """Rip every title of the disc, report rip-complete and eject.

        `drive_free`: the rip reads no drive (a remux from a kept backup, in
        the remux lane), so the drive counts as given back from the start."""
        run = _RipRun(drive_released=drive_free)
        if not drive_free:
            self._disc_run = run
        try:
            await _DiscRip(
                self, job_id=job_id, disc_type=disc_type, device_path=device_path, rip_start=rip_start, run=run
            ).run()
        finally:
            # Also on an abandon (cancel): the next job owns the drive again.
            if self._disc_run is run:
                self._disc_run = None

    def _drive_gone(self, path: str) -> bool:
        """The drive this rip runs on has left `path`: the poll loop moved the
        handle (absent, or a new node), or the node no longer answers."""
        if self._device.current != path:
            return True
        try:
            read_drive_status(path)
        except OSError as exc:
            return exc.errno in _ABSENT_ERRNOS
        return False

    async def _watch_drive(self, path: str, lost: asyncio.Event) -> None:
        """Set `lost` (which stops makemkvcon) once the drive leaves `path`.
        The poll loop keeps the handle current; this only reads it."""
        while not lost.is_set():
            if self._device.current != path:
                logger.warning("drive left %s mid-rip; stopping makemkvcon", path)
                lost.set()
                return
            await asyncio.sleep(DEVICE_WATCH_INTERVAL_SECONDS)

    async def _await_drive_return(self) -> str | None:
        """The node the drive came back on once it reports a readable disc,
        or None when it does not within DEVICE_RETURN_WAIT_SECONDS."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DEVICE_RETURN_WAIT_SECONDS
        while True:
            path = self._device.current
            if path is not None:
                try:
                    if read_drive_status(path) == DriveState.DISC_OK:
                        return path
                except OSError:
                    pass
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(DEVICE_WATCH_INTERVAL_SECONDS)

    async def _fail_unfinished(
        self, tracks: list[TrackView], unfinished: set[str], started: set[str], resumes: int
    ) -> None:
        """The drive did not come back: fail what it left, saying why."""
        reason = _disconnect_reason(resumes)
        logger.error("%s; %d title(s) not ripped", reason, len(unfinished))
        for track in tracks:
            if track.id not in unfinished:
                continue
            with with_log_context(track_id=track.id):
                if track.id not in started:
                    started.add(track.id)
                    await self._backend.patch_track(track.id, status=TrackStatus.IN_PROGRESS)
                await self._backend.patch_track(track.id, status=TrackStatus.FAILED, last_error=reason)

    async def _publish_progress(
        self, job_id: str, track_id: str, fraction: float, stats: TransferStats | None = None
    ) -> None:
        logger.debug("track %s progress=%.2f", track_id, fraction)
        if self._ws is None:
            return
        await self._ws.publish(
            topic=f"ripper.progress.{job_id}",
            event_type="ripper.progress",
            payload=_progress_payload(track_id, fraction, stats),
        )


class _MenuRow:
    """The disc menu row (fork only, design v5 A2), as the rip's `MenuSink`: its state goes to the backend as a
    track PATCH (in progress, then done, failed or skipped). A refused PATCH is logged and never stops the rip:
    the titles matter more than the menu."""

    def __init__(self, controller: JobController, job_id: str, track_id: str) -> None:
        self._controller = controller
        self._job_id = job_id
        self.track_id = track_id
        self.settled = False

    async def _patch(self, **fields: object) -> None:
        with with_log_context(track_id=self.track_id):
            try:
                await self._controller._backend.patch_track(self.track_id, **fields)
            except httpx.HTTPError as e:
                logger.warning("disc menu %s not updated to %s: %s", self.track_id, fields.get("status"), e)

    async def start(self) -> None:
        await self._patch(status=TrackStatus.IN_PROGRESS)

    async def progress(self, fraction: float) -> None:
        with with_log_context(track_id=self.track_id):
            await self._controller._publish_progress(self._job_id, self.track_id, fraction)

    async def finish(self, outcome: MenuOutcome) -> None:
        self.settled = True
        fields: dict[str, object] = {"status": outcome.status}
        if outcome.output_path is not None:
            fields["output_path"] = str(outcome.output_path)
        if outcome.size_bytes is not None:
            fields["size_bytes"] = outcome.size_bytes
        if outcome.error is not None:
            fields["last_error"] = outcome.error
        await self._patch(**fields)
        detail = f": {outcome.error}" if outcome.error else ""
        logger.info("disc menu %s %s%s", self.track_id, outcome.status.value, detail)


class _DiscRip:
    """One disc's rip across its resumes: rip every title, leave the titles a lost drive cut short for one more pass
    on the drive's new node, then report rip-complete and eject.

    Fork: `run` is the rip's own state (`_RipRun`): a remux from the kept disc backup (`drive_released` from the
    start) never reads the drive; backup-then-remux gives the disc back once its backup is done; the disc menu row
    (`_MenuRow`) is settled by the rip; a retry rips only the failed titles, into a folder of its own; and a disc
    parked after its backup (stop 2) reports rip-parked in place of rip-complete."""

    def __init__(
        self,
        controller: "JobController",
        *,
        job_id: str,
        disc_type: DiscType,
        device_path: str,
        rip_start: RipStartResponse,
        run: _RipRun,
    ) -> None:
        self._controller = controller
        self._job_id = job_id
        self._disc_type = disc_type
        self._rip_start = rip_start
        self._run = run
        # A remux from the kept disc backup: it never reads the drive.
        self._drive_free = run.drive_released
        # The node at rip start; the controller's own `_device_path` is the live one.
        self._start_path = device_path
        self._output_dir = RAW_ROOT / job_id
        self._tracks = list(rip_start.tracks)
        self._menu = (
            _MenuRow(controller, job_id, rip_start.extract_menu.track_id)
            if rip_start.extract_menu is not None
            else None
        )
        # Fork: a remux from the kept backup reads no drive, so there is none to lose.
        self._resumable = not self._drive_free and _resumable(disc_type, device_path, self._tracks)
        self._lost = asyncio.Event()
        # Track ids whose title the lost drive left unfinished, and the files
        # of every title that finished (kept across a resume).
        self._unfinished: set[str] = set()
        # A retry's DONE titles own their files from the first rip.
        self._kept: set[Path] = {
            Path(t.output_path) for t in self._tracks if t.status == TrackStatus.DONE and t.output_path
        }
        if rip_start.retry or self._drive_free:
            # Retry the failed titles only (POST /api/jobs/{id}/retry-failed):
            # DONE tracks count as left out, so backup-then-remux rips just
            # the rest, into a folder of its own (a remux names its file by
            # disc title, which can clash with the first rip's `mkv all` names).
            # So does any remux from the backup: one re-run after a ripper
            # restart never redoes the titles it already finished.
            self._tracks = [
                t.model_copy(update={"excluded": True}) if t.status == TrackStatus.DONE else t for t in self._tracks
            ]
        # IN_PROGRESS is sent once per track across every pass.
        self._started: set[str] = set()
        self._min_length_seconds = (
            rip_start.min_length_seconds
            if rip_start.min_length_seconds is not None
            else controller._default_min_length_seconds
        )
        self._rip_path = device_path
        self._rip_dir = self._output_dir
        if rip_start.retry:
            attempt = 1
            while (self._output_dir / f"retry-{attempt}").exists():
                attempt += 1
            self._rip_dir = self._output_dir / f"retry-{attempt}"
        self._skip: set[str] = set()
        self._resumes = 0
        self._watcher: asyncio.Task[None] | None = None

    async def _on_track_start(self, track: TrackView) -> None:
        """Mark a queued track IN_PROGRESS, once across every pass."""
        if track.status != TrackStatus.QUEUED or track.id in self._started:
            return
        self._started.add(track.id)
        with with_log_context(track_id=track.id):
            await self._controller._backend.patch_track(track.id, status=TrackStatus.IN_PROGRESS)

    async def _on_track_done(self, track: TrackView, result: RipResult) -> None:
        """Record a finished title as done, left for a resume, or failed."""
        with with_log_context(track_id=track.id):
            if result.ok:
                await self._record_ripped(track, result)
            elif self._drive_left_under_it():
                self._leave_for_resume(track, result)
            else:
                await self._record_failed(track, result)

    def _drive_left_under_it(self) -> bool:
        """Whether a failed title's drive went away under it (so it is left for a resume): only a resumable rip whose
        drive is already marked lost, or has left its node. Fork: never once the rip gave its disc back."""
        return (
            self._resumable
            and not self._run.drive_released
            and (self._lost.is_set() or self._controller._drive_gone(self._rip_path))
        )

    async def _record_ripped(self, track: TrackView, result: RipResult) -> None:
        """Keep a ripped title's file and patch the track DONE."""
        if result.output_path is not None:
            self._kept.add(Path(result.output_path))
        await self._controller._backend.patch_track(track.id, **_done_fields(result))
        logger.info(
            "track %s done size=%s duration=%s",
            track.id,
            result.size_bytes,
            result.duration_seconds,
        )

    def _leave_for_resume(self, track: TrackView, result: RipResult) -> None:
        """Mark the drive lost and leave the title unfinished for the resume pass."""
        # Not this title's fault: the drive went away under it.
        # Leave it for the resume rather than failing it.
        self._lost.set()
        self._unfinished.add(track.id)
        logger.info("track %s left for a resume: the drive disconnected (%s)", track.id, result.error)

    async def _record_failed(self, track: TrackView, result: RipResult) -> None:
        """Patch the track FAILED with the rip's error."""
        await self._controller._backend.patch_track(
            track.id,
            status=TrackStatus.FAILED,
            last_error=result.error or "unknown error",
        )
        logger.warning("track %s failed err=%s", track.id, result.error)

    async def _on_track_progress(
        self, track: TrackView, fraction: float, stats: TransferStats | None = None, /
    ) -> None:
        """Publish a track's progress to the websocket hub, when one is connected."""
        with with_log_context(track_id=track.id):
            await self._controller._publish_progress(self._job_id, track.id, fraction, stats)

    async def _on_backup_done(self) -> None:
        """Backup-then-remux has read the disc; the remuxes only read the backup. Give the disc back now, and stop
        watching the drive so an open tray or a new disc is never taken for a drop."""
        self._run.drive_released = True
        if self._watcher is not None:
            self._watcher.cancel()
        logger.info("disc backup finished; ejecting before the remuxes")
        await self._controller._ejector.eject(self._controller._device_path or self._rip_path)

    async def run(self) -> None:
        """Rip the disc through its resumes, then finish it."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        parked = False
        while True:
            parked = await self._run_pass()
            if not (self._lost.is_set() and self._unfinished):
                break
            # The drive dropped off the bus mid-rip. Keep the disc's finished
            # titles and, once it is back, rip the rest on its new node: one
            # more `mkv all`, or the kept titles' remuxes from a finished
            # backup (never per-title invocations against the drive).
            logger.warning(
                "drive disconnected mid-rip on %s; %d title(s) unfinished; waiting up to %.0fs for it to return",
                self._rip_path,
                len(self._unfinished),
                DEVICE_RETURN_WAIT_SECONDS,
            )
            returned = await self._controller._await_drive_return() if self._resumes < MAX_DEVICE_RESUMES else None
            if returned is None:
                await self._controller._fail_unfinished(self._tracks, self._unfinished, self._started, self._resumes)
                break
            self._prepare_resume(returned)

        await self._finish(parked)

    async def _run_pass(self) -> bool:
        """One `mkv all` pass over the disc on its current node, with a watcher that stops makemkvcon if the drive
        leaves that node (resumable rips only). Fork: returns whether the rip parked after its backup (stop 2); the
        watcher is a field so `_on_backup_done` can stop it."""
        menu = self._menu
        self._watcher = (
            asyncio.create_task(self._controller._watch_drive(self._rip_path, self._lost)) if self._resumable else None
        )
        try:
            return await rip_all(
                disc_type=self._disc_type,
                device_path=self._rip_path,
                tracks=self._tracks,
                scan_titles=self._rip_start.scan_titles,
                output_dir=self._rip_dir,
                on_track_start=self._on_track_start,
                on_track_done=self._on_track_done,
                on_track_progress=self._on_track_progress,
                min_length_seconds=self._min_length_seconds,
                stop=self._lost if self._resumable else None,
                skip_track_ids=self._skip,
                drive_lost=(
                    functools.partial(self._controller._drive_gone, self._rip_path) if self._resumable else None
                ),
                backup_full_selections=self._controller._backup_full_selections,
                # The job's, whichever folder this pass rips into, so a
                # resume remuxes from a backup an earlier pass finished.
                backup_dir=self._output_dir / SELECTION_BACKUP_DIRNAME,
                on_backup_done=self._on_backup_done,
                # Until a pass settles it: a resume after the drive
                # dropped during the backup copies it then.
                menu=menu if menu is not None and not menu.settled else None,
                drive_free=self._drive_free,
                park_after_backup=self._rip_start.park_after_backup and not self._drive_free,
            )
        finally:
            if self._watcher is not None:
                self._watcher.cancel()

    def _prepare_resume(self, returned: str) -> None:
        """Set up the next pass on the node the drive came back on: drop the partial files, skip every title already
        settled, and rip into a fresh resume folder."""
        self._resumes += 1
        _remove_partial_outputs(self._output_dir, self._kept)
        self._skip = {t.id for t in self._tracks} - self._unfinished
        self._unfinished.clear()
        self._lost.clear()
        self._rip_path = returned
        self._rip_dir = self._output_dir / f"resume-{self._resumes}"
        logger.info(
            # Fork: one more `mkv all`, or the kept titles' remuxes from a finished backup.
            "drive back at %s; resuming %d title(s) (resume %d)",
            self._rip_path,
            len(self._tracks) - len(self._skip),
            self._resumes,
        )

    async def _finish(self, parked: bool) -> None:
        """Report rip-complete, then eject the drive at the node it is on now and give it a moment to settle.

        Fork: a menu the rip never settled is failed; a disc parked after its backup (stop 2) reports rip-parked
        instead, its disc given back; and a rip that already gave its disc back (or never read it) ejects nothing."""
        menu = self._menu
        if menu is not None and not menu.settled:
            await menu.finish(menu_failed(MENU_NOT_COPIED))
        controller = self._controller
        job_id = self._job_id
        if parked:
            # Stop 2: the disc went back after its backup; its titles are chosen, then remuxed in the remux lane.
            if not self._run.drive_released:
                # A backup an earlier pass finished was reused, so nothing gave the disc back yet.
                self._run.drive_released = True
                await controller._ejector.eject(controller._device_path or self._start_path)
            await controller._backend.call(lambda: controller._client.rip_parked(job_id), f"rip-parked {job_id}")
            logger.info("rip-parked job_id=%s: backed up; its titles wait to be chosen", job_id)
            return
        completed = await controller._backend.rip_complete(job_id)
        logger.info("rip-complete job_id=%s status=%s", job_id, completed.status.value)

        if self._run.drive_released:
            # Ejected after the backup (or never read, for a remux); the tray
            # may hold the next disc by now.
            return
        # Eject the node the drive is at NOW — it may have renumbered during
        # the rip (USB replug); the path captured at rip start is a hint only.
        await self._controller._ejector.eject(self._controller._device_path or self._start_path)
        await asyncio.sleep(EJECT_GRACE_SECONDS)


def _remove_partial_outputs(output_dir: Path, kept: set[Path]) -> None:
    """Before a resume: delete every .mkv under the job's raw folder that no
    finished title owns (the title the drive dropped mid-write, and anything a
    stopped pass left behind)."""
    for f in output_dir.rglob("*.mkv"):
        if f not in kept:
            f.unlink(missing_ok=True)
