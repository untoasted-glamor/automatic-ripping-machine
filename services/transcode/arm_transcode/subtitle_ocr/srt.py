"""Write subtitle cues as SubRip (`.srt`): UTF-8, no BOM, one cue per event.

Cues may overlap (a sign at the top while dialogue changes at the bottom). A cue
from the top of the screen starts with `{\\an8}` so players that honour it draw
it at the top; italic lines are wrapped in `<i>…</i>` one line at a time.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str  # lines joined by "\n"
    top: bool = False
    italic: bool = False


def timestamp(ms: int) -> str:
    ms = max(0, ms)
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def cue_text(cue: Cue) -> str:
    lines = [line for line in cue.text.split("\n") if line]
    if cue.italic:
        lines = [f"<i>{line}</i>" for line in lines]
    if cue.top and lines:
        lines[0] = "{\\an8}" + lines[0]
    return "\n".join(lines)


def render(cues: Iterable[Cue]) -> str:
    blocks: list[str] = []
    for cue in cues:
        text = cue_text(cue)
        if not text:
            continue
        blocks.append(f"{len(blocks) + 1}\n{timestamp(cue.start_ms)} --> {timestamp(cue.end_ms)}\n{text}\n")
    return "\n".join(blocks)
