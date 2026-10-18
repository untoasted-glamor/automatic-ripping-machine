import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from arm_common import DiscType
from arm_common.schemas import ScanTitle, TrackView

from arm_ripper.rip.abcde_rip import rip_cd
from arm_ripper.rip.data_rip import rip_data
from arm_ripper.rip.makemkv_rip import RipResult, _output_files_by_position, backup_disc, remux_title, rip_disc
from arm_ripper.rip.title_map import match_titles
from arm_ripper.scan.makemkv import ScanError, list_source_titles
from arm_ripper.source import is_iso_source

logger = logging.getLogger("arm_ripper.rip.dispatcher")

OnTrackStart = Callable[[TrackView], Awaitable[None]]
OnTrackDone = Callable[[TrackView, RipResult], Awaitable[None]]
OnTrackProgress = Callable[[TrackView, float], Awaitable[None]]

# v3 default. 120s drops menu loops and vendor bumpers without cutting
# the 2–5 minute extras users typically care about. Sessions can
# override per-rip via `Session.overrides_json["min_length_seconds"]`.
DEFAULT_MIN_LENGTH_SECONDS = 120

# Backup-then-remux scratch space, inside the job's raw dir so job delete,
# abandon and the boot probe's wipe remove it with everything else.
# `.remux/` is deleted once the last title is remuxed. `.backup/` goes with
# it when every kept title was remuxed; if any failed, it stays so a retry
# can remux them with no disc, until the job is deleted or the operator
# deletes the backup.
BACKUP_DIRNAME = ".backup"
REMUX_DIRNAME = ".remux"
# Written into `.backup/` once a backup finished; holds the URL to read it
# back with (`file:<dir>` on Blu-ray, `iso:<image>` on DVD).
BACKUP_COMPLETE_MARKER = ".complete"
# Share of the job's progress bar the backup takes. The Hanako-kun Blu-ray
# backup took 48 min; remuxing one of its episodes from disk took 19-31 s,
# its 2:49 play-all 140 s.
BACKUP_PROGRESS_SHARE = 0.9
# The backup's titles are listed, and remuxed, at minlength 0: every title,
# numbered by its rank among all of them, whatever the rip's minimum.
REMUX_MIN_LENGTH_SECONDS = 0


async def rip_all(
    disc_type: DiscType,
    device_path: str,
    tracks: list[TrackView],
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None = None,
    min_length_seconds: int = DEFAULT_MIN_LENGTH_SECONDS,
    scan_titles: list[ScanTitle] | None = None,
    backup_full_selections: bool = False,
) -> None:
    """Rip every kept track in `tracks` and invoke the lifecycle callbacks.

    `scan_titles` is the job's stored disc scan (`RipStartResponse.scan_titles`),
    every title MakeMKV listed. DVD/BD use it to tell which output file is
    which title, and to find each kept title again in a backup; None falls
    back to the tracks' own durations and the `mkv all` path.

    Excluded tracks are never reported on (no start, no done): they stay
    `queued`, and the UI shows them as skipped.

    Per disc_type:
    - DVD / BD, every title at or above `min_length_seconds` kept: one
      `makemkvcon mkv ... all` invocation for the whole disc; per-title
      outcomes attributed from the robot stream and the produced files.
      Drive stays open the whole time — no gaps where USB autosuspend can
      drop the device. Selected titles get DONE/FAILED based on whether
      MakeMKV produced their file. Titles below `min_length_seconds` are
      skipped by MakeMKV; if the user requested one of those, we report
      FAILED with a clear reason rather than silently ignoring it.
    - DVD / BD, some of those titles not kept (excluded, or no track at
      all): back the disc up with one drive read, then remux each kept
      title from the backup (`.iso` sources skip the backup and remux from
      the image). `backup_full_selections` sends full selections from a
      drive down this path too (`ARM_BACKUP_FULL_SELECTIONS`).
    - CD: mark all tracks IN_PROGRESS, run abcde once for the whole disc,
      then emit DONE/FAILED per track from the bulk result.
    - DATA: a single dd dump assigned to the first (only) track.
    """
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
            scan_titles=scan_titles,
            backup_full_selections=backup_full_selections,
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
        result = await rip_data(device_path=device_path, output_dir=output_dir)
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
    scan_titles: list[ScanTitle] | None,
    backup_full_selections: bool,
) -> None:
    """Pick the DVD/BD strategy: `mkv all` when it would rip exactly what is
    kept, backup-then-remux otherwise (D1). Tracks with a malformed
    `source_ref` are failed up front."""
    # Pre-compute source_ref → track lookup so the rip-side callbacks
    # are O(1). Skip and pre-fail tracks with malformed source_ref.
    track_by_index: dict[int, TrackView] = {}
    for track in tracks:
        try:
            idx = int(track.source_ref)
        except ValueError:
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
    # Without a stored scan, fall back to the tracks' own durations.
    # Titles of unknown duration are treated as eligible so they aren't
    # dropped silently; if MakeMKV skips them, they fall through to
    # "produced no .mkv" downstream.
    if scan_titles is not None:
        duration_by_index = {t.index: t.duration_seconds for t in scan_titles}
    else:
        duration_by_index = {idx: t.duration_seconds for idx, t in track_by_index.items()}
    eligible_source_indexes = sorted(
        idx for idx, duration in duration_by_index.items() if duration is None or duration >= min_length_seconds
    )

    # Excluded tracks get no lifecycle callbacks at all: they stay `queued`.
    kept = {idx: t for idx, t in track_by_index.items() if not t.excluded}
    excluded = sorted(set(track_by_index) - set(kept))
    if excluded:
        logger.info("not ripping excluded titles %s", excluded)
    if not kept:
        return

    # `mkv all` rips every eligible title, so it only fits when all of them
    # are kept. Otherwise read the disc once (`backup`) and remux the kept
    # titles from disk; an `.iso` is already on disk and needs no backup.
    every_title_kept = set(eligible_source_indexes) <= set(kept)
    from_iso = is_iso_source(device_path)
    if scan_titles is not None and not (every_title_kept and (from_iso or not backup_full_selections)):
        await _rip_selected_titles(
            disc_type=disc_type,
            device_path=device_path,
            from_iso=from_iso,
            kept=kept,
            scan_titles=scan_titles,
            eligible_source_indexes=eligible_source_indexes,
            output_dir=output_dir,
            on_track_start=on_track_start,
            on_track_done=on_track_done,
            on_track_progress=on_track_progress,
            min_length_seconds=min_length_seconds,
        )
        return

    if not every_title_kept:
        # No stored scan to find the kept titles by in a backup.
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
) -> None:
    """Single-invocation DVD/BD rip with per-title attribution.

    The makemkvcon invocation rips every title ≥ minlength to
    `output_dir`. We map title_index → TrackView via `source_ref` and
    fan the lifecycle callbacks out as MakeMKV's stream announces each
    title. Tracks whose `source_ref` doesn't parse as an int are
    failed up front — same shape as the per-title implementation.

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

    started: set[int] = set()
    # Source indexes whose Track has already been PATCHed DONE via the
    # streamer's mid-rip on_title_done callback. The post-rip
    # attribution loop skips these to avoid double-PATCHing.
    done_emitted: set[int] = set()

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
        bar per disc (see [services/ui/src/components/JobCard.vue]),
        so the choice of track_id is purely a wire-format detail. The
        bar fills smoothly 0→100 % across the whole rip via the PRGV
        `total/max` channel. Per-title attribution still happens
        post-rip in the attribution loop below.
        """
        if on_track_progress is None or progress_index is None:
            return
        await on_track_progress(track_by_index[progress_index], fraction)

    async def _on_title_done(title_idx: int) -> None:
        """Mid-rip per-title finalisation. Stat the .mkv produced for
        this title's output position and PATCH the Track DONE with
        size + output_path. SHA256 is intentionally skipped here —
        hashing a 27 GB main feature would block the streamer for a
        minute and starve the next title's PRGC of attention; if the
        column is needed it can be filled in by a separate post-rip
        pass.

        Files are matched by name (same convention as rip_disc's
        post-rip attribution): the file whose `_tNN.mkv` suffix equals
        this title's position in the eligible list is this title's. If
        that file doesn't exist — MakeMKV skipped the title, or the
        stream named a position that isn't on disk — nothing is PATCHed
        here and the post-rip loop marks the track FAILED, rather than
        crediting it with another title's file.
        """
        track = track_by_index.get(title_idx)
        if track is None:
            return
        if title_idx in done_emitted:
            return
        try:
            position = eligible_source_indexes.index(title_idx)
        except ValueError:
            # MakeMKV announced a title we didn't list as eligible —
            # nothing to attribute live; the post-rip loop handles it.
            return
        file_path = _output_files_by_position(output_dir).get(position)
        if file_path is None:
            # No `_tNN` file for this title — defer to the post-rip
            # loop, which reports it FAILED if it never appears.
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
        await on_track_done(track, result)

    async def _ensure_started(title_idx: int, track: TrackView) -> None:
        """Guarantee on_track_start has fired for this track before any
        terminal PATCH. In `mkv all` mode the stream-driven PRGT often
        doesn't fire, so this synthesises the QUEUED → IN_PROGRESS
        transition from the post-rip attribution loop."""
        if title_idx in started:
            return
        started.add(title_idx)
        await on_track_start(track)

    disc_result = await rip_disc(
        device_path=device_path,
        output_dir=output_dir,
        minlength_seconds=min_length_seconds,
        eligible_source_indexes=eligible_source_indexes,
        on_title_start=_on_title_start,
        on_title_progress=_on_title_progress,
        on_disc_progress=_on_disc_progress,
        on_title_done=_on_title_done,
    )

    if disc_result.overall_error is not None:
        # Rip-level failure — mark every selected track FAILED with the
        # disc-level error so the operator knows what happened. Each
        # track must transit IN_PROGRESS first per the backend's state
        # machine. Tracks already PATCHed DONE via the live path
        # (titles that finalised before the rip aborted) keep their
        # DONE status — partial success is still success for those
        # files.
        logger.warning("rip_disc failed: %s", disc_result.overall_error)
        for title_idx, track in track_by_index.items():
            if title_idx in done_emitted:
                continue
            await _ensure_started(title_idx, track)
            await on_track_done(track, RipResult(ok=False, error=disc_result.overall_error))
        return

    # Per-title attribution for tracks the live path didn't already
    # finalise (e.g. below-minlength tracks MakeMKV skipped, or
    # eligible tracks that didn't produce a file). Tracks already in
    # `done_emitted` were PATCHed DONE by `_on_title_done` mid-rip and
    # are skipped here to avoid double-PATCHing.
    # - If rip_disc reports a result for it: use that. (Includes
    #   ok=False for eligible titles that didn't render — the live
    #   path can't report failures since it's only triggered by PRGC
    #   transitions on successful saves.)
    # - Otherwise: the track was below `min_length_seconds`, so MakeMKV
    #   never attempted it. FAILED with a clear reason so the operator
    #   can lower minlength and retry.
    for title_idx, track in track_by_index.items():
        if title_idx in done_emitted:
            continue
        await _ensure_started(title_idx, track)
        result = disc_result.titles.get(title_idx)
        if result is None:
            duration = duration_by_index.get(title_idx, track.duration_seconds)
            if duration is not None and duration < min_length_seconds:
                err = f"track duration {duration}s below minlength={min_length_seconds}s; makemkvcon skipped it"
            else:
                err = (
                    f"makemkvcon produced no output for title {title_idx} "
                    f"(eligible={title_idx in eligible_source_indexes})"
                )
            await on_track_done(track, RipResult(ok=False, error=err))
            continue
        # Carry duration_seconds through from the scan when MakeMKV
        # produced the file successfully. rip_disc doesn't measure
        # durations from the .mkv files, so we forward the scan-time
        # estimate. `track.duration_seconds` is the post-rip actual
        # (null until a previous rip set it — usually null here);
        # `track.expected_duration_seconds` is the scan-time estimate
        # populated by `select_tracks` at rip-start.
        if result.ok and result.duration_seconds is None:
            result = RipResult(
                ok=True,
                output_path=result.output_path,
                size_bytes=result.size_bytes,
                duration_seconds=track.duration_seconds or track.expected_duration_seconds,
                sha256=result.sha256,
            )
        await on_track_done(track, result)


async def _rip_selected_titles(
    *,
    disc_type: DiscType,
    device_path: str,
    from_iso: bool,
    kept: dict[int, TrackView],
    scan_titles: list[ScanTitle],
    eligible_source_indexes: list[int],
    output_dir: Path,
    on_track_start: OnTrackStart,
    on_track_done: OnTrackDone,
    on_track_progress: OnTrackProgress | None,
    min_length_seconds: int,
) -> None:
    """Backup-then-remux (D1): rip only the kept titles.

    1. `backup_disc`: one read of the whole disc into `.backup/` (skipped for
       an `.iso`, which is read directly).
    2. List the backup's titles and match each kept disc-scan title to one
       by what it plays (`title_map`): backup ids differ from the disc's.
    3. `remux_title` per kept title, in index order, from disk; each file is
       named by its disc-scan index (`<prefix>_tNN.mkv`).

    A backup failure fails every kept track with its error; one remux
    failure fails only its track. Kept tracks below the rip's minimum are
    FAILED "below minlength", as on the `mkv all` path. `.remux/` is
    deleted at the end, and `.backup/` too when every title it was read for
    was remuxed. If one couldn't be (remux failed, title not found, backup
    unreadable) the backup stays, so a retry needs no disc; it goes when
    the job is deleted or the operator deletes it. A backup that failed is
    deleted at once.
    """
    scan_by_index = {t.index: t for t in scan_titles}
    started: set[int] = set()

    async def finish(idx: int, result: RipResult) -> None:
        track = kept[idx]
        if idx not in started:
            started.add(idx)
            await on_track_start(track)
        await on_track_done(track, result)

    eligible = set(eligible_source_indexes)
    for idx in sorted(set(kept) - eligible):
        duration = scan_by_index[idx].duration_seconds if idx in scan_by_index else kept[idx].duration_seconds
        await finish(
            idx,
            RipResult(
                ok=False,
                error=f"track duration {duration}s below minlength={min_length_seconds}s; makemkvcon skipped it",
            ),
        )
    to_rip = sorted(set(kept) & eligible)
    if not to_rip:
        return

    async def fail_all(error: str) -> None:
        for idx in to_rip:
            await finish(idx, RipResult(ok=False, error=error))

    # One progress bar per job, credited to the first kept track.
    progress_track = kept[to_rip[0]]

    async def report(fraction: float) -> None:
        if on_track_progress is not None:
            await on_track_progress(progress_track, min(1.0, max(0.0, fraction)))

    backup_dir = output_dir / BACKUP_DIRNAME
    if from_iso:
        source_url, source_name, remux_from = f"iso:{device_path}", "the ISO", 0.0
    else:
        # A DVD backup is one ISO image; a Blu-ray backup a folder.
        target = backup_dir / ("disc.iso" if disc_type == DiscType.DVD else "disc")
        shutil.rmtree(backup_dir, ignore_errors=True)
        logger.info(
            "backing up disc %s to %s to rip %d of %d titles",
            device_path,
            target,
            len(to_rip),
            len(eligible_source_indexes),
        )

        async def backup_progress(fraction: float) -> None:
            await report(fraction * BACKUP_PROGRESS_SHARE)

        backup = await backup_disc(device_path, target, on_progress=backup_progress)
        if not backup.ok or backup.source_url is None:
            # A partial copy is no use to anyone.
            shutil.rmtree(backup_dir, ignore_errors=True)
            logger.warning("disc backup failed: %s", backup.error)
            await fail_all(f"disc backup failed: {backup.error}")
            return
        (backup_dir / BACKUP_COMPLETE_MARKER).write_text(backup.source_url + "\n")
        logger.info("backup done: %s", backup.source_url)
        source_url, source_name, remux_from = backup.source_url, "the backup", BACKUP_PROGRESS_SHARE

    try:
        source_titles = await list_source_titles(source_url, minlength_seconds=REMUX_MIN_LENGTH_SECONDS)
    except ScanError as exc:
        logger.warning("could not list the titles of %s: %s", source_url, exc)
        await fail_all(f"could not list the titles of {source_name}: {exc}")
        return
    source_id = match_titles([scan_by_index[idx] for idx in to_rip], source_titles)

    # The remux share of the bar, weighted by each title's expected size.
    weights = [scan_by_index[idx].size_bytes or kept[idx].expected_size_bytes or 1 for idx in to_rip]
    total_weight = sum(weights)
    scratch_root = output_dir / REMUX_DIRNAME
    # Titles that couldn't be remuxed; a retry would remux them from the backup.
    failed: list[int] = []
    try:
        done_weight = 0
        for n, (idx, weight) in enumerate(zip(to_rip, weights, strict=True), start=1):
            track = kept[idx]
            if idx not in source_id:
                failed.append(idx)
                await finish(idx, RipResult(ok=False, error=f"could not map title {idx} to {source_name}"))
                done_weight += weight
                continue
            started.add(idx)
            await on_track_start(track)
            logger.info("remuxing title %d (%d/%d) from %s title %d", idx, n, len(to_rip), source_name, source_id[idx])

            async def remux_progress(fraction: float, _base: int = done_weight, _weight: int = weight) -> None:
                share = (_base + fraction * _weight) / total_weight
                await report(remux_from + (1.0 - remux_from) * share)

            result = await remux_title(
                source_url,
                source_id[idx],
                scratch_dir=scratch_root / str(idx),
                output_dir=output_dir,
                output_index=idx,
                minlength_seconds=REMUX_MIN_LENGTH_SECONDS,
                on_progress=remux_progress,
            )
            if result.ok:
                result = replace(result, duration_seconds=track.duration_seconds or track.expected_duration_seconds)
            else:
                failed.append(idx)
                logger.warning("remux of title %d failed: %s", idx, result.error)
            await on_track_done(track, result)
            done_weight += weight
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)

    if from_iso:
        return
    if failed:
        logger.info("keeping the disc backup %s: titles %s failed and can be retried from it", backup_dir, failed)
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)
        logger.info("every kept title remuxed; removed the disc backup %s", backup_dir)
