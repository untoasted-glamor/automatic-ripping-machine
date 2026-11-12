"""Read the text of subtitle areas with the PP-OCRv6 text-line model on ONNX Runtime.

The model reads one text line per image, so each area is cut into lines at its
empty rows first. The preprocessing (white border, 48 px high, the batching by
width) and the CTC decode with the lowest character score follow pgsrip 0.3.0's
RapidOCR engine and RapidOCR 3.9.2's `TextRecognizer` (both MIT / Apache-2.0), so
the text matches what the 2026-11-07 accuracy test measured. Neither package is
used: the model carries its own character list, and needs only onnxruntime.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from arm_common.subtitle_ocr import BUNDLED_MODEL

#: env override of the model file, for checks outside the image
MODEL_ENV = "ARM_SUBTITLE_OCR_MODEL"
#: an area whose lowest character score is below this is doubtful
DOUBTFUL_BELOW = 0.90
#: a band of ink lower than this share of the tallest band joins its neighbour (umlaut dots, a line of dots)
MIN_LINE_SHARE = 0.4
#: white margin around each line before scaling to the model's height
BORDER = 4
#: lines in one model call
BATCH = 6
#: the model's input: 3 channels, 48 px high, at least 320 px wide
REC_SHAPE = (3, 48, 320)

Bitmap = npt.NDArray[np.uint8]


@dataclass(frozen=True)
class Reading:
    text: str  # lines joined by "\n"; empty when nothing was read
    score: float  # the lowest character score, 0-1; 0 when nothing was read
    doubtful: bool


class LineReader(Protocol):
    def read(self, lines: Sequence[Bitmap]) -> list[tuple[str, float]]:
        """The text and the lowest character score of each line image (0 ink, 255 background)."""
        ...


def model_path() -> Path:
    return Path(os.environ.get(MODEL_ENV) or BUNDLED_MODEL.path)


def split_lines(bitmap: Bitmap) -> list[Bitmap]:
    """Cut an area's bitmap at its empty rows: one image per text line.

    A part too low to be a line (the dots of an umlaut, a line of dots) goes
    with the part below it, or with the line above when it is the last part.
    """
    return [bitmap[top:bottom] for top, bottom in line_bounds(bitmap)]


def line_bounds(bitmap: Bitmap) -> list[tuple[int, int]]:
    """The (top, bottom) rows of each text line `split_lines` cuts, so a
    caller can cut another copy of the image (an anti-aliased one) at the
    same rows."""
    parts: list[list[int]] = []
    for row in np.flatnonzero((bitmap == 0).any(axis=1)).tolist():
        if parts and parts[-1][1] == row:
            parts[-1][1] = row + 1
        else:
            parts.append([row, row + 1])

    tallest = max((end - start for start, end in parts), default=0)
    lines: list[list[int]] = []
    start: int | None = None
    for part_start, part_end in parts:
        start = part_start if start is None else start
        if part_end - part_start >= MIN_LINE_SHARE * tallest:
            lines.append([start, part_end])
            start = None
    if start is not None and lines:
        lines[-1][1] = parts[-1][1]
    return [(top, bottom) for top, bottom in lines]


def add_border(line: Bitmap, border: int = BORDER) -> Bitmap:
    return np.pad(line, border, constant_values=255)


#: fixed-point scale of the resize coefficients (OpenCV's INTER_RESIZE_COEF_SCALE)
_COEF_SCALE = 2048

IntArray = npt.NDArray[np.int64]


def _taps(src: int, dst: int) -> tuple[IntArray, IntArray, IntArray, IntArray]:
    """For each output pixel: the two source pixels and their fixed-point weights."""
    scale = 1.0 / (dst / src)
    f = ((np.arange(dst, dtype=np.float64) + 0.5) * scale - 0.5).astype(np.float32)
    lo = np.floor(f).astype(np.int64)
    frac = (f - np.floor(f)).astype(np.float32)
    below = lo < 0
    frac[below] = 0
    lo[below] = 0
    above = lo >= src - 1
    frac[above] = 0
    lo[above] = src - 1
    hi = np.minimum(lo + 1, src - 1)
    w1 = np.rint(frac * np.float32(_COEF_SCALE)).astype(np.int64)
    w0 = np.rint((np.float32(1) - frac) * np.float32(_COEF_SCALE)).astype(np.int64)
    return lo, hi, w0, w1


def resize_linear(image: Bitmap, width: int, height: int) -> Bitmap:
    """Bilinear resize of an 8-bit image, bit-exact with OpenCV's `cv2.resize` (INTER_LINEAR).

    Pixel-centre alignment, edge clamping, 11-bit fixed-point weights, and the
    rounding of OpenCV's vectorised vertical pass. Bit-exact on every line of the
    ACCA BD E01 test (624 lines); the model's character scores are sensitive
    enough that a float resize moved 3 of 458 areas across the doubtful line.
    """
    src = image.astype(np.int64)
    y0, y1, b0, b1 = _taps(image.shape[0], height)
    x0, x1, a0, a1 = _taps(image.shape[1], width)
    rows = src[:, x0] * a0 + src[:, x1] * a1  # horizontal pass, every source row
    r0, r1 = rows[y0], rows[y1]
    out = (((b0[:, None] * (r0 >> 4)) >> 16) + ((b1[:, None] * (r1 >> 4)) >> 16) + 2) >> 2
    resized: Bitmap = np.clip(out, 0, 255).astype(np.uint8)
    return resized


def normalise(line: Bitmap, max_ratio: float) -> npt.NDArray[np.float32]:
    """One line as the model takes it: [3, 48, 48 * max_ratio], in -1..1, padded with 0 on the right."""
    channels, height, _ = REC_SHAPE
    width = int(height * max_ratio)
    ratio = line.shape[1] / float(line.shape[0])
    resized_w = min(width, int(math.ceil(height * ratio)))
    scaled = (resize_linear(line, resized_w, height).astype(np.float32) / 255 - 0.5) / 0.5
    out = np.zeros((channels, height, width), dtype=np.float32)
    out[:, :, :resized_w] = scaled[None, :, :]
    return out


def ctc_decode(preds: npt.NDArray[np.float32], characters: Sequence[str]) -> tuple[str, float]:
    """Greedy CTC decode of one line: its text, and its lowest character score (spaces don't count)."""
    chars: list[tuple[str, float]] = []
    previous = -1
    for index, score in zip(preds.argmax(axis=1).tolist(), preds.max(axis=1).tolist(), strict=True):
        if index != previous and index != 0:  # 0 is the CTC blank
            chars.append((characters[index], score))
        previous = index
    text = re.sub(" +", " ", "".join(c for c, _ in chars)).strip()
    return text, min((score for c, score in chars if c != " "), default=0.0)


class OnnxLineReader:
    """The PP-OCRv6 text-line model on ONNX Runtime, CPU only. Never downloads anything."""

    def __init__(self, path: Path | None = None, threads: int = 1, batch: int = BATCH) -> None:
        import onnxruntime as ort  # type: ignore[import-untyped]  # ships no type hints

        options = ort.SessionOptions()
        options.log_severity_level = 4
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.path = path or model_path()
        self.session: Any = ort.InferenceSession(
            str(self.path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        metadata = self.session.get_modelmeta().custom_metadata_map
        if "character" not in metadata:
            raise ValueError(f"{self.path.name} has no character list: not a PP-OCR text-line model")
        self.characters = ["blank", *metadata["character"].splitlines(), " "]
        self.input_name = self.session.get_inputs()[0].name
        self.batch = batch

    def read(self, lines: Sequence[Bitmap]) -> list[tuple[str, float]]:
        _, height, width = REC_SHAPE
        ratios = [line.shape[1] / line.shape[0] for line in lines]
        order = np.argsort(ratios)
        results: list[tuple[str, float]] = [("", 0.0)] * len(lines)
        for start in range(0, len(lines), self.batch):
            chunk = order[start : start + self.batch]
            max_ratio = max(width / height, *(ratios[i] for i in chunk))
            data = np.stack([normalise(lines[i], max_ratio) for i in chunk])
            preds = self.session.run(None, {self.input_name: data})[0]
            for row, i in enumerate(chunk):
                results[i] = ctc_decode(preds[row], self.characters)
        return results


def read_areas(reader: LineReader, bitmaps: Sequence[Bitmap]) -> list[Reading]:
    """Read each area's bitmap. All lines go to the reader in one call, so it can batch them."""
    lines_per_area = [[add_border(line) for line in split_lines(bm)] if bm.size else [] for bm in bitmaps]
    results = iter(reader.read([line for lines in lines_per_area for line in lines]))
    readings: list[Reading] = []
    for lines in lines_per_area:
        read = [next(results) for _ in lines]
        text = "\n".join(t for t, _ in read if t)
        if not text:
            readings.append(Reading("", 0.0, True))
            continue
        score = min(s for _, s in read)
        readings.append(Reading(text, score, score < DOUBTFUL_BELOW))
    return readings
