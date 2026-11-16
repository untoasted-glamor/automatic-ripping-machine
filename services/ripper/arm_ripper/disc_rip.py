"""One disc's rip across its resumes: rip every title, resume after a lost drive, report rip-complete and eject."""

import asyncio
import functools
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from arm_common import DiscType, TrackKind, TrackStatus, with_log_context
from arm_common.schemas import RipStartResponse, TrackView
from arm_ripper.backend_retry import BackendRetry
from arm_ripper.drive_handle import DriveHandle
from arm_ripper.drive_poll import DriveErrorKind, DriveState, classify_drive_error, read_drive_status
from arm_ripper.eject import Ejector
from arm_ripper.menus.extract import MenuOutcome
from arm_ripper.menus.extract import failed as menu_failed
from arm_ripper.rip import RipResult, rip_all
from arm_ripper.rip.optical_rip import SELECTION_BACKUP_DIRNAME
from arm_ripper.rip.track_callbacks import TransferStats
from arm_ripper.source import is_file_source
from arm_ripper.ws_client import WSClient

# The moved code keeps the job controller's logger name so existing log filters and log text stay the same.
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
class RipRun:
    """One running rip's own state. A disc rip and a remux from a kept disc
    backup can run at once (the remux lane), so it isn't kept on the
    controller. `drive_released`: the rip no longer uses the drive, from the
    start for a remux, or once backup-then-remux has ejected its disc. The
    drive is then the operator's: no drop detection, no second eject, and a
    disc inserted meanwhile is the next one."""

    drive_released: bool


def _disconnect_reason(resumes: int) -> str:
    """Why the titles a lost drive left are failed: it did not come back in time, or it dropped more often than a
    rip may resume."""
    return (
        f"the drive disconnected mid-rip and did not come back within {DEVICE_RETURN_WAIT_SECONDS:.0f}s"
        if resumes < MAX_DEVICE_RESUMES
        else f"the drive disconnected mid-rip {resumes + 1} times; giving up"
    )


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


class _MenuRow:
    """The disc menu row (fork only, design v5 A2), as the rip's `MenuSink`: its state goes to the backend as a
    track PATCH (in progress, then done, failed or skipped). A refused PATCH is logged and never stops the rip:
    the titles matter more than the menu."""

    def __init__(self, backend: BackendRetry, ws: WSClient | None, job_id: str, track_id: str) -> None:
        self._backend = backend
        self._ws = ws
        self._job_id = job_id
        self.track_id = track_id
        self.settled = False

    async def _patch(self, **fields: object) -> None:
        with with_log_context(track_id=self.track_id):
            try:
                await self._backend.patch_track(self.track_id, **fields)
            except httpx.HTTPError as e:
                logger.warning("disc menu %s not updated to %s: %s", self.track_id, fields.get("status"), e)

    async def start(self) -> None:
        await self._patch(status=TrackStatus.IN_PROGRESS)

    async def progress(self, fraction: float) -> None:
        with with_log_context(track_id=self.track_id):
            logger.debug("track %s progress=%.2f", self.track_id, fraction)
            if self._ws is not None:
                await self._ws.publish(
                    topic=f"ripper.progress.{self._job_id}",
                    event_type="ripper.progress",
                    payload=_progress_payload(self.track_id, fraction, None),
                )

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


class DiscRip:
    """One disc's rip across its resumes: rip every title, leave the titles a lost drive cut short for one more pass
    on the drive's new node, then report rip-complete and eject.

    Fork: `run` is the rip's own state (`RipRun`): a remux from the kept disc backup (`drive_released` from the
    start) never reads the drive; backup-then-remux gives the disc back once its backup is done; the disc menu row
    (`_MenuRow`) is settled by the rip; a retry rips only the failed titles, into a folder of its own; and a disc
    parked after its backup (stop 2) reports rip-parked in place of rip-complete."""

    def __init__(
        self,
        *,
        backend: BackendRetry,
        ws: WSClient | None,
        ejector: Ejector,
        device: DriveHandle,
        default_min_length_seconds: int,
        output_dir: Path,
        job_id: str,
        disc_type: DiscType,
        device_path: str,
        rip_start: RipStartResponse,
        backup_full_selections: bool = False,
        run: RipRun | None = None,
    ) -> None:
        self._backup_full_selections = backup_full_selections
        self._backend = backend
        self._ws = ws
        self._ejector = ejector
        self._device = device
        self._job_id = job_id
        self._disc_type = disc_type
        self._rip_start = rip_start
        # None: a disc rip that reads its drive.
        if run is None:
            run = RipRun(drive_released=False)
        self._run = run
        # A remux from the kept disc backup: it never reads the drive.
        self._drive_free = run.drive_released
        # The node at rip start; the shared handle's `current` is the live one.
        self._start_path = device_path
        self._output_dir = output_dir
        self._tracks = list(rip_start.tracks)
        self._menu = (
            _MenuRow(backend, ws, job_id, rip_start.extract_menu.track_id)
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
            rip_start.min_length_seconds if rip_start.min_length_seconds is not None else default_min_length_seconds
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
            await self._backend.patch_track(track.id, status=TrackStatus.IN_PROGRESS)

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
            and (self._lost.is_set() or self._drive_gone(self._rip_path))
        )

    async def _record_ripped(self, track: TrackView, result: RipResult) -> None:
        """Keep a ripped title's file and patch the track DONE."""
        if result.output_path is not None:
            self._kept.add(Path(result.output_path))
        await self._backend.patch_track(track.id, **_done_fields(result))
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
        await self._backend.patch_track(
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
            logger.debug("track %s progress=%.2f", track.id, fraction)
            if self._ws is not None:
                await self._ws.publish(
                    topic=f"ripper.progress.{self._job_id}",
                    event_type="ripper.progress",
                    payload=_progress_payload(track.id, fraction, stats),
                )

    async def _on_backup_done(self) -> None:
        """Backup-then-remux has read the disc; the remuxes only read the backup. Give the disc back now, and stop
        watching the drive so an open tray or a new disc is never taken for a drop."""
        self._run.drive_released = True
        if self._watcher is not None:
            self._watcher.cancel()
        logger.info("disc backup finished; ejecting before the remuxes")
        await self._ejector.eject(self._device.current or self._rip_path)

    def _drive_gone(self, path: str) -> bool:
        """The drive this rip runs on has left `path`: the poll loop moved the
        handle (absent, or a new node), or the node no longer answers."""
        if self._device.current != path:
            return True
        try:
            read_drive_status(path)
        except OSError as exc:
            return classify_drive_error(exc) is DriveErrorKind.ABSENT
        return False

    async def _watch_drive(self, path: str) -> None:
        """Set the lost event (which stops makemkvcon) once the drive leaves `path`.
        The poll loop keeps the handle current; this only reads it."""
        while not self._lost.is_set():
            if self._device.current != path:
                logger.warning("drive left %s mid-rip; stopping makemkvcon", path)
                self._lost.set()
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

    async def _fail_unfinished(self) -> None:
        """The drive did not come back: fail what it left, saying why."""
        reason = _disconnect_reason(self._resumes)
        logger.error("%s; %d title(s) not ripped", reason, len(self._unfinished))
        for track in self._tracks:
            if track.id not in self._unfinished:
                continue
            with with_log_context(track_id=track.id):
                if track.id not in self._started:
                    self._started.add(track.id)
                    await self._backend.patch_track(track.id, status=TrackStatus.IN_PROGRESS)
                await self._backend.patch_track(track.id, status=TrackStatus.FAILED, last_error=reason)

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
            returned = await self._await_drive_return() if self._resumes < MAX_DEVICE_RESUMES else None
            if returned is None:
                await self._fail_unfinished()
                break
            self._prepare_resume(returned)

        await self._finish(parked)

    async def _run_pass(self) -> bool:
        """One `mkv all` pass over the disc on its current node, with a watcher that stops makemkvcon if the drive
        leaves that node (resumable rips only). Fork: returns whether the rip parked after its backup (stop 2); the
        watcher is a field so `_on_backup_done` can stop it."""
        menu = self._menu
        self._watcher = asyncio.create_task(self._watch_drive(self._rip_path)) if self._resumable else None
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
                drive_lost=(functools.partial(self._drive_gone, self._rip_path) if self._resumable else None),
                backup_full_selections=self._backup_full_selections,
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
        job_id = self._job_id
        if parked:
            # Stop 2: the disc went back after its backup; its titles are chosen, then remuxed in the remux lane.
            if not self._run.drive_released:
                # A backup an earlier pass finished was reused, so nothing gave the disc back yet.
                self._run.drive_released = True
                await self._ejector.eject(self._device.current or self._start_path)
            await self._backend.rip_parked(job_id)
            logger.info("rip-parked job_id=%s: backed up; its titles wait to be chosen", job_id)
            return
        completed = await self._backend.rip_complete(job_id)
        logger.info("rip-complete job_id=%s status=%s", job_id, completed.status.value)

        if self._run.drive_released:
            # Ejected after the backup (or never read, for a remux); the tray
            # may hold the next disc by now.
            return
        # Eject the node the drive is at NOW — it may have renumbered during
        # the rip (USB replug); the path captured at rip start is a hint only.
        await self._ejector.eject(self._device.current or self._start_path)
        await asyncio.sleep(EJECT_GRACE_SECONDS)


def _remove_partial_outputs(output_dir: Path, kept: set[Path]) -> None:
    """Before a resume: delete every .mkv under the job's raw folder that no
    finished title owns (the title the drive dropped mid-write, and anything a
    stopped pass left behind)."""
    for f in output_dir.rglob("*.mkv"):
        if f not in kept:
            f.unlink(missing_ok=True)
