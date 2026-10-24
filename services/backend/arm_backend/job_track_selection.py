"""A job's own audio/subtitle track selection (`jobs.track_selection`).

Precedence: a job's override wins over its transcode preset's track fields;
with no override the preset decides. The transcoder never sees the
difference: the Backend applies the override to the preset view it returns
at `/register` (`effective_preset_view`), which is also the moment a task is
claimed. That is why the job page can change the override only until the
first of the job's transcode tasks is claimed (`edit_lock_reason`); after
that, Apply session (re-transcode) sets it for the new tasks.

The picker's default (`default_for_job`) is the track selection of the
transcode preset the job would use: an explicit session (Apply session),
else the job's latest applied session, else the routed session
(`resolve_routed_session_id`: per-rip choice, drive default, route). When
none of those names a transcode preset, it is the built-in default (every
audio track, every subtitle as a separate file).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.auto_session import resolve_routed_session_id
from arm_common import (
    Job,
    Session,
    SessionApplication,
    Track,
    TrackKind,
    TranscodePreset,
    TranscodeTask,
    TranscodeTaskStatus,
)
from arm_common.schemas import (
    TRACK_SELECTION_FIELDS,
    DiscLanguageTracks,
    DiscTrackLanguages,
    ScanResult,
    ScanTitle,
    TrackSelectionSettings,
    TranscodePresetView,
)

BUILTIN_DEFAULT_SOURCE = "built-in default"


def job_override(job: Job) -> TrackSelectionSettings | None:
    """The job's stored override, or None (also for a row that no longer
    validates: the preset then decides rather than the transcode failing)."""
    if not job.track_selection:
        return None
    try:
        return TrackSelectionSettings.model_validate(job.track_selection)
    except ValueError:
        return None


def preset_track_selection(preset: TranscodePreset | TranscodePresetView) -> TrackSelectionSettings:
    return TrackSelectionSettings.model_validate({f: getattr(preset, f) for f in TRACK_SELECTION_FIELDS})


def effective_preset_view(preset: TranscodePresetView | None, job: Job) -> TranscodePresetView | None:
    """The preset view a transcoder gets: the job's override, when set,
    replaces the preset's track fields."""
    override = job_override(job)
    if preset is None or override is None:
        return preset
    return preset.model_copy(update=override.model_dump())


@dataclass(frozen=True)
class DefaultSelection:
    settings: TrackSelectionSettings
    source: str
    preset_id: str | None


async def _session_preset(db: AsyncSession, session_id: str | None) -> TranscodePreset | None:
    if session_id is None:
        return None
    sess = (await db.execute(select(Session).where(col(Session.id) == session_id))).scalar_one_or_none()
    if sess is None or sess.transcode_preset_id is None:
        return None
    return (
        await db.execute(select(TranscodePreset).where(col(TranscodePreset.id) == sess.transcode_preset_id))
    ).scalar_one_or_none()


async def _latest_applied_session_id(db: AsyncSession, job: Job) -> str | None:
    apps = (
        (await db.execute(select(SessionApplication).where(col(SessionApplication.job_id) == job.id))).scalars().all()
    )
    if not apps:
        return None
    return max(apps, key=lambda a: (a.created_at is not None, a.created_at)).session_id


async def default_for_job(db: AsyncSession, job: Job, session_id: str | None = None) -> DefaultSelection:
    """The track selection the job gets without an override (see module doc)."""
    if session_id is None:
        session_id = await _latest_applied_session_id(db, job)
    if session_id is None:
        session_id = await resolve_routed_session_id(db, job)
    preset = await _session_preset(db, session_id)
    if preset is None:
        return DefaultSelection(TrackSelectionSettings(), BUILTIN_DEFAULT_SOURCE, None)
    return DefaultSelection(preset_track_selection(preset), preset.name, preset.id)


_CLAIMED = (TranscodeTaskStatus.IN_PROGRESS, TranscodeTaskStatus.DONE, TranscodeTaskStatus.FAILED)


async def edit_lock_reason(db: AsyncSession, job: Job) -> str | None:
    """Why the job page can no longer change the override, or None.

    A task that has been claimed (running, finished or failed) read the
    selection at its claim; changing it now would split the job's outputs."""
    apps = [
        a.id
        for a in (await db.execute(select(SessionApplication).where(col(SessionApplication.job_id) == job.id)))
        .scalars()
        .all()
    ]
    if not apps:
        return None
    claimed = (
        (
            await db.execute(
                select(TranscodeTask)
                .where(col(TranscodeTask.session_application_id).in_(apps))
                .where(col(TranscodeTask.status).in_(_CLAIMED))
            )
        )
        .scalars()
        .first()
    )
    if claimed is None:
        return None
    return "A transcode of this job has started; use Apply session to transcode it again with other tracks."


def _kept_titles(scan: ScanResult, tracks: list[Track]) -> list[ScanTitle]:
    """The scan titles the job keeps: its video Track rows that are not
    excluded, or (before any Track row exists) every title at or above the
    scan's minimum length."""
    video = [t for t in tracks if t.kind == TrackKind.VIDEO_TITLE]
    if video:
        kept = {t.source_ref for t in video if not t.excluded}
        return [t for t in scan.titles if str(t.index) in kept]
    minimum = scan.min_length_seconds or 0
    return [t for t in scan.titles if t.duration_seconds is None or t.duration_seconds >= minimum]


def _languages(titles: list[ScanTitle], kind: str) -> list[DiscLanguageTracks]:
    found: dict[str, DiscLanguageTracks] = {}
    forced: dict[str, bool] = {}
    for title in titles:
        seen_here: set[str] = set()
        for stream in title.streams:
            if stream.kind != kind:
                continue
            entry = found.setdefault(stream.language, DiscLanguageTracks(language=stream.language))
            if entry.language_name is None:
                entry.language_name = stream.language_name
            label = stream.description or stream.codec or stream.codec_id or stream.language
            if label not in entry.tracks:
                entry.tracks.append(label)
            forced[stream.language] = forced.get(stream.language, True) and stream.forced
            seen_here.add(stream.language)
        for language in seen_here:
            found[language].title_count += 1
    for language, entry in found.items():
        entry.forced_only = forced[language]
    return list(found.values())


async def disc_languages(db: AsyncSession, job: Job) -> DiscTrackLanguages:
    """The audio and subtitle languages on the job's kept titles."""
    scan_dict = (job.metadata_json or {}).get("scan_result")
    if not scan_dict:
        return DiscTrackLanguages()
    scan = ScanResult.model_validate(scan_dict)
    tracks = list((await db.execute(select(Track).where(col(Track.job_id) == job.id))).scalars().all())
    titles = _kept_titles(scan, tracks)
    if not any(t.streams for t in scan.titles):
        return DiscTrackLanguages()
    return DiscTrackLanguages(audio=_languages(titles, "audio"), subtitles=_languages(titles, "subtitle"), known=True)
