"""The review gate's play-all detector (track-select plan S12)."""

from arm_backend.identity.resolver import Resolution, Winner, apply_resolution
from arm_backend.track_selection import PLAY_ALL_DETECTOR, play_all_indexes, select_tracks_for_review
from arm_common import (
    DiscType,
    IdentificationMode,
    Job,
    JobStatus,
    MediaType,
    OutputMode,
    RipPreset,
    TrackSelection,
)
from arm_common.enums import TrackRole
from arm_common.schemas import ScanResult, ScanTitle

_JOB = "job_01JZXR7K3M5Q8N4VWA00000001"


def _preset() -> RipPreset:
    return RipPreset(
        id="rpr_test",
        name="test",
        media_type=MediaType.TV,
        is_builtin=False,
        track_selection=TrackSelection.ARCHIVE,
        identification_mode=IdentificationMode.REQUIRED,
        output_mode=OutputMode.TRACKS,
    )


def _titles(*durations: int | None) -> list[ScanTitle]:
    return [ScanTitle(index=i, duration_seconds=d) for i, d in enumerate(durations)]


def _hanako() -> list[ScanTitle]:
    """The recorded Hanako-kun D1 scan (`makemkvcon_bd_hanako_info.txt`):
    7 episodes on clips 14-20 and a play-all of all of them plus 24, 25."""
    episodes = [
        ScanTitle(
            index=i,
            duration_seconds=1446 if i == 2 else 1447,
            segment_map=str(14 + i),
            source_playlist=f"{9 + i:05d}.mpls",
        )
        for i in range(7)
    ]
    play_all = ScanTitle(
        index=7, duration_seconds=10139, segment_map="14,15,16,17,18,19,20,24,25", source_playlist="00008.mpls"
    )
    return [*episodes, play_all]


ACCA = _titles(*[1440] * 9, 9 * 1440)  # 9 episodes, then their play-all


def test_a_dvd_title_as_long_as_the_episodes_together_is_a_play_all():
    assert play_all_indexes(DiscType.DVD, ACCA) == {9}


def test_short_extras_dont_hide_it():
    extras = [ScanTitle(index=10 + i, duration_seconds=180) for i in range(3)]
    assert play_all_indexes(DiscType.DVD, [*ACCA, *extras]) == {9}


def test_a_movie_with_short_extras_has_none():
    assert play_all_indexes(DiscType.DVD, _titles(7200, 300, 180, 120)) == set()


def test_unknown_lengths_never_match():
    assert play_all_indexes(DiscType.DVD, _titles(1440, 1440, None)) == set()
    assert play_all_indexes(DiscType.DVD, _titles(1440, 1440, 1440, None, 4320)) == {4}


def test_two_titles_are_never_a_play_all_and_its_part():
    assert play_all_indexes(DiscType.DVD, _titles(1440, 1440)) == set()


def test_the_recorded_blu_ray_is_judged_by_its_clip_lists():
    assert play_all_indexes(DiscType.BLURAY, _hanako()) == {7}


def test_on_a_blu_ray_with_clip_lists_length_alone_is_not_enough():
    titles = [
        ScanTitle(index=0, duration_seconds=1440, segment_map="1"),
        ScanTitle(index=1, duration_seconds=1440, segment_map="2"),
        ScanTitle(index=2, duration_seconds=2880, segment_map="3"),  # same length, other clips
    ]
    assert play_all_indexes(DiscType.BLURAY, titles) == set()


def test_parts_must_cover_most_of_the_play_all():
    titles = _hanako()
    titles[7] = titles[7].model_copy(update={"duration_seconds": 20000})  # clips match, length doesn't
    assert play_all_indexes(DiscType.BLURAY, titles) == set()


def test_a_dvd_cell_map_is_no_clip_list():
    titles = [t.model_copy(update={"segment_map": f"{i * 6 + 1}-{i * 6 + 6}"}) for i, t in enumerate(ACCA)]
    assert play_all_indexes(DiscType.DVD, titles) == {9}


def test_review_leaves_the_play_all_out_and_says_why():
    tracks = select_tracks_for_review(_JOB, ScanResult(disc_type=DiscType.BLURAY, titles=_hanako()), _preset())

    by_index = {t.index: t for t in tracks}
    assert by_index[7].excluded is True
    assert by_index[7].role == TrackRole.PLAY_ALL
    assert by_index[7].identity_provenance == {"role": PLAY_ALL_DETECTOR}
    assert all(not by_index[i].excluded and by_index[i].role is None for i in range(7))


def test_a_cd_is_never_checked():
    scan = ScanResult(disc_type=DiscType.CD, titles=_titles(200, 200, 400))
    assert all(not t.excluded and t.role is None for t in select_tracks_for_review(_JOB, scan, _preset()))


def test_a_re_resolve_keeps_the_detectors_role_until_a_source_names_one():
    tracks = select_tracks_for_review(_JOB, ScanResult(disc_type=DiscType.BLURAY, titles=_hanako()), _preset())
    job = Job(id=_JOB, drive_id="drv_1", disc_type=DiscType.BLURAY, status=JobStatus.AWAITING_REVIEW)
    play_all = tracks[7]

    apply_resolution(job, tracks, Resolution(known_sources=frozenset({"preset", "thediscdb"})))
    assert play_all.role == TrackRole.PLAY_ALL

    named = Resolution(
        tracks={play_all.source_ref: {"role": Winner(TrackRole.EXTRA, "thediscdb")}},
        known_sources=frozenset({"preset", "thediscdb"}),
    )
    apply_resolution(job, tracks, named)
    assert play_all.role == TrackRole.EXTRA
    assert play_all.identity_provenance == {"role": "thediscdb"}


def test_blu_ray_titles_with_no_clip_list_or_length_are_neither_play_all_nor_part():
    titles = [
        *_hanako(),
        ScanTitle(index=8, duration_seconds=10139, segment_map=None),  # no clip list
        ScanTitle(index=9, duration_seconds=None, segment_map="14,15,16,17,18,19,20,24,25,26"),
        ScanTitle(index=10, duration_seconds=600, segment_map="1-3"),  # not a clip list
        ScanTitle(index=11, duration_seconds=900, segment_map="99"),  # in no play-all
    ]
    assert play_all_indexes(DiscType.BLURAY, titles) == {7}


def test_a_duplicate_part_is_counted_once():
    """Two playlists of the same episode clip overlap in the play-all: one
    counts, so two episodes can't pass for three."""
    titles = [
        ScanTitle(index=0, duration_seconds=1440, segment_map="1"),
        ScanTitle(index=1, duration_seconds=1440, segment_map="1"),
        ScanTitle(index=2, duration_seconds=1440, segment_map="2"),
        ScanTitle(index=3, duration_seconds=4320, segment_map="1,2"),
    ]
    assert play_all_indexes(DiscType.BLURAY, titles) == set()
