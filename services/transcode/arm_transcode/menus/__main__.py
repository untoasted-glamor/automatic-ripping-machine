"""Build one menu pack by hand: `python -m arm_transcode.menus <menu dir> <map.json> <show folder> <pack dir>`.

`<menu dir>` is a menu copy (`<job raw>/menu/`, holding `BDMV/` or `VIDEO_TS/`). The map names the disc and which
library item each title is (`arm_transcode.menus.model.load_map`); an episode entry with no `file` is found in the
show folder by `Season NN/*SnnEmm*.mkv`. Prints the build report as one JSON line.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any

from arm_transcode.menus.dvd_pack import build_dvd_pack
from arm_transcode.menus.hdmv_pack import PackRefused, build_hdmv_pack
from arm_transcode.menus.media import FfmpegTools
from arm_transcode.menus.model import load_map


def _resolve(entry: dict[str, Any], show: Path, season: int | None) -> dict[str, Any]:
    if entry.get("file") or entry.get("extra") or entry.get("episode") is None:
        return entry
    s = entry.get("season", season) or 0
    pattern = str(Path(glob.escape(str(show))) / f"Season {s:02d}" / f"*S{s:02d}E{entry['episode']:02d}*.mkv")
    hits = sorted(glob.glob(pattern))
    return (
        {**entry, "file": str(Path(hits[0]).resolve())}
        if hits
        else {**entry, "file": f"Season {s:02d}/missing S{s:02d}E{entry['episode']:02d}.mkv"}
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m arm_transcode.menus")
    ap.add_argument("menu")
    ap.add_argument("map")
    ap.add_argument("show")
    ap.add_argument("pack")
    ap.add_argument("--no-ffmpeg", action="store_true", help="no length checks and no backgrounds")
    a = ap.parse_args(argv)
    menu, show, pack = Path(a.menu), Path(a.show), Path(a.pack)
    raw = json.loads(Path(a.map).read_text())
    season = raw.get("season")
    for k in ("clips", "playlists"):
        raw[k] = {n: _resolve(e, show, season) for n, e in raw.get(k, {}).items()}
    raw["titles"] = {n: [_resolve(e, show, season) for e in spans] for n, spans in raw.get("titles", {}).items()}
    disc, media = load_map(raw, show)
    tools = None if a.no_ffmpeg else FfmpegTools()
    try:
        if (menu / "BDMV").is_dir():
            report = build_hdmv_pack(menu / "BDMV", media, disc, pack, tools)
        elif (menu / "VIDEO_TS").is_dir():
            report = build_dvd_pack(menu / "VIDEO_TS", media, disc, pack, tools)
        else:
            print(json.dumps({"error": f"{menu} holds neither BDMV/ nor VIDEO_TS/"}))
            return 2
    except PackRefused as e:
        print(json.dumps({"error": str(e)}))
        return 3
    print(json.dumps(report.as_json()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
