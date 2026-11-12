"""The menu extract (fork only, design v5 A2): the real copy on discs built at test time, and what the menu row
becomes. No disc data is committed."""

from __future__ import annotations

from pathlib import Path

import pytest
from arm_common.enums import TrackStatus
from arm_common.fileops import tree_size
from arm_ripper.menus import extract

from tests import _disc_menu_builders as b
from tests.test_disc_menu_copy import _bd, _dvd, _iso


async def test_an_hdmv_menu_is_done_with_its_folder_and_size(tmp_path: Path) -> None:
    dest = tmp_path / "job" / "menu"
    outcome = await extract.extract_menu(_bd(tmp_path / "bd"), dest)
    assert outcome.status == TrackStatus.DONE and outcome.error is None
    assert outcome.output_path == dest and outcome.size_bytes == tree_size(dest) > 0
    assert (dest / "BDMV" / "index.bdmv").is_file() and (dest / "BDMV" / "STREAM" / "00046.m2ts").is_file()


def test_a_dvd_menu_is_done_from_a_folder_and_from_an_image(tmp_path: Path) -> None:
    folder = _dvd(tmp_path / "dvd")
    assert extract.extract_menu_sync(folder, tmp_path / "a").status == TrackStatus.DONE
    image = _iso(tmp_path / "dvd.iso", {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*.*")})
    outcome = extract.extract_menu_sync(image, tmp_path / "b")
    assert outcome.status == TrackStatus.DONE, outcome.error
    assert (tmp_path / "b" / "VIDEO_TS" / "VIDEO_TS.IFO").is_file()


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (lambda root: _bd(root, top=("bdj", "00000")), extract.JAVA_MENU),
        (lambda root: _bd(root, ig=False), extract.NO_MENU),
        (lambda root: _dvd(root, menus=False), extract.NO_MENU),
        (lambda root: b.write_files(root, {"README.txt": b"hi"}), extract.NO_MENU_FILES),
    ],
    ids=["bdj", "bd-no-ig", "dvd-no-menus", "no-disc"],
)
def test_nothing_to_play_is_skipped_and_leaves_no_folder(tmp_path: Path, make, reason: str) -> None:
    dest = tmp_path / "menu"
    dest.mkdir()
    (dest / "stale").write_bytes(b"from an earlier copy")
    assert extract.extract_menu_sync(make(tmp_path / "src"), dest) == extract.skipped(reason)
    assert not dest.exists()


def test_a_lost_menu_clip_fails_the_menu(tmp_path: Path) -> None:
    dest = tmp_path / "menu"
    outcome = extract.extract_menu_sync(_bd(tmp_path / "bd"), dest, lost=["/BDMV/STREAM/00046.m2ts"])
    assert outcome == extract.failed("menu files lost or unreadable: BDMV/STREAM/00046.m2ts")
    assert not dest.exists()


def test_a_lost_file_the_copy_doesnt_take_costs_the_menu_nothing(tmp_path: Path) -> None:
    outcome = extract.extract_menu_sync(_bd(tmp_path / "bd"), tmp_path / "menu", lost=["/BDMV/JAR/00000/big.jar"])
    assert outcome.status == TrackStatus.DONE


def test_an_unreadable_source_fails(tmp_path: Path) -> None:
    outcome = extract.extract_menu_sync(tmp_path / "nothing.iso", tmp_path / "menu")
    assert outcome.status == TrackStatus.FAILED and outcome.error is not None
    assert outcome.error.startswith("could not read the disc's files")


def test_a_copy_that_raises_fails_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kw: object) -> None:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(extract.menu_copy, "copy_menu", boom)
    dest = tmp_path / "menu"
    assert extract.extract_menu_sync(_bd(tmp_path / "bd"), dest) == extract.failed("menu copy failed: parser bug")
    assert not dest.exists()


def test_a_copied_menu_that_cant_be_read_fails(tmp_path: Path) -> None:
    root = _bd(tmp_path / "bd")
    (root / "BDMV" / "index.bdmv").write_bytes(b"INDX0200 broken")
    outcome = extract.extract_menu_sync(root, tmp_path / "menu")
    assert outcome.status == TrackStatus.FAILED


# ------------------------------------------------------------------------------------------ encryption


def _clip(scrambled: bool) -> bytes:
    unit = bytearray(b.ts_packets(0x1011, b.pes(bytes(32 * 184), stream_id=0xE0))[: 32 * 192])
    if scrambled:
        unit[16:] = bytes((x ^ 0x5A) for x in unit[16:])
    return bytes(unit)


@pytest.mark.parametrize("scrambled", [False, True])
def test_an_encrypted_blu_ray_is_told_by_its_clips(tmp_path: Path, scrambled: bool) -> None:
    root = b.write_files(
        tmp_path / "bd",
        {"BDMV/index.bdmv": b.index_bdmv(), "BDMV/STREAM/00001.m2ts": _clip(scrambled), "BDMV/STREAM/00000.m2ts": b"x"},
    )
    assert extract.source_encrypted(root) is scrambled


@pytest.mark.parametrize("scrambled", [False, True])
def test_an_encrypted_dvd_is_told_by_its_vobs(tmp_path: Path, scrambled: bool) -> None:
    vob = b.nav_pack() + b.audio_pack() + b.video_pack(scrambled=scrambled)
    files = {"VIDEO_TS/VIDEO_TS.IFO": b.vmg_ifo(nvts=0), "VIDEO_TS/VTS_01_1.VOB": vob, "VIDEO_TS/VTS_01_0.IFO": b""}
    root = b.write_files(tmp_path / "dvd", files)
    assert extract.source_encrypted(root) is scrambled
    image = _iso(tmp_path / "dvd.iso", files)
    assert extract.source_encrypted(image) is scrambled


def test_no_disc_or_no_source_is_not_encrypted(tmp_path: Path) -> None:
    assert extract.source_encrypted(tmp_path / "missing") is False
    assert extract.source_encrypted(b.write_files(tmp_path / "x", {"a.txt": b"a"})) is False
    assert extract.source_encrypted(b.write_files(tmp_path / "bd", {"BDMV/index.bdmv": b""})) is False
