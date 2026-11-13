"""The `menu_pack` task's build: the job's menu copy and encodes into `<show folder>/.discmenus/<disc>/`.

The backend says what to build (`arm_common.schemas.MenuPackPlan`, from `arm_backend.disc_menus.packs`): the
pack folder it means, the disc's name and season, the job, and each library item with the disc title it is. Here
that becomes a `MediaMap` against the menu copy:

- a Blu-ray clip (`bd_clip`) maps every play of that clip;
- a Blu-ray playlist (`bd_playlist`) maps that playlist and every other playlist with the same play items (MakeMKV
  merges identical playlists into one title, and a button may play either);
- a DVD title (`dvd_title`) maps its title PGC's cells, found through `VIDEO_TS.IFO`'s title table.

**One job owns a pack folder.** A folder whose `menu.json` names another job (or none, a pack made by hand) is left
alone: the build takes `<disc>-2`, `<disc>-3`, ..., unless one of those is already the job's own.

**The swap.** The pack is built in `.<disc>.building` beside the target, then the old pack is moved aside and the
new one moved in, so a failed build leaves the old pack. The task fails when none of the encodes is in the media
folder any more ("build before moving").
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arm_common.disc_menus import dvd, hdmv
from arm_common.disc_menus import MenuParseError
from arm_common.schemas import MenuPackItem, MenuPackPlan

from arm_transcode.menus.dvd_pack import build_dvd_pack
from arm_transcode.menus.hdmv_pack import PackRefused, build_hdmv_pack
from arm_transcode.menus.media import MediaTools
from arm_transcode.menus.model import PACK_FILE, BuildReport, DiscInfo, DvdSpan, LibraryItem, MediaMap

logger = logging.getLogger(__name__)

MAX_FOLDERS = 9
Progress = Callable[[int, str], None]


class PackFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class PackResult:
    folder: Path
    report: BuildReport


def pack_owner(folder: Path) -> str | None:
    """The job a pack folder's `menu.json` names, or None."""
    try:
        data = json.loads((folder / PACK_FILE).read_text())
    except OSError, ValueError:
        return None
    disc = data.get("disc") if isinstance(data, dict) else None
    job = disc.get("job") if isinstance(disc, dict) else None
    return job if isinstance(job, str) else None


def choose_folder(target: Path, job_id: str) -> Path:
    """The folder this job's pack goes in: `target` or `<target>-<n>`, the job's own first, else the first free."""
    candidates = [target, *(target.with_name(f"{target.name}-{n}") for n in range(2, MAX_FOLDERS + 1))]
    for folder in candidates:
        if folder.is_dir() and pack_owner(folder) == job_id:
            return folder
    for folder in candidates:
        if not folder.exists():
            return folder
    raise PackFailed(f"{target.name} and {MAX_FOLDERS - 1} more pack folders beside it belong to other packs")


def _item(media_root: Path, entry: MenuPackItem) -> LibraryItem:
    return LibraryItem(media_root / entry.file, entry.episode, entry.season, entry.extra)


def _bd_map(bdmv: Path, plan: MenuPackPlan, media_root: Path, report: BuildReport) -> MediaMap:
    media = MediaMap()
    for entry in plan.items:
        item = _item(media_root, entry)
        if entry.bd_clip:
            media.clips[entry.bd_clip] = item
        if entry.bd_playlist:
            media.playlists[entry.bd_playlist] = item
    if not media.playlists:
        return media
    items: dict[str, tuple[tuple[str, int, int], ...]] = {}
    folder = bdmv / "PLAYLIST"
    for f in sorted(folder.iterdir()) if folder.is_dir() else []:
        if f.name.lower().endswith(".mpls"):
            try:
                pl = hdmv.parse_playlist(f.read_bytes())
            except MenuParseError:
                continue  # the builder notes it
            items[f.name[:5]] = tuple((i.clip, i.in_time, i.out_time) for i in pl.items)
    for name, item in list(media.playlists.items()):
        same = items.get(name)
        if same is None:
            report.note(f"playlist {name} isn't in the menu copy")
            continue
        for other, its in items.items():
            if other != name and its == same and other not in media.playlists:
                media.playlists[other] = item
    return media


def _dvd_map(video_ts: Path, plan: MenuPackPlan, media_root: Path, report: BuildReport) -> MediaMap:
    media = MediaMap()
    try:
        vmg = dvd.parse_vmg(_find(video_ts, "VIDEO_TS.IFO").read_bytes())
    except (OSError, MenuParseError) as e:
        raise PackFailed(f"VIDEO_TS.IFO unreadable ({e})") from None
    vts_cache: dict[int, dvd.Vts | None] = {}
    for entry in plan.items:
        if entry.dvd_title is None:
            continue
        n = entry.dvd_title
        if not 1 <= n <= len(vmg.titles):
            report.note(f"DVD title {n} isn't in the disc's title table")
            continue
        t = vmg.titles[n - 1]
        if t.vts not in vts_cache:
            try:
                vts_cache[t.vts] = dvd.parse_vts(_find(video_ts, f"VTS_{t.vts:02d}_0.IFO").read_bytes(), t.vts)
            except OSError, MenuParseError:
                vts_cache[t.vts] = None
        vts = vts_cache[t.vts]
        try:
            if vts is None:
                raise IndexError
            pgcn = vts.ptt[t.vts_ttn - 1][0][0]
            cells = len(vts.title_pgcs[pgcn - 1].cells)
        except IndexError:
            report.note(f"DVD title {n}: its title PGC isn't readable in the menu copy")
            continue
        media.titles[f"{t.vts}:{pgcn}"] = [DvdSpan(1, cells, _item(media_root, entry))]
    return media


def _find(folder: Path, name: str) -> Path:
    """`folder/name`, matched without case (a copy from a disc image may be upper or lower case)."""
    if folder.is_dir():
        for f in folder.iterdir():
            if f.name.upper() == name.upper():
                return f
    return folder / name


def _remove(folder: Path) -> None:
    if folder.is_dir():
        shutil.rmtree(folder)


def build_pack(
    plan: MenuPackPlan,
    menu_dir: Path,
    media_root: Path,
    tools: MediaTools | None,
    progress: Progress | None = None,
) -> PackResult:
    """Build the plan's pack and swap it into place. Raises PackFailed (or the builders' PackRefused)."""

    def step(pct: int, what: str) -> None:
        if progress is not None:
            progress(pct, what)

    step(2, "reading the menu")
    if not any((media_root / i.file).exists() for i in plan.items):
        raise PackFailed("the encodes have moved out of ARM's media folder; build the pack before moving them")
    disc = DiscInfo(plan.name, plan.season, plan.job_id)
    report = BuildReport("")
    target = choose_folder(media_root / plan.pack_dir, plan.job_id)
    building = target.with_name(f".{target.name}.building")
    old = target.with_name(f".{target.name}.old")
    _remove(building)
    target.parent.mkdir(parents=True, exist_ok=True)
    step(5, "building the pack")
    try:
        if (menu_dir / "BDMV").is_dir():
            media = _bd_map(menu_dir / "BDMV", plan, media_root, report)
            built = build_hdmv_pack(menu_dir / "BDMV", media, disc, building, tools)
        elif (menu_dir / "VIDEO_TS").is_dir():
            media = _dvd_map(menu_dir / "VIDEO_TS", plan, media_root, report)
            built = build_dvd_pack(menu_dir / "VIDEO_TS", media, disc, building, tools)
        else:
            raise PackFailed(f"{menu_dir} holds neither BDMV/ nor VIDEO_TS/")
        if built.cells == 0:
            raise PackFailed("no menu button plays any of the job's encodes")
    except BaseException:
        _remove(building)
        raise
    built.notes[:0] = [*plan.notes, *report.notes]
    step(95, "swapping the pack in")
    _remove(old)
    if target.exists():
        target.rename(old)
    building.rename(target)
    _remove(old)
    for note in built.notes:
        logger.info("menu pack note: %s", note)
    logger.info(
        "menu pack built folder=%s schema=%s cells=%d files=%d bytes=%d",
        target,
        built.schema,
        built.cells,
        built.files,
        built.bytes,
    )
    return PackResult(target, built)


__all__ = ["PackFailed", "PackRefused", "PackResult", "build_pack", "choose_folder", "pack_owner"]
