"""The ffmpeg and ffprobe work of a pack build, behind one seam so the builders test without either.

Backgrounds are made for browsers: H.264 (high, level 4.0, yuv420p) and stereo AAC in an MP4 with `faststart`, a
JPEG or PNG still, deinterlaced with `bwdif` (menus are interlaced on both disc types).
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30 * 60
_DEINT = "bwdif=mode=send_frame:deint=interlaced"
_X264 = ["-c:v", "libx264", "-preset", "slow", "-crf", "20", "-profile:v", "high", "-level", "4.0",
         "-pix_fmt", "yuv420p", "-g", "48"]  # fmt: skip
_AAC = ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
BD_BACKGROUND_SIZE = (1280, 720)


class MediaTools(Protocol):
    """Every method raises RuntimeError when the tool fails; the builders turn that into a note."""

    def duration(self, path: Path) -> float: ...

    def chapters(self, path: Path) -> list[float]: ...

    def video(self, src: Path, out: Path, size: tuple[int, int], *, audio: bool) -> None: ...

    def still(self, src: Path, out: Path, size: tuple[int, int], at: float) -> None: ...

    def audio(self, src: Path, out: Path) -> None: ...


class FfmpegTools:
    """MediaTools on the transcoder image's ffmpeg and ffprobe."""

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe

    def _probe(self, path: Path, *args: str) -> dict[str, object]:
        proc = subprocess.run(
            [self.ffprobe, "-v", "error", *args, "-of", "json", str(path)],
            capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
        )  # fmt: skip
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe exited rc={proc.returncode}: {proc.stderr[-300:]}")
        try:
            data: dict[str, object] = json.loads(proc.stdout)
        except ValueError as e:
            raise RuntimeError(f"ffprobe printed no JSON: {e}") from None
        return data

    def _ffmpeg(self, *args: str) -> None:
        proc = subprocess.run(
            [self.ffmpeg, "-nostdin", "-v", "error", "-y", *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg exited rc={proc.returncode}: {proc.stderr[-500:]}")

    def duration(self, path: Path) -> float:
        fmt = self._probe(path, "-show_entries", "format=duration").get("format")
        try:
            return float(fmt["duration"]) if isinstance(fmt, dict) else 0.0
        except KeyError, ValueError:
            raise RuntimeError(f"ffprobe gave no duration for {path.name}") from None

    def chapters(self, path: Path) -> list[float]:
        chs = self._probe(path, "-show_chapters").get("chapters")
        return [float(c["start_time"]) for c in chs] if isinstance(chs, list) else []

    def video(self, src: Path, out: Path, size: tuple[int, int], *, audio: bool) -> None:
        a = ["-map", "0:a:0?", *_AAC] if audio else ["-an"]
        vf = f"{_DEINT},scale={size[0]}:{size[1]},setsar=1"
        self._ffmpeg("-i", str(src), "-map", "0:v:0", *a, "-vf", vf, *_X264, "-movflags", "+faststart", str(out))

    def still(self, src: Path, out: Path, size: tuple[int, int], at: float) -> None:
        vf = f"{_DEINT},scale={size[0]}:{size[1]},setsar=1"
        q = ["-q:v", "3"] if out.suffix.lower() in (".jpg", ".jpeg") else []
        self._ffmpeg("-ss", f"{at:.3f}", "-i", str(src), "-frames:v", "1", "-vf", vf, *q, str(out))

    def audio(self, src: Path, out: Path) -> None:
        self._ffmpeg("-i", str(src), "-map", "0:a:0", "-vn", *_AAC, "-movflags", "+faststart", str(out))
