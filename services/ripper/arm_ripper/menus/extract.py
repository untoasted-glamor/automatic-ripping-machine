"""Extract a disc's menu into `<job raw>/menu/`: open the source, copy, and say what the menu row becomes (design v5 A2).

The copy itself is `menus.copy`. This module decides the menu row's outcome:

- `done`, with the folder and its size, when the copy took every file it needed and the copied files hold a menu a
  player can run (HDMV or DVD);
- `skipped` when there is nothing to play: the source has no BDMV or VIDEO_TS, or the copy shows a Java (BD-J) menu
  or no menu at all (a disc the scan called `unknown`). The folder is removed;
- `failed` when the source can't be read, or the copy lost a file it needed (a damaged backup, an unreadable file).
  A failed menu can be tried again (Rip remaining titles), from a kept backup or the disc.

`source_encrypted` tells whether a file source (an ISO or a folder) still carries AACS or CSS encryption, so the
caller can take a decrypted `backup` of it first: the structure files are never encrypted, but clips and VOBs are.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from arm_common.disc_menus import MenuKind, MenuParseError, dvd
from arm_common.enums import TrackStatus
from arm_common.fileops import tree_size

from arm_ripper.menus import copy as menu_copy
from arm_ripper.menus import probe
from arm_ripper.menus import tree as disc_tree

logger = logging.getLogger(__name__)

MENU_DIRNAME = "menu"
NO_MENU_FILES = "The disc has no menu files"
JAVA_MENU = "Java menus can't be played"
NO_MENU = "This disc has no menu"

# AACS encrypts a clip in aligned units of 32 source packets (6144 bytes); each unit's first 16 bytes stay clear.
_ALIGNED_UNIT = 32 * 192
# How many sectors of each VOB are looked at for CSS's scrambling flag.
_CSS_SECTORS = 64
# PES stream ids CSS scrambles: private stream 1 (AC-3, subpictures), MPEG audio, MPEG video.
_CSS_STREAMS = frozenset({0xBD, *range(0xC0, 0xF0)})


@dataclass(frozen=True)
class MenuOutcome:
    """What the menu row becomes: `done` (with the folder and its size), `failed` or `skipped` (with the reason)."""

    status: TrackStatus
    error: str | None = None
    output_path: Path | None = None
    size_bytes: int | None = None


def failed(error: str) -> MenuOutcome:
    return MenuOutcome(TrackStatus.FAILED, error=error)


def skipped(reason: str) -> MenuOutcome:
    return MenuOutcome(TrackStatus.SKIPPED, error=reason)


def _aligned_unit_scrambled(unit: bytes) -> bool:
    """True when an aligned unit's packets past the first lost their sync bytes (AACS encrypts after 16 bytes)."""
    if len(unit) < _ALIGNED_UNIT or unit[4] != 0x47:
        return False
    return any(unit[i * 192 + 4] != 0x47 for i in range(1, 32))


def _bd_encrypted(t: disc_tree.DiscTree) -> bool:
    for name, size in sorted(disc_tree.listdir(t, "BDMV/STREAM").items()):
        if not name.lower().endswith(".m2ts") or size < _ALIGNED_UNIT:
            continue
        path = disc_tree.find(t, f"BDMV/STREAM/{name}")
        if path is None:
            continue
        with t.open(path) as f:
            return _aligned_unit_scrambled(f.read(_ALIGNED_UNIT))
    return False


def _sector_scrambled(pk: bytes) -> bool:
    """CSS's PES scrambling-control bits on a pack's first audio, video or subpicture packet."""
    if len(pk) < dvd.SECTOR or pk[:4] != b"\x00\x00\x01\xba":
        return False
    o = 14 + (pk[13] & 7)
    if pk[o : o + 3] != b"\x00\x00\x01" or pk[o + 3] not in _CSS_STREAMS:
        return False
    return bool(pk[o + 6] & 0x30)


def _dvd_encrypted(t: disc_tree.DiscTree) -> bool:
    for name in sorted(disc_tree.listdir(t, "VIDEO_TS")):
        if not name.upper().endswith(".VOB"):
            continue
        path = disc_tree.find(t, f"VIDEO_TS/{name}")
        if path is None:
            continue
        with t.open(path) as f:
            for _ in range(_CSS_SECTORS):
                pk = f.read(dvd.SECTOR)
                if len(pk) < dvd.SECTOR:
                    break
                if _sector_scrambled(pk):
                    return True
    return False


def source_encrypted(source: str | Path) -> bool:
    """True when a file source's clips (Blu-ray) or VOBs (DVD) are still encrypted. False when they are clear or
    can't be read (the copy then says what it couldn't read)."""
    t = disc_tree.open_tree(source)
    if t is None:
        return False
    try:
        if disc_tree.find(t, "BDMV/index.bdmv") is not None:
            return _bd_encrypted(t)
        if disc_tree.find(t, "VIDEO_TS/VIDEO_TS.IFO") is not None:
            return _dvd_encrypted(t)
        return False
    except OSError as e:
        logger.info("menu extract: can't tell whether %s is encrypted: %s", source, e)
        return False
    finally:
        t.close()


def _judge(dest: Path, copied: menu_copy.MenuCopy) -> MenuOutcome:
    if copied.disc is None:
        return skipped(NO_MENU_FILES)
    if copied.missing:
        shown = ", ".join(sorted(copied.missing)[:5]) + (" and more" if len(copied.missing) > 5 else "")
        return failed(f"menu files lost or unreadable: {shown}")
    menu = disc_tree.FolderTree(dest)
    try:
        kind = probe.menu_kind_of(menu)
    except MenuParseError as e:
        return failed(f"the copied menu can't be read: {e}")
    if kind == MenuKind.BDJ:
        return skipped(JAVA_MENU)
    if kind == MenuKind.NONE:
        return skipped(NO_MENU)
    return MenuOutcome(TrackStatus.DONE, output_path=dest, size_bytes=tree_size(dest))


def extract_menu_sync(source: str | Path, dest: Path, *, lost: Iterable[str] = ()) -> MenuOutcome:
    """Copy the menu of `source` (a folder, an image or a device) into `dest`, replacing what is there. `lost`
    names files a damaged backup lost (see `menus.copy.copy_menu`). Never raises. A menu that isn't `done` leaves
    no folder behind."""
    shutil.rmtree(dest, ignore_errors=True)
    t = disc_tree.open_tree(source)
    if t is None:
        return failed(f"could not read the disc's files from {source}")
    try:
        copied = menu_copy.copy_menu(t, dest, lost=lost)
        outcome = _judge(dest, copied)
    except (MenuParseError, OSError) as e:
        outcome = failed(f"menu copy failed: {e}")
    except Exception as e:  # noqa: BLE001 - pycdlib and the parsers raise several flavours; never fail the rip
        logger.exception("menu copy of %s failed", source)
        outcome = failed(f"menu copy failed: {e}")
    finally:
        t.close()
    if outcome.status != TrackStatus.DONE:
        shutil.rmtree(dest, ignore_errors=True)
    return outcome


async def extract_menu(source: str | Path, dest: Path, *, lost: Iterable[str] = ()) -> MenuOutcome:
    """`extract_menu_sync` off the event loop: the copy reads up to a few hundred MB, and counting the subtitle
    events of the big files it leaves out reads those once."""
    outcome = await asyncio.to_thread(extract_menu_sync, source, dest, lost=tuple(lost))
    if outcome.status == TrackStatus.DONE:
        logger.info("disc menu copied from %s into %s (%s bytes)", source, dest, outcome.size_bytes)
    else:
        logger.warning("disc menu from %s %s: %s", source, outcome.status.value, outcome.error)
    return outcome
