"""Disc menus: readers for the DVD and Blu-ray (HDMV) structures a menu is made of.

Fork-only (untoasted-glamor): the design is `docs/plans/2026-11-10-disc-menus-v5.md`.

Pure Python, standard library only, so the ripper (scan-time probe, the menu copy) and the transcoder (the pack
builders) parse a disc the same way. Every reader takes bytes or a binary file and raises `MenuParseError` on data
it can't read; none of them touches a drive or mounts anything.

- `hdmv`: `index.bdmv`, `MovieObject.bdmv`, playlists (`.mpls`), clip info (`.clpi`), HDMV navigation commands.
- `ig`: interactive graphics (IG) streams: the menus, pages, buttons and their images.
- `ts`: 192-byte Blu-ray transport packets: PES reassembly and the PID filter.
- `pg`: presentation graphics (PG) event counts, for pairing a disc's subtitle streams with `.sup` sidecars.
- `dvd`: IFOs, program chains, DVD VM commands, NAV packs (buttons) and subpictures.
"""

from __future__ import annotations

from arm_common.enums import MenuKind

__all__ = ["PLAYABLE_MENU_KINDS", "MenuKind", "MenuParseError"]


class MenuParseError(ValueError):
    """A menu structure is short, truncated or not the format it claims to be."""


PLAYABLE_MENU_KINDS = frozenset({MenuKind.HDMV, MenuKind.DVD, MenuKind.UNKNOWN})
"""Kinds whose "Disc menu" row is made ready to rip (design v5: `unknown` is tried and skipped if empty)."""
