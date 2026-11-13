"""The `menu_pack` task's build (`arm_transcode.menus.task`, fork only, disc menus A3): the backend's plan as a media
map against the menu copy, one job per pack folder, and the build-then-swap. Menu copies and encodes are built at
test time; ffmpeg is faked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arm_common.schemas import MenuPackItem, MenuPackPlan
from arm_transcode.menus import task as pack_task
from arm_transcode.menus.model import PACK_FILE

from tests import _disc_menu_builders as b
from tests.test_disc_menu_packs import SEC, FakeTools, _bd_copy, _dvd_copy

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
SHOW = "Show (2020)"
PACK = f"{SHOW}/.discmenus/bd-d1"


def _media(root: Path) -> Path:
    return b.write_files(root, {
        f"{SHOW}/Season 01/Show - S01E01.mkv": b"mkv",
        f"{SHOW}/Season 01/Show - S01E02.mkv": b"mkv",
        f"{SHOW}/trailers/Trailer.mkv": b"mkv",
    })  # fmt: skip


def _bd_plan(**kw: object) -> MenuPackPlan:
    fields: dict[str, object] = dict(
        pack_dir=PACK,
        name="Show, Disc 1",
        season=1,
        job_id=JOB,
        items=[
            MenuPackItem(file=f"{SHOW}/Season 01/Show - S01E01.mkv", episode=1, season=1, bd_clip="00014"),
            MenuPackItem(file=f"{SHOW}/Season 01/Show - S01E02.mkv", episode=2, bd_clip="00015"),
            MenuPackItem(file=f"{SHOW}/trailers/Trailer.mkv", extra="trailers/Trailer.mkv", bd_playlist="00003"),
            MenuPackItem(file=f"{SHOW}/extras/Gone.mkv", extra="extras/Gone.mkv", bd_playlist="00077"),
        ],
        notes=["title 9 (x.mkv): left out, a title with role none"],
    )
    fields.update(kw)
    return MenuPackPlan(**fields)  # type: ignore[arg-type]


def _bd(tmp_path: Path) -> tuple[Path, Path]:
    menu = _bd_copy(tmp_path / "menu")
    # The same play items as 00003 under another number: MakeMKV lists only one of the two.
    (menu / "BDMV/PLAYLIST/00013.mpls").write_bytes(b.mpls([("00020", 0, 30 * SEC), ("00021", 0, 5 * SEC)]))
    return menu, _media(tmp_path / "media")


def _pack(media: Path, folder: str = PACK) -> dict[str, object]:
    data: dict[str, object] = json.loads((media / folder / PACK_FILE).read_text())
    return data


def test_a_blu_ray_pack_from_the_plan(tmp_path: Path) -> None:
    menu, media = _bd(tmp_path)
    steps: list[tuple[int, str]] = []
    result = pack_task.build_pack(_bd_plan(), menu, media, FakeTools(), lambda pct, what: steps.append((pct, what)))
    assert result.folder == media / PACK
    pack = _pack(media)
    assert pack["disc"]["job"] == JOB and pack["disc"]["name"] == "Show, Disc 1"  # type: ignore[index]
    cells = pack["media"]  # type: ignore[index]
    assert cells["00007"]["cells"]["0"]["episode"] == 1  # type: ignore[index]
    assert cells["00002"]["cells"]["0"]["file"] == "Show - S01E01.mkv"  # type: ignore[index]
    # The merged twin of the trailer's playlist plays it too.
    for name in ("00003", "00013"):
        assert cells[name]["cells"]["0"]["extra"] == "trailers/Trailer.mkv"  # type: ignore[index]
    assert result.report.notes[0].startswith("title 9")
    assert "playlist 00077 isn't in the menu copy" in result.report.notes
    assert steps[0] == (2, "reading the menu") and steps[-1] == (95, "swapping the pack in")
    assert sorted(p.name for p in (media / PACK).parent.iterdir()) == ["bd-d1"]


def test_a_rebuild_swaps_the_old_pack_out(tmp_path: Path) -> None:
    menu, media = _bd(tmp_path)
    pack_task.build_pack(_bd_plan(), menu, media, None)
    (media / PACK / "stale.png").write_bytes(b"old")
    (media / PACK).with_name(".bd-d1.building").mkdir()  # a crashed build's leftovers
    (media / PACK).with_name(".bd-d1.old").mkdir()
    pack_task.build_pack(_bd_plan(), menu, media, None)
    assert not (media / PACK / "stale.png").exists()
    assert sorted(p.name for p in (media / PACK).parent.iterdir()) == ["bd-d1"]


def test_a_failed_build_keeps_the_old_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    menu, media = _bd(tmp_path)
    pack_task.build_pack(_bd_plan(), menu, media, None)

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(pack_task, "build_hdmv_pack", boom)
    with pytest.raises(RuntimeError, match="ffmpeg died"):
        pack_task.build_pack(_bd_plan(), menu, media, None)
    assert _pack(media)["disc"]["job"] == JOB  # type: ignore[index]
    assert sorted(p.name for p in (media / PACK).parent.iterdir()) == ["bd-d1"]


def test_another_jobs_pack_is_left_alone(tmp_path: Path) -> None:
    menu, media = _bd(tmp_path)
    b.write_files(media, {
        f"{PACK}/{PACK_FILE}": json.dumps({"disc": {"job": "job_other"}}).encode(),
        f"{PACK}-2/{PACK_FILE}": b"{}",  # by hand: no job
    })  # fmt: skip
    result = pack_task.build_pack(_bd_plan(), menu, media, None)
    assert result.folder.name == "bd-d1-3"
    assert pack_task.pack_owner(media / PACK) == "job_other"
    # The job's own folder is found again, wherever it is.
    assert pack_task.build_pack(_bd_plan(), menu, media, None).folder.name == "bd-d1-3"


def test_choose_folder_gives_up_after_nine(tmp_path: Path) -> None:
    target = tmp_path / "bd-d1"
    for n in ["", *(f"-{i}" for i in range(2, 10))]:
        b.write_files(tmp_path, {f"bd-d1{n}/{PACK_FILE}": b'{"disc": "x"}'})
    with pytest.raises(pack_task.PackFailed, match="belong to other packs"):
        pack_task.choose_folder(target, JOB)
    assert pack_task.pack_owner(tmp_path / "nowhere") is None
    assert pack_task.pack_owner(target) is None


def test_moved_encodes_fail_the_build(tmp_path: Path) -> None:
    menu, _ = _bd(tmp_path)
    with pytest.raises(pack_task.PackFailed, match="build the pack before moving them"):
        pack_task.build_pack(_bd_plan(), menu, tmp_path / "empty", None)


def test_a_pack_that_plays_nothing_fails(tmp_path: Path) -> None:
    menu, media = _bd(tmp_path)
    plan = _bd_plan(items=[MenuPackItem(file=f"{SHOW}/Season 01/Show - S01E01.mkv", episode=1, bd_clip="00777")])
    with pytest.raises(pack_task.PackFailed, match="no menu button plays"):
        pack_task.build_pack(plan, menu, media, None)
    assert not (media / PACK).parent.exists() or not any((media / PACK).parent.iterdir())


def test_a_menu_copy_with_neither_kind(tmp_path: Path) -> None:
    _, media = _bd(tmp_path)
    (tmp_path / "bare").mkdir()
    with pytest.raises(pack_task.PackFailed, match="neither BDMV/ nor VIDEO_TS/"):
        pack_task.build_pack(_bd_plan(), tmp_path / "bare", media, None)


def test_a_blu_ray_with_no_playlist_folder_maps_clips_only(tmp_path: Path) -> None:
    menu, _ = _bd(tmp_path)
    plan = _bd_plan(items=[MenuPackItem(file="x.mkv", extra="trailers/x.mkv", bd_playlist="00003")])
    from arm_transcode.menus.model import BuildReport

    (tmp_path / "nopl" / "BDMV").mkdir(parents=True)
    report = BuildReport("")
    media = pack_task._bd_map(tmp_path / "nopl" / "BDMV", plan, tmp_path, report)
    assert list(media.playlists) == ["00003"]
    assert report.notes == ["playlist 00003 isn't in the menu copy"]
    # An unreadable playlist is the builder's to note.
    (menu / "BDMV/PLAYLIST/00010.mpls").write_bytes(b"junk")
    assert "00003" in pack_task._bd_map(menu / "BDMV", plan, tmp_path, BuildReport("")).playlists


# ------------------------------------------------------------------------------------------ DVD


def _dvd_plan(*titles: int) -> MenuPackPlan:
    return MenuPackPlan(
        pack_dir=f"{SHOW}/.discmenus/dvd-d1",
        name="Show, Disc 1",
        season=1,
        job_id=JOB,
        items=[MenuPackItem(file=f"{SHOW}/Season 01/Show - S01E01.mkv", episode=1, dvd_title=n) for n in titles]
        + [MenuPackItem(file=f"{SHOW}/Season 01/Show - S01E02.mkv", episode=2)],
    )


def test_a_dvd_pack_maps_a_title_to_its_whole_pgc(tmp_path: Path) -> None:
    menu = _dvd_copy(tmp_path / "menu")
    media = _media(tmp_path / "media")
    result = pack_task.build_pack(_dvd_plan(1, 5), menu, media, FakeTools())
    cells = _pack(media, f"{SHOW}/.discmenus/dvd-d1")["media"]["1:1"]["cells"]  # type: ignore[index]
    assert sorted(cells) == ["1", "2", "3", "4"]
    assert {c["episode"] for c in cells.values()} == {1}
    assert "DVD title 5 isn't in the disc's title table" in result.report.notes


def test_a_dvd_title_whose_ifo_is_missing_or_unreadable(tmp_path: Path) -> None:
    menu = _dvd_copy(tmp_path / "menu")
    media = _media(tmp_path / "media")
    (menu / "VIDEO_TS/VTS_01_0.IFO").write_bytes(b"junk")
    with pytest.raises(pack_task.PackFailed, match="no menu button plays"):
        pack_task.build_pack(_dvd_plan(1, 1), menu, media, None)
    (menu / "VIDEO_TS/VIDEO_TS.IFO").unlink()
    with pytest.raises(pack_task.PackFailed, match="VIDEO_TS.IFO unreadable"):
        pack_task.build_pack(_dvd_plan(1), menu, media, None)
