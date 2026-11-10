"""Text subtitles (`.srt`) read from an encode's image subtitles.

The model is built into the transcode image (`arm_common.subtitle_ocr`).
Settings turn the feature on and set the default for discs; each job can
carry its own choice (`jobs.subtitle_ocr`), and the job page can make text
subtitles for finished encodes after the fact.
"""

from typing import Literal

from pydantic import BaseModel, Field

from arm_common.schemas.sessions import TranscodeTaskView


class SubtitleOcrModelView(BaseModel):
    """The text-line model built into the transcode image."""

    name: str
    version: str
    size: int
    sha256: str
    licence: str
    source: str


class JobSubtitleOcrChoice(BaseModel):
    """A job's text subtitles choice. `effective` is what its encodes get on
    completion: `override` when set, else `default` (the Settings default)."""

    override: bool | None = None
    default: bool
    effective: bool


class JobSubtitleOcrView(JobSubtitleOcrChoice):
    """The job page's text subtitles: the choice, and what Make text
    subtitles would read."""

    # Finished encodes of the job that wrote image subtitles (`.sup`).
    eligible_encodes: int = 0
    # Of those, how many already have a `.srt` beside them on disk.
    with_text_subtitles: int = 0


class SubtitleOcrRequest(BaseModel):
    """POST /api/jobs/{id}/subtitle-ocr."""

    # Replace `.srt` files that already exist. Without it, an encode that
    # already has one is skipped.
    overwrite: bool = False


class SubtitleOcrSkip(BaseModel):
    encode_task_id: str
    output_path: str | None
    # `running`: a text subtitles task for this encode is already queued or
    # running. `exists`: its `.srt` files exist and `overwrite` was not set.
    reason: Literal["running", "exists"]


class SubtitleOcrResponse(BaseModel):
    tasks: list[TranscodeTaskView] = Field(default_factory=list)
    skipped: list[SubtitleOcrSkip] = Field(default_factory=list)
