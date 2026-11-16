import asyncio
import contextlib
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from arm_common import DiscType, Job, JobStatus, with_log_context
from arm_common.schemas import (
    JobView,
    RipperScanActivity,
    RipStartResponse,
    ScanResult,
    WSEnvelope,
)
from arm_ripper import raw_dir
from arm_ripper.backend_client import BackendClient
from arm_ripper.background_tasks import spawn_tracked
from arm_ripper.backend_retry import BackendRetry
from arm_ripper.disc_rip import DiscRip, RipRun
from arm_ripper.drive_handle import DriveHandle
from arm_ripper.eject import Ejector
from arm_ripper.makemkv_prep import MakeMKVPrep
from arm_ripper.ripper_config import ripper_config_or_none
from arm_ripper.resolution_wait import IDENTIFY_WAIT, RESOLUTION_TIMEOUT_UNSET, REVIEW_WAIT, ResolutionWaiter, WaitSpec
from arm_ripper.rip.dispatcher import DEFAULT_MIN_LENGTH_SECONDS
from arm_ripper.rip.optical_rip import BACKUP_COMPLETE_MARKER, SELECTION_BACKUP_DIRNAME
from arm_ripper.disc_scan import SCAN_NOT_READY_MAX_ATTEMPTS, scan_or_none
from arm_ripper.drive_poll import DriveState, read_drive_status
from arm_ripper.source import is_file_source
from arm_ripper.ws_client import WSClient

logger = logging.getLogger("arm_ripper.job_controller")


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
        self._disc_run: RipRun | None = None
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
        target = raw_dir.RAW_ROOT / job_id
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
        if not (raw_dir.RAW_ROOT / job_id / SELECTION_BACKUP_DIRNAME / BACKUP_COMPLETE_MARKER).is_file():
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
        """`disc_scan.scan_or_none`, reported as scan activity from the first attempt until the scan returns or
        raises, including a cancellation (fork). A DVD/BD scan gets the ripper's title-length baseline: the
        MakeMKV scan lists every title, and the backend gives Track rows only to those at or above it."""
        started_at = datetime.now(timezone.utc)

        def on_attempt(attempt: int) -> None:
            self._set_scan_activity(
                RipperScanActivity(started_at=started_at, attempt=attempt, max_attempts=SCAN_NOT_READY_MAX_ATTEMPTS)
            )

        try:
            result = await scan_or_none(device_path, on_attempt=on_attempt)
        finally:
            self._set_scan_activity(None)
        if result is not None and result.disc_type in (DiscType.DVD, DiscType.BLURAY):
            result = result.model_copy(update={"min_length_seconds": self._default_min_length_seconds})
        return result

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
        run = RipRun(drive_released=drive_free)
        if not drive_free:
            self._disc_run = run
        try:
            await DiscRip(
                backend=self._backend,
                ws=self._ws,
                ejector=self._ejector,
                device=self._device,
                default_min_length_seconds=self._default_min_length_seconds,
                output_dir=raw_dir.RAW_ROOT / job_id,
                job_id=job_id,
                disc_type=disc_type,
                device_path=device_path,
                rip_start=rip_start,
                backup_full_selections=self._backup_full_selections,
                run=run,
            ).run()
        finally:
            # Also on an abandon (cancel): the next job owns the drive again.
            if self._disc_run is run:
                self._disc_run = None
