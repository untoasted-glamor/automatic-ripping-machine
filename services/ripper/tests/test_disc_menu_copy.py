"""The ripper's disc menu side: the read-only disc tree, the scan-time menu probe and the menu copy (fork-only).

Discs are built at test time (`tests._disc_menu_builders`): folders of structure files, and pycdlib images for the
image path. No disc data is committed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest
from arm_common.disc_menus import MenuKind
from arm_ripper.menus import copy as menu_copy
from arm_ripper.menus import probe, tree

from tests import _disc_menu_builders as b

IG = 0x91
PG = 0x90


def _bd(root: Path, *, top: tuple[str, int | str] = ("hdmv", 1), ig: bool = True, big_ig: bool = False) -> Path:
    """A small HDMV disc: a menu clip (IG), two episode clips (PG), and a big title clip."""
    menu = b.ig_clip(0x1400, [b.ics([b.ig_button(1, 10, 10, normal=1, selected=1, activated=1)]),
                              b.pds(0, {1: (200, 128, 128, 255)}), b.ods(1, [[1, 1]])])  # fmt: skip
    files = {
        "BDMV/index.bdmv": b.index_bdmv(("hdmv", 0), top, [("hdmv", 2)]),
        "BDMV/MovieObject.bdmv": b.movie_objects([[b.play_pl(1)], [b.play_pl(1)], [b.play_pl(2)]]),
        "BDMV/PLAYLIST/00001.mpls": b.mpls([("00008", 0, 45000)], ig=[(0x1400, "eng", 0)], subpaths=[(3, ["00046"])]),
        "BDMV/PLAYLIST/00002.mpls": b.mpls([("00014", 0, 90000)], pg=[(0x1200, PG, "eng")]),
        "BDMV/CLIPINF/00008.clpi": b.clpi([(0x1011, 0x1B, "")]),
        "BDMV/CLIPINF/00014.clpi": b.clpi(
            [(0x1011, 0x1B, ""), (0x1200, PG, "eng"), (0x1201, PG, "eng")] + ([(0x1400, IG, "eng")] if big_ig else [])
        ),  # fmt: skip
        "BDMV/CLIPINF/00046.clpi": b.clpi([(0x1400, IG, "eng")] if ig else [(0x1100, 0x81, "eng")]),
        "BDMV/STREAM/00008.m2ts": b.ts_packets(0x1011, b.pes(bytes(300), stream_id=0xE0)),
        "BDMV/STREAM/00014.m2ts": b.pg_clip({0x1200: 3, 0x1201: 1}, pad_packets=4) + (menu if big_ig else b""),
        "BDMV/STREAM/00046.m2ts": menu,
        "BDMV/JAR/00000/big.jar": bytes(64),
        "BDMV/BACKUP/index.bdmv": b.index_bdmv(),
        "CERTIFICATE/id.bdmv": bytes(16),
    }
    return b.write_files(root, files)


def _dvd(root: Path, *, menus: bool = True, title_buttons: bool = False) -> Path:
    button = ((10, 10, 50, 30), (1, 1, 1, 1), b.dvd_cmd_link_pgcn(1))
    menu_vob = b.nav_pack([button]) + b.video_pack() + b.video_pack()
    root_menu = b.pgc(cells=[(0, 2, 10.0)])
    t1 = b.nav_pack([button] if title_buttons else []) + b.video_pack()
    t2 = b.nav_pack() + b.spu_pack(0, b"\x00") + b.spu_pack(1, b"\x00") + b.spu_pack(1, b"\x00")
    files = {
        "VIDEO_TS/VIDEO_TS.IFO": b.vmg_ifo(
            nvts=1, titles=[(1, 1, 1)], menus={"en": [(3, root_menu)]} if menus else None
        ),
        "VIDEO_TS/VIDEO_TS.BUP": b"bup",
        "VIDEO_TS/VIDEO_TS.VOB": menu_vob if menus else b"",
        "VIDEO_TS/VTS_01_0.IFO": b.vts_ifo(
            title_pgcs=[b.pgc(cells=[(0, 1, 30.0), (2, 4, 30.0)])], chapters=[[(1, 1), (1, 2)]], subp=["en", "en"]
        ),  # fmt: skip
        "VIDEO_TS/VTS_01_0.BUP": b"bup",
        "VIDEO_TS/VTS_01_1.VOB": t1,
        "VIDEO_TS/VTS_01_2.VOB": t2,
    }
    return b.write_files(root, files)


def _iso(path: Path, files: dict[str, bytes]) -> Path:
    import pycdlib

    iso = pycdlib.PyCdlib()
    iso.new(joliet=3)
    made: set[str] = set()
    for rel, data in files.items():
        parts = rel.split("/")
        for i in range(1, len(parts)):
            d = "/".join(parts[:i])
            if d not in made:
                iso.add_directory("/" + d.upper()[:8], joliet_path="/" + d)
                made.add(d)
        name = parts[-1].upper()
        iso.add_fp(io.BytesIO(data), len(data), "/" + "/".join([*(p.upper()[:8] for p in parts[:-1]), name]) + ";1",
                   joliet_path="/" + rel)  # fmt: skip
    iso.write(str(path))
    iso.close()
    return path


# ------------------------------------------------------------------------------------------ tree


def test_folder_tree_lists_and_reads(tmp_path: Path) -> None:
    root = _bd(tmp_path / "bd")
    os.symlink(root / "BDMV/index.bdmv", root / "BDMV/link.bdmv")
    with tree.FolderTree(root) as t:
        assert "BDMV/link.bdmv" not in t.files()
        assert t.files()["BDMV/STREAM/00046.m2ts"] == len((root / "BDMV/STREAM/00046.m2ts").read_bytes())
        assert tree.find(t, "bdmv/INDEX.BDMV") == "BDMV/index.bdmv"
        assert tree.read(t, "BDMV/index.bdmv") == (root / "BDMV/index.bdmv").read_bytes()
        assert tree.read(t, "BDMV/nothing") is None
        assert set(tree.listdir(t, "bdmv/playlist")) == {"00001.mpls", "00002.mpls"}


def test_pycdlib_tree_strips_versions_and_reads(tmp_path: Path) -> None:
    img = _iso(tmp_path / "d.iso", {"VIDEO_TS/VIDEO_TS.IFO": b"ifo", "VIDEO_TS/VTS_01_0.VOB": b"v" * 5000})
    t = tree.open_tree(img)
    assert isinstance(t, tree.PycdlibTree)
    try:
        assert t.files() == {"VIDEO_TS/VIDEO_TS.IFO": 3, "VIDEO_TS/VTS_01_0.VOB": 5000}
        assert tree.read(t, "video_ts/vts_01_0.vob") == b"v" * 5000
    finally:
        t.close()


def test_pycdlib_tree_without_joliet_uses_iso9660(tmp_path: Path) -> None:
    import pycdlib

    iso = pycdlib.PyCdlib()
    iso.new()
    iso.add_directory("/VIDEO_TS")
    iso.add_fp(io.BytesIO(b"ifo"), 3, "/VIDEO_TS/VIDEO_TS.IFO;1")
    iso.write(str(tmp_path / "plain.iso"))
    iso.close()
    with tree.PycdlibTree(str(tmp_path / "plain.iso")) as t:
        assert t.files() == {"VIDEO_TS/VIDEO_TS.IFO": 3}


def _fake_7z(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "payload").write_bytes(payload)
    exe = bindir / "7z"
    exe.write_text(f"#!/bin/sh\ncat '{tmp_path / 'payload'}'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")


def test_open_tree_falls_back_to_7z(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from arm_ripper.scan import udf_image

    (tmp_path / "udf.img").write_bytes(b"not an ISO 9660 image")
    _fake_7z(tmp_path, monkeypatch, b"index bytes")
    monkeypatch.setattr(udf_image, "list_files", lambda _p: [("BDMV/index.bdmv", 11)])
    t = tree.open_tree(tmp_path / "udf.img")
    assert isinstance(t, tree.SevenZipTree)
    assert tree.read(t, "BDMV/index.bdmv") == b"index bytes"
    t.close()


def test_open_tree_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from arm_ripper.scan import udf_image

    (tmp_path / "junk").write_bytes(b"junk")
    monkeypatch.setattr(udf_image, "list_files", lambda _p: None)
    assert tree.open_tree(tmp_path / "junk") is None
    monkeypatch.setattr(tree, "FolderTree", _raise_oserror)
    assert tree.open_tree(tmp_path) is None


def _raise_oserror(*_a: object) -> None:
    raise OSError("unreadable")


def test_seven_zip_tree_needs_7z(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from arm_ripper.scan import udf_image

    monkeypatch.setattr(udf_image, "list_files", lambda _p: [("a", 1)])
    t = tree.SevenZipTree(str(tmp_path / "x"))
    monkeypatch.setattr(tree.shutil, "which", lambda _n: None)
    with pytest.raises(OSError), t.open("a"):
        pass


def test_empty_pycdlib_image_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from arm_ripper.scan import udf_image

    img = _iso(tmp_path / "empty.iso", {})
    monkeypatch.setattr(udf_image, "list_files", lambda _p: None)
    assert tree.open_tree(img) is None


# ------------------------------------------------------------------------------------------ probe


@pytest.mark.parametrize(
    ("kw", "kind"),
    [
        ({}, MenuKind.HDMV),
        ({"top": ("bdj", "00000")}, MenuKind.BDJ),
        ({"ig": False}, MenuKind.NONE),
    ],
)
def test_probe_blu_ray(tmp_path: Path, kw: dict[str, object], kind: MenuKind) -> None:
    assert probe.probe_menu_kind(_bd(tmp_path / "bd", **kw)) == kind  # type: ignore[arg-type]


def test_probe_blu_ray_skips_unreadable_clip_info(tmp_path: Path) -> None:
    root = _bd(tmp_path / "bd", ig=False)
    (root / "BDMV/CLIPINF/00001.clpi").write_bytes(b"junk")
    (root / "BDMV/CLIPINF/readme.txt").write_bytes(b"x")
    assert probe.probe_menu_kind(root) == MenuKind.NONE


def test_probe_dvd(tmp_path: Path) -> None:
    assert probe.probe_menu_kind(_dvd(tmp_path / "a")) == MenuKind.DVD
    assert probe.probe_menu_kind(_dvd(tmp_path / "b", menus=False)) == MenuKind.NONE


def test_probe_dvd_menus_in_a_title_set_only(tmp_path: Path) -> None:
    root = _dvd(tmp_path / "d", menus=False)
    (root / "VIDEO_TS/VTS_01_0.IFO").write_bytes(b.vts_ifo(menus={"en": [(3, b.pgc(cells=[(0, 0, 1.0)]))]}))
    assert probe.probe_menu_kind(root) == MenuKind.DVD


def test_probe_dvd_image(tmp_path: Path) -> None:
    root = _dvd(tmp_path / "d")
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert probe.probe_menu_kind(_iso(tmp_path / "d.iso", files)) == MenuKind.DVD


def test_probe_unknown_and_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b.write_files(tmp_path / "half", {"BDMV/PLAYLIST/00001.mpls": b"x"})
    assert probe.probe_menu_kind(tmp_path / "half") == MenuKind.UNKNOWN
    b.write_files(tmp_path / "data", {"README.TXT": b"x"})
    assert probe.probe_menu_kind(tmp_path / "data") == MenuKind.NONE
    b.write_files(tmp_path / "broken", {"BDMV/index.bdmv": b"INDX0200"})
    assert probe.probe_menu_kind(tmp_path / "broken") == MenuKind.UNKNOWN
    monkeypatch.setattr(tree, "open_tree", lambda _s: None)
    assert probe.probe_menu_kind(tmp_path / "data") == MenuKind.UNKNOWN


def test_probe_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _bd(tmp_path / "bd")
    monkeypatch.setattr(probe, "menu_kind_of", _boom)
    assert probe.probe_menu_kind(root) == MenuKind.UNKNOWN


def _boom(_t: object) -> MenuKind:
    raise RuntimeError("pycdlib went wrong")


def test_probe_kinds_with_missing_files() -> None:
    class Empty:
        def files(self) -> dict[str, int]:
            return {}

        def open(self, path: str) -> contextlib.AbstractContextManager[IO[bytes]]:
            raise AssertionError(path)

        def close(self) -> None:
            return None

    assert probe.bd_kind(Empty()) == MenuKind.UNKNOWN
    assert probe.dvd_kind(Empty()) == MenuKind.UNKNOWN


# ------------------------------------------------------------------------------------------ copy


def _cap(src: Path) -> int:
    """A clip cap that keeps the menu clip whole and leaves the title clip out."""
    cap = (src / "BDMV/STREAM/00046.m2ts").stat().st_size
    assert (src / "BDMV/STREAM/00014.m2ts").stat().st_size > cap
    return cap


def test_copy_blu_ray(tmp_path: Path) -> None:
    src = _bd(tmp_path / "bd")
    dest = tmp_path / "menu"
    big = (src / "BDMV/STREAM/00014.m2ts").stat().st_size
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, dest, clip_cap=big - 1)
    assert r.disc == "bd" and r.complete and r.missing == [] and r.ig_filtered == []
    got = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    assert got == [
        "BDMV/BACKUP/index.bdmv", "BDMV/CLIPINF/00008.clpi", "BDMV/CLIPINF/00014.clpi", "BDMV/CLIPINF/00046.clpi",
        "BDMV/MovieObject.bdmv", "BDMV/PLAYLIST/00001.mpls", "BDMV/PLAYLIST/00002.mpls", "BDMV/STREAM/00008.m2ts",
        "BDMV/STREAM/00046.m2ts", "BDMV/index.bdmv", "counts.json", "listing.json",
    ]  # fmt: skip
    assert r.files == 10 and r.bytes == sum((dest / p).stat().st_size for p in got[:10])
    assert json.loads((dest / "counts.json").read_text()) == {"bd": {"00014": {"4608": 3, "4609": 1}}}
    listing = json.loads((dest / "listing.json").read_text())
    assert listing["files"]["BDMV/STREAM/00014.m2ts"] == big and listing["lost"] == []


def test_copy_blu_ray_filters_a_big_clip_with_a_menu(tmp_path: Path) -> None:
    src = _bd(tmp_path / "bd", big_ig=True)
    dest = tmp_path / "menu"
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, dest, clip_cap=_cap(src), count=False)
    assert r.ig_filtered == ["00014"]
    kept = (dest / "BDMV/STREAM/00014.ig.m2ts").read_bytes()
    assert kept == b.ig_clip(0x1400, [b.ics([b.ig_button(1, 10, 10, normal=1, selected=1, activated=1)]),
                                     b.pds(0, {1: (200, 128, 128, 255)}), b.ods(1, [[1, 1]])])  # fmt: skip
    assert not (dest / "BDMV/STREAM/00014.m2ts").exists()
    assert json.loads((dest / "counts.json").read_text()) == {}


def test_copy_blu_ray_lost_files(tmp_path: Path) -> None:
    src = _bd(tmp_path / "bd", big_ig=True)
    dest = tmp_path / "menu"
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, dest, clip_cap=_cap(src), lost=["/bdmv/stream/00014.m2ts", "BDMV/STREAM/00046.m2ts"])
    assert sorted(r.missing) == ["BDMV/STREAM/00014.m2ts", "BDMV/STREAM/00046.m2ts"] and not r.complete
    assert not (dest / "BDMV/STREAM/00046.m2ts").exists()
    assert "bdmv/stream/00046.m2ts" in json.loads((dest / "listing.json").read_text())["lost"]


def test_copy_blu_ray_lost_big_clip_without_a_menu(tmp_path: Path) -> None:
    src = _bd(tmp_path / "bd")
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, tmp_path / "menu", clip_cap=_cap(src), lost=["BDMV/STREAM/00014.m2ts"])
    assert r.missing == [] and r.counts == {}


def test_copy_blu_ray_skips_unreadable_clip_info(tmp_path: Path) -> None:
    src = _bd(tmp_path / "bd")
    (src / "BDMV/CLIPINF/00014.clpi").write_bytes(b"junk")
    (src / "BDMV/STREAM/00099.m2ts").write_bytes(bytes(20_000))  # no clip info at all
    (src / "BDMV/STREAM/SSIF").mkdir()
    (src / "BDMV/STREAM/SSIF/00014.ssif").write_bytes(bytes(20_000))
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, tmp_path / "menu", clip_cap=_cap(src))
    assert r.counts == {} and r.missing == []


class _Flaky:
    """A tree whose reads of some files fail, or whose streams can't seek."""

    def __init__(self, inner: tree.DiscTree, bad: set[str], seekable: bool = True) -> None:
        self.inner, self.bad, self.seekable = inner, bad, seekable

    def files(self) -> dict[str, int]:
        return self.inner.files()

    @contextlib.contextmanager
    def open(self, path: str) -> Iterator[IO[bytes]]:
        if path in self.bad:
            raise OSError("read error")
        with self.inner.open(path) as f:
            if self.seekable:
                yield f
            else:
                yield _NoSeek(f.read())

    def close(self) -> None:
        self.inner.close()


class _NoSeek(io.BytesIO):
    def seekable(self) -> bool:
        return False


def test_copy_blu_ray_read_errors(tmp_path: Path) -> None:
    src = _bd(tmp_path / "bd", big_ig=True)
    with tree.FolderTree(src) as inner:
        t = _Flaky(inner, {"BDMV/STREAM/00046.m2ts", "BDMV/STREAM/00014.m2ts"})
        r = menu_copy.copy_menu(t, tmp_path / "menu", clip_cap=_cap(src))
    assert sorted(r.missing) == ["BDMV/STREAM/00014.m2ts", "BDMV/STREAM/00046.m2ts"]
    assert not (tmp_path / "menu/BDMV/STREAM/00014.ig.m2ts").exists() and r.counts == {}


def test_copy_dvd(tmp_path: Path) -> None:
    src = _dvd(tmp_path / "dvd")
    dest = tmp_path / "menu"
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, dest)
    assert r.disc == "dvd" and r.title_sets == [] and r.complete
    got = sorted(p.name for p in (dest / "VIDEO_TS").iterdir())
    assert got == ["VIDEO_TS.BUP", "VIDEO_TS.IFO", "VIDEO_TS.VOB", "VTS_01_0.BUP", "VTS_01_0.IFO"]
    assert json.loads((dest / "counts.json").read_text()) == {"dvd": {"1": {"0": 1, "1": 2}}}


def test_copy_dvd_takes_title_vobs_with_buttons(tmp_path: Path) -> None:
    src = _dvd(tmp_path / "dvd", title_buttons=True)
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, tmp_path / "menu")
    assert r.title_sets == [1] and r.counts == {}
    assert (tmp_path / "menu/VIDEO_TS/VTS_01_2.VOB").exists()


def test_copy_dvd_title_buttons_in_the_second_vob_through_a_stream(tmp_path: Path) -> None:
    src = _dvd(tmp_path / "dvd")
    button = ((10, 10, 50, 30), (1, 1, 1, 1), b.dvd_cmd_link_pgcn(1))
    (src / "VIDEO_TS/VTS_01_2.VOB").write_bytes(b.nav_pack([button]) + b.video_pack() + b.video_pack())
    with tree.FolderTree(src) as inner:
        r = menu_copy.copy_menu(_Flaky(inner, set(), seekable=False), tmp_path / "menu")
    assert r.title_sets == [1]


def test_copy_dvd_cell_past_the_vobs_and_unreadable_ifo(tmp_path: Path) -> None:
    src = _dvd(tmp_path / "dvd")
    (src / "VIDEO_TS/VTS_01_0.IFO").write_bytes(b.vts_ifo(title_pgcs=[b.pgc(cells=[(500, 501, 1.0)])]))
    with tree.FolderTree(src) as t:
        assert menu_copy.copy_menu(t, tmp_path / "a").title_sets == []
    (src / "VIDEO_TS/VTS_01_0.IFO").write_bytes(b"DVDVIDEO-VTS")
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, tmp_path / "b")
    assert r.title_sets == [1]  # unreadable title cells: the title VOBs come along to be safe


def test_copy_dvd_lost_or_unreadable_title_vobs(tmp_path: Path) -> None:
    src = _dvd(tmp_path / "dvd")
    with tree.FolderTree(src) as t:
        r = menu_copy.copy_menu(t, tmp_path / "a", lost=["VIDEO_TS/VTS_01_2.VOB"])
    assert r.counts == {}
    with tree.FolderTree(src) as inner:
        t2 = _Flaky(inner, {"VIDEO_TS/VTS_01_2.VOB"})
        bad = _Flaky(inner, {"VIDEO_TS/VTS_01_1.VOB"})
        assert menu_copy.copy_menu(t2, tmp_path / "b").counts == {}
        assert menu_copy._count_subpictures(bad, [("VIDEO_TS/VTS_01_1.VOB", 1)]) is None


def test_copy_without_a_disc(tmp_path: Path) -> None:
    b.write_files(tmp_path / "data", {"README.TXT": b"x"})
    with tree.FolderTree(tmp_path / "data") as t:
        r = menu_copy.copy_menu(t, tmp_path / "menu")
    assert r.disc is None and not r.complete and not (tmp_path / "menu/listing.json").exists()


def test_copy_refuses_names_that_escape(tmp_path: Path) -> None:
    class Evil:
        def files(self) -> dict[str, int]:
            return {"BDMV/index.bdmv": 1, "BDMV/../../escape": 1}

        @contextlib.contextmanager
        def open(self, path: str) -> Iterator[IO[bytes]]:
            yield io.BytesIO(b"x")

        def close(self) -> None:
            return None

    r = menu_copy.copy_menu(Evil(), tmp_path / "menu")
    assert r.missing == ["BDMV/../../escape"] and not (tmp_path / "escape").exists()
