"""The disc menu preview (`arm_transcode.menus.preview`, fork only; disc menus M1): the pages, the button crops and
labels, `preview.json`, the walk under Node, and the label reader. Menu copies are built at test time
(`tests.test_disc_menu_packs`), ffmpeg and the walk are faked, and the line reader is a stand-in."""

from __future__ import annotations

import json
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from arm_transcode.menus import preview as pv
from arm_transcode.menus import model
from arm_transcode.subtitle_ocr.engine import BORDER, Bitmap, line_bounds
from PIL import Image, ImageDraw

from tests.test_disc_menu_packs import FakeTools, _bd_copy, _dvd_copy


class Reader:
    """Reads every line as `line<height>`, so a test can tell how the crop was cut."""

    def __init__(self) -> None:
        self.calls: list[list[tuple[int, int]]] = []

    def read(self, lines: Sequence[Bitmap]) -> list[tuple[str, float]]:
        self.calls.append([ln.shape for ln in lines])
        return [(f"line{ln.shape[0]}", 0.9 + i / 100) for i, ln in enumerate(lines)]


class StillTools(FakeTools):
    """FakeTools whose stills are real images (dark grey), at `scale` of the size asked."""

    def __init__(self, scale: float = 1.0) -> None:
        super().__init__()
        self.scale = scale

    def still(self, src: Path, out: Path, size: tuple[int, int], at: float) -> None:
        self._check("still", out.name)
        w, h = int(size[0] * self.scale), int(size[1] * self.scale)
        Image.new("RGB", (w, h), (40, 40, 40)).save(out, format="PNG" if out.suffix == ".png" else "JPEG")


def _bd_walk(pack_file: Path, languages: list[str] | None) -> dict[str, Any]:
    assert languages == ["eng"]
    pack = json.loads(pack_file.read_text())
    images = pack["menus"]["00046.ig1"]["pages"][0]["bogs"][0]["buttons"][0]["images"]
    # Give the solid test button some dark "text" so there is a label to read.
    button = pack_file.parent / images["normal"][0]
    with Image.open(button) as img:
        img = img.convert("RGBA")
        img.putpixel((3, 1), (0, 0, 0, 255))
        img.putpixel((4, 2), (0, 0, 0, 255))
        img.save(button)
    page: dict[str, Any] = {"menu": "00046.ig1", "page": 0, "language": "eng", "path": []}
    return {
        "schema": "walk-1",
        "version": "1.2.3",
        "disc": "bd",
        "first_play": [{"kind": "plays", "playlist": "00007", "start": 0, "mark": 0, "seconds": 60}],
        "truncated": False,
        "unsupported": [],
        "pages": [
            {**page, "at": "00046.ig1 p0 00001", "playlist": "00001", "buttons": [
                {"id": 1, "x": 100, "y": 200, "w": 8, "h": 4, "image": images["normal"][0],
                 "outcome": {"kind": "plays", "playlist": "00002", "start": 0, "mark": 0, "seconds": 0}},
                {"id": 2, "x": 100, "y": 300, "w": 0, "h": 0, "image": None, "outcome": {"kind": "menu", "at": "x"}},
                {"id": 3, "x": 0, "y": 0, "w": 0, "h": 0, "image": "missing.png",
                 "outcome": {"kind": "plays", "playlist": "00404", "start": 0, "mark": 0, "seconds": 0}},
            ]},
            {**page, "at": "00046.ig1 p0 00005", "playlist": "00005", "path": ["1"], "buttons": []},
        ],
    }  # fmt: skip


def test_bd_preview(tmp_path: Path) -> None:
    menu = _bd_copy(tmp_path / "menu")
    steps: list[int] = []
    reader = Reader()
    result = pv.make_preview(
        menu, reader=reader, tools=StillTools(), walker=_bd_walk, progress=lambda p, w: steps.append(p)
    )
    out = menu / "preview"
    assert result.out_dir == out and not (menu / ".preview.arm-inprogress").exists()
    assert (result.pages, result.buttons, result.labels) == (2, 3, 1)
    assert steps[0] == 5 and steps[-1] == 100
    assert result.size() == sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    preview = json.loads((out / pv.PREVIEW_FILE).read_text())
    assert (preview["schema"], preview["disc"], preview["walk_version"]) == ("menu-preview-1", "bd", "1.2.3")
    assert preview["first_play"][0]["playlist"] == "00007"
    assert "00046.ig1 p0 00005: no background still; buttons drawn over black" in preview["notes"]
    p1, p2 = preview["pages"]
    assert (p1["id"], p1["image"], p1["size"], p1["playlist"]) == ("1", "pages/1.jpg", [1920, 1080], "00001")
    b1, b2, b3 = p1["buttons"]
    assert b1 == {"id": 1, "box": [100, 200, 108, 204], "visible": True, "crop": "crops/1.b1.png", "label": f"line{2 + 2 * BORDER}",
                  "score": 0.9, "outcome": p1["buttons"][0]["outcome"]}  # fmt: skip
    assert (b2["visible"], b2["box"], "label" in b2) == (False, [100, 300, 100, 300], False)
    assert (b3["visible"], b3["outcome"]["playlist"]) == (False, "00404")  # its image is missing
    assert p2["buttons"] == [] and p2["path"] == ["1"]
    # Only playlists in the pack are listed; 00404 isn't.
    assert sorted(preview["playlists"]) == ["00002", "00007"]
    assert preview["playlists"]["00002"]["items"][1]["clip"] == "00015"
    assert preview["playlists"]["00002"]["marks"]
    with Image.open(out / "pages/1.jpg") as img:
        assert img.size == (pv.PAGE_WIDTH, 720)
    assert (out / "crops/1.b1.png").is_file() and json.loads((out / "walk.json").read_text())["version"] == "1.2.3"
    assert (out / "pack" / model.PACK_FILE).is_file()


def _dvd_walk(pack_file: Path, languages: list[str] | None) -> dict[str, Any]:
    assert languages is None
    page: dict[str, Any] = {"menu": "vmgm en 1", "page": 0, "language": "en", "path": []}
    play = {"kind": "plays_title", "title": 1, "chapter": 1, "vts": 1, "pgc": 1, "cell": 1}
    return {
        "version": "1.2.3",
        "disc": "dvd",
        "pages": [
            {**page, "at": "vmgm root", "buttons": [
                {"id": 1, "x": 10, "y": 10, "w": 40, "h": 20, "image": "VMGM.en.pgc1.c1.image.png", "outcome": play},
                {"id": 2, "x": 60, "y": 0, "w": 0, "h": 2, "image": "VMGM.en.pgc1.c1.image.png", "outcome": play},
            ]},
            {**page, "at": "vmgm root c2", "buttons": [
                {"id": 1, "x": 10, "y": 10, "w": 40, "h": 20, "image": None, "outcome": {"kind": "stopped", "why": "x"}},
            ]},
            {**page, "at": "vmgm empty", "buttons": []},
        ],
    }  # fmt: skip


class LabelTools(StillTools):
    """Stills with two lines of 'text' (white bars) inside the first button's box, at half the menu's size."""

    def still(self, src: Path, out: Path, size: tuple[int, int], at: float) -> None:
        super().still(src, out, size, at)
        with Image.open(out) as img:
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            draw.rectangle((7, 7, 22, 9), fill=(230, 230, 230))
            draw.rectangle((7, 12, 18, 13), fill=(230, 230, 230))
            img.save(out)


def test_dvd_preview(tmp_path: Path) -> None:
    menu = _dvd_copy(tmp_path / "menu")
    reader = Reader()
    result = pv.make_preview(menu, reader=reader, tools=LabelTools(scale=0.5), walker=_dvd_walk)
    preview = json.loads((menu / "preview" / pv.PREVIEW_FILE).read_text())
    assert preview["disc"] == "dvd" and preview["playlists"] == {} and preview["first_play"] == []
    p1, p2, p3 = preview["pages"]
    # The size is the menu's (from the VMG), not the half-size still's; the crop is scaled to the still.
    assert p1["size"] == [720, 480]
    b1, b2 = p1["buttons"]
    assert (b1["label"], b1["score"], b1["crop"]) == (
        f"line{3 + 2 * BORDER} line{2 + 2 * BORDER}",
        0.9,
        "crops/1.b1.png",
    )
    assert len(reader.calls[0]) == 2
    assert (b2["label"], b2["score"], "crop" in b2) == ("", 0.0, False)
    assert "vmgm root c2: no background image; labels can't be read" in preview["notes"]
    assert p2["size"] == [720, 480] and p2["buttons"][0]["label"] == ""
    assert p3["size"] == [720, 480] and p3["buttons"] == []
    assert result.labels == 1
    with Image.open(menu / "preview" / "pages/1.jpg") as img:
        assert img.size == (720, 480)


def test_a_new_preview_replaces_the_old_one_only_when_done(tmp_path: Path) -> None:
    menu = _bd_copy(tmp_path / "menu")
    (menu / "preview").mkdir()
    (menu / "preview" / "old.txt").write_text("old")

    def broken(pack_file: Path, languages: list[str] | None) -> dict[str, Any]:
        raise pv.PreviewFailed("the menu walk exited rc=1")

    with pytest.raises(pv.PreviewFailed, match="rc=1"):
        pv.make_preview(menu, reader=Reader(), tools=StillTools(), walker=broken)
    assert (menu / "preview" / "old.txt").is_file() and not (menu / ".preview.arm-inprogress").exists()
    pv.make_preview(menu, reader=Reader(), tools=StillTools(), walker=_bd_walk)
    assert not (menu / "preview" / "old.txt").exists() and (menu / "preview" / pv.PREVIEW_FILE).is_file()


def test_preview_refusals(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(pv.PreviewFailed, match="neither BDMV/ nor VIDEO_TS/"):
        pv.make_preview(tmp_path / "empty", reader=Reader(), walker=_bd_walk)
    menu = _bd_copy(tmp_path / "menu")
    (menu / "BDMV/index.bdmv").unlink()
    with pytest.raises(pv.PreviewFailed, match="no index.bdmv"):
        pv.make_preview(menu, reader=Reader(), tools=StillTools(), walker=_bd_walk)


def test_stills_only_skips_video_and_audio(tmp_path: Path) -> None:
    tools = pv._StillsOnly()
    assert tools.video(tmp_path / "a", tmp_path / "b", (1, 1), audio=True) is None
    assert tools.audio(tmp_path / "a", tmp_path / "b") is None
    assert not list(tmp_path.iterdir())


# ------------------------------------------------------------------------------------------------ labels


def _crop(*bars: tuple[int, int, int, int], bg: int = 30, fg: int = 220) -> Image.Image:
    img = Image.new("L", (60, 30), bg)
    draw = ImageDraw.Draw(img)
    for bar in bars:
        draw.rectangle(bar, fill=fg)
    return img.convert("RGB")


def test_ink() -> None:
    assert pv.ink(Image.new("RGB", (0, 0))).shape == (0, 0)
    assert pv.ink(_crop()).shape == (0, 0)
    assert pv.ink(_crop(fg=60)).shape == (0, 0)  # too close to the border colour to be ink
    bm = pv.ink(_crop((10, 10, 19, 14)))
    # Trimmed to the ink with two rows/columns of background each side; ink is 0, background 255.
    assert bm.shape == (5 + 4, 10 + 4)
    assert bm[2, 2] == 0 and bm[0, 0] == 255
    # Dark text on a light button reads too: the distance from the border is what counts.
    assert pv.ink(_crop((10, 10, 19, 14), bg=220, fg=20))[2, 2] == 0


def test_read_label_cuts_each_line() -> None:
    reader = Reader()
    assert pv.read_label(reader, _crop()) == ("", 0.0) and reader.calls == []
    text, score = pv.read_label(reader, _crop((5, 4, 50, 11), (5, 16, 30, 23)))
    assert (text, score) == (f"line{8 + 2 * BORDER} line{8 + 2 * BORDER}", 0.9)


def test_line_bounds() -> None:
    bm = np.full((12, 5), 255, np.uint8)
    bm[1:4] = 0
    bm[6:9] = 0
    bm[10] = 0  # a dot under the last line (too short for a line of its own) joins it
    assert line_bounds(bm) == [(1, 4), (6, 11)]
    assert line_bounds(np.full((3, 3), 255, np.uint8)) == []


# ------------------------------------------------------------------------------------------------ the walk


def _node(tmp_path: Path, body: str) -> str:
    path = tmp_path / "node"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_run_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "walk.mjs"
    monkeypatch.setattr(pv, "WALK_BUNDLE", bundle)
    with pytest.raises(pv.PreviewFailed, match="isn't in this image"):
        pv.run_walk(tmp_path / "menu.json")
    bundle.write_text("")
    monkeypatch.setattr(pv, "NODE", _node(tmp_path, 'printf \'{"args": "%s"}\' "$*"'))
    assert pv.run_walk(tmp_path / "menu.json", ["eng", "jpn"]) == {
        "args": f"{bundle} {tmp_path / 'menu.json'} --lang eng,jpn"
    }
    assert pv.run_walk(tmp_path / "menu.json") == {"args": f"{bundle} {tmp_path / 'menu.json'}"}
    monkeypatch.setattr(pv, "NODE", _node(tmp_path, "echo boom >&2; exit 3"))
    with pytest.raises(pv.PreviewFailed, match="exited rc=3: boom"):
        pv.run_walk(tmp_path / "menu.json")
    monkeypatch.setattr(pv, "NODE", _node(tmp_path, "echo not json"))
    with pytest.raises(pv.PreviewFailed, match="printed no JSON"):
        pv.run_walk(tmp_path / "menu.json")
    monkeypatch.setattr(pv, "NODE", str(tmp_path / "no-node"))
    with pytest.raises(pv.PreviewFailed, match="could not run"):
        pv.run_walk(tmp_path / "menu.json")
