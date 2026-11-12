"""How each transcode task kind is run, in one table.

The dispatcher, the transcoder's `/register`, the Transcoder page's counts and
job delete each treat a task by its kind. They used to ask "subtitle OCR or
not", so any other kind was run as an encode: a preset resolved, the
passthrough path, an encode slot and a GPU. Each now reads its decision from
`TASK_KIND_RULES`, which names every `TranscodeTaskKind` (a test checks it), so
a new kind has to say how every one of those places treats it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from arm_backend.sidecars import TEXT_SUBTITLE_SUFFIX
from arm_common import TranscodeTaskKind


@dataclass(frozen=True)
class TaskKindRules:
    # Writes a video (or audio) file from the raw rip: resolves the session's
    # transcode preset (which may be a passthrough move), takes an encode
    # slot and a GPU, gets the preset and the raw input at `/register`, sets
    # its session application's status, is counted as an encode on the
    # Transcoder page, and job delete removes its output file. Otherwise the
    # task runs in the dispatcher's other slots (`ARM_SUBTITLE_OCR_MAX_PARALLEL`)
    # with no preset and no GPU.
    encodes: bool
    # Its container gets the raw rips folder (`/raw`, read-only unless
    # `raw_writable`).
    mounts_raw: bool
    # Only the sidecars it reports with this suffix are recorded as its own
    # (None: every one next to its output).
    sidecar_suffix: str | None
    # A failed task's files (its sidecars, and those it was to replace) are
    # removed with the job, like a finished one's. An encode's are not: a
    # failed encode wrote nothing that is kept.
    failed_owns_files: bool
    # `/raw` is mounted read-write: the task writes into the job's raw folder
    # (a menu preview, into `<job raw>/menu/preview/`).
    raw_writable: bool = False
    # Which image runs it: the transcode image (`ARM_TRANSCODE_IMAGE`, and its
    # GPU vendor variants for an encode) or the menu preview image
    # (`ARM_MENU_PREVIEW_IMAGE`: Python, the OCR model and Node; no GPU).
    image: Literal["transcode", "menu_preview"] = "transcode"


TASK_KIND_RULES: dict[TranscodeTaskKind, TaskKindRules] = {
    TranscodeTaskKind.ENCODE: TaskKindRules(
        encodes=True, mounts_raw=True, sidecar_suffix=None, failed_owns_files=False
    ),
    TranscodeTaskKind.SUBTITLE_OCR: TaskKindRules(
        encodes=False, mounts_raw=False, sidecar_suffix=TEXT_SUBTITLE_SUFFIX, failed_owns_files=True
    ),
    # Writes only under the job's raw folder, which job delete removes with
    # the raw rip; it records no media files.
    TranscodeTaskKind.MENU_PREVIEW: TaskKindRules(
        encodes=False,
        mounts_raw=True,
        sidecar_suffix=None,
        failed_owns_files=False,
        raw_writable=True,
        image="menu_preview",
    ),
}


def rules_for(kind: TranscodeTaskKind) -> TaskKindRules:
    return TASK_KIND_RULES[kind]
