"""Track-selection rule unit tests."""

import pytest

from arm_backend.track_selection import (
    ALL_TRACKS_MIN_SECONDS,
    MAIN_FEATURE_MIN_SECONDS,
    select_tracks,
    select_tracks_for_review,
)
from arm_common import (
    DiscType,
    IdentificationMode,
    MediaType,
    OutputMode,
    RipPreset,
    TrackKind,
    TrackSelection,
)
from arm_common.schemas import ScanResult, ScanTitle


def _preset(rule: TrackSelection) -> RipPreset:
    return RipPreset(
        id="rpr_test",
        name="test",
        media_type=MediaType.MOVIE,
        is_builtin=False,
        track_selection=rule,
        identification_mode=IdentificationMode.REQUIRED,
        output_mode=OutputMode.TRACKS,
    )


def _scan(*durations: int) -> ScanResult:
    return ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=i, duration_seconds=d) for i, d in enumerate(durations)],
    )


def test_archive_returns_every_title():
    scan = _scan(120, 90 * 60, 30, 60 * 60)
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ARCHIVE))
    assert [t.index for t in tracks] == [0, 1, 2, 3]
    assert all(t.kind == TrackKind.VIDEO_TITLE for t in tracks)


def test_all_tracks_filters_below_minimum():
    short = ALL_TRACKS_MIN_SECONDS - 1
    scan = _scan(short, ALL_TRACKS_MIN_SECONDS, 5 * 60, short)
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ALL_TRACKS))
    assert [t.index for t in tracks] == [1, 2]


def test_main_feature_returns_longest_above_threshold():
    scan = _scan(60 * 30, 90 * 60, 60 * 60, 45 * 60)
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.MAIN_FEATURE))
    assert len(tracks) == 1
    assert tracks[0].index == 1
    assert tracks[0].expected_duration_seconds == 90 * 60


def test_main_feature_falls_back_to_longest_when_no_threshold_match():
    short = MAIN_FEATURE_MIN_SECONDS - 1
    scan = _scan(short, short - 100, short - 50)
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.MAIN_FEATURE))
    assert len(tracks) == 1
    assert tracks[0].index == 0  # longest of the shorts
    assert tracks[0].expected_duration_seconds == short


def test_custom_without_filters_raises():
    from arm_backend.track_selection import TrackSelectionError

    with pytest.raises(TrackSelectionError):
        select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", _scan(60), _preset(TrackSelection.CUSTOM))


def test_empty_scan_returns_empty():
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", _scan(), _preset(TrackSelection.ARCHIVE))
    assert tracks == []


def test_all_tracks_excludes_unknown_duration():
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=0, duration_seconds=3600), ScanTitle(index=1, duration_seconds=None)],
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ALL_TRACKS))
    assert [t.index for t in tracks] == [0]


def test_main_feature_prefers_known_duration_over_unknown():
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=0, duration_seconds=None), ScanTitle(index=1, duration_seconds=90 * 60)],
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.MAIN_FEATURE))
    assert [t.index for t in tracks] == [1]


def test_main_feature_fallback_prefers_known_short_title_over_unknown():
    # Nothing reaches the main-feature length: the longest known title wins,
    # never one whose duration is unknown.
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=0, duration_seconds=None), ScanTitle(index=1, duration_seconds=0)],
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.MAIN_FEATURE))
    assert [t.index for t in tracks] == [1]


def test_main_feature_falls_back_to_first_title_when_all_durations_unknown():
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=0, duration_seconds=None), ScanTitle(index=1, duration_seconds=None)],
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.MAIN_FEATURE))
    assert [t.index for t in tracks] == [0]
    assert tracks[0].expected_duration_seconds is None


def test_review_keeps_title_with_unknown_duration_excluded():
    """An undated title still gets a Track row in review, excluded by
    default since no rule can show it meets a length threshold."""
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=0, duration_seconds=90 * 60), ScanTitle(index=1, duration_seconds=None)],
    )
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ALL_TRACKS))
    assert [(t.index, t.excluded) for t in tracks] == [(0, False), (1, True)]


def test_data_disc_emits_single_dump():
    scan = ScanResult(disc_type=DiscType.DATA, volume_label="DATA_DISC")
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ALL_TRACKS))
    assert len(tracks) == 1
    assert tracks[0].kind == TrackKind.DATA_DUMP
    assert tracks[0].source_ref == "full"


def test_cd_emits_audio_tracks():
    scan = ScanResult(
        disc_type=DiscType.CD,
        titles=[ScanTitle(index=i, duration_seconds=180) for i in range(1, 4)],
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ALL_TRACKS))
    assert [t.kind for t in tracks] == [TrackKind.AUDIO_TRACK] * 3
    assert [t.index for t in tracks] == [1, 2, 3]


# --- select_tracks_for_review (timed review gate: persist ALL, default excluded) ---


def test_review_persists_all_titles_with_main_feature_defaults():
    """MAIN_FEATURE keeps only the longest title; review persists ALL of them,
    marking the non-kept ones excluded by default."""
    scan = _scan(120, 90 * 60, 30, 60 * 60)  # indices 0..3; longest = index 1
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.MAIN_FEATURE))
    assert [t.index for t in tracks] == [0, 1, 2, 3]  # every title persisted
    kept = {t.index for t in tracks if not t.excluded}
    assert kept == {1}  # only the longest is kept by default
    assert all(t.excluded for t in tracks if t.index != 1)


def test_review_archive_keeps_all_unexcluded():
    """ARCHIVE keeps every title -> none excluded by default."""
    scan = _scan(120, 5400, 30)
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ARCHIVE))
    assert len(tracks) == 3
    assert all(not t.excluded for t in tracks)


def test_review_all_tracks_excludes_below_minlength():
    """ALL_TRACKS drops titles under the min-length -> those persist excluded."""
    short = ALL_TRACKS_MIN_SECONDS - 1
    scan = _scan(short, 3600, short)
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ALL_TRACKS))
    assert len(tracks) == 3  # all persisted
    kept = {t.index for t in tracks if not t.excluded}
    assert kept == {1}  # only the above-threshold one kept by default


def test_review_data_disc_delegates_to_single_dump():
    scan = ScanResult(disc_type=DiscType.DATA, volume_label="DATA_DISC")
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ALL_TRACKS))
    assert len(tracks) == 1
    assert tracks[0].kind == TrackKind.DATA_DUMP


# --- ISO output mode: a full-disc dump whatever the disc holds ---


def _iso_preset() -> RipPreset:
    return RipPreset(
        id="rpr_builtin_iso_dump",
        name="ISO: Full-disc dump",
        media_type=MediaType.ISO,
        is_builtin=True,
        track_selection=TrackSelection.ALL_TRACKS,
        identification_mode=IdentificationMode.SKIP,
        output_mode=OutputMode.ISO,
    )


@pytest.mark.parametrize("disc_type", [DiscType.BLURAY, DiscType.DVD])
def test_iso_preset_dumps_a_video_disc_that_has_titles(disc_type: DiscType):
    """The ISO preset copies the disc whole. Titles in the scan must not turn
    it into a MakeMKV rip of those titles."""
    scan = ScanResult(
        disc_type=disc_type,
        titles=[ScanTitle(index=i, duration_seconds=d) for i, d in enumerate([7200, 1800, 300])],
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _iso_preset())
    assert [(t.kind, t.source_ref) for t in tracks] == [(TrackKind.DATA_DUMP, "full")]


def test_iso_preset_dumps_a_video_disc_without_titles():
    scan = ScanResult(disc_type=DiscType.BLURAY)
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _iso_preset())
    assert [t.kind for t in tracks] == [TrackKind.DATA_DUMP]


def test_review_iso_preset_persists_the_single_dump():
    """The review gate shows the one dump, not every title marked excluded."""
    scan = _scan(7200, 1800)
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _iso_preset())
    assert [(t.kind, t.excluded) for t in tracks] == [(TrackKind.DATA_DUMP, False)]


# --- FOLDER output mode: one decrypted-backup track, Blu-ray only ---


def _backup_preset() -> RipPreset:
    return RipPreset(
        id="rpr_builtin_bd_backup",
        name="Blu-ray: Decrypted backup",
        media_type=MediaType.ISO,
        is_builtin=True,
        track_selection=TrackSelection.ALL_TRACKS,
        identification_mode=IdentificationMode.SKIP,
        output_mode=OutputMode.FOLDER,
    )


def test_folder_preset_backs_up_a_bluray_with_titles():
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=i, duration_seconds=d) for i, d in enumerate([7200, 1800])],
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _backup_preset())
    assert [(t.kind, t.index, t.source_ref) for t in tracks] == [(TrackKind.DISC_BACKUP, 0, "full")]


def test_folder_preset_backs_up_a_bluray_without_titles():
    """A backup copies the disc structure; it does not need MakeMKV's title list."""
    scan = ScanResult(disc_type=DiscType.BLURAY)
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _backup_preset())
    assert [t.kind for t in tracks] == [TrackKind.DISC_BACKUP]


@pytest.mark.parametrize("disc_type", [DiscType.DVD, DiscType.DATA, DiscType.CD])
def test_folder_preset_refuses_anything_but_a_bluray(disc_type: DiscType):
    from arm_backend.track_selection import DiscBackupNeedsBluray

    scan = ScanResult(disc_type=disc_type, titles=[ScanTitle(index=1, duration_seconds=3600)])
    with pytest.raises(DiscBackupNeedsBluray, match=f"disc backup needs a Blu-ray, not disc_type={disc_type.value}"):
        select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _backup_preset())


def test_review_folder_preset_persists_the_single_backup():
    scan = ScanResult(disc_type=DiscType.BLURAY, titles=[ScanTitle(index=0, duration_seconds=7200)])
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _backup_preset())
    assert [(t.kind, t.excluded) for t in tracks] == [(TrackKind.DISC_BACKUP, False)]


# --- scans that list every title (min_length_seconds set by the ripper) ---

# The Sintel DVD at MakeMKV --minlength=0: 14:58, 14:57, 1:59, 6:05, 3:36, 1:47, 58:24.
_SINTEL_DURATIONS = (898, 897, 119, 365, 216, 107, 3504)


def _full_scan(min_length_seconds: int | None) -> ScanResult:
    return _scan(*_SINTEL_DURATIONS).model_copy(update={"min_length_seconds": min_length_seconds})


def test_archive_skips_titles_below_the_scan_minimum():
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", _full_scan(120), _preset(TrackSelection.ARCHIVE))
    assert [t.index for t in tracks] == [0, 1, 3, 4, 6]


def test_review_has_no_rows_for_titles_below_the_scan_minimum():
    tracks = select_tracks_for_review(
        "job_01JZXR7K3M5Q8N4VWA00000001", _full_scan(120), _preset(TrackSelection.ARCHIVE)
    )
    assert [t.index for t in tracks] == [0, 1, 3, 4, 6]
    assert not any(t.excluded for t in tracks)


@pytest.mark.parametrize("rule", [TrackSelection.ALL_TRACKS, TrackSelection.MAIN_FEATURE])
def test_presets_choose_among_titles_at_or_above_the_scan_minimum(rule):
    review = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", _full_scan(600), _preset(rule))
    assert [t.index for t in review] == [0, 1, 6]


def test_scan_minimum_keeps_titles_of_unknown_duration():
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=0, duration_seconds=10), ScanTitle(index=1, duration_seconds=None)],
        min_length_seconds=120,
    )
    tracks = select_tracks("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ARCHIVE))
    assert [t.index for t in tracks] == [1]


def test_scan_without_a_minimum_is_used_as_is():
    """Scans stored before the scan listed every title carry no minimum:
    every title they hold still gets a row."""
    tracks = select_tracks_for_review(
        "job_01JZXR7K3M5Q8N4VWA00000001", _full_scan(None), _preset(TrackSelection.ARCHIVE)
    )
    assert [t.index for t in tracks] == [0, 1, 2, 3, 4, 5, 6]


def test_cd_tracks_ignore_a_minimum():
    scan = ScanResult(
        disc_type=DiscType.CD,
        titles=[ScanTitle(index=i, duration_seconds=d) for i, d in ((1, 30), (2, 200))],
        min_length_seconds=120,
    )
    tracks = select_tracks_for_review("job_01JZXR7K3M5Q8N4VWA00000001", scan, _preset(TrackSelection.ARCHIVE))
    assert [t.index for t in tracks] == [1, 2]
