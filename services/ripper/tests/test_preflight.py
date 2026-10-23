"""`estimate_backup_bytes`: what a disc backup writes, from the stored scan (S8)."""

from __future__ import annotations

from arm_common import DiscType
from arm_common.schemas import ScanTitle

from arm_ripper.rip.preflight import DVD_MAX_BYTES, estimate_backup_bytes

_GB = 1_000_000_000


def _title(index: int, size: int | None, segment_map: str | None = None) -> ScanTitle:
    return ScanTitle(index=index, duration_seconds=1800, size_bytes=size, segment_map=segment_map)


def test_a_dvd_sums_its_titles_capped_at_a_dual_layer_disc():
    assert estimate_backup_bytes([_title(0, 2 * _GB), _title(1, 3 * _GB)], DiscType.DVD) == 5 * _GB
    assert estimate_backup_bytes([_title(i, 4 * _GB) for i in range(4)], DiscType.DVD) == DVD_MAX_BYTES


def test_a_dvd_title_larger_than_the_cap_is_never_estimated_below_itself():
    assert estimate_backup_bytes([_title(0, 9 * _GB)], DiscType.DVD) == 9 * _GB


def test_a_blu_ray_title_whose_clips_are_counted_adds_nothing():
    titles = [_title(0, 10 * _GB, "1,2"), _title(1, 5 * _GB, "1"), _title(2, 5 * _GB, "2"), _title(3, _GB, "3")]
    assert estimate_backup_bytes(titles, DiscType.BLURAY) == 11 * _GB


def test_a_blu_ray_title_with_no_clip_list_is_counted():
    titles = [_title(0, 10 * _GB, "1,2"), _title(1, 5 * _GB, None), _title(2, 5 * _GB, "x")]
    assert estimate_backup_bytes(titles, DiscType.BLURAY) == 20 * _GB


def test_no_sizes_estimates_nothing():
    assert estimate_backup_bytes([_title(0, None)], DiscType.BLURAY) == 0
