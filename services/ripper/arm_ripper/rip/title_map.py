"""Match disc-scan titles to the titles of a backup (or an `.iso`).

MakeMKV numbers titles per source and per `--minlength`, so the same title
can have one id on the disc and another in its backup: on the Hanako-kun
Blu-ray every id differed (backup id 0 was the play-all, disc id 7). What
stays the same is what the title plays:

- Blu-ray: TINFO 16, the playlist (`00008.mpls`) or bare clip (`00002.m2ts`).
- DVD: TINFO 24, the disc's own title number.

Titles are matched on that key; duration (±2 s) must agree when both sides
know it, and chapter count then size break any tie that is left (only when a
key is missing). A title with no single match is left unmatched, so the
caller fails it rather than remuxing the wrong one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence

from arm_common.schemas import ScanTitle

logger = logging.getLogger("arm_ripper.rip.title_map")

DURATION_TOLERANCE_SECONDS = 2


def title_key(title: ScanTitle) -> str | None:
    """What the title plays, comparable across sources; None if MakeMKV gave neither key."""
    if title.source_playlist:
        return f"playlist:{title.source_playlist}"
    if title.original_title_id is not None:
        return f"dvd-title:{title.original_title_id}"
    return None


def _same_duration(a: ScanTitle, b: ScanTitle) -> bool:
    if a.duration_seconds is None or b.duration_seconds is None:
        return True
    return abs(a.duration_seconds - b.duration_seconds) <= DURATION_TOLERANCE_SECONDS


def _same_chapters(a: ScanTitle, b: ScanTitle) -> bool:
    return a.chapter_count == b.chapter_count


def _same_size(a: ScanTitle, b: ScanTitle) -> bool:
    return a.size_bytes == b.size_bytes


_TIE_BREAKERS: tuple[Callable[[ScanTitle, ScanTitle], bool], ...] = (_same_chapters, _same_size)


def _candidates(wanted: ScanTitle, pool: list[ScanTitle]) -> list[ScanTitle]:
    key = title_key(wanted)
    if key is not None:
        pool = [t for t in pool if title_key(t) == key]
    pool = [t for t in pool if _same_duration(wanted, t)]
    for same in _TIE_BREAKERS:
        if len(pool) <= 1:
            break
        narrowed = [t for t in pool if same(wanted, t)]
        if narrowed:
            pool = narrowed
    return pool


def match_titles(wanted: Iterable[ScanTitle], source: Sequence[ScanTitle]) -> dict[int, int]:
    """Map each wanted disc-scan title's index to the index of the `source`
    title that plays the same thing. Each source title is used at most once;
    a wanted title with no single match is missing from the result.
    """
    taken: set[int] = set()
    matched: dict[int, int] = {}
    for title in wanted:
        pool = [t for t in source if t.index not in taken]
        found = _candidates(title, pool)
        if len(found) != 1:
            logger.warning(
                "title %d (%s, %ss) has %d matches in the source; not remuxing it",
                title.index,
                title_key(title) or "no key",
                title.duration_seconds,
                len(found),
            )
            continue
        matched[title.index] = found[0].index
        taken.add(found[0].index)
    return matched
