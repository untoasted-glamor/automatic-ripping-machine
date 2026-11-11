"""Copy a disc's menu out of a backup or a file source into `<job raw>/menu/` (design v5, "What the ripper copies").

The copy is generous and unparsed: what it leaves out can't be got back without the disc once the backup is gone,
so it takes more than any one pack rule needs, and every parse happens later in the pack build, which can be redone.

Blu-ray, into `menu/BDMV/` in the disc's own layout:
- all of `BDMV/` except `STREAM/` and `JAR/` (index, movie objects, playlists, clip info, `AUXDATA/`, `META/`,
  `BDJO/` and their `BACKUP/` copies);
- every clip in `STREAM/` at or under `clip_cap` bytes (every menu clip and background on the discs tested);
- for each bigger clip whose clip info lists an IG stream, only that clip's IG packets (with the PAT and PMT), as
  `STREAM/<clip>.ig.m2ts`, so a menu muxed into a title clip still comes along.

DVD, into `menu/VIDEO_TS/`: every `.IFO` and `.BUP`, `VIDEO_TS.VOB`, every `VTS_nn_0.VOB` (the menu VOBs), and the
title VOBs of any title set whose title cells show buttons (motion menus built as titles with highlights).

`listing.json` beside them records the source's whole file list with sizes, for rules that come later, and
`counts.json` the subtitle events of each clip or title set left out (PG events per PID of a Blu-ray clip,
subpicture units per stream of a DVD title set), so the pack build can still pair the disc's subtitle streams with
the encodes' `.sup` sidecars once the backup is gone. Counting reads those big files once (about 2.3 s per GiB).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from arm_common.disc_menus import MenuParseError, dvd, hdmv, pg, ts
from arm_common.fileops import safe_join
from arm_ripper.menus import tree as disc_tree

logger = logging.getLogger(__name__)

CLIP_CAP = 100 * 2**20
LISTING = "listing.json"
COUNTS = "counts.json"
_CHUNK = 4 * 2**20
_BD_SKIP = ("bdmv/stream/", "bdmv/jar/")
_DVD_ALWAYS = re.compile(r"^(?:.+\.(?:IFO|BUP)|VIDEO_TS\.VOB|VTS_\d\d_0\.VOB)$", re.IGNORECASE)
_DVD_TITLE_VOB = re.compile(r"^VTS_(\d\d)_([1-9])\.VOB$", re.IGNORECASE)
_VTS_IFO = re.compile(r"^VTS_(\d\d)_0\.IFO$", re.IGNORECASE)


@dataclass
class MenuCopy:
    """What one copy took."""

    disc: str | None = None  # "bd", "dvd", or None when the source has neither BDMV nor VIDEO_TS
    files: int = 0
    bytes: int = 0
    missing: list[str] = field(default_factory=list)  # files the copy needed that are lost or unreadable
    ig_filtered: list[str] = field(default_factory=list)  # big clips reduced to their IG packets
    title_sets: list[int] = field(default_factory=list)  # DVD title sets whose title VOBs came along
    counts: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)  # counts.json

    @property
    def complete(self) -> bool:
        return self.disc is not None and not self.missing


def _write(t: disc_tree.DiscTree, src: str, dest: Path, rel: str, result: MenuCopy, lost: set[str]) -> None:
    if src.lower() in lost:
        result.missing.append(src)
        return
    try:
        target = safe_join(dest, rel)
    except ValueError:
        logger.warning("menu copy: %r would land outside the menu folder; skipped", src)
        result.missing.append(src)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with t.open(src) as f, target.open("wb") as out:
            n = _pump(f, out)
    except OSError as e:
        logger.warning("menu copy: %s unreadable: %s", src, e)
        target.unlink(missing_ok=True)
        result.missing.append(src)
        return
    result.files += 1
    result.bytes += n


def _pump(f: IO[bytes], out: IO[bytes]) -> int:
    n = 0
    while chunk := f.read(_CHUNK):
        out.write(chunk)
        n += len(chunk)
    return n


def _copy_bd(t: disc_tree.DiscTree, dest: Path, cap: int, lost: set[str], result: MenuCopy, *, count: bool) -> None:
    files = t.files()
    stream = {k: v for k, v in files.items() if k.lower().startswith("bdmv/stream/")}
    for src in sorted(files):
        if not src.lower().startswith("bdmv/") or src.lower().startswith(_BD_SKIP):
            continue
        _write(t, src, dest, src, result, lost)
    clpi = disc_tree.listdir(t, "BDMV/CLIPINF")
    for src, size in sorted(stream.items()):
        name = src.rsplit("/", 1)[1]
        if not name.lower().endswith(".m2ts") or "/" in src[len("bdmv/stream/") :]:
            continue
        if size <= cap:
            _write(t, src, dest, src, result, lost)
            continue
        info = next((k for k in clpi if k.lower() == name[:5].lower() + ".clpi"), None)
        data = disc_tree.read(t, f"BDMV/CLIPINF/{info}") if info else None
        if data is None:
            continue
        try:
            ci = hdmv.parse_clip_info(data)
        except MenuParseError:
            continue
        ig_pids, pg_pids = ci.pids(hdmv.IG_CODING), ci.pids(hdmv.PG_CODING)
        if src.lower() in lost:
            if ig_pids:
                result.missing.append(src)
            continue
        if ig_pids:
            _filter_ig(t, src, dest, {0, *ci.pmt_pids, *ig_pids}, result)
        if pg_pids and count:
            counted = _count_pg(t, src, set(pg_pids))
            if counted is not None:
                result.counts.setdefault("bd", {})[name[:5]] = counted


def _filter_ig(t: disc_tree.DiscTree, src: str, dest: Path, pids: set[int], result: MenuCopy) -> None:
    target = safe_join(dest, src[: -len(".m2ts")] + ".ig.m2ts")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with t.open(src) as f, target.open("wb") as out:
            n = ts.filter_pids(f, out, pids)
    except OSError as e:
        logger.warning("menu copy: IG filter of %s failed: %s", src, e)
        target.unlink(missing_ok=True)
        result.missing.append(src)
        return
    result.files += 1
    result.bytes += n * ts.PACKET
    result.ig_filtered.append(src.rsplit("/", 1)[1][:5])


def _count_pg(t: disc_tree.DiscTree, src: str, pids: set[int]) -> dict[str, int] | None:
    """Shown PG events per PID of one clip (the PIDs as decimal strings), or None when it can't be read."""
    payloads: dict[int, list[bytes]] = {p: [] for p in pids}
    try:
        with t.open(src) as f:
            for pid, _pts, data in ts.pes_payloads(ts.packets(f), pids):
                payloads[pid].append(data)
    except OSError as e:
        logger.warning("menu copy: PG count of %s failed: %s", src, e)
        return None
    return {str(p): pg.count_payloads(v) for p, v in sorted(payloads.items())}


def _count_subpictures(t: disc_tree.DiscTree, vobs: list[tuple[str, int]]) -> dict[str, int] | None:
    def packs() -> Iterator[bytes]:
        for name, _size in vobs:
            with t.open(name) as f:
                while len(pk := f.read(dvd.SECTOR)) == dvd.SECTOR:
                    yield pk

    try:
        return {str(k): v for k, v in sorted(dvd.count_subpictures(packs()).items())}
    except OSError as e:
        logger.warning("menu copy: subpicture count of %s failed: %s", vobs[0][0] if vobs else "?", e)
        return None


def _read_sector(t: disc_tree.DiscTree, vobs: list[tuple[str, int]], sector: int) -> bytes | None:
    """Sector `sector` of a title set's title VOBs taken as one stream (VTS_nn_1.VOB, _2, ...)."""
    off = sector * dvd.SECTOR
    for name, size in vobs:
        if off < size:
            with t.open(name) as f:
                if f.seekable():
                    f.seek(off)
                else:  # a 7-Zip stream: read up to it
                    left = off
                    while left > 0 and (chunk := f.read(min(left, _CHUNK))):
                        left -= len(chunk)
                return f.read(dvd.SECTOR)
        off -= size
    return None


def title_buttons(t: disc_tree.DiscTree, vts_ifo: bytes, n: int, vobs: list[tuple[str, int]]) -> bool:
    """True when any title cell of title set `n` starts with a NAV pack that shows buttons."""
    for pgc in dvd.parse_vts(vts_ifo, n).title_pgcs:
        for cell in pgc.cells:
            pk = _read_sector(t, vobs, cell.first)
            if pk is not None and dvd.highlight_of(pk) is not None:
                return True
    return False


def _copy_dvd(t: disc_tree.DiscTree, dest: Path, lost: set[str], result: MenuCopy, *, count: bool) -> None:
    folder = disc_tree.listdir(t, "VIDEO_TS")
    prefix = next(k for k in t.files() if k.lower().startswith("video_ts/")).split("/", 1)[0]
    for name in sorted(folder):
        if _DVD_ALWAYS.match(name):
            _write(t, f"{prefix}/{name}", dest, f"{prefix}/{name}", result, lost)
    titles: dict[int, list[tuple[str, int]]] = {}
    for name, size in sorted(folder.items()):
        if m := _DVD_TITLE_VOB.match(name):
            titles.setdefault(int(m.group(1)), []).append((f"{prefix}/{name}", size))
    for name in sorted(folder):
        m = _VTS_IFO.match(name)
        if not m or int(m.group(1)) not in titles:
            continue
        n = int(m.group(1))
        vobs = titles[n]
        ifo = disc_tree.read(t, f"{prefix}/{name}")
        buttons = True
        try:
            buttons = ifo is not None and title_buttons(t, ifo, n, vobs)
        except (MenuParseError, OSError) as e:
            logger.info("menu copy: title set %d's title cells unreadable: %s; taking its title VOBs", n, e)
        if buttons:
            result.title_sets.append(n)
            for src, _size in vobs:
                _write(t, src, dest, src, result, lost)
        elif count and not any(src.lower() in lost for src, _ in vobs):
            counted = _count_subpictures(t, vobs)
            if counted:
                result.counts.setdefault("dvd", {})[str(n)] = counted


def copy_menu(
    t: disc_tree.DiscTree,
    dest: Path,
    *,
    clip_cap: int = CLIP_CAP,
    lost: Iterable[str] = (),
    count: bool = True,
) -> MenuCopy:
    """Copy the menu of the disc in `t` into `dest` (made if missing). `lost` names files a damaged backup lost
    (relative paths, any case): the copy never reads them, and lists them in `missing` when it needed them.
    `count` False skips `counts.json` (no read of the big files)."""
    lost_set = {p.strip("/").lower() for p in lost}
    result = MenuCopy()
    files = t.files()
    dest.mkdir(parents=True, exist_ok=True)
    if disc_tree.find(t, "BDMV/index.bdmv") is not None:
        result.disc = "bd"
        _copy_bd(t, dest, clip_cap, lost_set, result, count=count)
    elif disc_tree.find(t, "VIDEO_TS/VIDEO_TS.IFO") is not None:
        result.disc = "dvd"
        _copy_dvd(t, dest, lost_set, result, count=count)
    else:
        return result
    (dest / LISTING).write_text(json.dumps({"files": dict(sorted(files.items())), "lost": sorted(lost_set)}, indent=1))
    (dest / COUNTS).write_text(json.dumps(result.counts, indent=1, sort_keys=True))
    return result
