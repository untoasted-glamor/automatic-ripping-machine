"""A disc menu preview: every reachable menu page as an image, each button's label, and what each button plays.

The `menu_preview` task (design `docs/plans/2026-11-10-disc-menus-v5.md`, M1-M2; build plan
`docs/plans/2026-11-12-menu-naming.md`) runs this on the job's menu copy (`<job raw>/menu/`) and writes into
`<job raw>/menu/preview/`:

- `pack/`: a pack built with no media map (`hdmv_pack` or `dvd_pack`), with background stills but no background
  videos, for the walk and the images.
- `walk.json`: the plugin's walk (`menu_core/walk.mjs` under Node): every page reachable from first play and what
  each shown button does, once per menu language.
- `pages/<n>.jpg`: each walked page drawn (the background still with every shown button in its normal state).
- `crops/<n>.b<id>.png`: each visible button as the reader saw it.
- `preview.json` (schema `menu-preview-1`): the pages with their buttons (box, label, score, outcome), and the
  playlists the outcomes name (items and marks, so the backend can match a playlist to the scan's titles).

The backend turns `preview.json` into name proposals (`arm_backend.disc_menus.proposals`); nothing here knows the
job's tracks. A label is read the way B2a tested it (ledger T46): ink is every pixel far enough from the crop's
border colour, kept anti-aliased; the lines are found on a binary copy and cut from the anti-aliased one at the same
rows, and read with the subtitle OCR's text-line model.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from PIL import Image

from arm_transcode.menus.dvd_pack import build_dvd_pack
from arm_transcode.menus.hdmv_pack import PackRefused, build_hdmv_pack
from arm_transcode.menus.media import FfmpegTools, MediaTools
from arm_transcode.menus.model import PACK_FILE, DiscInfo, MediaMap
from arm_transcode.subtitle_ocr.engine import LineReader, OnnxLineReader, add_border, line_bounds

logger = logging.getLogger(__name__)

SCHEMA = "menu-preview-1"
PREVIEW_DIR = "preview"
PREVIEW_FILE = "preview.json"
# Where the image keeps the walk bundle (services/transcode/menu_core/, vendored by sha256).
WALK_BUNDLE = Path(os.environ.get("ARM_MENU_WALK", "/opt/arm/menu_core/walk.mjs"))
NODE = os.environ.get("ARM_NODE", "node")
WALK_TIMEOUT_SECONDS = 15 * 60
# A pixel is ink when its luminance is at least this far from the crop's border median (B2a).
INK_DIFF = 60
PAGE_WIDTH = 1280  # page images are scaled to this width (JPEG)

Progress = Callable[[int, str], None]


class PreviewFailed(RuntimeError):
    pass


@dataclass
class PreviewResult:
    out_dir: Path
    pages: int = 0
    buttons: int = 0
    labels: int = 0
    notes: list[str] = field(default_factory=list)

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.out_dir.rglob("*") if p.is_file())


class _StillsOnly(FfmpegTools):
    """The pack builders' ffmpeg steps without the background videos and audio a preview never plays."""

    def video(self, src: Path, out: Path, size: tuple[int, int], *, audio: bool) -> None:
        return None

    def audio(self, src: Path, out: Path) -> None:
        return None


# ------------------------------------------------------------------------------------------------ labels


def ink(crop: Image.Image) -> npt.NDArray[np.uint8]:
    """0 ink / 255 background, the way the line reader takes it, trimmed to the ink; empty when there is none."""
    g = np.asarray(crop.convert("L")).astype(np.float32)
    if g.size == 0:
        return np.zeros((0, 0), np.uint8)
    border = np.concatenate([g[0], g[-1], g[:, 0], g[:, -1]])
    diff = np.abs(g - np.median(border))
    if diff.max() < INK_DIFF:
        return np.zeros((0, 0), np.uint8)
    out = np.where(diff >= INK_DIFF, 255 - np.clip(diff * 255 / diff.max(), 0, 255), 255).astype(np.uint8)
    ys, xs = np.where(out < 255)
    top, bottom = max(int(ys.min()) - 2, 0), int(ys.max()) + 3
    left, right = max(int(xs.min()) - 2, 0), int(xs.max()) + 3
    return out[top:bottom, left:right]


def read_label(reader: LineReader, crop: Image.Image) -> tuple[str, float]:
    """A button's label: its text lines joined with spaces, and the lowest line score (0 for no text)."""
    bm = ink(crop)
    if not bm.size:
        return "", 0.0
    rows = line_bounds(np.where(bm < 255, 0, 255).astype(np.uint8))
    res = reader.read([add_border(bm[a:b]) for a, b in rows])
    text = " ".join(t for t, _ in res if t).strip()
    return text, min((s for t, s in res if t), default=0.0)


# ------------------------------------------------------------------------------------------------ the walk


def run_walk(pack_file: Path, languages: list[str] | None = None) -> dict[str, Any]:
    """Run the walk bundle under Node on a pack file; its JSON result."""
    if not WALK_BUNDLE.is_file():
        raise PreviewFailed(f"the menu walk isn't in this image ({WALK_BUNDLE})")
    cmd = [NODE, str(WALK_BUNDLE), str(pack_file)]
    if languages:
        cmd += ["--lang", ",".join(languages)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=WALK_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PreviewFailed(f"the menu walk could not run: {e}") from None
    if proc.returncode != 0:
        raise PreviewFailed(f"the menu walk exited rc={proc.returncode}: {proc.stderr[-300:]}")
    try:
        result: dict[str, Any] = json.loads(proc.stdout)
    except ValueError as e:
        raise PreviewFailed(f"the menu walk printed no JSON: {e}") from None
    return result


# ------------------------------------------------------------------------------------------------ pages


def _open_rgba(path: Path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGBA")
    except OSError, ValueError:
        return None


def _hdmv_pages(pack_dir: Path, pack: dict[str, Any], walk: dict[str, Any], out: Path, reader: LineReader,
                result: PreviewResult, progress: Progress) -> list[dict[str, Any]]:  # fmt: skip
    pages: list[dict[str, Any]] = []
    total = max(len(walk["pages"]), 1)
    for n, wp in enumerate(walk["pages"], 1):
        menu = pack["menus"].get(wp["menu"], {})
        w, h = menu.get("size") or pack["disc"].get("size") or [1920, 1080]
        bg = (pack["playlists"].get(wp.get("playlist") or "", {}).get("background") or {}).get("still")
        still = _open_rgba(pack_dir / bg) if bg else None
        base = still.resize((w, h)) if still else Image.new("RGBA", (w, h), (0, 0, 0, 255))
        if still is None:
            result.notes.append(f"{wp['at']}: no background still; buttons drawn over black")
        page_img = base.copy()
        buttons: list[dict[str, Any]] = []
        for b in wp["buttons"]:
            img = _open_rgba(pack_dir / b["image"]) if b.get("image") else None
            box = [b["x"], b["y"], b["x"] + (img.width if img else 0), b["y"] + (img.height if img else 0)]
            entry: dict[str, Any] = {"id": b["id"], "box": box, "visible": img is not None, "outcome": b["outcome"]}
            if img is not None:
                page_img.alpha_composite(img, (b["x"], b["y"]))
                # Each button over the still alone, as B2a read it: overlapping groups don't bleed in.
                one = base.copy()
                one.alpha_composite(img, (b["x"], b["y"]))
                crop = one.crop(tuple(box)).convert("RGB")
                entry["label"], entry["score"] = _read(reader, crop, out, f"{n}.b{b['id']}", entry)
                result.labels += bool(entry["label"])
            buttons.append(entry)
            result.buttons += 1
        pages.append(_page_entry(n, wp, page_img, (w, h), out, buttons))
        progress(30 + 65 * n // total, "reading menu labels")
    return pages


def _dvd_pages(pack_dir: Path, pack: dict[str, Any], walk: dict[str, Any], out: Path, reader: LineReader,
               result: PreviewResult, progress: Progress) -> list[dict[str, Any]]:  # fmt: skip
    pages: list[dict[str, Any]] = []
    total = max(len(walk["pages"]), 1)
    for n, wp in enumerate(walk["pages"], 1):
        first = wp["buttons"][0] if wp["buttons"] else {}
        bg = _open_rgba(pack_dir / first["image"]) if first.get("image") else None
        size = _dvd_menu_size(pack, wp) or ((bg.width, bg.height) if bg else (720, 480))
        if bg is None:
            result.notes.append(f"{wp['at']}: no background image; labels can't be read")
            bg = Image.new("RGBA", size, (0, 0, 0, 255))
        sx, sy = bg.width / size[0], bg.height / size[1]
        page_img = bg.resize(size)
        buttons: list[dict[str, Any]] = []
        for b in wp["buttons"]:
            box = [b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]]
            entry: dict[str, Any] = {"id": b["id"], "box": box, "visible": True, "outcome": b["outcome"]}
            crop = bg.crop((int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy))).convert("RGB")
            entry["label"], entry["score"] = _read(reader, crop, out, f"{n}.b{b['id']}", entry)
            result.labels += bool(entry["label"])
            buttons.append(entry)
            result.buttons += 1
        pages.append(_page_entry(n, wp, page_img, size, out, buttons))
        progress(30 + 65 * n // total, "reading menu labels")
    return pages


def _dvd_menu_size(pack: dict[str, Any], wp: dict[str, Any]) -> tuple[int, int] | None:
    domains = [pack.get("vmg") or {}, *(pack.get("vts") or {}).values()]
    for info in domains:
        for pgcs in info.get("menus", {}).values():
            for pgc in pgcs:
                for cell in pgc.get("cells", []):
                    m = cell.get("menu") or {}
                    bg = (m.get("background") or {}).get("image")
                    if bg and wp["buttons"] and bg == wp["buttons"][0].get("image"):
                        w, h = m["size"]
                        return int(w), int(h)
    return None


def _read(reader: LineReader, crop: Image.Image, out: Path, tag: str, entry: dict[str, Any]) -> tuple[str, float]:
    if crop.width == 0 or crop.height == 0:
        return "", 0.0
    rel = f"crops/{tag}.png"
    crop.save(out / rel)
    entry["crop"] = rel
    text, score = read_label(reader, crop)
    return text, round(score, 3)


def _page_entry(n: int, wp: dict[str, Any], img: Image.Image, size: tuple[int, int], out: Path,
                buttons: list[dict[str, Any]]) -> dict[str, Any]:  # fmt: skip
    rel = f"pages/{n}.jpg"
    scaled = img.convert("RGB")
    if scaled.width > PAGE_WIDTH:
        scaled = scaled.resize((PAGE_WIDTH, round(scaled.height * PAGE_WIDTH / scaled.width)))
    scaled.save(out / rel, quality=85)
    entry: dict[str, Any] = {
        "id": str(n),
        "at": wp["at"],
        "menu": wp["menu"],
        "page": wp["page"],
        "language": wp["language"],
        "path": wp["path"],
        "image": rel,
        "size": list(size),
        "buttons": buttons,
    }
    if wp.get("playlist"):
        entry["playlist"] = wp["playlist"]
    return entry


def _playlists(pack: dict[str, Any], walk: dict[str, Any]) -> dict[str, Any]:
    """The playlists the outcomes name, with their play items and marks."""
    names = {
        o["playlist"]
        for o in [*walk.get("first_play", []), *(b["outcome"] for p in walk["pages"] for b in p["buttons"])]
        if o.get("kind") == "plays"
    }
    out: dict[str, Any] = {}
    for name in sorted(names):
        pl = pack["playlists"].get(name)
        if pl:
            out[name] = {"items": pl["items"], "marks": pl["marks"]}
    return out


# ------------------------------------------------------------------------------------------------ entry


def make_preview(
    menu_dir: Path,
    *,
    reader: LineReader | None = None,
    tools: MediaTools | None = None,
    walker: Callable[[Path, list[str] | None], dict[str, Any]] = run_walk,
    progress: Progress = lambda pct, what: None,
) -> PreviewResult:
    """Write `<menu_dir>/preview/` (replacing an earlier one only once the new one is complete)."""
    if (menu_dir / "BDMV").is_dir():
        kind = "bd"
    elif (menu_dir / "VIDEO_TS").is_dir():
        kind = "dvd"
    else:
        raise PreviewFailed(f"{menu_dir} holds neither BDMV/ nor VIDEO_TS/")
    final = menu_dir / PREVIEW_DIR
    work = menu_dir / f".{PREVIEW_DIR}.arm-inprogress"
    shutil.rmtree(work, ignore_errors=True)
    (work / "pages").mkdir(parents=True)
    (work / "crops").mkdir()
    result = PreviewResult(work)
    try:
        progress(5, "building the menu")
        pack_dir = work / "pack"
        tools = tools if tools is not None else _StillsOnly()
        disc = DiscInfo("menu preview")
        try:
            if kind == "bd":
                report = build_hdmv_pack(menu_dir / "BDMV", MediaMap(), disc, pack_dir, tools)
            else:
                report = build_dvd_pack(menu_dir / "VIDEO_TS", MediaMap(), disc, pack_dir, tools)
        except PackRefused as e:
            raise PreviewFailed(str(e)) from None
        result.notes.extend(report.notes)
        pack = json.loads((pack_dir / PACK_FILE).read_text())
        progress(25, "walking the menus")
        langs = pack.get("disc", {}).get("menu_languages") or None
        walk = walker(pack_dir / PACK_FILE, langs)
        (work / "walk.json").write_text(json.dumps(walk, indent=1))
        reader = reader if reader is not None else OnnxLineReader(threads=1)
        build = _hdmv_pages if kind == "bd" else _dvd_pages
        pages = build(pack_dir, pack, walk, work, reader, result, progress)
        result.pages = len(pages)
        preview = {
            "schema": SCHEMA,
            "disc": kind,
            "walk_version": walk.get("version"),
            "first_play": walk.get("first_play", []),
            "truncated": walk.get("truncated", False),
            "unsupported": walk.get("unsupported", []),
            "notes": result.notes,
            "pages": pages,
            "playlists": _playlists(pack, walk) if kind == "bd" else {},
        }
        (work / PREVIEW_FILE).write_text(json.dumps(preview, indent=1))
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(final, ignore_errors=True)
    work.rename(final)
    result.out_dir = final
    progress(100, "done")
    return result
