"""Match disc-scan titles to the titles of a backup, an ISO or a disc folder.

MakeMKV numbers titles per source, so the same title can have one id on the
disc and another in its backup: on the Hanako-kun Blu-ray (MakeMKV 2.0.0)
every id differed, the play-all was disc 7 and backup 0. Whether that holds
for a decrypted backup on 1.18.4 is unknown, so ids are never compared. What
stays the same is what the title plays:

- Blu-ray: TINFO 16, the playlist (`00008.mpls`) or bare clip (`00002.m2ts`).
- DVD: TINFO 24, the disc's own title number.

A title with a key matches only source titles with the same key. Duration
must agree within 2 s whenever both sides know it. Only a title with no key
falls back to chapter count, then size, to pick one of several candidates.
A title with no single match is left unmatched, so the caller fails it
rather than remuxing the wrong one, and each source title is used once.

MakeMKV lists one playlist of each set it finds equal and skips the rest
(`MSG:3309`), and which one it keeps can differ between the disc and the
backup (Princess Tutu BD-1: disc 00007.mpls, backup 00017.mpls). So a
playlist with no exact match falls back to the source title MakeMKV found
equal to it (`SourceTitle.equal_playlists`), as long as the duration, and
the clip list when both sides know it, agree. Exact matches are made first,
so a twin never takes a title another playlist names exactly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence

from arm_common.schemas import ScanTitle
from arm_ripper.scan.makemkv import SourceTitle

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


def _same_clips(a: ScanTitle, b: ScanTitle) -> bool:
    if a.segment_map is None or b.segment_map is None:
        return True
    return a.segment_map == b.segment_map


def _same_chapters(a: ScanTitle, b: ScanTitle) -> bool:
    return a.chapter_count == b.chapter_count


def _same_size(a: ScanTitle, b: ScanTitle) -> bool:
    return a.size_bytes == b.size_bytes


_TIE_BREAKERS: tuple[Callable[[ScanTitle, ScanTitle], bool], ...] = (_same_chapters, _same_size)


def _candidates(wanted: ScanTitle, pool: list[ScanTitle]) -> list[ScanTitle]:
    key = title_key(wanted)
    if key is not None:
        return [t for t in pool if title_key(t) == key and _same_duration(wanted, t)]
    pool = [t for t in pool if _same_duration(wanted, t)]
    for same in _TIE_BREAKERS:
        if len(pool) <= 1:
            break
        narrowed = [t for t in pool if same(wanted, t)]
        if narrowed:
            pool = narrowed
    return pool


def _equal_candidates(wanted: ScanTitle, pool: list[ScanTitle]) -> list[ScanTitle]:
    """Source titles MakeMKV kept in place of `wanted`'s playlist, which it found equal to theirs."""
    playlist = wanted.source_playlist
    if playlist is None:
        return []
    return [
        t
        for t in pool
        if isinstance(t, SourceTitle)
        and playlist in t.equal_playlists
        and _same_duration(wanted, t)
        and _same_clips(wanted, t)
    ]


def match_titles(wanted: Iterable[ScanTitle], source: Sequence[ScanTitle]) -> dict[int, int]:
    """Map each wanted disc-scan title's index to the index of the `source`
    title that plays the same thing. Each source title is used at most once;
    a wanted title with no single match is missing from the result. Titles
    with a key are matched first, exactly and then through a playlist MakeMKV
    found equal, so a keyless one can't take their twin.
    """
    wanted = list(wanted)
    keyed = [t for t in wanted if title_key(t) is not None]
    taken: set[int] = set()
    matched: dict[int, int] = {}
    found_count: dict[int, int] = {}

    def pick(title: ScanTitle, find: Callable[[ScanTitle, list[ScanTitle]], list[ScanTitle]]) -> ScanTitle | None:
        found = find(title, [t for t in source if t.index not in taken])
        found_count[title.index] = len(found)
        if len(found) != 1:
            return None
        matched[title.index] = found[0].index
        taken.add(found[0].index)
        return found[0]

    for title in keyed:
        pick(title, _candidates)
    for title in keyed:
        if title.index in matched or found_count[title.index] != 0:
            continue
        twin = pick(title, _equal_candidates)
        if twin is not None:
            logger.info(
                "title %d (%s) maps to source title %d (%s), which MakeMKV found equal to it",
                title.index,
                title_key(title),
                twin.index,
                title_key(twin),
            )
    for title in wanted:
        if title_key(title) is None:
            pick(title, _candidates)
    for title in wanted:
        if title.index not in matched:
            logger.warning(
                "title %d (%s, %ss) has %d matches in the source; not remuxing it",
                title.index,
                title_key(title) or "no key",
                title.duration_seconds,
                found_count[title.index],
            )
    return matched
