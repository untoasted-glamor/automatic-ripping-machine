"""Names for a job's titles, read from its disc menu (design v5, "Naming the titles automatically from the menu").

A finished `menu_preview` task leaves `preview.json` (`arm_transcode.menus.preview`): every page the walk reached,
each button's label as read, and what it plays. `make_proposals` turns each labelled button that plays a title
into a `menu_proposals` row:

1. **Which track.** A Blu-ray playlist matches the scan title whose segment map (`TINFO 26`, the clip list) is the
   playlist's clip list, the one MakeMKV named after it first. A button that starts a playlist of several clips
   anywhere but a "Play All" (an episode button playing the play-all from its mark) matches the title of the one
   clip it starts in (ledger T46). A DVD title matches the scan title with that title number (`TINFO 24`). No title
   in the scan: "not ripped". A title the job has no track for (one under the rip's minimum length) gets one,
   left out and kept whatever its length (`any_length`), so it can be ticked in the job's title table.
2. **Role and name from the label.** "Play All" is a play-all. A title identity numbered as an episode is checked,
   never renamed: the label's number confirms it or the mismatch is noted. Otherwise keywords pick the role
   (trailer, promo, PV, CM, teaser: `trailer`; opening, ending, OP, ED, creditless, textless: `other`; special,
   OVA, OAD: `special`; preview: `extra`), then a label that names an episode is a `special` (B2a), and anything
   else is an `extra`. The cleaned label is the name.
3. **One row per title.** Several buttons (pages, languages) for one title keep the best-read label.

Rows for bonus titles are `proposed`, left-out ones included; episodes, play-alls, chapters and titles the scan
doesn't have are `info`. Re-running a preview replaces the `proposed` and `info` rows and leaves the owner's
decisions (`accepted`, `applied`, `rejected`) and their tracks.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from arm_backend.track_selection import video_track
from arm_common import (
    Job,
    MenuProposal,
    MenuProposalStatus,
    Track,
    TrackKind,
    TrackRole,
    TranscodeTask,
)
from arm_common.schemas.ripper import ScanTitle

logger = logging.getLogger("arm_backend.disc_menus.proposals")

PREVIEW_FILE = "preview.json"
TICKS = 45000

PLAY_ALL = re.compile(r"\bplay\s*all\b", re.I)
_KEYWORDS: list[tuple[TrackRole, re.Pattern[str]]] = [
    (TrackRole.TRAILER, re.compile(r"\b(?:trailers?|teasers?|PVs?|CMs?|commercials?|promos?|promotional)\b", re.I)),
    (TrackRole.OTHER, re.compile(r"\b(?:opening|ending|OP|ED|creditless|non-?credit|textless)\b", re.I)),
    (TrackRole.SPECIAL, re.compile(r"\b(?:specials?|OVA|OAD)\b", re.I)),
    (TrackRole.EXTRA, re.compile(r"\bpreviews?\b", re.I)),
]
_EPISODE_WORD = re.compile(r"\bepisode\b|\bep\.?\s*\d", re.I)
# The number a label leads with: "Episode 3", "Spook 8: ...", "03 - ...".
_LEAD_NUMBER = re.compile(r"^\W*(?:[A-Za-z]+\.?\s*)?(\d{1,3})\b")
# Marks a reader may take for text around a label: bullets, boxes, dashes.
_EDGE_JUNK = re.compile(r"^[\W_]+|[\s\-‐-―■-◿_|]+$")


@dataclass(frozen=True)
class Button:
    page: str
    button: int
    label: str
    score: float
    outcome: dict[str, Any]


def clean_label(label: str) -> str:
    text = re.sub(r"\s+", " ", label).strip()
    return _EDGE_JUNK.sub("", text).strip()


def role_of(label: str) -> TrackRole:
    """The bonus role a label names (a label on a title that isn't an episode)."""
    if PLAY_ALL.search(label):
        return TrackRole.PLAY_ALL
    for role, rx in _KEYWORDS:
        if rx.search(label):
            return role
    if _EPISODE_WORD.search(label):
        return TrackRole.SPECIAL
    return TrackRole.EXTRA


def lead_number(label: str) -> int | None:
    m = _LEAD_NUMBER.search(label)
    return int(m.group(1)) if m else None


def _clips(value: object) -> list[int]:
    if isinstance(value, str):
        return [int(x) for x in re.findall(r"\d+", value)]
    return []


def title_clips(title: dict[str, Any]) -> list[int]:
    """The clips a scan title plays (its segment map)."""
    return _clips(title.get("segment_map"))


def scan_titles(job: Job) -> list[dict[str, Any]]:
    scan = (job.metadata_json or {}).get("scan_result") or {}
    titles = scan.get("titles") if isinstance(scan, dict) else None
    return [t for t in titles if isinstance(t, dict)] if isinstance(titles, list) else []


def playlist_clips(playlists: dict[str, Any], name: str | None) -> list[int]:
    """The clips a Blu-ray playlist plays, in order ([] when the preview doesn't list it)."""
    pl = playlists.get(name or "")
    return [int(it["clip"]) for it in (pl.get("items") or [])] if pl else []


def clip_title(titles: list[dict[str, Any]], clip: int) -> int | None:
    """The scan title of one clip on its own, or None."""
    single = [t for t in titles if _clips(t.get("segment_map")) == [clip]]
    return int(single[0]["index"]) if single else None


def whole_title(titles: list[dict[str, Any]], name: str | None, clips: list[int]) -> int | None:
    """The scan title MakeMKV made of a whole playlist (the one named after it first, since MakeMKV merges
    identical playlists into one title), or None."""
    whole = [t for t in titles if clips and _clips(t.get("segment_map")) == clips]
    whole.sort(key=lambda t: t.get("source_playlist") != f"{name}.mpls")
    return int(whole[0]["index"]) if whole else None


def _item_at(items: list[dict[str, Any]], start: int) -> int | None:
    """Which of a playlist's items a play starting `start` ticks in lands on (None: past its end)."""
    at = 0
    for i, it in enumerate(items):
        at += (int(it["out"]) - int(it["in"])) * int(it.get("repeat") or 1)
        if start < at:
            return i
    return None


def jumped_into(preview: dict[str, Any]) -> frozenset[str]:
    """The playlists some button starts partway through (past its first clip): a play-all whose parts the menu
    plays one by one, like a play-all of episodes with a button per episode."""
    playlists = preview.get("playlists") or {}
    out: set[str] = set()
    for page in preview.get("pages", []):
        for b in page.get("buttons", []):
            o = b.get("outcome") or {}
            pl = playlists.get(o.get("playlist") or "") if o.get("kind") == "plays" else None
            if pl and (_item_at(pl.get("items") or [], int(o.get("start") or 0)) or 0) > 0:
                out.add(str(o["playlist"]))
    return frozenset(out)


def match_bd(
    playlists: dict[str, Any],
    titles: list[dict[str, Any]],
    outcome: dict[str, Any],
    label: str,
    jumped: frozenset[str] = frozenset(),
) -> int | None:
    """The scan title index a Blu-ray button's play starts on, or None. A playlist of several clips is one title
    (the title MakeMKV made of it) unless the menu jumps into it partway (`jumped`): then each of its buttons plays the
    clip it starts on, as an episode button does in a play-all of episodes. A "Play All" button plays the whole."""
    name = outcome.get("playlist")
    pl = playlists.get(name or "")
    if not pl:
        return None
    items = pl.get("items") or []
    whole = whole_title(titles, name, playlist_clips(playlists, name))
    if len(items) > 1 and not PLAY_ALL.search(label) and (name in jumped or whole is None):
        i = _item_at(items, int(outcome.get("start") or 0))
        single = clip_title(titles, int(items[i]["clip"])) if i is not None else None
        if single is not None:
            return single
    return whole


def match_dvd(titles: list[dict[str, Any]], outcome: dict[str, Any]) -> int | None:
    for t in titles:
        if t.get("original_title_id") is not None and int(t["original_title_id"]) == int(outcome.get("title") or 0):
            return int(t["index"])
    return None


def plays_text(outcome: dict[str, Any]) -> str:
    if outcome.get("kind") == "plays_title":
        return f"title {outcome.get('title')}, chapter {outcome.get('chapter')}"
    text = f"{outcome.get('playlist')}.mpls"
    secs = int((outcome.get("start") or 0) / TICKS)
    if secs:
        text += f" from {secs // 60}:{secs % 60:02d}"
    return text


def buttons_of(preview: dict[str, Any]) -> list[Button]:
    out: list[Button] = []
    for page in preview.get("pages", []):
        for b in page.get("buttons", []):
            label = clean_label(str(b.get("label") or ""))
            outcome = b.get("outcome") or {}
            if label and outcome.get("kind") in ("plays", "plays_title"):
                out.append(Button(str(page["id"]), int(b["id"]), label, float(b.get("score") or 0.0), outcome))
    return out


def _not_ripped(job: Job, index: int | None) -> str:
    """Why the job has no track for a title a button plays."""
    if index is None:
        return "not ripped (the disc scan has no title for it)"
    scan = (job.metadata_json or {}).get("scan_result") or {}
    floor = scan.get("min_length_seconds") if isinstance(scan, dict) else None
    title = next((t for t in scan_titles(job) if t.get("index") == index), {})
    duration = title.get("duration_seconds")
    if isinstance(floor, int) and isinstance(duration, int) and duration < floor:
        return f"not ripped: {duration} s, under the rip's {floor} s minimum"
    return "not ripped"


def _proposal(
    job: Job, task: TranscodeTask | None, b: Button, track: Track | None, index: int | None, chapter_of: bool
) -> MenuProposal:
    row = MenuProposal(
        job_id=job.id,
        track_id=track.id if track is not None else None,
        scan_title=index,
        task_id=task.id if task is not None else None,
        page=b.page,
        button=b.button,
        label=b.label,
        score=b.score,
        plays=plays_text(b.outcome),
        status=MenuProposalStatus.INFO,
    )
    if PLAY_ALL.search(b.label):
        row.role, row.note = TrackRole.PLAY_ALL, "plays the episodes back to back"
        return row
    if track is not None and track.role == TrackRole.EPISODE:
        n, have = lead_number(b.label), track.episode_number
        row.role = TrackRole.EPISODE
        if n is None or have is None:
            row.note = "an episode button"
        elif n == have:
            row.note = f"confirms episode {have}"
        else:
            row.note = f"identity has episode {have}, the menu says {n}"
        return row
    row.role, row.name = role_of(b.label), b.label
    if row.role == TrackRole.SPECIAL:
        row.episode_number = None  # TMDB's season 0 number, set by the owner
    if track is None:
        row.note = _not_ripped(job, index)
    elif chapter_of:
        row.note = "a chapter of this title"
    else:
        row.status = MenuProposalStatus.PROPOSED
        if track.excluded:
            row.note = "left out of the rip"
    return row


def _menu_title_track(job: Job, index: int) -> Track:
    """A left-out track for a title a button plays that the job has none for, kept whatever its length when ticked.
    `index` is a scan title's (match_bd and match_dvd return no other)."""
    title = next(t for t in scan_titles(job) if t.get("index") == index)
    duration, size = title.get("duration_seconds"), title.get("size_bytes")
    track = video_track(
        job.id,
        ScanTitle(
            index=index,
            duration_seconds=duration if isinstance(duration, int) else None,
            size_bytes=size if isinstance(size, int) else None,
        ),
    )
    track.excluded = True
    track.any_length = True
    return track


async def make_proposals(
    db: AsyncSession, job: Job, preview: dict[str, Any], task: TranscodeTask | None = None
) -> list[MenuProposal]:
    """Replace the job's undecided proposals with those `preview` gives. Flushes; the caller commits."""
    tracks = (await db.execute(select(Track).where(col(Track.job_id) == job.id))).scalars().all()
    titles_by_ref = {t.source_ref: t for t in tracks if t.kind == TrackKind.VIDEO_TITLE}
    scan = scan_titles(job)
    playlists = preview.get("playlists") or {}
    jumped = jumped_into(preview)
    existing = (await db.execute(select(MenuProposal).where(col(MenuProposal.job_id) == job.id))).scalars().all()
    undecided = (MenuProposalStatus.PROPOSED, MenuProposalStatus.INFO)
    decided_tracks = {p.track_id for p in existing if p.track_id is not None and p.status not in undecided}
    for old in existing:
        if old.status in undecided:
            await db.delete(old)

    # Best-read button per title (or per "plays" for a title with no track).
    best: dict[str, tuple[Button, Track | None, int | None]] = {}
    chapters: dict[str, set[int]] = {}
    for b in buttons_of(preview):
        if b.outcome.get("kind") == "plays_title":
            index = match_dvd(scan, b.outcome)
        else:
            index = match_bd(playlists, scan, b.outcome, b.label, jumped)
        track = titles_by_ref.get(str(index)) if index is not None else None
        if track is not None:
            key = track.id
        elif index is not None:
            key = f"title:{index}"
        else:
            key = f"plays:{plays_text(b.outcome)}"
        if b.outcome.get("kind") == "plays_title":
            chapters.setdefault(key, set()).add(int(b.outcome.get("chapter") or 0))
        if PLAY_ALL.search(b.label):
            key = f"play_all:{key}"
        prior = best.get(key)
        if prior is None or b.score > prior[0].score:
            best[key] = (b, track, index)
    rows: list[MenuProposal] = []
    for key, (b, track, index) in best.items():
        if track is not None and track.id in decided_tracks and not key.startswith("play_all:"):
            continue
        chapter_of = len(chapters.get(key, set())) > 1
        if track is None and index is not None and not chapter_of and not key.startswith("play_all:"):
            track = _menu_title_track(job, index)
            db.add(track)
            await db.flush()
        row = _proposal(job, task, b, track, index, chapter_of)
        db.add(row)
        rows.append(row)
    await db.flush()
    logger.info(
        "menu proposals job_id=%s rows=%d proposed=%d",
        job.id,
        len(rows),
        sum(1 for r in rows if r.status == MenuProposalStatus.PROPOSED),
    )
    return rows


def read_preview(preview_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((preview_dir / PREVIEW_FILE).read_text())
    except (OSError, ValueError) as e:
        logger.warning("menu preview unreadable dir=%s: %s", preview_dir, e)
        return None
    return data if isinstance(data, dict) else None
