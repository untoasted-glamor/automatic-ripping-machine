"""The text-line model built into the `arm-transcode` image for subtitle OCR.

ARM never downloads a model at run time: the image build fetches this file
(`services/transcode/Dockerfile`, `ADD --checksum`), and the build fails if
its SHA-256 changes. Shared here so the Backend and UI can show what is
bundled without opening the file. Listed in `services/transcode/README.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BundledModel:
    name: str
    version: str
    path: str  # inside the arm-transcode image
    size: int  # bytes
    sha256: str
    licence: str
    source: str


BUNDLED_MODEL = BundledModel(
    name="PP-OCRv6_rec_small",
    version="RapidOCR 3.9.2",
    path="/opt/arm/models/PP-OCRv6_rec_small.onnx",
    size=21_234_383,
    sha256="6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    licence="Apache-2.0",
    source=(
        "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx"
    ),
)
