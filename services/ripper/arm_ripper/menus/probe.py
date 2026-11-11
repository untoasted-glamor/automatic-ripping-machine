"""What a disc's menu is, read at scan time from the device, an image or a folder (design v5, "The menu as a track").

- Blu-ray: `index.bdmv`'s first play and top menu. Either one BD-J -> `bdj` (the player can't run Java). Else
  `hdmv` when any clip's info lists an interactive graphics (IG) stream, `none` when none does.
- DVD: `hdmv`'s counterpart is `dvd`, when the video manager or any title set has menu program chains; else `none`.
- `unknown` when the files can't be read.

None of these files is encrypted (AACS and CSS cover clips and VOB sectors only), so this reads the drive for a few
small files and never mounts anything.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from arm_common.disc_menus import MenuKind, MenuParseError, dvd, hdmv
from arm_ripper.menus import tree as disc_tree

logger = logging.getLogger(__name__)

_VTS_IFO = re.compile(r"^VTS_(\d\d)_0\.IFO$", re.IGNORECASE)


def bd_kind(t: disc_tree.DiscTree) -> MenuKind:
    raw = disc_tree.read(t, "BDMV/index.bdmv")
    if raw is None:
        return MenuKind.UNKNOWN
    index = hdmv.parse_index(raw)
    if index.first_play.is_bdj or index.top_menu.is_bdj:
        return MenuKind.BDJ
    for name in sorted(disc_tree.listdir(t, "BDMV/CLIPINF")):
        if not name.lower().endswith(".clpi"):
            continue
        data = disc_tree.read(t, f"BDMV/CLIPINF/{name}")
        if data is None:
            continue
        try:
            if hdmv.parse_clip_info(data).pids(hdmv.IG_CODING):
                return MenuKind.HDMV
        except MenuParseError:
            continue
    return MenuKind.NONE


def dvd_kind(t: disc_tree.DiscTree) -> MenuKind:
    raw = disc_tree.read(t, "VIDEO_TS/VIDEO_TS.IFO")
    if raw is None:
        return MenuKind.UNKNOWN
    if dvd.has_menu_pgcs(dvd.parse_vmg(raw).menus):
        return MenuKind.DVD
    for name in sorted(disc_tree.listdir(t, "VIDEO_TS")):
        m = _VTS_IFO.match(name)
        if not m:
            continue
        data = disc_tree.read(t, f"VIDEO_TS/{name}")
        if data is not None and dvd.has_menu_pgcs(dvd.parse_vts(data, int(m.group(1))).menus):
            return MenuKind.DVD
    return MenuKind.NONE


def menu_kind_of(t: disc_tree.DiscTree) -> MenuKind:
    """The menu kind of an open tree. Raises on a structure it can't parse."""
    if disc_tree.find(t, "BDMV/index.bdmv") is not None:
        return bd_kind(t)
    if disc_tree.find(t, "VIDEO_TS/VIDEO_TS.IFO") is not None:
        return dvd_kind(t)
    if any(k.lower().startswith(("bdmv/", "video_ts/")) for k in t.files()):
        return MenuKind.UNKNOWN
    return MenuKind.NONE


def probe_menu_kind(source: str | Path) -> MenuKind:
    """The menu kind of a device, an image or a folder. Never raises: any failure is `unknown`."""
    t = disc_tree.open_tree(source)
    if t is None:
        return MenuKind.UNKNOWN
    try:
        return menu_kind_of(t)
    except (MenuParseError, OSError) as e:
        logger.info("menu probe: %s unreadable: %s", source, e)
        return MenuKind.UNKNOWN
    except Exception as e:  # noqa: BLE001 - pycdlib raises several flavours mid-read; never fail a scan
        logger.info("menu probe: %s failed: %s", source, e)
        return MenuKind.UNKNOWN
    finally:
        t.close()
