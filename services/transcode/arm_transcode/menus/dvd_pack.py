"""Build a DVD menu pack, schema `dvd-1` (plugin plan v2's P1, archived; design v5 "Pack schemas").

Input: the `VIDEO_TS/` folder of the ripper's menu copy (IFOs, menu VOBs, title VOBs of title sets with buttons in
their titles, and `counts.json` beside it), the media map (title PGC cells -> library items) and the encodes.
Output: `menu.json` plus, per menu cell `<domain>.<lang>.pgc<n>.c<m>`: the subpicture overlay, each button's
selected and activated images, and a background, flat in `pack_dir`.

`menu.json` keeps the disc's own commands (hex), so the player runs a whole DVD VM; `txt` fields are for reading.

Backgrounds, from each menu cell's own sectors: a cell whose video moves (more than `MOTION_BYTES_PER_SECOND` of
video) gets an H.264 + AAC video; any other a still image, plus its audio when it has a second or more of it. A cell
with no buttons that lasts under a second (a black frame between menus) gets nothing, and the player shows black.

Cell offsets come from the encode's chapter marks when a span has as many cells as the encode has chapters, else
from the DVD's cell lengths. Subtitles pair by event count rank within a language, as `hdmv_pack` does.
"""

from __future__ import annotations

import functools
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from arm_common.disc_menus import MenuParseError, dvd

from arm_transcode.menus import images
from arm_transcode.menus.hdmv_pack import COUNTS, PackRefused, _find, _sidecar_counts
from arm_transcode.menus.media import MediaTools
from arm_transcode.menus.model import BuildReport, DiscInfo, DvdSpan, MediaMap, write_pack

logger = logging.getLogger(__name__)

SCHEMA = "dvd-1"
MOTION_BYTES_PER_SECOND = 64 * 1024
_MIN_AUDIO_SECONDS = 1.0
_TITLE_VOB = re.compile(r"^VTS_(\d\d)_([1-9])\.VOB$", re.IGNORECASE)


def _fixed(colors: images.Colors | None, _x: int, _y: int) -> images.Colors | None:
    """One button state's colours, whatever the pixel."""
    return colors


def background_size(v: dvd.VideoAttr) -> tuple[int, int]:
    h = v.size[1]
    w = round(h * (16 / 9 if v.aspect == "16:9" else 4 / 3))
    return w + (w & 1), h


def _pgc_json(pgc: dvd.Pgc, menu_cells: list[dict[str, Any] | None] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "entry": pgc.entry,
        "next": pgc.next,
        "prev": pgc.prev,
        "goup": pgc.goup,
        "still": pgc.still,
        "program_map": list(pgc.program_map),
        "pre": [c.hex for c in pgc.pre],
        "post": [c.hex for c in pgc.post],
        "cellcmds": [c.hex for c in pgc.cellcmds],
        "txt": {
            "pre": [c.text for c in pgc.pre],
            "post": [c.text for c in pgc.post],
            "cellcmds": [c.text for c in pgc.cellcmds],
        },
        "cells": [],
    }
    for i, c in enumerate(pgc.cells):
        cell: dict[str, Any] = {"dur": c.duration, "still": c.still, "cmd": c.cmd}
        if menu_cells is not None and i < len(menu_cells) and menu_cells[i] is not None:
            cell["menu"] = menu_cells[i]
        out["cells"].append(cell)
    return out


class _CellScan:
    """What one menu cell's sectors hold: its first highlight, first whole subpicture per stream, and how much
    video and audio."""

    def __init__(self, vob: bytes, cell: dvd.Cell) -> None:
        self.highlight: dvd.Highlight | None = None
        self.spus: dict[int, dvd.Spu] = {}
        self.video_bytes = 0
        self.audio = False
        self.scrambled = False
        bufs: dict[int, bytearray] = {}
        for pk in dvd.sectors(vob, cell.first, cell.last):
            for p in dvd.parse_pack(pk):
                self.scrambled |= p.scrambled
                if p.kind == "pci" and self.highlight is None:
                    self.highlight = dvd.parse_pci(p.data)
                elif p.kind == "video":
                    self.video_bytes += dvd.SECTOR
                elif p.kind == "audio":
                    self.audio = True
                elif p.kind == "spu" and p.stream not in self.spus:
                    buf = bufs.setdefault(p.stream, bytearray())
                    if p.pts is not None and buf:
                        buf.clear()
                    buf += p.data
                    if len(buf) >= 2 and len(buf) >= (n := int.from_bytes(buf[:2], "big")):
                        spu = dvd.decode_spu(bytes(buf[:n]))
                        if spu is not None:
                            self.spus[p.stream] = spu
                        buf.clear()


def _spu_for(scan: _CellScan, pgc: dvd.Pgc, aspect: str) -> dvd.Spu | None:
    """The subpicture stream the menu shows: logical stream 0's physical stream (16:9 wide or 4:3)."""
    ctl = pgc.subp_ctl[0] if pgc.subp_ctl else 0
    phys = 0
    if ctl & 0x80000000:
        phys = (ctl >> 16) & 0x1F if aspect == "16:9" else (ctl >> 24) & 0x1F
    return scan.spus.get(phys) or next(iter(scan.spus.values()), None)


class _Builder:
    def __init__(
        self, video_ts: Path, pack_dir: Path, tools: MediaTools | None, backgrounds: bool, report: BuildReport
    ):
        self.video_ts, self.pack_dir, self.tools, self.backgrounds, self.report = (
            video_ts, pack_dir, tools, backgrounds, report,
        )  # fmt: skip
        self.tmp = Path(tempfile.mkdtemp(prefix="arm-dvdmenu-"))

    def menu_cell(
        self, key: str, vob: bytes, pgc: dvd.Pgc, cell: dvd.Cell, video: dvd.VideoAttr
    ) -> dict[str, Any] | None:
        scan = _CellScan(vob, cell)
        if scan.scrambled:
            self.report.note(f"{key}: CSS-scrambled sectors; the menu copy must come from a decrypted source")
        h = scan.highlight
        if h is None and cell.duration < _MIN_AUDIO_SECONDS:
            return None  # a short cell with no buttons (a black frame between menus): the player shows black
        bg = self.background(key, vob, cell, scan, video)
        if h is None and bg is None:
            return None
        m: dict[str, Any] = {
            "size": list(video.size),
            "aspect": video.aspect,
            "background": bg,
            "overlay": None,
            "buttons": [],
        }
        if h is None:
            return m
        m["forced_select"], m["forced_action"] = h.forced_select, h.forced_action
        spu = _spu_for(scan, pgc, video.aspect)
        size = video.size
        if spu is not None:
            name = f"{key}.overlay.png"
            images.spu_image(spu, pgc.palette, size, lambda x, y: None).save(self.pack_dir / name, optimize=True)
            m["overlay"] = name
        for b in h.buttons:
            if b.group != 1:
                continue
            btn: dict[str, Any] = {
                "n": b.n,
                "rect": list(b.rect),
                "nav": b.nav,
                "auto": b.auto_action,
                "cmd": b.cmd.hex,
                "txt": b.cmd.text,
            }
            if spu is not None and b.rect[2] >= b.rect[0] and b.rect[3] >= b.rect[1]:
                for si, state in enumerate(("selected", "activated")):
                    cc = h.colors[b.color - 1][si] if 1 <= b.color <= 3 else None
                    img = images.spu_image(spu, pgc.palette, size, functools.partial(_fixed, cc), box=b.rect)
                    name = f"{key}.b{b.n:02d}.{state}.png"
                    img.save(self.pack_dir / name, optimize=True)
                    btn[state] = name
            m["buttons"].append(btn)
        return m

    def background(
        self, key: str, vob: bytes, cell: dvd.Cell, scan: _CellScan, video: dvd.VideoAttr
    ) -> dict[str, str] | None:
        if not scan.video_bytes or not self.backgrounds or self.tools is None:
            return None
        src = self.tmp / f"{key}.vob"
        src.write_bytes(vob[cell.first * dvd.SECTOR : (cell.last + 1) * dvd.SECTOR])
        size = background_size(video)
        dur = max(cell.duration, 0.5)
        out: dict[str, str] = {}
        try:
            if scan.video_bytes / dur > MOTION_BYTES_PER_SECOND and cell.duration >= _MIN_AUDIO_SECONDS:
                out["video"] = f"{key}.video.mp4"
                self.tools.video(src, self.pack_dir / out["video"], size, audio=scan.audio)
            else:
                out["image"] = f"{key}.image.png"
                self.tools.still(src, self.pack_dir / out["image"], size, 0.0)
                if scan.audio and cell.duration >= _MIN_AUDIO_SECONDS:
                    out["audio"] = f"{key}.audio.m4a"
                    self.tools.audio(src, self.pack_dir / out["audio"])
        except (RuntimeError, OSError) as e:
            self.report.note(f"{key}: background failed ({e})")
            for f in out.values():
                (self.pack_dir / f).unlink(missing_ok=True)
            return None
        finally:
            src.unlink(missing_ok=True)
        return out

    def menus(
        self, domain: str, vob_name: str, menus: dict[str, tuple[dvd.Pgc, ...]], video: dvd.VideoAttr
    ) -> dict[str, list[dict[str, Any]]]:
        if not menus:
            return {}
        vob_file = _find(self.video_ts, vob_name)
        vob = vob_file.read_bytes() if vob_file else b""
        if not vob:
            self.report.note(f"{domain}: {vob_name} isn't in the menu copy; its menus have no buttons")
        out: dict[str, list[dict[str, Any]]] = {}
        for lang, pgcs in menus.items():
            out[lang] = []
            for pn, pgc in enumerate(pgcs, 1):
                cells = [
                    self.menu_cell(f"{domain}.{lang}.pgc{pn}.c{cn}", vob, pgc, c, video) if vob else None
                    for cn, c in enumerate(pgc.cells, 1)
                ]
                out[lang].append(_pgc_json(pgc, cells))
        return out


def _offsets(
    span: DvdSpan, pgc: dvd.Pgc, tools: MediaTools | None, cache: dict[Path, list[float]], report: BuildReport
) -> list[float]:
    cells = pgc.cells[span.first - 1 : span.last]
    if tools is not None and span.item.file.exists():
        if span.item.file not in cache:
            try:
                cache[span.item.file] = tools.chapters(span.item.file)
            except RuntimeError as e:
                report.note(f"{span.item.file.name}: chapters unreadable ({e})")
                cache[span.item.file] = []
        starts = cache[span.item.file]
        if len(starts) == len(cells):
            return [round(s, 3) for s in starts]
        report.note(f"{span.item.file.name}: {len(starts)} chapters for {len(cells)} cells; used the DVD's cell times")
    out, t = [], 0.0
    for c in cells:
        out.append(round(t, 3))
        t += c.duration
    return out


def _title_counts(video_ts: Path, vts: int) -> dict[int, int] | None:
    vobs = sorted(
        p for p in video_ts.iterdir() if (m := _TITLE_VOB.match(p.name)) and int(m.group(1)) == vts
    ) if video_ts.is_dir() else []  # fmt: skip
    if not vobs:
        return None

    def packs() -> Any:
        for p in vobs:
            with p.open("rb") as f:
                while len(pk := f.read(dvd.SECTOR)) == dvd.SECTOR:
                    yield pk

    return dvd.count_subpictures(packs())


def build_dvd_pack(
    video_ts: Path,
    media: MediaMap,
    disc: DiscInfo,
    pack_dir: Path,
    tools: MediaTools | None,
    *,
    backgrounds: bool = True,
) -> BuildReport:
    """Write a `dvd-1` pack into `pack_dir` (made if missing). `tools` None skips every ffmpeg step."""
    report = BuildReport(SCHEMA)
    vmg_file = _find(video_ts, "VIDEO_TS.IFO")
    if vmg_file is None:
        raise PackRefused("the menu copy has no VIDEO_TS.IFO")
    vmg = dvd.parse_vmg(vmg_file.read_bytes())
    pack_dir.mkdir(parents=True, exist_ok=True)
    b = _Builder(video_ts, pack_dir, tools, backgrounds, report)
    langs = list(vmg.menus)
    vts_all: dict[int, dvd.Vts] = {}
    for v in range(1, vmg.nvts + 1):
        f = _find(video_ts, f"VTS_{v:02d}_0.IFO")
        if f is None:
            report.note(f"VTS_{v:02d}_0.IFO isn't in the menu copy")
            continue
        try:
            vts_all[v] = dvd.parse_vts(f.read_bytes(), v)
        except MenuParseError as e:
            report.note(f"VTS_{v:02d}_0.IFO unreadable ({e})")
            continue
        langs += [lang for lang in vts_all[v].menus if lang not in langs]
    pack: dict[str, Any] = {
        "schema": SCHEMA,
        "disc": {
            "type": "dvd",
            "name": disc.name,
            **({"season": disc.season} if disc.season is not None else {}),
            **({"job": disc.job} if disc.job else {}),
        },
        "menu_lang": "en" if "en" in langs or not langs else langs[0],
        "vmg": {
            "fp": _pgc_json(vmg.first_play),
            "menus": b.menus("VMGM", "VIDEO_TS.VOB", vmg.menus, vmg.menu_video),
            "titles": [{"vts": t.vts, "ttn": t.vts_ttn, "chapters": t.chapters} for t in vmg.titles],
        },
        "vts": {},
        "media": {},
    }
    for v, vts in vts_all.items():
        pack["vts"][str(v)] = {
            "audio": [
                {"codec": a.codec, "lang": a.lang, "lang3": dvd.lang3(a.lang), "channels": a.channels}
                for a in vts.audio
            ],
            "subp": [{"lang": s, "lang3": dvd.lang3(s)} for s in vts.subp],
            "menus": b.menus(f"VTSM{v:02d}", f"VTS_{v:02d}_0.VOB", vts.menus, vts.menu_video),
            "title_pgcs": [_pgc_json(p) for p in vts.title_pgcs],
            "ptt": [[list(x) for x in t] for t in vts.ptt],
        }
    report.menus = sorted(
        f"{d}:{lang}" for d, menus in [("VMGM", pack["vmg"]["menus"])] + [(f"VTSM{v}", e["menus"]) for v, e in pack["vts"].items()]
        for lang in menus
    )  # fmt: skip

    chapters: dict[Path, list[float]] = {}
    first_items: dict[int, DvdSpan] = {}
    for where, spans in media.titles.items():
        try:
            v, p = (int(x) for x in where.split(":"))
            pgc = vts_all[v].title_pgcs[p - 1]
        except ValueError, KeyError, IndexError:
            report.note(f"title {where}: no such title PGC, cells left out")
            continue
        cells: dict[str, dict[str, Any]] = {}
        for span in spans:
            if not span.item.file.exists():
                report.note(f"title {where} cells {span.first}-{span.last}: {span.item.file.name} is missing")
                continue
            for i, off in enumerate(_offsets(span, pgc, tools, chapters, report)):
                cells[str(span.first + i)] = {**span.item.cell(disc.season), "offset": off, "file": span.item.file.name}
            first_items.setdefault(v, span)
        if cells:
            pack["media"][where] = {"cells": cells}
            report.cells += len(cells)

    _pair_subtitles(video_ts, pack, vts_all, first_items, report)
    write_pack(pack_dir, pack, report)
    for f in b.tmp.iterdir():
        f.unlink()
    b.tmp.rmdir()
    return report


def _pair_subtitles(
    video_ts: Path, pack: dict[str, Any], vts_all: dict[int, dvd.Vts], first: dict[int, DvdSpan], report: BuildReport
) -> None:
    counts_file = _find(video_ts.parent, COUNTS)
    stored: dict[str, dict[str, int]] = {}
    if counts_file is not None:
        try:
            stored = json.loads(counts_file.read_text()).get("dvd", {})
        except (OSError, ValueError) as e:
            report.note(f"{COUNTS} unreadable ({e})")
    for v, span in first.items():
        subp = pack["vts"][str(v)]["subp"]
        if not subp:
            continue
        side = _sidecar_counts(span.item)
        if not side:
            report.note(f"title set {v}: {span.item.file.name} has no .sup sidecars")
            continue
        counts = {int(k): c for k, c in stored[str(v)].items()} if str(v) in stored else _title_counts(video_ts, v)
        if counts is None:
            report.note(f"title set {v}: no subpicture counts; subtitles not paired")
            continue
        for lang in {s["lang3"] for s in subp}:
            dv = sorted((counts.get(n, 0), n) for n, s in enumerate(subp) if s["lang3"] == lang)
            sv = sorted((c, suf) for suf, c in side.items() if suf.startswith(f".{lang}."))
            for (dc, n), (sc, suf) in zip(dv, sv, strict=False):
                subp[n].update({"sidecar": suf, "dvd_events": dc, "sidecar_events": sc})
