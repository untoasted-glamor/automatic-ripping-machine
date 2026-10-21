import asyncio
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict

from arm_common import DiscType
from arm_common.enums import TrackKind
from arm_common.schemas import ScanTitle, TrackView

from arm_ripper.rip.abcde_rip import rip_cd
from arm_ripper.rip.data_rip import rip_data
from arm_ripper.rip.makemkv_backup import (
    BACKUP_DIRNAME,
    BackupSourceError,
    backup_source,
    rip_backup,
    selection_backup,
)
from arm_ripper.rip.makemkv_rip import (
    _OUTPUT_FILE_RE,
    REMUX_MIN_LENGTH_SECONDS,
    STOPPED_ERROR,
    RipDiscResult,
    RipResult,
    _output_files_by_position,
    remux_title,
    rip_disc,
)
from arm_ripper.rip.title_hasher import OnHashed, TitleHasher
from arm_ripper.rip.title_map import match_titles
from arm_ripper.scan.makemkv import ScanError, list_source_titles
from arm_ripper.source import is_file_source, makemkv_source_url

logger = logging.getLogger("arm_ripper.rip.dispatcher")

# Backup-then-remux scratch space (track-select plan D1), inside the job's raw
# folder so job delete, abandon and the boot probe's wipe remove it with
# everything else. Never upstream's `backup/` (a FOLDER preset's finished
# output). `.remux/<index>/` is removed after each run; `.backup/` once every
# kept title it was read for is remuxed, else it stays for a retry with no
# disc until the job or the backup is deleted.
SELECTION_BACKUP_DIRNAME = ".backup"
REMUX_DIRNAME = ".remux"
# Written into `.backup/` once a backup finished; holds the URL that reads it
# back. A later pass over the same job (a D-133 resume) remuxes from it with
# no second drive read.
BACKUP_COMPLETE_MARKER = ".complete"
# Share of the job's progress bar the backup takes; the remuxes from disk
# fill the rest. The Hanako-kun Blu-ray backup took 48 min, remuxing one of
# its episodes 19-31 s and its 2:49 play-all 140 s.
BACKUP_PROGRESS_SHARE = 0.9

OnTrackStart = Callable[[TrackView], Awaitable[None]]
OnTrackDone = Callable[[TrackView, RipResult], Awaitable[None]]


class TransferStats(TypedDict):
    """Byte-level detail for a copy whose size is known (the full-disc dump)."""

    bytes_done: int
    bytes_total: int | None
    rate_bps: float | None
    # "verifying" while the ripped files are hashed after makemkvcon exits.
    phase: NotRequired[str]


class OnTrackProgress(Protocol):
    def __call__(self, track: TrackView, fraction: float, stats: TransferStats | None = None, /) -> Awaitable[None]: ...


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
) -> None:
    """Rip every kept track in `tracks` and invoke the lifecycle callbacks.

    `scan_titles` is the job's stored disc scan (`RipStartResponse.scan_titles`),
    every title MakeMKV listed. DVD/BD use it to tell which output file is
    which title, and to find each kept title again in a backup; None falls
    back to the tracks' own durations and the `mkv all` path.

    Excluded DVD/BD tracks are never reported on (no start, no done): they
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
    passes the job's so a finished backup is reused.

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
      first track.
    - CD: mark all tracks IN_PROGRESS, run abcde once for the whole disc,
      then emit DONE/FAILED per track from the bulk result.
    - DATA: a single dd dump assigned to the first (only) track.
    """
    if tracks and all(t.kind == TrackKind.DISC_BACKUP for t in tracks):
        backup = tracks[0]
        await on_track_start(backup)

        async def _on_backup_progress(fraction: float) -> None:
            if on_track_progress is not None:
                await on_track_progress(backup, fraction)

        # MakeMKV's `backup` takes a drive only as `disc:<N>` (`dev:` is refused).
        try:
            source = await backup_source(device_path)
        except BackupSourceError as exc:
            await on_track_done(backup, RipResult(ok=False, error=str(exc)))
            return
        result = await rip_backup(source, output_dir / BACKUP_DIRNAME, decrypt=True, on_progress=_on_backup_progress)
        await on_track_done(backup, result)
        return

    if tracks and all(t.kind == TrackKind.DATA_DUMP for t in tracks):
        # A full-disc dump is a dump whatever the scan called the disc: an
        # operator can pick the full-disc dump session for a bluray/dvd ISO,
        # and makemkvcon must not be asked to rip it.
        disc_type = DiscType.DATA
    if disc_type in (DiscType.DVD, DiscType.BLURAY):
        await _rip_optical(
            disc_type=disc_type,
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
        )
        return

    if disc_type == DiscType.CD:
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
        return

    if disc_type == DiscType.DATA:
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
        return

    for track in tracks:
        await on_track_done(
            track,
            RipResult(ok=False, error=f"no rip path for disc_type={disc_type.value}"),
        )


async def _rip_optical(
    *,
    disc_type: DiscType,
    device_path: str,
    tracks: list[TrackView],
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None,
    min_length_seconds: int,
    stop: asyncio.Event | None = None,
    skip_track_ids: Collection[str] = (),
    drive_lost: Callable[[], bool] | None = None,
    scan_titles: list[ScanTitle] | None = None,
    backup_full_selections: bool = False,
    backup_dir: Path,
) -> None:
    """Pick the DVD/BD strategy: `mkv all` when it rips exactly what is kept,
    backup-then-remux otherwise (track-select plan D1). Tracks with a
    malformed `source_ref` are failed up front; excluded tracks get no
    callbacks at all."""
    # Pre-compute source_ref → track lookup so the rip-side callbacks
    # are O(1). Skip and pre-fail tracks with malformed source_ref.
    track_by_index: dict[int, TrackView] = {}
    for track in tracks:
        try:
            idx = int(track.source_ref)
        except ValueError:
            if track.id in skip_track_ids or track.excluded:
                continue
            # No need to fire on_track_start for an invalid-source_ref
            # track — the backend rejects the track entirely via the
            # FAILED-from-QUEUED guard. Falling through to on_track_done
            # is consistent with how the per-title implementation handled
            # it, and the upstream patch_with_retry catches the 409 if
            # it ever shows up here.
            await on_track_done(
                track,
                RipResult(ok=False, error=f"invalid source_ref: {track.source_ref!r}"),
            )
            continue
        track_by_index[idx] = track

    if not track_by_index:
        return

    # Eligible source indexes — every title MakeMKV will rip at this
    # `--minlength`, sorted ascending. MakeMKV's `mkv all` output files
    # carry an output-position suffix (`_t00`, `_t01`, ...) rather than
    # the source title index, numbered densely over the titles that pass
    # the minimum; file `_tNN` belongs to the NN-th entry of this list,
    # which is how attribution gets back to tracks.
    #
    # Built from the scan, not the tracks: the scan lists every title
    # (`--minlength=0`, ids = rank among them), while tracks exist only
    # for the titles the preset kept, so a track-based list misses titles
    # MakeMKV rips anyway (a minimum below the scan's baseline, or a
    # preset that keeps fewer) and shifts every later file onto the wrong
    # track. A `_tNN` whose title has no track is left on disk unclaimed.
    # Without a stored scan, fall back to the tracks' durations
    # (`_title_duration`: a fresh rip's tracks have no measured one yet).
    # A title with no duration at all is treated as eligible so it isn't
    # dropped silently; if MakeMKV skips it, it falls through to "produced
    # no .mkv" downstream.
    if scan_titles is not None:
        duration_by_index = {t.index: t.duration_seconds for t in scan_titles}
    else:
        duration_by_index = {idx: _title_duration(t) for idx, t in track_by_index.items()}
    eligible_source_indexes = sorted(
        idx for idx, duration in duration_by_index.items() if duration is None or duration >= min_length_seconds
    )

    # Excluded tracks get no lifecycle callbacks: they stay `queued` (D3).
    # They stay in `eligible_source_indexes` above, since `mkv all` without
    # a stored scan still rips them and its file positions count them.
    kept = {idx: t for idx, t in track_by_index.items() if not t.excluded}
    excluded = sorted(set(track_by_index) - set(kept))
    if excluded:
        logger.info("not ripping excluded titles %s", excluded)
    if not kept:
        return

    # `mkv all` rips every eligible title, so it fits only when all of them
    # are kept. Otherwise read the disc once and remux the kept titles from
    # disk; an ISO or folder source is already on disk.
    every_title_kept = set(eligible_source_indexes) <= set(kept)
    file_source = is_file_source(device_path)
    if scan_titles is not None and not (every_title_kept and (file_source or not backup_full_selections)):
        await _rip_kept_titles(
            disc_type=disc_type,
            device_path=device_path,
            file_source=file_source,
            kept=kept,
            scan_titles=scan_titles,
            eligible_source_indexes=eligible_source_indexes,
            output_dir=output_dir,
            backup_dir=backup_dir,
            on_track_start=on_track_start,
            on_track_done=on_track_done,
            on_track_progress=on_track_progress,
            min_length_seconds=min_length_seconds,
            stop=stop,
            skip_track_ids=skip_track_ids,
            drive_lost=drive_lost,
        )
        return

    if not every_title_kept:
        # Nothing to find the kept titles by in a backup.
        logger.warning("job has no stored scan: ripping every title with mkv all; titles not kept stay unclaimed")
    await _rip_every_title(
        device_path=device_path,
        track_by_index=kept,
        eligible_source_indexes=eligible_source_indexes,
        duration_by_index=duration_by_index,
        output_dir=output_dir,
        on_track_start=on_track_start,
        on_track_done=on_track_done,
        on_track_progress=on_track_progress,
        min_length_seconds=min_length_seconds,
        stop=stop,
        skip_track_ids=skip_track_ids,
        drive_lost=drive_lost,
    )


async def _rip_every_title(
    *,
    device_path: str,
    track_by_index: dict[int, TrackView],
    eligible_source_indexes: list[int],
    duration_by_index: dict[int, int | None],
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None,
    min_length_seconds: int,
    stop: asyncio.Event | None,
    skip_track_ids: Collection[str],
    drive_lost: Callable[[], bool] | None,
) -> None:
    """Single-invocation DVD/BD rip with per-title attribution.

    The makemkvcon invocation rips every title ≥ minlength to
    `output_dir`. We map title_index → TrackView via `source_ref` and
    fan the lifecycle callbacks out as MakeMKV's stream announces each
    title (`track_by_index`: the kept tracks, keyed by `source_ref`).

    Lifecycle correctness: the backend's track state machine requires
    `queued → in_progress → done|failed`. PRGT-driven `on_track_start`
    is best-effort — empirically MakeMKV in `mkv all` mode emits a
    single overall "Saving all titles to MKV files" PRGT rather than
    per-title milestones, so `_on_title_start` may never fire even on
    a healthy rip. We track which tracks already had `on_track_start`
    called via the stream, then synthesise it from the post-rip
    attribution for any track that didn't — so PATCH `done` / `failed`
    always lands on a row in `in_progress`.
    """
    # Disc-level progress is credited to one track; the first eligible
    # title may have no track of its own.
    progress_index = next((idx for idx in eligible_source_indexes if idx in track_by_index), None)

    # Titles an earlier pass of this job already settled (a resume after the
    # drive dropped off the bus): makemkvcon rips them again, as `mkv all`
    # does, but they get no callbacks and their new files are deleted.
    skipped = {idx for idx, t in track_by_index.items() if t.id in skip_track_ids}
    started: set[int] = set(skipped)
    # Source indexes whose Track has already been PATCHed DONE via the
    # streamer's mid-rip on_title_done callback. The post-rip
    # attribution loop skips these to avoid double-PATCHing.
    done_emitted: set[int] = set(skipped)

    def _drive_dropped() -> bool:
        """The drive left the bus under this rip (D-133). The first time
        `drive_lost` says so, set `stop`, which kills makemkvcon."""
        if stop is None:
            return False
        if not stop.is_set() and drive_lost is not None and drive_lost():
            logger.warning("drive dropped off the bus mid-rip; stopping makemkvcon")
            stop.set()
        return stop.is_set()

    async def _on_title_start(title_idx: int) -> None:
        track = track_by_index.get(title_idx)
        if track is None:
            # MakeMKV announced a title we didn't select (e.g. user
            # picked TRACKS mode but rip_disc rips all ≥ minlength).
            return
        if title_idx in started:
            return
        started.add(title_idx)
        await on_track_start(track)

    async def _on_title_progress(title_idx: int, fraction: float) -> None:
        if on_track_progress is None:
            return
        track = track_by_index.get(title_idx)
        if track is None:
            return
        await on_track_progress(track, fraction)

    async def _on_disc_progress(fraction: float) -> None:
        """Disc-overall PRGV fallback for `mkv all`.

        MakeMKV in `mkv all` mode emits no per-title PRGT, so the
        streamer's `current_title` stays None and no `_on_title_progress`
        ever fires. Without this fallback the dashboard bar would stay
        at 0 % for the whole rip even though the file is being written.

        We attribute disc-level progress to the first eligible title
        that has a track —
        the WS payload requires a track_id, but the UI's rips store
        keys live progress by `job_id` and only ever displays a single
        bar per disc (see services/ui-neu/frontend/src/lib/stores/rips.svelte.ts),
        so the choice of track_id is purely a wire-format detail. The
        bar fills smoothly 0→100 % across the whole rip via the PRGV
        `total/max` channel. Per-title attribution still happens
        post-rip in the attribution loop below.
        """
        if on_track_progress is None or progress_index is None:
            return
        await on_track_progress(track_by_index[progress_index], fraction)

    hasher = TitleHasher()

    def _finalise_after_hash(track: TrackView, result: RipResult) -> OnHashed:
        async def then(digest: str | None) -> None:
            await on_track_done(
                track,
                RipResult(
                    ok=True,
                    output_path=result.output_path,
                    size_bytes=result.size_bytes,
                    duration_seconds=result.duration_seconds,
                    sha256=digest,
                ),
            )

        return then

    async def _on_verify_progress(done: int, total: int, rate: float | None) -> None:
        if on_track_progress is None or progress_index is None:
            return
        first_track = track_by_index[progress_index]
        stats: TransferStats = {"bytes_done": done, "bytes_total": total, "rate_bps": rate, "phase": "verifying"}
        await on_track_progress(first_track, min(done / total, 1.0) if total else 0.0, stats)

    async def _on_title_done(title_idx: int) -> None:
        """Mid-rip per-title finalisation. Stat the .mkv produced for
        this title's output position and hand it to the background
        hasher, which PATCHes the Track DONE with size, output_path and
        sha256 once the file is hashed. The hash never runs on the
        streamer: a 27 GB main feature would block it for minutes and
        starve the next title's PRGC of attention.

        Files are matched by name (same convention as rip_disc's
        post-rip attribution): the file whose `_tNN.mkv` suffix equals
        this title's position in the eligible list is this title's. If
        that file doesn't exist (MakeMKV skipped the title, or the
        stream named a position that isn't on disk), nothing is PATCHed
        here and the post-rip loop marks the track FAILED, rather than
        crediting it with another title's file.
        """
        track = track_by_index.get(title_idx)
        if track is None:
            return
        if title_idx in done_emitted:
            return
        if _drive_dropped():
            # makemkvcon moved on because the drive dropped under this title,
            # so its file is cut short. The stopped-rip path below reports it
            # unfinished.
            return
        try:
            position = eligible_source_indexes.index(title_idx)
        except ValueError:
            # MakeMKV announced a title we didn't list as eligible —
            # nothing to attribute live; the post-rip loop handles it.
            return
        file_path = _output_files_by_position(output_dir).get(position)
        if file_path is None:
            # No `_tNN` file for this title: defer to the post-rip loop,
            # which reports it FAILED if it never appears.
            logger.debug(
                "on_title_done: no file for source_idx=%d position=%d; deferring",
                title_idx,
                position,
            )
            return
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            logger.warning(
                "on_title_done: stat failed source_idx=%d path=%s err=%s",
                title_idx,
                file_path,
                exc,
            )
            return
        await _ensure_started(title_idx, track)
        result = RipResult(
            ok=True,
            output_path=file_path,
            size_bytes=size,
            duration_seconds=track.duration_seconds or track.expected_duration_seconds,
        )
        done_emitted.add(title_idx)
        hasher.submit(file_path, size, _finalise_after_hash(track, result))

    async def _ensure_started(title_idx: int, track: TrackView) -> None:
        """Guarantee on_track_start has fired for this track before any
        terminal PATCH. In `mkv all` mode the stream-driven PRGT often
        doesn't fire, so this synthesises the QUEUED → IN_PROGRESS
        transition from the post-rip attribution loop."""
        if title_idx in started:
            return
        started.add(title_idx)
        await on_track_start(track)

    try:
        disc_result = await rip_disc(
            device_path=device_path,
            output_dir=output_dir,
            minlength_seconds=min_length_seconds,
            eligible_source_indexes=eligible_source_indexes,
            on_title_start=_on_title_start,
            on_title_progress=_on_title_progress,
            on_disc_progress=_on_disc_progress,
            on_title_done=_on_title_done,
            hash_outputs=False,
            stop=stop,
        )
        _delete_skipped_outputs(output_dir, eligible_source_indexes, skipped)
        if disc_result.overall_error is None and _drive_dropped():
            # makemkvcon exited on its own after the drive dropped: a title
            # not finalised above may be cut short, so none of them count.
            disc_result = RipDiscResult(overall_error=STOPPED_ERROR, titles={})

        if disc_result.overall_error is not None:
            # Rip-level failure — mark every selected track FAILED with the
            # disc-level error so the operator knows what happened. Each
            # track must transit IN_PROGRESS first per the backend's state
            # machine. Tracks already handed to the hasher via the live
            # path (titles that finalised before the rip aborted) are
            # PATCHed DONE once hashed — partial success is still success
            # for those files.
            logger.warning("rip_disc failed: %s", disc_result.overall_error)
            await hasher.finish(_on_verify_progress)
            for title_idx, track in track_by_index.items():
                if title_idx in done_emitted:
                    continue
                await _ensure_started(title_idx, track)
                await on_track_done(track, RipResult(ok=False, error=disc_result.overall_error))
            return

        # Per-title attribution for tracks the live path didn't already
        # finalise (e.g. below-minlength tracks MakeMKV skipped, or
        # eligible tracks that didn't produce a file). Tracks already in
        # `done_emitted` were handed to the hasher by `_on_title_done`
        # mid-rip and are skipped here to avoid double-PATCHing.
        # - If rip_disc reports a result for it: use that. (Includes
        #   ok=False for eligible titles that didn't render — the live
        #   path can't report failures since it's only triggered by PRGC
        #   transitions on successful saves.) A successful file goes to
        #   the hasher like a live one.
        # - Otherwise: the track was below `min_length_seconds`, so MakeMKV
        #   never attempted it. FAILED with a clear reason so the operator
        #   can lower minlength and retry.
        for title_idx, track in track_by_index.items():
            if title_idx in done_emitted:
                continue
            await _ensure_started(title_idx, track)
            result = disc_result.titles.get(title_idx)
            if result is None:
                duration = duration_by_index.get(title_idx, _title_duration(track))
                if duration is not None and duration < min_length_seconds:
                    err = f"track duration {duration}s below minlength={min_length_seconds}s; makemkvcon skipped it"
                else:
                    err = (
                        f"makemkvcon produced no output for title {title_idx} "
                        f"(eligible={title_idx in eligible_source_indexes})"
                    )
                await on_track_done(track, RipResult(ok=False, error=err))
                continue
            output_path = result.output_path
            if not result.ok or output_path is None:
                await on_track_done(track, result)
                continue
            # Carry duration_seconds through from the scan when MakeMKV
            # produced the file successfully. rip_disc doesn't measure
            # durations from the .mkv files, so we forward the scan-time
            # estimate. `track.duration_seconds` is the post-rip actual
            # (null until a previous rip set it — usually null here);
            # `track.expected_duration_seconds` is the scan-time estimate
            # populated by `select_tracks` at rip-start.
            if result.duration_seconds is None:
                result = RipResult(
                    ok=True,
                    output_path=result.output_path,
                    size_bytes=result.size_bytes,
                    duration_seconds=track.duration_seconds or track.expected_duration_seconds,
                )
            done_emitted.add(title_idx)
            hasher.submit(output_path, result.size_bytes or 0, _finalise_after_hash(track, result))

        await hasher.finish(_on_verify_progress)
    except BaseException:
        # Abandoned mid-rip (CancelledError) or a PATCH that gave up: stop
        # hashing files that are about to be wiped.
        hasher.cancel()
        raise


def _title_duration(track: TrackView) -> int | None:
    """The title's length as far as `--minlength` is concerned: the measured
    duration once a rip has set it, else the scan's estimate. At rip-start
    `duration_seconds` is null (it's the post-rip actual), so judging by it
    alone counted every title eligible, and one MakeMKV left out for being
    short shifted every later `_tNN` file onto the wrong track."""
    if track.duration_seconds is not None:
        return track.duration_seconds
    return track.expected_duration_seconds


def _delete_skipped_outputs(output_dir: Path, eligible_source_indexes: list[int], skipped: set[int]) -> None:
    """Delete the files a resume pass wrote for titles an earlier pass already
    settled. Matched by their `_tNN` position suffix, not by list order, so a
    title that produced no file never shifts the others."""
    if not skipped:
        return
    positions = {pos for pos, idx in enumerate(eligible_source_indexes) if idx in skipped}
    for f in output_dir.glob("*.mkv"):
        m = _OUTPUT_FILE_RE.search(f.name)
        if m and int(m.group(1)) in positions:
            f.unlink(missing_ok=True)


def _complete_backup(backup_dir: Path) -> str | None:
    """The read-back URL of a finished backup in `backup_dir`, or None."""
    try:
        url = (backup_dir / BACKUP_COMPLETE_MARKER).read_text().strip()
    except OSError:
        return None
    return url or None


async def _rip_kept_titles(
    *,
    disc_type: DiscType,
    device_path: str,
    file_source: bool,
    kept: dict[int, TrackView],
    scan_titles: list[ScanTitle],
    eligible_source_indexes: list[int],
    output_dir: Path,
    backup_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None,
    min_length_seconds: int,
    stop: asyncio.Event | None,
    skip_track_ids: Collection[str],
    drive_lost: Callable[[], bool] | None,
) -> None:
    """Backup-then-remux (track-select plan D1): rip only the kept titles.

    1. A drive is read once: `rip_backup` of the whole disc into
       `backup_dir`, taken as `selection_backup` says, and marked complete.
       A backup an earlier pass of this job finished is reused, so a resume
       reads the drive no more. An ISO or folder source is read directly.
    2. The source's titles are listed and each kept disc-scan title is found
       among them by what it plays (`title_map`); ids are never trusted.
    3. `remux_title` per kept title, in index order, from disk, with no eye
       on the drive (`stop` only stops the backup, and a backup the drive
       dropped under counts as stopped even if makemkvcon exited 0). Each
       file is named by its disc-scan index and hashed like an `mkv all` file.

    Tracks in `skip_track_ids` (settled by an earlier pass) get no callbacks.
    A kept track goes IN_PROGRESS when its remux starts. A backup failure
    fails every kept track with its error, and the partial backup is
    deleted; one remux failure, or a title not found in the source, fails
    only that track. Kept tracks below the rip's minimum are FAILED "below
    minlength", as on the `mkv all` path. `.remux/` goes at the end, and
    `.backup/` too once every title it was read for is remuxed; if one
    wasn't, the backup stays so a retry needs no disc.
    """
    scan_by_index = {t.index: t for t in scan_titles}
    pending = {idx: t for idx, t in kept.items() if t.id not in skip_track_ids}
    started: set[int] = set()

    async def start(idx: int) -> None:
        if idx not in started:
            started.add(idx)
            await on_track_start(pending[idx])

    async def fail(idx: int, error: str) -> None:
        await start(idx)
        await on_track_done(pending[idx], RipResult(ok=False, error=error))

    eligible = set(eligible_source_indexes)
    for idx in sorted(set(pending) - eligible):
        if idx not in scan_by_index:
            await fail(idx, f"title {idx} is not in the disc scan")
            continue
        duration = scan_by_index[idx].duration_seconds
        await fail(idx, f"track duration {duration}s below minlength={min_length_seconds}s; makemkvcon skipped it")
    to_rip = sorted(set(pending) & eligible)
    if not to_rip:
        return

    # One progress bar per job (the dashboard keys it by job), credited to
    # the first kept track: the backup fills BACKUP_PROGRESS_SHARE of it,
    # the remuxes the rest, weighted by each title's expected size.
    progress_track = pending[to_rip[0]]

    async def report(fraction: float, stats: TransferStats | None = None) -> None:
        if on_track_progress is not None:
            await on_track_progress(progress_track, min(1.0, max(0.0, fraction)), stats)

    if file_source:
        source_url, source_name, remux_from = makemkv_source_url(device_path), "the source", 0.0
    else:
        source_name, remux_from = "the backup", BACKUP_PROGRESS_SHARE
        reused = _complete_backup(backup_dir)
        if reused is not None:
            source_url = reused
            logger.info("remuxing %d title(s) from the finished disc backup %s; no drive read", len(to_rip), reused)
        else:
            try:
                choice = await selection_backup(disc_type, device_path, backup_dir)
            except BackupSourceError as exc:
                logger.warning("disc backup failed: %s", exc)
                for idx in to_rip:
                    await fail(idx, f"disc backup failed: {exc}")
                return
            shutil.rmtree(backup_dir, ignore_errors=True)
            logger.info(
                "backing up disc %s to %s to rip %d of %d titles",
                device_path,
                choice.target,
                len(to_rip),
                len(eligible_source_indexes),
            )

            async def backup_progress(fraction: float) -> None:
                await report(fraction * BACKUP_PROGRESS_SHARE)

            backup = await rip_backup(
                choice.source, choice.target, decrypt=choice.decrypt, on_progress=backup_progress, stop=stop
            )
            if backup.ok and stop is not None and (stop.is_set() or (drive_lost is not None and drive_lost())):
                # makemkvcon finished on its own after the drive dropped: the
                # copy may be cut short, so it doesn't count.
                logger.warning("drive dropped off the bus during the disc backup")
                stop.set()
                backup = RipResult(ok=False, error=STOPPED_ERROR)
            if not backup.ok:
                # A partial copy is no use to anyone.
                shutil.rmtree(backup_dir, ignore_errors=True)
                logger.warning("disc backup failed: %s", backup.error)
                for idx in to_rip:
                    await fail(idx, f"disc backup failed: {backup.error}")
                return
            source_url = choice.read_back_url()
            (backup_dir / BACKUP_COMPLETE_MARKER).write_text(source_url + "\n")
            logger.info("disc backup done: %s", source_url)

    try:
        source_titles = await list_source_titles(source_url, minlength_seconds=REMUX_MIN_LENGTH_SECONDS)
    except ScanError as exc:
        logger.warning("could not list the titles of %s: %s", source_url, exc)
        for idx in to_rip:
            await fail(idx, f"could not list the titles of {source_name}: {exc}")
        return
    source_id = match_titles([scan_by_index[idx] for idx in to_rip], source_titles)

    weights = [pending[idx].expected_size_bytes or scan_by_index[idx].size_bytes or 1 for idx in to_rip]
    total_weight = sum(weights)
    scratch_root = output_dir / REMUX_DIRNAME
    hasher = TitleHasher()
    # Kept titles that weren't remuxed; a retry could remux them from the backup.
    failed: list[int] = []

    async def verify_progress(done: int, total: int, rate: float | None) -> None:
        stats: TransferStats = {"bytes_done": done, "bytes_total": total, "rate_bps": rate, "phase": "verifying"}
        await report(done / total if total else 0.0, stats)

    try:
        done_weight = 0
        for n, (idx, weight) in enumerate(zip(to_rip, weights, strict=True), start=1):
            track = pending[idx]
            if idx not in source_id:
                failed.append(idx)
                await fail(idx, f"could not map title {idx} to {source_name}")
                done_weight += weight
                continue
            await start(idx)
            logger.info("remuxing title %d (%d/%d) from %s title %d", idx, n, len(to_rip), source_name, source_id[idx])

            async def remux_progress(fraction: float, _base: int = done_weight, _weight: int = weight) -> None:
                await report(remux_from + (1.0 - remux_from) * (_base + fraction * _weight) / total_weight)

            result = await remux_title(
                source_url,
                source_id[idx],
                scratch_dir=scratch_root / str(idx),
                output_dir=output_dir,
                output_index=idx,
                on_progress=remux_progress,
            )
            done_weight += weight
            if not result.ok or result.output_path is None:
                failed.append(idx)
                logger.warning("remux of title %d failed: %s", idx, result.error)
                await on_track_done(track, result)
                continue
            done = RipResult(
                ok=True,
                output_path=result.output_path,
                size_bytes=result.size_bytes,
                duration_seconds=track.duration_seconds or track.expected_duration_seconds,
            )
            hasher.submit(result.output_path, result.size_bytes or 0, _report_after_hash(on_track_done, track, done))
        await hasher.finish(verify_progress)
    except BaseException:
        # Abandoned mid-rip (CancelledError) or a PATCH that gave up: stop
        # hashing files that are about to be wiped.
        hasher.cancel()
        raise
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    if file_source:
        return
    if failed:
        logger.info("keeping the disc backup %s: titles %s failed and can be retried from it", backup_dir, failed)
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)
        logger.info("every kept title remuxed; removed the disc backup %s", backup_dir)


def _report_after_hash(on_track_done: OnTrackDone, track: TrackView, result: RipResult) -> OnHashed:
    """Report `track` DONE with `result` and the digest once its file is hashed."""

    async def then(digest: str | None) -> None:
        await on_track_done(
            track,
            RipResult(
                ok=True,
                output_path=result.output_path,
                size_bytes=result.size_bytes,
                duration_seconds=result.duration_seconds,
                sha256=digest,
            ),
        )

    return then
