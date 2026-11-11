"""What a pack build is told, and what it reports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACK_FILE = "menu.json"


@dataclass(frozen=True)
class LibraryItem:
    """One library file a menu cell plays: an episode (with `season` when it isn't the disc's) or an extra."""

    file: Path
    episode: int | None = None
    season: int | None = None
    extra: str | None = None  # its path under the show folder: other/…, trailers/…, extras/…

    def key(self, disc_season: int | None) -> tuple[object, ...]:
        if self.extra:
            return ("extra", self.extra)
        return ("episode", self.season if self.season is not None else disc_season, self.episode, str(self.file))

    def cell(self, disc_season: int | None) -> dict[str, Any]:
        """The cell fields that name the item (design v5, `hdmv-1` "media")."""
        out: dict[str, Any] = {}
        if self.extra:
            out["extra"] = self.extra
        elif self.episode is not None:
            if self.season is not None and self.season != disc_season:
                out["season"] = self.season
            out["episode"] = self.episode
        return out


@dataclass(frozen=True)
class DiscInfo:
    name: str  # shown by the player: "<Show>, Disc <n>"
    season: int | None = None  # None for a movie disc
    job: str | None = None  # the job that wrote the pack (one job owns a pack folder)
    psr: dict[int, int] = field(default_factory=dict)  # Blu-ray register overrides
    still_at: dict[str, float] = field(default_factory=dict)  # background clip -> seconds for its still


@dataclass(frozen=True)
class DvdSpan:
    """Cells `first`..`last` (1-based, inclusive) of a DVD title PGC, played by one library item."""

    first: int
    last: int
    item: LibraryItem


@dataclass(frozen=True)
class MediaMap:
    """Which library item each disc title is.

    Blu-ray: by clip (every play item on a mapped clip starts or extends a cell) or by whole playlist.
    DVD: by title PGC (`"<vts>:<pgc>"`), as spans of its cells.
    """

    clips: dict[str, LibraryItem] = field(default_factory=dict)
    playlists: dict[str, LibraryItem] = field(default_factory=dict)
    titles: dict[str, list[DvdSpan]] = field(default_factory=dict)


@dataclass
class BuildReport:
    schema: str
    files: int = 0
    bytes: int = 0
    cells: int = 0
    menus: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "files": self.files,
            "bytes": self.bytes,
            "cells": self.cells,
            "menus": self.menus,
            "notes": self.notes,
        }


def write_pack(pack_dir: Path, pack: dict[str, Any], report: BuildReport) -> None:
    (pack_dir / PACK_FILE).write_text(json.dumps(pack, indent=1))
    files = [p for p in pack_dir.iterdir() if p.is_file()]
    report.files = len(files)
    report.bytes = sum(p.stat().st_size for p in files)


def load_map(raw: dict[str, Any], base: Path) -> tuple[DiscInfo, MediaMap]:
    """A map file (the by-hand builders' format, plus explicit files) -> (DiscInfo, MediaMap).

    {"name": ..., "season": 1, "job": ..., "psr": {"4": 0}, "still_at": {"00008": 12.0},
     "clips": {"00014": {"episode": 10, "file": "Season 01/... S01E10 ....mkv"}},
     "playlists": {"00021": {"extra": "trailers/Trailers.mkv"}},
     "titles": {"7:1": [{"cells": [1, 6], "episode": 1, "file": "..."}]}}
    A relative `file` is under `base` (the show folder); an extra's file defaults to `base/<extra>`.
    """

    def item(d: dict[str, Any]) -> LibraryItem:
        f = d.get("file") or d.get("extra")
        if not f:
            raise ValueError(f"map entry {d} names no file")
        return LibraryItem(base / f, d.get("episode"), d.get("season"), d.get("extra"))

    disc = DiscInfo(
        raw["name"],
        raw.get("season"),
        raw.get("job"),
        {int(k): int(v) for k, v in raw.get("psr", {}).items()},
        {str(k): float(v) for k, v in raw.get("still_at", {}).items()},
    )
    titles = {
        k: [DvdSpan(int(s["cells"][0]), int(s["cells"][1]), item(s)) for s in spans]
        for k, spans in raw.get("titles", {}).items()
    }
    return disc, MediaMap(
        {k: item(v) for k, v in raw.get("clips", {}).items()},
        {k: item(v) for k, v in raw.get("playlists", {}).items()},
        titles,
    )
