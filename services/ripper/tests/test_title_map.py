"""Matching disc-scan titles to a backup's titles (track-select S7.2), on the
recordings. Whether a 1.18.4 decrypted Blu-ray backup keeps the disc's ids is
unknown (S17 17.4), so both cases are covered: ids fully permuted (the
2.0.0 recording) and ids identical."""

from __future__ import annotations

import pytest

from arm_common.schemas import ScanTitle

from arm_ripper.rip.title_map import match_titles
from arm_ripper.scan.makemkv import parse_makemkvcon_info
from tests._makemkvcon_fake import FIXTURES


def _titles(name: str) -> list[ScanTitle]:
    _, titles, _ = parse_makemkvcon_info((FIXTURES / name).read_text().splitlines())
    return titles


def _assert_twins(matched: dict[int, int], disc: list[ScanTitle], source: list[ScanTitle]) -> None:
    by_index = {t.index: t for t in source}
    for disc_title in disc:
        twin = by_index[matched[disc_title.index]]
        assert (twin.source_playlist, twin.original_title_id, twin.duration_seconds, twin.size_bytes) == (
            disc_title.source_playlist,
            disc_title.original_title_id,
            disc_title.duration_seconds,
            disc_title.size_bytes,
        )


def test_blu_ray_titles_match_their_backup_titles_by_playlist_when_every_id_differs() -> None:
    """Recorded on the Hanako-kun BD at the default minimum: every id differs
    between the disc and its backup (the play-all is disc 7, backup 0)."""
    disc = _titles("makemkvcon_bd_hanako_info.txt")
    backup = _titles("makemkvcon_bd_hanako_info_file_backup.txt")

    matched = match_titles(disc, backup)

    assert matched == {0: 6, 1: 7, 2: 5, 3: 4, 4: 1, 5: 2, 6: 3, 7: 0}
    _assert_twins(matched, disc, backup)


@pytest.mark.parametrize(
    "disc_fixture",
    ["makemkvcon_bd_hanako_info_minlength0.txt", "makemkvcon_dvd_acca_info_minlength0.txt"],
    ids=["blu-ray", "dvd"],
)
def test_titles_match_a_source_with_the_same_ids_one_to_one(disc_fixture: str) -> None:
    """A backup that keeps the disc's numbering (as the ACCA DVD image does,
    and a 1.18.4 Blu-ray backup may) maps every title to its own id."""
    disc = _titles(disc_fixture)

    assert match_titles(disc, disc) == {t.index: t.index for t in disc}


def test_every_blu_ray_title_at_minimum_zero_finds_its_permuted_backup_title() -> None:
    """At minlength 0 (how the selection path lists a backup) the 39 titles,
    clips included, each find their own backup title."""
    disc = _titles("makemkvcon_bd_hanako_info_minlength0.txt")
    backup = _titles("makemkvcon_bd_hanako_info_file_backup_minlength0.txt")

    matched = match_titles(disc, backup)

    assert sorted(matched) == [t.index for t in disc]
    assert matched[7] == 0
    _assert_twins(matched, disc, backup)


def test_dvd_titles_match_their_iso_image_backup_by_title_number() -> None:
    disc = _titles("makemkvcon_dvd_acca_info_minlength0.txt")
    backup = _titles("makemkvcon_dvd_acca_info_iso_backup_minlength0_noscan_tray_open.txt")

    assert match_titles(disc, backup) == {0: 0, 1: 1, 2: 2, 3: 3}


def test_a_title_whose_key_is_missing_from_the_backup_is_unmatched() -> None:
    disc = [ScanTitle(index=0, duration_seconds=1447, source_playlist="00009.mpls")]
    backup = [ScanTitle(index=0, duration_seconds=1447, source_playlist="00010.mpls")]

    assert match_titles(disc, backup) == {}


def test_the_same_key_with_a_different_duration_is_unmatched() -> None:
    disc = [ScanTitle(index=0, duration_seconds=1447, original_title_id=4)]
    backup = [ScanTitle(index=2, duration_seconds=1452, original_title_id=4)]

    assert match_titles(disc, backup) == {}


def test_a_key_shared_by_two_source_titles_is_ambiguous_whatever_the_chapters() -> None:
    """Chapters and size only break ties for titles with no key."""
    disc = [ScanTitle(index=0, duration_seconds=1447, chapter_count=6, original_title_id=4)]
    backup = [
        ScanTitle(index=0, duration_seconds=1447, chapter_count=6, original_title_id=4),
        ScanTitle(index=1, duration_seconds=1447, chapter_count=5, original_title_id=4),
    ]

    assert match_titles(disc, backup) == {}


def test_titles_without_keys_fall_back_to_duration_chapters_and_size() -> None:
    """Two 24-minute episodes, told apart by chapter count, then by size; a
    third pair identical in every field stays unmatched rather than guessed."""
    disc = [
        ScanTitle(index=0, duration_seconds=1447, chapter_count=6, size_bytes=10),
        ScanTitle(index=1, duration_seconds=1446, chapter_count=5, size_bytes=20),
        ScanTitle(index=2, duration_seconds=1447, chapter_count=5, size_bytes=30),
        ScanTitle(index=3, duration_seconds=600, chapter_count=1, size_bytes=5),
    ]
    backup = [
        ScanTitle(index=0, duration_seconds=1447, chapter_count=5, size_bytes=30),
        ScanTitle(index=1, duration_seconds=1447, chapter_count=5, size_bytes=20),
        ScanTitle(index=2, duration_seconds=1448, chapter_count=6, size_bytes=10),
        ScanTitle(index=3, duration_seconds=600, chapter_count=1, size_bytes=5),
        ScanTitle(index=4, duration_seconds=600, chapter_count=1, size_bytes=5),
    ]

    assert match_titles(disc, backup) == {0: 2, 1: 1, 2: 0}


def test_a_keyless_title_cannot_take_a_keyed_titles_twin() -> None:
    """Keyed titles are matched first, whatever order they are asked in, so
    the keyless one gets the title left over instead of two candidates."""
    disc = [
        ScanTitle(index=0, duration_seconds=1447),
        ScanTitle(index=1, duration_seconds=1447, source_playlist="00009.mpls"),
    ]
    backup = [
        ScanTitle(index=5, duration_seconds=1447, source_playlist="00009.mpls"),
        ScanTitle(index=6, duration_seconds=1447, source_playlist="00010.mpls"),
    ]

    assert match_titles(disc, backup) == {1: 5, 0: 6}
