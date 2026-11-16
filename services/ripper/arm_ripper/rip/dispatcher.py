import asyncio
import logging
import time
from collections.abc import Callable, Collection
from pathlib import Path

from arm_common import DiscType
from arm_common.enums import TrackKind
from arm_common.schemas import ScanTitle, TrackView

from arm_ripper.menus import extract as menu_extract
from arm_ripper.rip.abcde_rip import rip_cd
from arm_ripper.rip.data_rip import rip_data
from arm_ripper.rip.makemkv_backup import (
    BACKUP_DIRNAME,
    BackupSourceError,
    backup_source,
    rip_backup,
)
from arm_ripper.rip.optical_rip import (
    SELECTION_BACKUP_DIRNAME,
    MenuSink,
    OnBackupDone,
    _MenuRun,
    rip_optical,
)
from arm_ripper.rip.preflight import check_backup_space
from arm_ripper.rip.result import RipResult
from arm_ripper.rip.track_callbacks import OnTrackDone, OnTrackProgress, OnTrackStart, TransferStats

logger = logging.getLogger("arm_ripper.rip.dispatcher")

# v3 default. 120s drops menu loops and vendor bumpers without cutting
# the 2–5 minute extras users typically care about. Sessions can
# override per-rip via `Session.overrides_json["min_length_seconds"]`.
DEFAULT_MIN_LENGTH_SECONDS = 120


async def rip_all(
    disc_type: DiscType,
    device_path: str,
    tracks: list[TrackView],
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None = None,
    min_length_seconds: int = DEFAULT_MIN_LENGTH_SECONDS,
    stop: asyncio.Event | None = None,
    skip_track_ids: Collection[str] = (),
    drive_lost: Callable[[], bool] | None = None,
    scan_titles: list[ScanTitle] | None = None,
    backup_full_selections: bool = False,
    backup_dir: Path | None = None,
    on_backup_done: OnBackupDone | None = None,
    menu: MenuSink | None = None,
    drive_free: bool = False,
    park_after_backup: bool = False,
) -> bool:
    """Rip every kept track in `tracks` and invoke the lifecycle callbacks.

    `scan_titles` is the job's stored disc scan (`RipStartResponse.scan_titles`),
    every title MakeMKV listed. DVD/BD use it to tell which output file is
    which title, and to find each kept title again in a backup; None falls
    back to the tracks' own durations and the `mkv all` path.

    Excluded tracks are never reported on (no start, no done): they
    stay `queued`, and the UI shows them as skipped.

    DVD / BD only: `stop` kills makemkvcon when set (the drive dropped off the
    bus), and `skip_track_ids` names tracks an earlier pass already settled.
    `drive_lost` is asked as each title finishes: makemkvcon moves on from a
    title the drive dropped under before anything else may notice, and that
    title's file is cut short, so it is reported unfinished and `stop` is set.
    A resume passes every track so the `mkv all` output positions still line
    up, and the skipped tracks get no callbacks (their new files are deleted).
    `backup_dir` is where backup-then-remux keeps its disc backup (default
    `<output_dir>/.backup`); a resume, which rips into a folder of its own,
    passes the job's so a finished backup is reused. `on_backup_done` runs
    once backup-then-remux has finished reading a drive (the remuxes only read
    the backup), so the caller can give the disc back before they run.
    `menu` (fork only, design v5 A2): the disc menu is wanted too. It is copied
    into `<job raw>/menu/` (beside `backup_dir`) out of a disc backup, so a
    drive goes through backup-then-remux even when every title is kept, or
    straight out of an ISO or folder source; see `rip_optical`.
    `drive_free` (a remux from the job's kept disc backup): the drive is never
    read, since it may hold the next disc. DVD/BD titles come only from the
    finished backup in `backup_dir`, one remux each (never `mkv all`); with no
    finished backup every kept title fails.
    `park_after_backup` (stop 2, fork only): a DVD/BD from a drive is backed up
    whole (and its menu copied), then nothing more: the titles stay queued and
    the backup stays, to be chosen and remuxed later. Returns True when it
    parked so; a failed or no-room backup rips with `mkv all` instead.

    Per disc_type:
    - DVD / BD, every title at or above `min_length_seconds` kept: one
      `makemkvcon mkv ... all` invocation for the whole
      disc; per-title outcomes attributed from the robot stream and
      the produced files. Drive stays open the whole time — no gaps
      where USB autosuspend can drop the device. Selected titles get
      DONE/FAILED based on whether MakeMKV produced their file. Titles
      below `min_length_seconds` are skipped by MakeMKV; if the user
      requested one of those, we report FAILED with a clear reason
      rather than silently ignoring it.
    - DVD / BD with some of those titles not kept (excluded, or with no
      track at all): backup-then-remux (`_rip_kept_titles`). A drive is read
      once by a `backup`; an ISO or folder source needs none. Each kept
      title is then remuxed from disk. `backup_full_selections`
      (`ARM_BACKUP_FULL_SELECTIONS`) sends full selections from a drive
      down this path too.
    - Any disc whose tracks are all DISC_BACKUP: one `makemkvcon backup --decrypt disc:<N>` (`rip_backup`) for the
      first track, once `/raw` is shown to have room for it (when a scan is stored).
    - CD: mark the kept tracks IN_PROGRESS, run abcde once with them as its
      track list, then emit DONE/FAILED per track from the bulk result.
      Excluded tracks are never read and get no callbacks, as on DVD/BD.
    - DATA: a single dd dump assigned to the first (only) track.
    """
    run = _MenuRun(menu) if menu is not None else None
    if run is not None and (
        disc_type not in (DiscType.DVD, DiscType.BLURAY)
        or (tracks and all(t.kind in (TrackKind.DISC_BACKUP, TrackKind.DATA_DUMP) for t in tracks))
    ):
        # The backend makes a menu row only beside DVD and Blu-ray titles.
        await run.finish(menu_extract.skipped("Only a DVD or Blu-ray rip of its titles copies the disc menu"))
        run = None
    if tracks and all(t.kind == TrackKind.DISC_BACKUP for t in tracks):
        await _rip_disc_backup(
            device_path,
            tracks[0],
            output_dir,
            on_track_start,
            on_track_done,
            on_track_progress,
            disc_type=disc_type,
            scan_titles=scan_titles,
        )
        return False

    rip_as = _effective_disc_type(disc_type, tracks)
    if rip_as in (DiscType.DVD, DiscType.BLURAY):
        return await rip_optical(
            disc_type=rip_as,
            device_path=device_path,
            tracks=tracks,
            output_dir=output_dir,
            on_track_start=on_track_start,
            on_track_done=on_track_done,
            on_track_progress=on_track_progress,
            min_length_seconds=min_length_seconds,
            stop=stop,
            skip_track_ids=skip_track_ids,
            drive_lost=drive_lost,
            scan_titles=scan_titles,
            backup_full_selections=backup_full_selections,
            backup_dir=output_dir / SELECTION_BACKUP_DIRNAME if backup_dir is None else backup_dir,
            on_backup_done=on_backup_done,
            menu=run,
            drive_free=drive_free,
            park=park_after_backup,
        )

    if rip_as == DiscType.CD:
        await _rip_audio_cd(device_path, tracks, output_dir, on_track_start, on_track_done)
        return False

    if rip_as == DiscType.DATA:
        await _rip_data_dump(device_path, tracks, output_dir, on_track_start, on_track_done, on_track_progress)
        return False

    await _fail_unsupported(rip_as, tracks, on_track_done)
    return False


def _effective_disc_type(disc_type: DiscType, tracks: list[TrackView]) -> DiscType:
    """The disc type to rip as: DATA when every track is a full-disc dump, else the type the scan gave."""
    if tracks and all(t.kind == TrackKind.DATA_DUMP for t in tracks):
        # A full-disc dump is a dump whatever the scan called the disc: an
        # operator can pick the full-disc dump session for a bluray/dvd ISO,
        # and makemkvcon must not be asked to rip it.
        return DiscType.DATA
    return disc_type


async def _rip_disc_backup(
    device_path: str,
    backup: TrackView,
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None,
    *,
    disc_type: DiscType,
    scan_titles: list[ScanTitle] | None,
) -> None:
    """Back up the whole disc with one decrypting `makemkvcon backup`, reported as the single `backup` track.

    Fork: once `/raw` is shown to have room for it (when a scan is stored), from
    the drive as `disc:<N>`; a backup missing files is a failed one."""
    await on_track_start(backup)

    async def _on_backup_progress(fraction: float) -> None:
        if on_track_progress is not None:
            await on_track_progress(backup, fraction)

    # Room for the whole disc before the drive is touched; with no stored
    # scan there is nothing to estimate from.
    if scan_titles is not None:
        shortfall = check_backup_space(
            output_dir,
            titles=scan_titles,
            disc_type=disc_type,
            kept_bytes=0,
            backup_dirname=SELECTION_BACKUP_DIRNAME,
        )
        if shortfall is not None:
            logger.warning("not backing up the disc: %s", shortfall)
            await on_track_done(backup, RipResult(ok=False, error=shortfall))
            return
    # MakeMKV's `backup` takes a drive only as `disc:<N>` (`dev:` is refused).
    try:
        source = await backup_source(device_path)
    except BackupSourceError as exc:
        await on_track_done(backup, RipResult(ok=False, error=str(exc)))
        return
    copied = await rip_backup(source, output_dir / BACKUP_DIRNAME, decrypt=True, on_progress=_on_backup_progress)
    if copied.ok and copied.damaged:
        # The backup is the finished output, so one that lacks files is
        # not one; the copy stays on disk for a look.
        await on_track_done(backup, RipResult(ok=False, error=f"disc backup damaged: {copied.damage_text()}"))
    else:
        await on_track_done(backup, copied)


async def _rip_audio_cd(
    device_path: str,
    tracks: list[TrackView],
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
) -> None:
    """Rip an audio CD with one abcde run: start every track, then report each one as abcde finishes it.

    Fork: excluded tracks are never read and get no callbacks."""
    excluded_cd = sorted(t.index for t in tracks if t.excluded)
    if excluded_cd:
        logger.info("not ripping excluded tracks %s", excluded_cd)
    tracks = [t for t in tracks if not t.excluded]
    if not tracks:
        return
    for track in tracks:
        await on_track_start(track)
    track_by_index = {t.index: t for t in tracks}

    async def _cd_on_track_done(idx: int, result: RipResult) -> None:
        track = track_by_index.get(idx)
        if track is None:
            return
        await on_track_done(track, result)

    results = await rip_cd(
        device_path=device_path,
        output_dir=output_dir,
        track_indexes=[t.index for t in tracks],
        on_track_done=_cd_on_track_done,
    )
    # Defensive: rip_cd's contract is to fire on_track_done for every
    # track index it's given, so this loop is a no-op on the happy
    # path. It only catches a track that fell through (e.g. a code
    # path inside rip_cd that returned without firing the callback)
    # so the row doesn't stay IN_PROGRESS forever.
    for track in tracks:
        if track.index not in results:
            await on_track_done(
                track,
                RipResult(ok=False, error=f"abcde produced no entry for track {track.index}"),
            )


async def _rip_data_dump(
    device_path: str,
    tracks: list[TrackView],
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None,
) -> None:
    """Copy the whole disc or image with one dd run, reported as the first track with byte-level progress."""
    if not tracks:
        return
    first = tracks[0]
    await on_track_start(first)
    started = time.monotonic()

    async def _on_data_progress(done: int, total: int | None) -> None:
        if on_track_progress is None:
            return
        elapsed = time.monotonic() - started
        stats: TransferStats = {
            "bytes_done": done,
            "bytes_total": total,
            "rate_bps": done / elapsed if elapsed > 0 else None,
        }
        await on_track_progress(first, min(done / total, 1.0) if total else 0.0, stats)

    result = await rip_data(device_path=device_path, output_dir=output_dir, on_progress=_on_data_progress)
    await on_track_done(first, result)


async def _fail_unsupported(rip_as: DiscType, tracks: list[TrackView], on_track_done: OnTrackDone) -> None:
    """Fail every track of a disc type no ripper handles."""
    for track in tracks:
        await on_track_done(
            track,
            RipResult(ok=False, error=f"no rip path for disc_type={rip_as.value}"),
        )
