"""Subtitle sidecar files that a transcode task writes next to its video.

A transcode preset with `subtitle_output` `separate` or `both` makes the
transcoder write each kept subtitle track as its own file in the video's
folder, named `<video stem>.<lang>[.<n>][.forced].<ext>`, and report them at
/complete. The Backend records them on `transcode_tasks.sidecar_paths`
(media-root-relative, like `output_path`) so job delete removes them with the
video, and the startup `.arm-inprogress` sweep leaves a running task's
partial sidecars alone.

An overwrite re-apply evicts the old task but keeps its sidecars: the new
task carries them as `replaced_sidecar_paths` and `drop_replaced_sidecars`
removes them only when it completes, when its video has replaced the old one.
A failed re-encode leaves the old video with its subtitles.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from arm_common.fileops import is_within

logger = logging.getLogger("arm_backend.sidecars")


def is_sidecar_of(output_path: str, candidate: str) -> bool:
    """True when `candidate` names a sidecar of the video at `output_path`:
    same folder, and its name is the video's stem followed by a dot."""
    video = PurePosixPath(output_path)
    other = PurePosixPath(candidate)
    if other.is_absolute() or ".." in other.parts or other == video:
        return False
    return other.parent == video.parent and other.name.startswith(video.stem + ".")


def accepted_sidecars(output_path: str, sidecar_paths: Iterable[str]) -> list[str]:
    """The reported sidecars that really sit next to `output_path`.

    Anything else (another folder, an absolute path, `..`) is dropped with a
    warning, so a recorded sidecar can never point job delete at a file
    outside the video's folder."""
    kept: list[str] = []
    for candidate in sidecar_paths:
        if is_sidecar_of(output_path, candidate):
            if candidate not in kept:
                kept.append(candidate)
        else:
            logger.warning("ignoring reported sidecar %r: not next to output %r", candidate, output_path)
    return kept


def sidecar_files(media_root: Path, sidecar_paths: Iterable[str] | None) -> list[Path]:
    """Absolute paths of recorded sidecars that resolve inside `media_root`."""
    files: list[Path] = []
    for relative in sidecar_paths or ():
        path = media_root / relative
        if is_within(path, media_root):
            files.append(path)
        else:
            logger.warning("ignoring recorded sidecar outside the media root: %r", relative)
    return files


def remove_sidecars(media_root: Path, sidecar_paths: Iterable[str] | None) -> int:
    """Best-effort unlink of recorded sidecars. Returns how many were removed."""
    removed = 0
    for path in sidecar_files(media_root, sidecar_paths):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("could not remove sidecar path=%s err=%s", path, exc)
            continue
        removed += 1
        logger.info("removed sidecar path=%s", path)
    return removed


def drop_replaced_sidecars(media_root: Path, replaced: Iterable[str] | None, written: Iterable[str] | None) -> int:
    """Remove the replaced task's sidecars the completing task did not write
    again (a file it wrote under the same name is its own now)."""
    keep = set(written or ())
    return remove_sidecars(media_root, [p for p in replaced or () if p not in keep])
