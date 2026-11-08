"""Split each composition into text areas, and follow the areas through time as events.

A sign at the top of the screen and dialogue at the bottom are drawn in one
composition but are separate subtitles: they start and end at different times.
Each composition is cut into horizontal bands separated by a run of empty rows
(the area gap), each band is one area, and an area that stays on screen with the
same pixels across compositions that follow each other without a break is one
event.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
from PIL import Image

from arm_transcode.subtitle_ocr.pgs import Composition

#: a row or column with any pixel more opaque than this has something drawn on it
MIN_ALPHA = 40
#: rows of empty space that separate two areas on a 1080-line screen; scaled to the screen height
AREA_GAP_1080 = 40
#: a composition with no end ends this long after it starts
DEFAULT_DURATION_MS = 5000

TOP = "top"
BOTTOM = "bottom"


@dataclass(frozen=True)
class Area:
    """One text area: its image (RGBA, cropped to what is drawn) and where it is on screen."""

    image: Image.Image
    left: int
    top: int
    position: str  # TOP or BOTTOM
    digest: str  # the same pixels at the same size give the same digest


@dataclass
class Event:
    """One area shown from `start_ms` to `end_ms`."""

    start_ms: int
    end_ms: int
    area: Area


def area_gap(screen_height: int) -> int:
    return max(1, round(AREA_GAP_1080 * screen_height / 1080))


def split(composition: Composition) -> list[Area]:
    """The text areas of one composition, top to bottom."""
    alpha = np.asarray(composition.image)[:, :, 3]
    rows = np.flatnonzero(alpha.max(axis=1) > MIN_ALPHA)
    if not len(rows):
        return []
    gap = area_gap(composition.screen[1])
    bands: list[tuple[int, int]] = []
    start = previous = int(rows[0])
    for row in rows[1:].tolist():
        if row - previous > gap:
            bands.append((start, previous))
            start = row
        previous = row
    bands.append((start, previous))

    left0, top0 = composition.box[0], composition.box[1]
    half = composition.screen[1] / 2
    areas: list[Area] = []
    for y0, y1 in bands:
        cols = np.flatnonzero(alpha[y0 : y1 + 1].max(axis=0) > MIN_ALPHA)
        x0, x1 = int(cols[0]), int(cols[-1])
        image = composition.image.crop((x0, y0, x1 + 1, y1 + 1))
        top = top0 + y0
        centre = top + (y1 - y0 + 1) / 2
        digest = hashlib.sha256(f"{image.width}x{image.height}".encode() + image.tobytes()).hexdigest()
        areas.append(Area(image, left0 + x0, top, TOP if centre < half else BOTTOM, digest))
    return areas


def events(compositions: Iterable[Composition]) -> list[Event]:
    """The events of a track, by start time (top of the screen first on a tie).

    An area continues an event when the same pixels are at the same position in
    the composition that starts exactly when the event's last one ended. After a
    break (nothing shown in between), the same line is a new event.
    """
    done: list[Event] = []
    # (position, digest, n): n tells apart identical areas at one position in one composition
    open_events: dict[tuple[str, str, int], Event] = {}
    for composition in compositions:
        end = composition.end_ms
        if end is None:
            end = composition.start_ms + DEFAULT_DURATION_MS
        still_open: dict[tuple[str, str, int], Event] = {}
        for area in split(composition):
            n = 0
            while (area.position, area.digest, n) in still_open:
                n += 1
            key = (area.position, area.digest, n)
            event = open_events.pop(key, None)
            if event is not None and event.end_ms == composition.start_ms:
                event.end_ms = end
            else:
                if event is not None:
                    done.append(event)
                event = Event(composition.start_ms, end, area)
            still_open[key] = event
        done.extend(open_events.values())
        open_events = still_open
    done.extend(open_events.values())
    done.sort(key=lambda e: (e.start_ms, e.area.position != TOP, e.area.top))
    return done


def on_black(image: Image.Image) -> Image.Image:
    """The area drawn over black, as RGB."""
    background = Image.new("RGBA", image.size, (0, 0, 0, 255))
    background.alpha_composite(image)
    return background.convert("RGB")


#: luminance (BT.601, video range, as PGS palettes store it) from which a pixel is ink
MIN_INK_LUMINANCE = 128


def ink_bitmap(image: Image.Image) -> np.ndarray:
    """The area as the reader takes it: 0 is ink, 255 is background, cropped to the ink.

    The ink is the bright fill of the letters, not their dark outline. An area
    with no ink gives an empty array.
    """
    rgb = np.asarray(on_black(image)).astype(np.float32)
    luma = 16 + (65.481 * rgb[..., 0] + 128.553 * rgb[..., 1] + 24.966 * rgb[..., 2]) / 255
    bitmap = np.where(luma >= MIN_INK_LUMINANCE, 0, 255).astype(np.uint8)
    rows = np.flatnonzero((bitmap == 0).any(axis=1))
    cols = np.flatnonzero((bitmap == 0).any(axis=0))
    if not len(rows):
        return bitmap[:0, :0]
    return bitmap[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1].copy()
