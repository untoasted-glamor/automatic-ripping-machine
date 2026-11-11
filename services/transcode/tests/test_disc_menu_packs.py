"""Disc menu packs (`arm_transcode.menus`): the `hdmv-1` and `dvd-1` builders, the CLI and the ffmpeg seam
(fork-only). Menu copies and encodes are built at test time (`tests._disc_menu_builders`); ffmpeg is faked."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from arm_transcode.menus import __main__ as cli
from arm_transcode.menus import dvd_pack, hdmv_pack, media, model
from PIL import Image

from tests import _disc_menu_builders as b

PG = 0x90
IG = 0x91
SEC = 45000


class FakeTools:
    """MediaTools that write placeholder files and remember what they were asked."""

    def __init__(
        self, lengths: dict[str, float] | None = None, chapters: dict[str, list[float]] | None = None, fail: str = ""
    ) -> None:
        self.lengths, self.chs, self.fail = lengths or {}, chapters or {}, fail
        self.calls: list[tuple[str, str]] = []

    def _check(self, what: str, name: str) -> None:
        self.calls.append((what, name))
        if self.fail and self.fail in (what, name):
            raise RuntimeError(f"{what} failed")

    def duration(self, path: Path) -> float:
        self._check("duration", path.name)
        return self.lengths.get(path.name, 0.0)

    def chapters(self, path: Path) -> list[float]:
        self._check("chapters", path.name)
        return self.chs.get(path.name, [])

    def video(self, src: Path, out: Path, size: tuple[int, int], *, audio: bool) -> None:
        self._check("video", out.name)
        out.write_bytes(f"video {size} {audio}".encode())

    def still(self, src: Path, out: Path, size: tuple[int, int], at: float) -> None:
        self._check("still", out.name)
        out.write_bytes(f"still {size} {at}".encode())

    def audio(self, src: Path, out: Path) -> None:
        self._check("audio", out.name)
        out.write_bytes(b"audio")


# ------------------------------------------------------------------------------------------ Blu-ray


def _menu_clip(*, popup: bool = False, twice: bool = False, pid: int = 0x1400) -> bytes:
    def ds(cmd: int) -> list[bytes]:
        return [
            b.ics(
                [
                    b.ig_button(1, 100, 200, normal=1, selected=2, activated=2, cmds=[b.play_pl(cmd)], nav=2),
                    b.ig_button(2, 100, 300, normal=9, selected=9, activated=9),
                ],
                popup=popup,
                default_selected=1,
            ),
            b.pds(0, {1: (235, 128, 128, 255), 2: (81, 90, 240, 255)}),
            b.ods(1, [[1] * 8] * 4),
            b.ods(2, [[2] * 8] * 4),
        ]

    segs = ds(2) + (ds(7) if twice else [])
    return b.ig_clip(pid, segs)


def _bd_copy(root: Path, *, counts: bool = True) -> Path:
    pgs = [(0x1200, PG, "eng"), (0x1201, PG, "eng")]
    files = {
        "BDMV/index.bdmv": b.index_bdmv(("hdmv", 0), ("hdmv", 1), [("hdmv", 2), ("bdj", "00000")]),
        "BDMV/MovieObject.bdmv": b.movie_objects([[b.play_pl(1)], [b.play_pl(1)], [b.play_pl(2)]], 0x8000),
        "BDMV/PLAYLIST/00001.mpls": b.mpls(
            [("00008", 0, 10 * SEC)] * 3, ig=[(0x1400, "eng", 0)], subpaths=[(3, ["00046"])]
        ),  # fmt: skip
        "BDMV/PLAYLIST/00002.mpls": b.mpls(
            [("00014", 0, 60 * SEC), ("00015", 0, 90 * SEC)], pg=pgs, marks=[(1, 0, 0), (1, 1, 0)]
        ),  # fmt: skip
        "BDMV/PLAYLIST/00003.mpls": b.mpls([("00020", 0, 30 * SEC), ("00021", 0, 5 * SEC)]),
        "BDMV/PLAYLIST/00004.mpls": b.mpls([("00099", 0, SEC)], ig=[(0x1401, "eng", None)]),
        "BDMV/PLAYLIST/00005.mpls": b.mpls([("00008", 0, SEC)], ig=[(0x1400, "eng", 5)]),
        "BDMV/PLAYLIST/00006.mpls": b.mpls([("00008", 0, SEC)], ig=[(0x1499, "eng", 0)], subpaths=[(3, ["00046"])]),
        "BDMV/PLAYLIST/00007.mpls": b.mpls([("00014", 0, 60 * SEC)], pg=pgs),
        "BDMV/PLAYLIST/00009.mpls": b.mpls([("00015", 0, 90 * SEC)], pg=pgs),
        "BDMV/PLAYLIST/00010.mpls": b"junk",
        "BDMV/PLAYLIST/00011.mpls": b.mpls([("00047", 0, SEC)], ig=[(0x1402, "eng", None)]),
        "BDMV/PLAYLIST/00012.mpls": b.mpls([("00048", 0, SEC)], ig=[(0x1403, "eng", None)]),
        "BDMV/PLAYLIST/00018.mpls": b.mpls([("00050", 0, SEC)], ig=[(0x1404, "eng", None)]),
        "BDMV/PLAYLIST/readme.txt": b"x",
        "BDMV/CLIPINF/00008.clpi": b.clpi([(0x1011, 0x1B, "")]),
        "BDMV/CLIPINF/00014.clpi": b.clpi([(0x1011, 0x1B, ""), *pgs]),
        "BDMV/CLIPINF/00015.clpi": b.clpi([(0x1011, 0x1B, ""), *pgs]),
        "BDMV/CLIPINF/00046.clpi": b.clpi([(0x1400, IG, "eng")]),
        "BDMV/CLIPINF/00047.clpi": b.clpi([(0x1402, IG, "eng")]),
        "BDMV/CLIPINF/00048.clpi": b.clpi([(0x1403, IG, "eng")]),
        "BDMV/CLIPINF/00099.clpi": b.clpi([(0x1401, IG, "eng")]),
        "BDMV/CLIPINF/00050.clpi": b"junk",
        "BDMV/STREAM/00008.m2ts": b.ts_packets(0x1011, b.pes(bytes(300), stream_id=0xE0)),
        "BDMV/STREAM/00046.m2ts": _menu_clip(twice=True),
        "BDMV/STREAM/00099.ig.m2ts": _menu_clip(popup=True, pid=0x1401),
        "BDMV/STREAM/00048.m2ts": b.ts_packets(0x1403, b.pes(b"\x99\x00\x01\x00")),
    }
    if counts:
        files["counts.json"] = json.dumps({"bd": {"00014": {"4608": 3, "4609": 1}}}).encode()
    return b.write_files(root, files)


def _show(root: Path) -> Path:
    files = {
        "Season 01/Show - S01E01.mkv": b"mkv",
        "Season 01/Show - S01E01.eng.sup": b.sup(3, blank=3),
        "Season 01/Show - S01E01.eng.2.sup": b.sup(2),
        "Season 01/Show - S01E01.eng.srt": b"srt",
        "Season 01/Show - S01E02.mkv": b"mkv",
        "Season 01/Show - S01E02.eng.sup": b.sup(4),
        "Season 01/Show - S01E02.eng.2.sup": b.sup(1),
        "trailers/Trailer.mkv": b"mkv",
    }
    return b.write_files(root, files)


def _bd_map(show: Path) -> model.MediaMap:
    e1 = model.LibraryItem(show / "Season 01/Show - S01E01.mkv", 1)
    e2 = model.LibraryItem(show / "Season 01/Show - S01E02.mkv", 2)
    return model.MediaMap(
        clips={"00014": e1, "00015": e2, "00030": model.LibraryItem(show / "Season 00/missing.mkv", 1, 0)},
        playlists={"00003": model.LibraryItem(show / "trailers/Trailer.mkv", extra="trailers/Trailer.mkv")},
    )


def _bd_build(tmp_path: Path, tools: FakeTools | None, **kw: object) -> tuple[dict[str, object], model.BuildReport]:
    copy = _bd_copy(tmp_path / "menu", **kw)  # type: ignore[arg-type]
    show = _show(tmp_path / "show")
    disc = model.DiscInfo("Show, Disc 1", 1, "job_1", {4: 0}, {"00008": 3.0})
    report = hdmv_pack.build_hdmv_pack(copy / "BDMV", _bd_map(show), disc, tmp_path / "pack", tools)
    return json.loads((tmp_path / "pack" / model.PACK_FILE).read_text()), report


def test_hdmv_pack(tmp_path: Path) -> None:
    tools = FakeTools({"Show - S01E01.mkv": 60.0, "Show - S01E02.mkv": 95.0}, fail="Trailer.mkv")
    pack, report = _bd_build(tmp_path, tools)
    assert pack["schema"] == "hdmv-1"
    assert pack["disc"] == {"type": "bd", "name": "Show, Disc 1", "season": 1, "job": "job_1", "size": [1920, 1080],
                            "menu_languages": ["eng"]}  # fmt: skip
    assert pack["psr"] == {"4": 0}
    assert pack["index"] == {"first_play": 0, "top_menu": 1, "titles": [{"n": 1, "object": 2}, {"n": 2, "bdj": True}]}
    objects = pack["objects"]
    assert isinstance(objects, dict) and objects["0"]["resume"] and objects["2"]["txt"] == ["play_pl 0x2"]
    playlists = pack["playlists"]
    assert isinstance(playlists, dict)
    menu_pl = playlists["00001"]
    assert menu_pl["items"] == [{"clip": "00008", "in": 0, "out": 10 * SEC, "repeat": 3}]
    assert menu_pl["menus"] == {"1": "00046.ig1"} and menu_pl["background"] == {"video": "bg-00008.mp4",
                                                                                  "still": "bg-00008.jpg"}  # fmt: skip
    assert (tmp_path / "pack/bg-00008.jpg").read_bytes() == b"still (1280, 720) 3.0"
    menus = pack["menus"]
    assert isinstance(menus, dict)
    top = menus["00046.ig1"]
    assert (top["model"], top["stream_model"], top["clip"], top["pid"]) == ("always_on", "preloaded", "00046", 0x1400)
    b1, b2 = top["pages"][0]["bogs"][0]["buttons"]
    assert b1["images"] == {"normal": ["00046.ig1.o1.p0.png"], "selected": ["00046.ig1.o2.p0.png"],
                            "activated": ["00046.ig1.o2.p0.png"]}  # fmt: skip
    assert b1["txt"] == ["play_pl 0x2"] and b1["numeric"] is None and not b1["sounds"]
    assert b2["images"] == {"normal": [], "selected": [], "activated": []}
    with Image.open(tmp_path / "pack/00046.ig1.o1.p0.png") as im:
        assert im.size == (8, 4) and im.mode == "RGBA"
    assert menus["00099.ig1"] == {"clip": "00099", "pid": 0x1401, "model": "popup", "omitted": True}
    assert report.menus == ["00046.ig1"]
    media_cells = pack["media"]
    assert isinstance(media_cells, dict)
    assert media_cells["00002"]["cells"] == {
        "0": {"episode": 1, "offset": 0.0, "dur": 60.0, "file": "Show - S01E01.mkv"},
        str(60 * SEC): {"episode": 2, "offset": 0.0, "dur": 90.0, "file": "Show - S01E02.mkv"},
    }
    assert media_cells["00003"]["cells"] == {
        "0": {"extra": "trailers/Trailer.mkv", "offset": 0.0, "dur": 35.0, "file": "Trailer.mkv"}
    }
    assert report.cells == 5
    pg = playlists["00002"]["streams"]["pg"]
    assert pg == [
        {"n": 1, "lang3": "eng", "sidecar": ".eng.sup", "bd_events": 3, "sidecar_events": 3},
        {"n": 2, "lang3": "eng", "sidecar": ".eng.2.sup", "bd_events": 1, "sidecar_events": 2},
    ]
    assert playlists["00009"]["streams"]["pg"][0]["sidecar"] == ".eng.sup"
    assert playlists["00002"]["streams"]["audio"] == [{"n": 1, "lang3": "eng", "codec": "ac3", "channels": 2}]
    notes = "\n".join(report.notes)
    for expected in [
        "clip 00046 IG pid 0x1400: several different compositions, kept the first",
        "playlist 00005: IG stream 1 points at no clip",
        "playlist 00006: IG stream 1 pid 0x1499 isn't in clip 00046",
        "playlist 00010.mpls: unreadable",
        "clip 00047 carries IG but the menu copy has no stream file for it",
        "menu 00047.ig1: no display set found",
        "clip 00050: clip info unreadable",
        f"playlist 00002 at {60 * SEC}: cell 90.00s but Show - S01E02.mkv is 95.00s",
        "Trailer.mkv: length unreadable",
        "playlist 00002: PG stream 2 has 1 events, .eng.2.sup 2",
        "playlist 00009: no counts for its clip; sidecars paired by a playlist with the same PG streams",
    ]:
        assert expected in notes
    assert report.files == len(list((tmp_path / "pack").iterdir())) and report.bytes > 0
    assert report.as_json()["schema"] == "hdmv-1"


def test_hdmv_pack_counts_from_the_clip_and_unpaired_streams(tmp_path: Path) -> None:
    copy = _bd_copy(tmp_path / "menu", counts=False)
    (copy / "counts.json").write_text("{nope")
    (copy / "BDMV/STREAM/00014.m2ts").write_bytes(b.pg_clip({0x1200: 3, 0x1201: 2}))
    show = _show(tmp_path / "show")
    (show / "Season 01/Show - S01E02.eng.sup").unlink()
    (show / "Season 01/Show - S01E02.eng.2.sup").unlink()
    m = _bd_map(show)
    m.clips["00016"] = m.clips["00014"]
    (copy / "BDMV/PLAYLIST/00017.mpls").write_bytes(b.mpls([("00016", 0, 90 * SEC)], pg=[(0x1209, PG, "eng")]))
    r = hdmv_pack.build_hdmv_pack(copy / "BDMV", m, model.DiscInfo("Show", None), tmp_path / "pack", None)
    pack = json.loads((tmp_path / "pack/menu.json").read_text())
    assert "season" not in pack["disc"] and "job" not in pack["disc"]
    assert pack["playlists"]["00002"]["streams"]["pg"][1]["bd_events"] == 2
    assert "background" in pack["playlists"]["00001"] and not (tmp_path / "pack/bg-00008.mp4").exists()
    notes = "\n".join(r.notes)
    assert "counts.json unreadable" in notes
    assert "Show - S01E02.mkv has no .sup sidecars" in notes
    assert "playlist 00017: PG streams not paired" in notes


def test_hdmv_pack_background_failure_and_missing_clip(tmp_path: Path) -> None:
    pack, report = _bd_build(tmp_path, FakeTools(fail="still"))
    playlists = pack["playlists"]
    assert isinstance(playlists, dict) and "background" not in playlists["00001"]
    assert not (tmp_path / "pack/bg-00008.mp4").exists()
    assert "playlist 00001: background failed (still failed)" in report.notes
    (tmp_path / "menu/BDMV/STREAM/00008.m2ts").unlink()
    r = hdmv_pack.build_hdmv_pack(tmp_path / "menu/BDMV", model.MediaMap(), model.DiscInfo("x"), tmp_path / "p2", None)
    assert "playlist 00001: background clip 00008 isn't in the menu copy" in r.notes


def test_hdmv_pack_keeps_an_existing_background(tmp_path: Path) -> None:
    (tmp_path / "pack").mkdir()
    (tmp_path / "pack/bg-00008.mp4").write_bytes(b"old")
    tools = FakeTools()
    _bd_build(tmp_path, tools)
    assert ("video", "bg-00008.mp4") not in tools.calls


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"BDMV/index.bdmv": b.index_bdmv(("bdj", "00000"))}, "first play is BD-J"),
        ({"BDMV/index.bdmv": b.index_bdmv(("hdmv", 0), ("bdj", "00001"))}, "top menu is BD-J"),
        ({"BDMV/index.bdmv": None}, "no index.bdmv"),
        ({"BDMV/MovieObject.bdmv": None}, "no MovieObject.bdmv"),
    ],
)
def test_hdmv_pack_refused(tmp_path: Path, change: dict[str, bytes | None], message: str) -> None:
    copy = _bd_copy(tmp_path / "menu")
    for rel, data in change.items():
        if data is None:
            (copy / rel).unlink()
        else:
            (copy / rel).write_bytes(data)
    with pytest.raises(hdmv_pack.PackRefused, match=message):
        hdmv_pack.build_hdmv_pack(copy / "BDMV", model.MediaMap(), model.DiscInfo("x"), tmp_path / "pack", None)


def test_hdmv_pack_with_no_playlists_and_case_folded_names(tmp_path: Path) -> None:
    b.write_files(tmp_path / "menu", {"BDMV/INDEX.BDMV": b.index_bdmv(), "BDMV/movieobject.bdmv": b.movie_objects([])})
    r = hdmv_pack.build_hdmv_pack(tmp_path / "menu/BDMV", model.MediaMap(), model.DiscInfo("x"), tmp_path / "p", None)
    pack = json.loads((tmp_path / "p/menu.json").read_text())
    assert pack["playlists"] == {} and pack["disc"]["size"] == [1920, 1080] and r.cells == 0
    assert hdmv_pack._find(tmp_path / "nowhere", "x") is None


def test_library_item_cells() -> None:
    special = model.LibraryItem(Path("/s/x.mkv"), 1, 0)
    assert special.cell(1) == {"season": 0, "episode": 1}
    assert model.LibraryItem(Path("/s/x.mkv"), 3, 1).cell(1) == {"episode": 3}
    assert model.LibraryItem(Path("/s/m.mkv")).cell(None) == {}
    assert special.key(1) != model.LibraryItem(Path("/s/x.mkv"), 1).key(1)


def test_load_map(tmp_path: Path) -> None:
    disc, m = model.load_map(
        {
            "name": "Show, Disc 2",
            "season": 1,
            "psr": {"4": "0"},
            "still_at": {"00008": "2"},
            "clips": {"00014": {"episode": 10, "file": "Season 01/e10.mkv"}},
            "playlists": {"00021": {"extra": "trailers/T.mkv"}},
            "titles": {"7:1": [{"cells": [1, 6], "episode": 1, "file": "/abs/e1.mkv"}]},
        },
        tmp_path,
    )
    assert disc == model.DiscInfo("Show, Disc 2", 1, None, {4: 0}, {"00008": 2.0})
    assert m.clips["00014"].file == tmp_path / "Season 01/e10.mkv"
    assert m.playlists["00021"].file == tmp_path / "trailers/T.mkv"
    assert m.titles["7:1"] == [model.DvdSpan(1, 6, model.LibraryItem(Path("/abs/e1.mkv"), 1))]
    with pytest.raises(ValueError, match="names no file"):
        model.load_map({"name": "x", "clips": {"1": {"episode": 1}}}, tmp_path)


# ------------------------------------------------------------------------------------------ DVD


def _palette() -> list[tuple[int, int, int]]:
    return [(16 + i * 14, 128, 128) for i in range(16)]


def _spu() -> bytes:
    rows = [[1] * 64 if y % 4 else [3] * 64 for y in range(32)]
    return b.spu_unit((0, 0, 63, 31), rows)


def _dvd_copy(root: Path, *, counts: bool = True, scrambled: bool = False) -> Path:
    btn = ((10, 10, 50, 30), (1, 1, 2, 2), b.dvd_cmd_link_pgcn(2))
    btn2 = ((60, 0, 62, 2), (2, 2, 1, 1), b.dvd_cmd_jump_tt(1))
    spu = _spu()
    still_cell = [b.nav_pack([btn, btn2]), b.spu_pack(0, spu[:600]), b.spu_pack(0, spu[600:], pts=None),
                  b.spu_pack(1, b"\x00\x02"), b.video_pack(scrambled), b.video_pack(), b.audio_pack()]  # fmt: skip
    vmgm_vob = b"".join(still_cell) + b.nav_pack([btn])
    root_pgc = b.pgc(cells=[(0, 6, 10.0), (7, 7, 10.0)], palette=_palette(), subp_ctl0=0x80000000)
    motion = [b.nav_pack()] + [b.video_pack()] * 40 + [b.audio_pack()]
    vtsm_vob = b"".join(motion) + b.nav_pack() + b.nav_pack() + b.video_pack()
    vts_menus = {"en": [(3, b.pgc(cells=[(0, 41, 1.0)])), (7, b.pgc(cells=[(42, 42, 0.5), (43, 44, 2.0)]))]}
    title = b.pgc(cells=[(0, 0, 10.0), (1, 1, 20.0), (2, 2, 30.0), (3, 3, 40.0)])
    files = {
        "VIDEO_TS/VIDEO_TS.IFO": b.vmg_ifo(nvts=2, titles=[(1, 1, 4)], menus={"en": [(3, root_pgc)], "fr": []}),
        "VIDEO_TS/VIDEO_TS.VOB": vmgm_vob,
        "VIDEO_TS/VTS_01_0.IFO": b.vts_ifo(
            title_pgcs=[title],
            chapters=[[(1, 1), (1, 2), (1, 3), (1, 4)]],
            menus=vts_menus,
            subp=["en", "en"],
            audio=[(0, "ja", 2)],
        ),  # fmt: skip
        "VIDEO_TS/VTS_01_0.VOB": vtsm_vob,
    }
    if counts:
        files["counts.json"] = json.dumps({"dvd": {"1": {"0": 5, "1": 50}}}).encode()
    return b.write_files(root, files)


def _dvd_show(root: Path) -> Path:
    return b.write_files(root, {
        "Season 01/Show - S01E01.mkv": b"mkv",
        "Season 01/Show - S01E01.eng.sup": b.sup(50),
        "Season 01/Show - S01E01.eng.2.sup": b.sup(5),
        "Season 01/Show - S01E02.mkv": b"mkv",
    })  # fmt: skip


def _dvd_map(show: Path) -> model.MediaMap:
    e1 = model.LibraryItem(show / "Season 01/Show - S01E01.mkv", 1)
    e2 = model.LibraryItem(show / "Season 01/Show - S01E02.mkv", 2)
    gone = model.LibraryItem(show / "Season 01/gone.mkv", 3)
    return model.MediaMap(
        titles={
            "1:1": [model.DvdSpan(1, 2, e1), model.DvdSpan(3, 4, e2), model.DvdSpan(5, 5, gone)],
            "9:9": [model.DvdSpan(1, 1, e1)],
            "x": [model.DvdSpan(1, 1, e1)],
        }
    )


def test_dvd_pack(tmp_path: Path) -> None:
    copy = _dvd_copy(tmp_path / "menu", scrambled=True)
    show = _dvd_show(tmp_path / "show")
    tools = FakeTools(chapters={"Show - S01E01.mkv": [0.0, 11.0], "Show - S01E02.mkv": [0.0, 1.0, 2.0]})
    disc = model.DiscInfo("Show, Disc 1", 1, "job_2")
    report = dvd_pack.build_dvd_pack(copy / "VIDEO_TS", _dvd_map(show), disc, tmp_path / "pack", tools)
    pack = json.loads((tmp_path / "pack/menu.json").read_text())
    assert pack["schema"] == "dvd-1" and pack["menu_lang"] == "en"
    assert pack["disc"] == {"type": "dvd", "name": "Show, Disc 1", "season": 1, "job": "job_2"}
    assert pack["vmg"]["titles"] == [{"vts": 1, "ttn": 1, "chapters": 4}]
    root_cells = pack["vmg"]["menus"]["en"][0]["cells"]
    menu = root_cells[0]["menu"]
    assert menu["size"] == [720, 480] and menu["aspect"] == "16:9"
    assert menu["background"] == {"image": "VMGM.en.pgc1.c1.image.png", "audio": "VMGM.en.pgc1.c1.audio.m4a"}
    assert menu["overlay"] == "VMGM.en.pgc1.c1.overlay.png"
    assert [bt["n"] for bt in menu["buttons"]] == [1, 2]
    assert (
        menu["buttons"][0]["txt"] == "LinkPGCN 2"
        and menu["buttons"][0]["selected"] == "VMGM.en.pgc1.c1.b01.selected.png"
    )
    with Image.open(tmp_path / "pack/VMGM.en.pgc1.c1.b01.selected.png") as im:
        assert im.size == (41, 21)
    with Image.open(tmp_path / "pack/VMGM.en.pgc1.c1.overlay.png") as im:
        assert im.size == (720, 480) and im.getpixel((0, 1))[3] == 255
    assert root_cells[1]["menu"]["background"] is None and "selected" not in root_cells[1]["menu"]["buttons"][0]
    assert pack["vmg"]["menus"]["fr"] == []
    vts = pack["vts"]["1"]
    assert vts["audio"] == [{"codec": "ac3", "lang": "ja", "lang3": "jpn", "channels": 2}]
    motion = vts["menus"]["en"][0]["cells"][0]["menu"]
    assert motion["background"] == {"video": "VTSM01.en.pgc1.c1.video.mp4"} and motion["buttons"] == []
    assert (tmp_path / "pack/VTSM01.en.pgc1.c1.video.mp4").read_bytes() == b"video (854, 480) True"
    black, logo = vts["menus"]["en"][1]["cells"]
    assert "menu" not in black and logo["menu"]["background"] == {"image": "VTSM01.en.pgc2.c2.image.png"}
    assert vts["ptt"] == [[[1, 1], [1, 2], [1, 3], [1, 4]]]
    cells = pack["media"]["1:1"]["cells"]
    assert cells == {
        "1": {"episode": 1, "offset": 0.0, "file": "Show - S01E01.mkv"},
        "2": {"episode": 1, "offset": 11.0, "file": "Show - S01E01.mkv"},
        "3": {"episode": 2, "offset": 0.0, "file": "Show - S01E02.mkv"},
        "4": {"episode": 2, "offset": 30.0, "file": "Show - S01E02.mkv"},
    }
    assert report.cells == 4
    assert vts["subp"] == [
        {"lang": "en", "lang3": "eng", "sidecar": ".eng.2.sup", "dvd_events": 5, "sidecar_events": 5},
        {"lang": "en", "lang3": "eng", "sidecar": ".eng.sup", "dvd_events": 50, "sidecar_events": 50},
    ]
    notes = "\n".join(report.notes)
    for expected in [
        "VTS_02_0.IFO isn't in the menu copy",
        "VMGM.en.pgc1.c1: CSS-scrambled sectors",
        "Show - S01E02.mkv: 3 chapters for 2 cells; used the DVD's cell times",
        "title 1:1 cells 5-5: gone.mkv is missing",
        "title 9:9: no such title PGC",
        "title x: no such title PGC",
    ]:
        assert expected in notes
    assert report.menus == ["VMGM:en", "VMGM:fr", "VTSM1:en"]
    assert not any(
        p.name.startswith("arm-dvdmenu-") for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("arm-dvdmenu-*")
    )


def test_dvd_pack_counts_from_title_vobs_and_failures(tmp_path: Path) -> None:
    copy = _dvd_copy(tmp_path / "menu", counts=False)
    (copy / "counts.json").write_text("[]x")
    vob = b.spu_pack(0, b"\x00") + b.spu_pack(1, b"\x00") * 3
    (copy / "VIDEO_TS/VTS_01_1.VOB").write_bytes(vob)
    show = _dvd_show(tmp_path / "show")
    (show / "Season 01/Show - S01E01.eng.sup").write_bytes(b.sup(3))
    (show / "Season 01/Show - S01E01.eng.2.sup").write_bytes(b.sup(1))
    tools = FakeTools(fail="chapters")
    r = dvd_pack.build_dvd_pack(copy / "VIDEO_TS", _dvd_map(show), model.DiscInfo("Movie"), tmp_path / "p", tools)
    pack = json.loads((tmp_path / "p/menu.json").read_text())
    assert [s["dvd_events"] for s in pack["vts"]["1"]["subp"]] == [1, 3]
    assert pack["media"]["1:1"]["cells"]["2"]["offset"] == 10.0
    notes = "\n".join(r.notes)
    assert "counts.json unreadable" in notes and "Show - S01E01.mkv: chapters unreadable (chapters failed)" in notes


def test_dvd_pack_without_counts_sidecars_or_menu_vobs(tmp_path: Path) -> None:
    copy = _dvd_copy(tmp_path / "menu", counts=False)
    (copy / "VIDEO_TS/VIDEO_TS.VOB").unlink()
    (copy / "VIDEO_TS/VTS_02_0.IFO").write_bytes(b"junk")
    show = _dvd_show(tmp_path / "show")
    r = dvd_pack.build_dvd_pack(copy / "VIDEO_TS", _dvd_map(show), model.DiscInfo("x"), tmp_path / "p", None)
    pack = json.loads((tmp_path / "p/menu.json").read_text())
    assert "menu" not in pack["vmg"]["menus"]["en"][0]["cells"][0]
    assert pack["vts"]["1"]["menus"]["en"][0]["cells"][0].get("menu") is None  # no tools: no background
    notes = "\n".join(r.notes)
    assert "VMGM: VIDEO_TS.VOB isn't in the menu copy" in notes
    assert "VTS_02_0.IFO unreadable" in notes
    assert "title set 1: no subpicture counts; subtitles not paired" in notes
    for f in show.glob("Season 01/*.sup"):
        f.unlink()
    r2 = dvd_pack.build_dvd_pack(copy / "VIDEO_TS", _dvd_map(show), model.DiscInfo("x"), tmp_path / "p3", None)
    assert "title set 1: Show - S01E01.mkv has no .sup sidecars" in r2.notes


def test_dvd_pack_background_failure(tmp_path: Path) -> None:
    copy = _dvd_copy(tmp_path / "menu")
    show = _dvd_show(tmp_path / "show")
    r = dvd_pack.build_dvd_pack(
        copy / "VIDEO_TS", _dvd_map(show), model.DiscInfo("x"), tmp_path / "p", FakeTools(fail="audio")
    )
    pack = json.loads((tmp_path / "p/menu.json").read_text())
    assert pack["vmg"]["menus"]["en"][0]["cells"][0]["menu"]["background"] is None
    assert not (tmp_path / "p/VMGM.en.pgc1.c1.image.png").exists()
    assert "VMGM.en.pgc1.c1: background failed (audio failed)" in r.notes


def test_dvd_pack_refused_without_a_vmg(tmp_path: Path) -> None:
    (tmp_path / "VIDEO_TS").mkdir()
    with pytest.raises(hdmv_pack.PackRefused):
        dvd_pack.build_dvd_pack(tmp_path / "VIDEO_TS", model.MediaMap(), model.DiscInfo("x"), tmp_path / "p", None)


def test_dvd_pack_menu_language_and_spu_choice(tmp_path: Path) -> None:
    menus = {"fr": [(3, b.pgc(cells=[(0, 0, 1.0)]))]}
    b.write_files(tmp_path / "m", {"VIDEO_TS/VIDEO_TS.IFO": b.vmg_ifo(nvts=0, menus=menus)})
    dvd_pack.build_dvd_pack(tmp_path / "m/VIDEO_TS", model.MediaMap(), model.DiscInfo("x"), tmp_path / "p", None)
    assert json.loads((tmp_path / "p/menu.json").read_text())["menu_lang"] == "fr"
    assert dvd_pack.background_size(dvd_pack.dvd.VideoAttr("PAL", "4:3", (720, 576))) == (768, 576)
    spu = b.spu_unit((0, 0, 1, 1), [[1, 1], [1, 1]])
    scan = dvd_pack._CellScan(b.spu_pack(3, spu), dvd_pack.dvd.Cell(0, 0, 0, 1.0, 0, 0, 0))
    pgc43 = dvd_pack.dvd.parse_pgc(b.pgc(subp_ctl0=0x83000000), 0)
    assert dvd_pack._spu_for(scan, pgc43, "4:3") is scan.spus[3]
    assert dvd_pack._spu_for(scan, pgc43, "16:9") is scan.spus[3]  # stream 0 asked for, 3 is all there is


# ------------------------------------------------------------------------------------------ CLI and ffmpeg


def test_cli_builds_both_kinds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    copy = _bd_copy(tmp_path / "bd")
    show = _show(tmp_path / "show")
    mp = tmp_path / "map.json"
    mp.write_text(json.dumps({"name": "Show", "season": 1, "clips": {"00014": {"episode": 1}, "00015": {"episode": 9}},
                              "playlists": {"00003": {"extra": "trailers/Trailer.mkv"}}}))  # fmt: skip
    assert cli.main([str(copy), str(mp), str(show), str(tmp_path / "p1"), "--no-ffmpeg"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"] == "hdmv-1" and any("missing S01E09.mkv is missing" in n for n in out["notes"])
    dvd = _dvd_copy(tmp_path / "dvd")
    mp.write_text(json.dumps({"name": "Show", "season": 1, "titles": {"1:1": [{"cells": [1, 2], "episode": 1}]}}))
    assert cli.main([str(dvd), str(mp), str(_dvd_show(tmp_path / "s2")), str(tmp_path / "p2"), "--no-ffmpeg"]) == 0
    assert json.loads(capsys.readouterr().out)["cells"] == 2


def test_cli_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mp = tmp_path / "map.json"
    mp.write_text(json.dumps({"name": "x"}))
    (tmp_path / "empty").mkdir()
    assert cli.main([str(tmp_path / "empty"), str(mp), str(tmp_path), str(tmp_path / "p")]) == 2
    assert "neither" in capsys.readouterr().out
    b.write_files(tmp_path / "bdj", {"BDMV/index.bdmv": b.index_bdmv(("bdj", "00000"))})
    assert cli.main([str(tmp_path / "bdj"), str(mp), str(tmp_path), str(tmp_path / "p"), "--no-ffmpeg"]) == 3
    assert "BD-J" in capsys.readouterr().out


def _script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_ffmpeg_tools(tmp_path: Path) -> None:
    probe = _script(
        tmp_path / "ffprobe",
        """
case "$*" in
  *show_chapters*bad.mkv) echo '{"chapters": [{"start_time": "x"}]}' ;;
  *show_chapters*) echo '{"chapters": [{"start_time": "0.0"}, {"start_time": "12.5"}]}' ;;
  *nojson*) echo 'nope' ;;
  *noduration*) echo '{"format": {}}' ;;
  *broken*) echo 'bad file' >&2; exit 1 ;;
  *) echo '{"format": {"duration": "61.25"}}' ;;
esac
""",
    )
    log = tmp_path / "ffmpeg.log"
    ffmpeg = _script(tmp_path / "ffmpeg", f'echo "$@" >> "{log}"\ncase "$*" in *fail*) echo boom >&2; exit 3 ;; esac\n')
    t = media.FfmpegTools(ffmpeg, probe)
    assert t.duration(tmp_path / "a.mkv") == 61.25
    assert t.chapters(tmp_path / "a.mkv") == [0.0, 12.5]
    for name, msg in [
        ("broken.mkv", "ffprobe exited rc=1"),
        ("nojson.mkv", "no JSON"),
        ("noduration.mkv", "no duration"),
    ]:
        with pytest.raises(RuntimeError, match=msg):
            t.duration(tmp_path / name)
    t.video(tmp_path / "in.vob", tmp_path / "out.mp4", (854, 480), audio=True)
    t.video(tmp_path / "in.vob", tmp_path / "silent.mp4", (854, 480), audio=False)
    t.still(tmp_path / "in.vob", tmp_path / "s.jpg", (854, 480), 1.5)
    t.still(tmp_path / "in.vob", tmp_path / "s.png", (854, 480), 0.0)
    t.audio(tmp_path / "in.vob", tmp_path / "a.m4a")
    lines = log.read_text().splitlines()
    assert "-map 0:a:0? -c:a aac" in lines[0] and "scale=854:480" in lines[0] and "+faststart" in lines[0]
    assert "-an" in lines[1]
    assert lines[2].startswith("-nostdin -v error -y -ss 1.500") and "-q:v 3" in lines[2]
    assert "-q:v" not in lines[3]
    assert "-vn" in lines[4]
    with pytest.raises(RuntimeError, match="ffmpeg exited rc=3"):
        t.audio(tmp_path / "fail.vob", tmp_path / "x.m4a")
