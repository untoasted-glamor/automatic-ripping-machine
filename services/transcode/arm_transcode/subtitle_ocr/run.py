"""One image subtitle sidecar in, one `.srt` beside it out."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from arm_common.fileops import atomic_output

from arm_transcode.subtitle_ocr import areas, engine, italic, pgs, srt


@dataclass(frozen=True)
class ReadEvent:
    event: areas.Event
    reading: engine.Reading
    italic: bool


@dataclass(frozen=True)
class Stats:
    compositions: int
    events: int
    unique_areas: int
    doubtful: int  # unique areas
    italic: int  # unique areas
    unread: int  # unique areas with ink but no text
    cues: int
    seconds: float


def srt_path_for(sidecar: Path) -> Path:
    """`….eng.2.sup` gives `….eng.2.srt`."""
    return sidecar.with_suffix(".srt")


def read_track(data: bytes, reader: engine.LineReader) -> tuple[list[pgs.Composition], list[ReadEvent]]:
    """Decode a PGS track and read each of its events. Identical areas are read once."""
    compositions = pgs.decode(data)
    track_events = areas.events(compositions)
    unique: dict[str, areas.Area] = {}
    for event in track_events:
        unique.setdefault(event.area.digest, event.area)
    digests = list(unique)
    readings = engine.read_areas(reader, [areas.ink_bitmap(unique[d].image) for d in digests])
    by_digest = {d: (r, italic.is_italic(unique[d].image)) for d, r in zip(digests, readings, strict=True)}
    read_events = [ReadEvent(e, *by_digest[e.area.digest]) for e in track_events]
    return compositions, read_events


def cues_of(read_events: list[ReadEvent]) -> list[srt.Cue]:
    return [
        srt.Cue(
            start_ms=r.event.start_ms,
            end_ms=r.event.end_ms,
            text=r.reading.text,
            top=r.event.area.position == areas.TOP,
            italic=r.italic,
        )
        for r in read_events
        if r.reading.text
    ]


def convert(sidecar: Path, reader: engine.LineReader, output: Path | None = None) -> tuple[Path, Stats]:
    """Write the `.srt` of one PGS sidecar (through `*.arm-inprogress`) and return its path and stats."""
    started = time.monotonic()
    compositions, read_events = read_track(sidecar.read_bytes(), reader)
    cues = cues_of(read_events)
    target = output or srt_path_for(sidecar)
    with atomic_output(target) as tmp:
        tmp.write_text(srt.render(cues), encoding="utf-8")

    unique: dict[str, ReadEvent] = {}
    for r in read_events:
        unique.setdefault(r.event.area.digest, r)
    stats = Stats(
        compositions=len(compositions),
        events=len(read_events),
        unique_areas=len(unique),
        doubtful=sum(1 for r in unique.values() if r.reading.doubtful and r.reading.text),
        italic=sum(1 for r in unique.values() if r.italic),
        unread=sum(1 for r in unique.values() if not r.reading.text),
        cues=len(cues),
        seconds=round(time.monotonic() - started, 1),
    )
    return target, stats
