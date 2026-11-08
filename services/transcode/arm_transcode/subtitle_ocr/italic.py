"""Tell italic subtitles from upright ones by their slant.

No OCR model reports italics, so the slant is measured on the image: each text
line's ink is sheared back by a range of angles, and the angle that makes the
vertical strokes sharpest (the highest variance of the column sums) is that
line's slant. The area's slant is the median over its lines; measuring the lines
apart keeps centred lines of different widths from blurring each other.

Fonts lean by different amounts. Over every text area of ACCA BD E01 and
Hanako-kun BD E01 (1,004 unique), italic lines measure 13 degrees or more on
ACCA and 6.75 or more on Hanako-kun; upright lines measure 2 or less, except one
short word with diagonal letters ("Jeez.") at 4. The cut sits at 5.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from arm_transcode.subtitle_ocr.areas import on_black

#: the angle from which a line is italic
ITALIC_DEGREES = 5.0
#: the angles tried, in degrees
ANGLES = np.arange(-6, 22, 0.5)
#: luminance (0-255) from which a pixel is the letters' fill, not their outline
FILL_LUMINANCE = 160
#: black margin around each line
PAD = 16
#: a run of inked rows shorter than this share of the tallest run is not a text line (dots, stray marks)
MIN_LINE_SHARE = 1 / 3


def _ink_slant(ink: np.ndarray) -> float:
    ink = np.pad(ink, PAD)
    height, width = ink.shape
    rows, cols = np.nonzero(ink)
    offsets = rows - height / 2
    best_angle, best_variance = 0.0, -1.0
    for degrees in ANGLES.tolist():
        shift = np.tan(np.radians(degrees))
        xs = (cols + offsets * shift).round().astype(int)
        # column sums of the sheared ink, over a range wide enough for any shear tried
        columns = np.bincount(xs + height, minlength=width + 2 * height)
        variance = float(columns.var())
        if variance > best_variance:
            best_angle, best_variance = degrees, variance
    return best_angle


def _lines(ink: np.ndarray) -> list[np.ndarray]:
    """The runs of rows that hold ink, the short ones left out."""
    inked = ink.any(axis=1).tolist()
    runs: list[tuple[int, int]] = []
    start = None
    for row, has_ink in enumerate([*inked, False]):
        if has_ink and start is None:
            start = row
        elif not has_ink and start is not None:
            runs.append((start, row))
            start = None
    if not runs:
        return []
    tallest = max(end - begin for begin, end in runs)
    return [ink[begin:end] for begin, end in runs if end - begin >= tallest * MIN_LINE_SHARE]


def slant(image: Image.Image) -> float:
    """The slant of an area's text, in degrees (positive leans right)."""
    ink = np.asarray(on_black(image).convert("L")) > FILL_LUMINANCE
    lines = _lines(ink)
    if not lines:
        return 0.0
    return float(np.median([_ink_slant(line) for line in lines]))


def is_italic(image: Image.Image) -> bool:
    return slant(image) >= ITALIC_DEGREES
