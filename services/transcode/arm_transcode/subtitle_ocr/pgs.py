"""Decode a PGS (`.sup`) subtitle stream into the compositions it shows.

A composition is everything on screen from one display set until the next one
changes it: all of its objects drawn with the palette in force, cropped as the
presentation segment says. Unlike a reader that keeps only the first image of
each epoch, every display set that shows something is kept, so dialogue added
under a sign in a later "normal" display set is not lost.

Segment layout: https://blog.thescorpius.com/index.php/2017/07/15/presentation-graphic-stream-sup-files-bluray-subtitle-format/
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from PIL import Image

PCS = 0x16  # presentation composition
WDS = 0x17  # window definition
PDS = 0x14  # palette definition
ODS = 0x15  # object definition
END = 0x80  # end of display set

EPOCH_START = 0x80
FIRST_IN_SEQUENCE = 0x80
LAST_IN_SEQUENCE = 0x40
OBJECT_CROPPED = 0x80

#: a palette entry: (Y, Cr, Cb, alpha)
PaletteEntry = tuple[int, int, int, int]


class PgsError(ValueError):
    """The stream is not PGS, or is cut short."""


@dataclass(frozen=True)
class Composition:
    """One displayed image.

    `end_ms` is None only for a composition the stream never takes down.
    `box` is (left, top, right, bottom) on a screen of `screen` (width, height).
    """

    start_ms: int
    end_ms: int | None
    screen: tuple[int, int]
    box: tuple[int, int, int, int]
    image: Image.Image  # RGBA, the size of `box`


@dataclass(frozen=True)
class _Placed:
    object_id: int
    x: int
    y: int
    crop: tuple[int, int, int, int] | None


@dataclass(frozen=True)
class _Pcs:
    time_ms: float
    screen: tuple[int, int]
    palette_id: int
    objects: tuple[_Placed, ...]


def segments(data: bytes) -> Iterator[tuple[float, int, bytes]]:
    """Each segment as (presentation time in ms, type, payload)."""
    offset = 0
    while offset + 13 <= len(data):
        if data[offset : offset + 2] != b"PG":
            raise PgsError(f"no PGS segment at byte {offset}")
        pts = struct.unpack(">I", data[offset + 2 : offset + 6])[0]
        kind = data[offset + 10]
        size = struct.unpack(">H", data[offset + 11 : offset + 13])[0]
        payload = data[offset + 13 : offset + 13 + size]
        if len(payload) < size:
            raise PgsError(f"segment at byte {offset} is cut short")
        yield pts / 90.0, kind, payload
        offset += 13 + size
    if offset < len(data):
        # A cut-short last header, or not PGS at all: never an empty track.
        raise PgsError(f"{len(data) - offset} stray bytes at byte {offset}")


def decode_rle(data: bytes, width: int, height: int) -> bytes:
    """The palette index of each pixel, row by row."""
    out = bytearray(width * height)
    i = x = y = 0
    n = len(data)
    while i < n and y < height:
        b = data[i]
        i += 1
        if b:
            if x < width:
                out[y * width + x] = b
            x += 1
            continue
        if i >= n:
            break
        b2 = data[i]
        i += 1
        if b2 == 0:
            x = 0
            y += 1
            continue
        flag = b2 >> 6
        if flag == 0:
            run, colour = b2 & 0x3F, 0
        elif flag == 1:
            run, colour = ((b2 & 0x3F) << 8) | data[i], 0
            i += 1
        elif flag == 2:
            run, colour = b2 & 0x3F, data[i]
            i += 1
        else:
            run, colour = ((b2 & 0x3F) << 8) | data[i], data[i + 1]
            i += 2
        end = min(x + run, width)
        if x < width:
            out[y * width + x : y * width + end] = bytes([colour]) * (end - x)
        x += run
    return bytes(out)


def ycbcr_to_rgb(y: int, cr: int, cb: int) -> tuple[int, int, int]:
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return (_clamp(r), _clamp(g), _clamp(b))


def _clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _parse_pcs(time_ms: float, d: bytes) -> tuple[_Pcs, bool]:
    width, height = struct.unpack(">HH", d[0:4])
    state = d[7]
    palette_id = d[9]
    count = d[10]
    placed: list[_Placed] = []
    o = 11
    for _ in range(count):
        object_id = struct.unpack(">H", d[o : o + 2])[0]
        flags = d[o + 3]
        x, y = struct.unpack(">HH", d[o + 4 : o + 8])
        o += 8
        crop: tuple[int, int, int, int] | None = None
        if flags & OBJECT_CROPPED:
            crop = struct.unpack(">HHHH", d[o : o + 8])
            o += 8
        placed.append(_Placed(object_id, x, y, crop))
    return _Pcs(time_ms, (width, height), palette_id, tuple(placed)), bool(state & EPOCH_START)


def _render(
    pcs: _Pcs,
    palettes: dict[int, dict[int, PaletteEntry]],
    objects: dict[int, tuple[int, int, bytes]],
) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
    palette = palettes.get(pcs.palette_id, {})
    lut = bytearray(256 * 4)
    for index, (y, cr, cb, alpha) in palette.items():
        lut[index * 4 : index * 4 + 4] = bytes((*ycbcr_to_rgb(y, cr, cb), alpha))

    drawn: list[tuple[int, int, Image.Image]] = []
    for placed in pcs.objects:
        obj = objects.get(placed.object_id)
        if obj is None:
            continue
        width, height, pixels = obj
        indexed = Image.frombytes("P", (width, height), pixels)
        indexed.putpalette(bytes(lut), rawmode="RGBA")
        image = indexed.convert("RGBA")
        if placed.crop is not None:
            cx, cy, cw, ch = placed.crop
            image = image.crop((cx, cy, cx + cw, cy + ch))
        drawn.append((placed.x, placed.y, image))
    if not drawn:
        return None

    left = min(x for x, _, _ in drawn)
    top = min(y for _, y, _ in drawn)
    right = max(x + im.width for x, _, im in drawn)
    bottom = max(y + im.height for _, y, im in drawn)
    canvas = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    for x, y, image in drawn:
        canvas.alpha_composite(image, (x - left, y - top))
    return canvas, (left, top, right, bottom)


def decode(data: bytes) -> list[Composition]:
    """Every composition the stream shows, in time order."""
    palettes: dict[int, dict[int, PaletteEntry]] = {}
    objects: dict[int, tuple[int, int, bytes]] = {}
    pending: dict[int, tuple[int, int, bytearray]] = {}
    pcs: _Pcs | None = None
    shown: list[Composition] = []
    current: tuple[int, tuple[int, int], tuple[int, int, int, int], Image.Image] | None = None

    for time_ms, kind, d in segments(data):
        if kind == PCS:
            pcs, epoch_start = _parse_pcs(time_ms, d)
            if epoch_start:
                palettes.clear()
                objects.clear()
        elif kind == PDS:
            entries = palettes.setdefault(d[0], {})
            for o in range(2, len(d) - 4, 5):
                index, y, cr, cb, alpha = d[o : o + 5]
                entries[index] = (y, cr, cb, alpha)
        elif kind == ODS:
            object_id = struct.unpack(">H", d[0:2])[0]
            sequence = d[3]
            if sequence & FIRST_IN_SEQUENCE:
                width, height = struct.unpack(">HH", d[7:11])
                pending[object_id] = (width, height, bytearray(d[11:]))
            elif object_id in pending:
                pending[object_id][2].extend(d[4:])
            if sequence & LAST_IN_SEQUENCE and object_id in pending:
                width, height, raw = pending.pop(object_id)
                objects[object_id] = (width, height, decode_rle(bytes(raw), width, height))
        elif kind == END and pcs is not None:
            if current is not None:
                start, screen, box, image = current
                shown.append(Composition(start, round(pcs.time_ms), screen, box, image))
                current = None
            rendered = _render(pcs, palettes, objects)
            if rendered is not None:
                image, box = rendered
                current = (round(pcs.time_ms), pcs.screen, box, image)
            pcs = None

    if current is not None:
        start, screen, box, image = current
        shown.append(Composition(start, None, screen, box, image))
    return shown
