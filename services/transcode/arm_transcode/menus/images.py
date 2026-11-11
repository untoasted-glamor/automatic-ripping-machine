"""Button and overlay images for packs (Pillow)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from arm_common.disc_menus import dvd, ig
from PIL import Image

Colors = tuple[tuple[int, ...], tuple[int, ...]]  # (colour indexes, contrasts) for e2, e1, pattern, background


def save_ig_object(ds: ig.DisplaySet, oid: int, palette: int, path: Path) -> bool:
    """One IG object drawn with a palette, as a PNG. False when the display set lacks the object."""
    drawn = ig.object_rgba(ds, oid, palette)
    if drawn is None:
        return False
    w, h, rgba = drawn
    Image.frombytes("RGBA", (w, h), rgba).save(path, optimize=True)
    return True


def ycrcb_601(p: tuple[int, int, int]) -> tuple[int, int, int]:
    """A DVD palette entry (Y, Cr, Cb; BT.601) as RGB."""
    y, cr, cb = p
    r = y + 1.402 * (cr - 128)
    g = y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)
    b = y + 1.772 * (cb - 128)
    return max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))


def spu_image(
    spu: dvd.Spu,
    palette: tuple[tuple[int, int, int], ...],
    size: tuple[int, int],
    colors_for: Callable[[int, int], Colors | None],
    box: tuple[int, int, int, int] | None = None,
) -> Image.Image:
    """A subpicture drawn over a transparent canvas. `colors_for(x, y)` gives a pixel's colours and contrasts
    (a button's highlight), or None for the subpicture's own. `box` (x0, y0, x1, y1, inclusive) crops to a button:
    the image is then the box's size."""
    x0, y0 = (box[0], box[1]) if box else (0, 0)
    w, h = (box[2] - box[0] + 1, box[3] - box[1] + 1) if box else size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    assert px is not None
    rgb = [ycrcb_601(p) for p in palette]
    own: Colors = (spu.colors, spu.contrast)
    ax, ay = spu.area[0], spu.area[1]
    for j, row in enumerate(spu.pixels):
        y = ay + j
        if not y0 <= y < y0 + h:
            continue
        for i, v in enumerate(row):
            x = ax + i
            if not x0 <= x < x0 + w:
                continue
            cols, cons = colors_for(x, y) or own
            a = cons[3 - v]
            if a:
                px[x - x0, y - y0] = (*rgb[cols[3 - v] & 0xF], a * 17)
    return img
