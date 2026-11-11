"""Build a Blu-ray (HDMV) menu pack, schema `hdmv-1` (design v5, "Pack schemas").

Input: the `BDMV/` folder of the ripper's menu copy (`<job raw>/menu/BDMV`: structure files, small clips, big
clips' IG packets as `<clip>.ig.m2ts`, and `counts.json` beside it), the media map, and the encodes it names.
Output: `menu.json` plus button images (`<menu>.o<object>.p<palette>.png`) and backgrounds (`bg-<clip>.mp4`/`.jpg`),
flat in `pack_dir`.

Subtitles: each playlist that hands off to the library pairs its PG streams with the first cell's encode's `.sup`
sidecars, by event count rank within a language. Counts come from the clip when the copy kept it, else from the
copy's `counts.json`; a playlist with neither takes the pairing of another with the same PG streams.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from arm_common.disc_menus import MenuParseError, hdmv, ig, pg, ts

from arm_transcode.menus import images
from arm_transcode.menus.media import BD_BACKGROUND_SIZE, MediaTools
from arm_transcode.menus.model import BuildReport, DiscInfo, LibraryItem, MediaMap, write_pack

logger = logging.getLogger(__name__)

SCHEMA = "hdmv-1"
COUNTS = "counts.json"
_LENGTH_SLACK_SECONDS = 2.0


class PackRefused(Exception):
    """The disc gets no pack (a BD-J first play or top menu, or no index)."""


def _ticks90(v: int) -> float | None:
    return None if not v else round(v / 90000, 3)


def _find(folder: Path, name: str) -> Path | None:
    """`folder/name`, matched without regard to case."""
    p = folder / name
    if p.exists():
        return p
    if folder.is_dir():
        low = name.lower()
        return next((q for q in folder.iterdir() if q.name.lower() == low), None)
    return None


class _Clips:
    """Clip info and IG display sets, read once per clip."""

    def __init__(self, bdmv: Path, report: BuildReport) -> None:
        self.bdmv, self.report = bdmv, report
        self.info: dict[str, hdmv.ClipInfo | None] = {}
        self.ig: dict[str, dict[int, ig.DisplaySet]] = {}

    def clip_info(self, clip: str) -> hdmv.ClipInfo | None:
        if clip not in self.info:
            p = _find(self.bdmv / "CLIPINF", f"{clip}.clpi")
            try:
                self.info[clip] = hdmv.parse_clip_info(p.read_bytes()) if p else None
            except MenuParseError as e:
                self.report.note(f"clip {clip}: clip info unreadable ({e})")
                self.info[clip] = None
        return self.info[clip]

    def stream_file(self, clip: str) -> Path | None:
        stream = self.bdmv / "STREAM"
        return _find(stream, f"{clip}.m2ts") or _find(stream, f"{clip}.ig.m2ts")

    def ig_pids(self, clip: str) -> list[int]:
        ci = self.clip_info(clip)
        return ci.pids(hdmv.IG_CODING) if ci else []

    def display_sets(self, clip: str) -> dict[int, ig.DisplaySet]:
        """The first display set of each of a clip's IG PIDs."""
        if clip in self.ig:
            return self.ig[clip]
        out: dict[int, ig.DisplaySet] = {}
        pids = self.ig_pids(clip)
        f = self.stream_file(clip)
        if pids and f is None:
            self.report.note(f"clip {clip} carries IG but the menu copy has no stream file for it")
        elif pids and f is not None:
            payloads: dict[int, list[bytes]] = {p: [] for p in pids}
            with f.open("rb") as fh:
                for pid, _pts, data in ts.pes_payloads(ts.packets(fh), set(pids)):
                    payloads[pid].append(data)
            for pid, lst in payloads.items():
                sets = ig.display_sets(lst)
                if len({s.ics.signature() for s in sets}) > 1:
                    self.report.note(f"clip {clip} IG pid 0x{pid:x}: several different compositions, kept the first")
                if sets:
                    out[pid] = sets[0]
        self.ig[clip] = out
        return out


def _menu(key: str, ds: ig.DisplaySet, pack_dir: Path, pngs: dict[str, str | None]) -> dict[str, Any]:
    ics = ds.ics
    if ics.popup:
        return {"model": "popup", "omitted": True}

    def img(oid: int, pal: int) -> str | None:
        name = f"{key}.o{oid}.p{pal}.png"
        if name not in pngs:
            pngs[name] = name if images.save_ig_object(ds, oid, pal, pack_dir / name) else None
        return pngs[name]

    def frames(state: ig.ButtonState, pal: int) -> list[str]:
        return [f for f in (img(i, pal) for i in state.object_ids()) if f]

    pages = []
    for page in ics.pages:
        pal = page.palette
        bogs = []
        for g in page.bogs:
            buttons = []
            for bt in g.buttons:
                buttons.append({
                    "id": bt.id,
                    "numeric": ig.no_id(bt.numeric),
                    "x": bt.x,
                    "y": bt.y,
                    "auto": bt.auto_action,
                    "nav": bt.nav,
                    "images": {
                        "normal": frames(bt.normal, pal),
                        "selected": frames(bt.selected, pal),
                        "activated": frames(bt.activated, pal),
                    },
                    "sounds": bt.selected.sound != 0xFF or bt.activated.sound != 0xFF,
                    "cmds": [c.hex for c in bt.cmds],
                    "txt": [c.text for c in bt.cmds],
                })  # fmt: skip
            bogs.append({"default": ig.no_id(g.default_valid), "buttons": buttons})
        pages.append({
            "id": page.id,
            "default_selected": ig.no_id(page.default_selected),
            "default_activated": ig.no_id(page.default_activated),
            "effects": bool(page.in_effects.effects or page.out_effects.effects),
            "bogs": bogs,
        })  # fmt: skip
    return {
        "model": "always_on",
        "stream_model": "preloaded" if ics.preloaded else "multiplexed",
        "size": [ics.width, ics.height],
        "timeout": {"composition": None, "selection": None, "user": _ticks90(ics.user_timeout)},
        "pages": pages,
    }


def _collapse(items: tuple[hdmv.PlayItem, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        last = out[-1] if out else None
        if last and (last["clip"], last["in"], last["out"]) == (it.clip, it.in_time, it.out_time):
            last["repeat"] = last.get("repeat", 1) + 1
        else:
            out.append({"clip": it.clip, "in": it.in_time, "out": it.out_time})
    return out


def _streams(stn: dict[str, tuple[hdmv.StreamEntry, ...]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "audio": [
            {"n": e.n, "lang3": e.lang3, "codec": e.codec, "channels": hdmv.CHANNELS.get(e.audio_format, 0)}
            for e in stn.get("audio", ())
        ],
        "pg": [{"n": e.n, "lang3": e.lang3} for e in stn.get("pg", ()) if e.coding == hdmv.PG_CODING],
        "ig": [{"n": e.n, "lang3": e.lang3} for e in stn.get("ig", ())],
    }


def _sidecar_counts(item: LibraryItem) -> dict[str, int]:
    """Shown events of each `.sup` sidecar beside an encode, by suffix (`.eng.sup`, `.eng.2.sup`)."""
    stem = item.file.with_suffix("").name
    out: dict[str, int] = {}
    folder = item.file.parent
    if not folder.is_dir():
        return out
    for s in sorted(folder.iterdir()):
        if s.name.startswith(stem + ".") and s.name.endswith(".sup"):
            with s.open("rb") as f:
                out[s.name[len(stem) :]] = pg.count_sup(f)
    return out


def _cells(
    name: str,
    pl: hdmv.Playlist,
    media: MediaMap,
    disc: DiscInfo,
    tools: MediaTools | None,
    lengths: dict[Path, float],
    report: BuildReport,
) -> dict[str, tuple[dict[str, Any], LibraryItem]]:
    spans: list[tuple[int, int, LibraryItem]] = []
    whole = media.playlists.get(name)
    if whole:
        spans.append((0, pl.length, whole))
    else:
        starts = pl.starts
        for i, it in enumerate(pl.items):
            m = media.clips.get(it.clip)
            if not m:
                continue
            start, dur = starts[i], it.length
            if spans and spans[-1][0] + spans[-1][1] == start and spans[-1][2].key(disc.season) == m.key(disc.season):
                spans[-1] = (spans[-1][0], spans[-1][1] + dur, m)
            else:
                spans.append((start, dur, m))
    cells: dict[str, tuple[dict[str, Any], LibraryItem]] = {}
    for start, dur, m in spans:
        if not m.file.exists():
            report.note(f"playlist {name} at {start}: {m.file.name} is missing, cell left out")
            continue
        secs = dur / hdmv.TICKS_PER_SECOND
        cell = {**m.cell(disc.season), "offset": 0.0, "dur": round(secs, 3), "file": m.file.name}
        cells[str(start)] = (cell, m)
        if tools is not None:
            if m.file not in lengths:
                try:
                    lengths[m.file] = tools.duration(m.file)
                except RuntimeError as e:
                    report.note(f"{m.file.name}: length unreadable ({e})")
                    lengths[m.file] = secs
            if abs(lengths[m.file] - secs) > _LENGTH_SLACK_SECONDS:
                report.note(f"playlist {name} at {start}: cell {secs:.2f}s but {m.file.name} is {lengths[m.file]:.2f}s")
    return cells


def _pg_counts(clips: _Clips, counts: dict[str, dict[str, int]], clip: str, pids: list[int]) -> dict[int, int] | None:
    if clip in counts:
        return {int(k): v for k, v in counts[clip].items()}
    f = clips.stream_file(clip)
    if f is None or not f.name.lower().endswith(f"{clip.lower()}.m2ts"):
        return None
    payloads: dict[int, list[bytes]] = {p: [] for p in pids}
    with f.open("rb") as fh:
        for pid, _pts, data in ts.pes_payloads(ts.packets(fh), set(pids)):
            payloads[pid].append(data)
    return {p: pg.count_payloads(v) for p, v in payloads.items()}


def build_hdmv_pack(
    bdmv: Path,
    media: MediaMap,
    disc: DiscInfo,
    pack_dir: Path,
    tools: MediaTools | None,
    *,
    backgrounds: bool = True,
) -> BuildReport:
    """Write an `hdmv-1` pack into `pack_dir` (made if missing; existing files are left for the caller to swap).
    `tools` None skips every ffmpeg step: no length checks and no backgrounds."""
    report = BuildReport(SCHEMA)
    index_file = _find(bdmv, "index.bdmv")
    if index_file is None:
        raise PackRefused("the menu copy has no index.bdmv")
    index = hdmv.parse_index(index_file.read_bytes())
    for what, o in (("first play", index.first_play), ("top menu", index.top_menu)):
        if o.is_bdj:
            raise PackRefused(f"the {what} is BD-J: Java menus can't be played")
    mobj_file = _find(bdmv, "MovieObject.bdmv")
    if mobj_file is None:
        raise PackRefused("the menu copy has no MovieObject.bdmv")
    mobj = hdmv.parse_movie_objects(mobj_file.read_bytes())
    pack_dir.mkdir(parents=True, exist_ok=True)

    playlists: dict[str, hdmv.Playlist] = {}
    for f in sorted((bdmv / "PLAYLIST").iterdir()) if (bdmv / "PLAYLIST").is_dir() else []:
        if f.name.lower().endswith(".mpls"):
            try:
                playlists[f.name[:5]] = hdmv.parse_playlist(f.read_bytes())
            except MenuParseError as err:
                report.note(f"playlist {f.name}: unreadable ({err})")

    pack: dict[str, Any] = {
        "schema": SCHEMA,
        "disc": {
            "type": "bd",
            "name": disc.name,
            **({"season": disc.season} if disc.season is not None else {}),
            **({"job": disc.job} if disc.job else {}),
            "size": None,
            "menu_languages": [],
        },
        "psr": {str(k): v for k, v in sorted(disc.psr.items())},
        "index": {
            "first_play": index.first_play.object_id,
            "top_menu": index.top_menu.object_id,
            "titles": [
                {"n": i, "bdj": True} if t.is_bdj else {"n": i, "object": t.object_id}
                for i, t in enumerate(index.titles, 1)
            ],
        },
        "objects": {
            str(i): {
                "resume": m.resume,
                "menu_call_mask": m.menu_call_mask,
                "title_search_mask": m.title_search_mask,
                "cmds": [c.hex for c in m.cmds],
                "txt": [c.text for c in m.cmds],
            }
            for i, m in enumerate(mobj)
        },
        "playlists": {},
        "menus": {},
        "media": {},
    }
    clips = _Clips(bdmv, report)
    pngs: dict[str, str | None] = {}
    bg_clips: dict[str, str] = {}
    for name, pl in playlists.items():
        menus: dict[str, str] = {}
        for e in pl.streams.get("ig", ()):
            clip: str | None
            if e.ref == "item":
                clip = pl.items[0].clip  # streams come from a play item's STN table, so there is one
            else:
                sp = pl.subpaths[e.subpath] if e.subpath < len(pl.subpaths) else None
                clip = sp.clips[e.subclip] if sp and e.subclip < len(sp.clips) else None
            if clip is None:
                report.note(f"playlist {name}: IG stream {e.n} points at no clip")
                continue
            pids = clips.ig_pids(clip)
            if e.pid not in pids:
                report.note(f"playlist {name}: IG stream {e.n} pid 0x{e.pid:x} isn't in clip {clip}")
                continue
            key = f"{clip}.ig{pids.index(e.pid) + 1}"
            menus[str(e.n)] = key
            if key in pack["menus"]:
                continue
            ds = clips.display_sets(clip).get(e.pid)
            if ds is None:
                report.note(f"menu {key}: no display set found")
                continue
            m = _menu(key, ds, pack_dir, pngs)
            pack["menus"][key] = {"clip": clip, "pid": e.pid, **m}
            if m["model"] == "always_on":
                pack["disc"]["size"] = pack["disc"]["size"] or m["size"]
                if e.lang3 and e.lang3 not in pack["disc"]["menu_languages"]:
                    pack["disc"]["menu_languages"].append(e.lang3)
        entry: dict[str, Any] = {
            "items": _collapse(pl.items),
            "marks": [{"type": m.type, "item": m.item, "time": m.time, "at": m.at} for m in pl.marks],
            "streams": _streams(pl.streams),
            "menus": menus,
        }
        if pl.items and any(pack["menus"].get(k, {}).get("model") == "always_on" for k in menus.values()):
            bg_clips[name] = pl.items[0].clip
        pack["playlists"][name] = entry
    report.menus = sorted(k for k, v in pack["menus"].items() if v.get("model") == "always_on")

    # Media cells
    lengths: dict[Path, float] = {}
    cell_items: dict[str, dict[str, tuple[dict[str, Any], LibraryItem]]] = {}
    for name, pl in playlists.items():
        cells = _cells(name, pl, media, disc, tools, lengths, report)
        if cells:
            cell_items[name] = cells
            pack["media"][name] = {"cells": {k: c for k, (c, _m) in cells.items()}}
            report.cells += len(cells)

    _pair_subtitles(bdmv, pack, playlists, cell_items, clips, report)

    # Backgrounds: the menu playlist's first clip, once
    for name, clip in bg_clips.items():
        mp4, jpg = f"bg-{clip}.mp4", f"bg-{clip}.jpg"
        src = clips.stream_file(clip)
        if src is None or not src.name.lower().endswith(f"{clip.lower()}.m2ts"):
            report.note(f"playlist {name}: background clip {clip} isn't in the menu copy")
            continue
        pack["playlists"][name]["background"] = {"video": mp4, "still": jpg}
        if not backgrounds or tools is None or (pack_dir / mp4).exists():
            continue
        it = playlists[name].items[0]
        at = disc.still_at.get(clip, round(it.length / hdmv.TICKS_PER_SECOND / 2, 2))
        try:
            tools.video(src, pack_dir / mp4, BD_BACKGROUND_SIZE, audio=True)
            tools.still(src, pack_dir / jpg, BD_BACKGROUND_SIZE, at)
        except RuntimeError as e:
            report.note(f"playlist {name}: background failed ({e})")
            del pack["playlists"][name]["background"]
            for out in (mp4, jpg):
                (pack_dir / out).unlink(missing_ok=True)

    pack["disc"]["size"] = pack["disc"]["size"] or [1920, 1080]
    write_pack(pack_dir, pack, report)
    return report


def _pair_subtitles(
    bdmv: Path,
    pack: dict[str, Any],
    playlists: dict[str, hdmv.Playlist],
    cell_items: dict[str, dict[str, tuple[dict[str, Any], LibraryItem]]],
    clips: _Clips,
    report: BuildReport,
) -> None:
    counts_file = _find(bdmv.parent, COUNTS)
    counts: dict[str, dict[str, int]] = {}
    if counts_file is not None:
        try:
            counts = json.loads(counts_file.read_text()).get("bd", {})
        except (OSError, ValueError) as e:
            report.note(f"{COUNTS} unreadable ({e})")
    pg_pids: dict[str, list[tuple[int, int, str]]] = {}  # playlist -> [(n, pid, lang3)]
    for name, pl in playlists.items():
        pg_pids[name] = [(e.n, e.pid, e.lang3) for e in pl.streams.get("pg", ()) if e.coding == hdmv.PG_CODING]
    paired: dict[str, dict[int, dict[str, Any]]] = {}
    for name, cells in cell_items.items():
        streams = pg_pids[name]
        if not streams:
            continue
        pl = playlists[name]
        start, (_cell, item) = next(iter(cells.items()))
        starts = pl.starts
        idx = max(i for i, s in enumerate(starts) if s <= int(start))
        clip = pl.items[idx].clip
        side = _sidecar_counts(item)
        if not side:
            report.note(
                f"playlist {name}: {item.file.name} has no .sup sidecars; the player picks subtitles by language"
            )
            paired[name] = {}
            continue
        disc_counts = _pg_counts(clips, counts, clip, [pid for _n, pid, _l in streams])
        if disc_counts is None:
            continue
        pairs: dict[int, dict[str, Any]] = {}
        for lang in {lang for _n, _p, lang in streams}:
            bd = sorted((disc_counts.get(pid, 0), n) for n, pid, lg in streams if lg == lang)
            sv = sorted((c, suf) for suf, c in side.items() if suf.startswith(f".{lang}."))
            for (bc, n), (sc, suf) in zip(bd, sv, strict=False):
                pairs[n] = {"sidecar": suf, "bd_events": bc, "sidecar_events": sc}
                if bc != sc:
                    report.note(f"playlist {name}: PG stream {n} has {bc} events, {suf} {sc}")
        paired[name] = pairs
    layouts: dict[tuple[tuple[int, str], ...], dict[int, dict[str, Any]]] = {}
    for name, pairs in paired.items():
        if pairs:
            layouts.setdefault(tuple((pid, lg) for _n, pid, lg in pg_pids[name]), pairs)
    for name in cell_items:
        if pg_pids[name] and name not in paired:
            same = layouts.get(tuple((pid, lg) for _n, pid, lg in pg_pids[name]))
            if same:
                paired[name] = {n: {"sidecar": v["sidecar"]} for n, v in same.items()}
                report.note(
                    f"playlist {name}: no counts for its clip; sidecars paired by a playlist with the same PG streams"
                )
            else:
                report.note(f"playlist {name}: PG streams not paired")
    for name, entry in pack["playlists"].items():
        for s in entry["streams"]["pg"]:
            s.update(paired.get(name, {}).get(s["n"], {}))
