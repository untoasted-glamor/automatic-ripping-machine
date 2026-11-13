"""Phase 7 wire schemas: transcoder ↔ Backend.

The transcode container is short-lived and single-purpose; its API surface is
five endpoints (register / claim / heartbeat / complete / fail). The Backend
spawns one container per `transcode_tasks` row, the container runs the
encoder, and these schemas carry the state-machine transitions.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arm_common.enums import GpuStatus, GpuVendor, TranscodeTaskKind
from arm_common.schemas.jobs import TrackView
from arm_common.schemas.sessions import (
    SessionView,
    TranscodePresetView,
    TranscodeTaskView,
)


class HardwareCaps(BaseModel):
    """Self-reported transcoder capabilities. Phase 7 (CPU-only) ignores these;
    Phase 7b consumes them for GPU dispatch + `gpus` row writes.
    """

    cpu_count: int = Field(ge=1)
    has_vaapi: bool = False
    has_nvenc: bool = False
    has_qsv: bool = False


class RegisterTranscoderRequest(BaseModel):
    task_id: str
    hostname: str
    hw_caps: HardwareCaps


class MenuPackItem(BaseModel):
    """One library file a disc menu pack's cells play, and which disc title it is (fork only, disc menus A3).

    `file` is relative to the media root. It is an episode (`episode`, with `season` when it isn't the disc's: 0
    for a special), an extra (`extra`, its path under the show folder: `trailers/...`, `other/...`, `extras/...`)
    or, on a movie disc, neither. The disc title is a Blu-ray clip (`bd_clip`, a title of one clip: every play of
    it, the episode buttons' marks into a play-all included), a Blu-ray playlist (`bd_playlist`, a title of
    several clips) or a DVD title number (`dvd_title`, the whole title).
    """

    file: str
    episode: int | None = None
    season: int | None = None
    extra: str | None = None
    bd_clip: str | None = None
    bd_playlist: str | None = None
    dvd_title: int | None = None


class MenuPackPlan(BaseModel):
    """What a `menu_pack` task builds (fork only, disc menus A3): `pack_dir` (relative to the media root,
    `<show folder>/.discmenus/<disc>`) unless another job's pack holds it, the disc's name and season, the job
    that owns the pack, and the library items. `notes` says what the backend left out of the map, and why."""

    pack_dir: str
    name: str
    season: int | None = None
    job_id: str
    items: list[MenuPackItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class RegisterTranscoderResponse(BaseModel):
    """Bootstrap bundle the transcoder needs before it can claim + run.

    For an `encode` task, `raw_input_path` is the absolute path the
    transcoder should read from (`/raw/<job>/title_tNN.mkv` for video,
    `/raw/<job>/track_NN.wav` for audio, `/raw/<job>/dump.iso` for data).
    `media_root` plus the task's `output_path` is where the final file lands.

    A `subtitle_ocr` task has no raw input and no preset: it reads
    `input_sidecar_paths` (the image subtitle sidecars of its parent encode
    task, relative to `media_root`) and writes a `.srt` beside each.

    A `menu_pack` task's `raw_input_path` is the job's copied disc menu, and
    `menu_pack` says what to build from it.
    """

    task: TranscodeTaskView
    session: SessionView
    transcode_preset: TranscodePresetView | None
    source_track: TrackView
    raw_input_path: str | None
    media_root: str
    input_sidecar_paths: list[str] = Field(default_factory=list)
    # A `menu_pack` task's build (its `raw_input_path` is the menu copy).
    menu_pack: MenuPackPlan | None = None


class ClaimTaskResponse(BaseModel):
    task: TranscodeTaskView


class HeartbeatRequest(BaseModel):
    progress_pct: int = Field(ge=0, le=100)
    current_pass: str | None = None
    eta_seconds: int | None = Field(default=None, ge=0)


class CompleteTaskRequest(BaseModel):
    output_path: str
    # Subtitle files written next to the video, relative to the media root
    # like `output_path`. Empty when the preset embeds or drops subtitles.
    sidecar_paths: list[str] = Field(default_factory=list, max_length=256)
    size_bytes: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    sha256: str | None = None


class FailTaskRequest(BaseModel):
    last_error: str = Field(min_length=1)
    # Files the task finished before it failed and leaves in place (a
    # `subtitle_ocr` task keeps each `.srt` it wrote), relative to the media
    # root and next to the task's `output_path`. Recorded like /complete's,
    # so job delete removes them.
    sidecar_paths: list[str] = Field(default_factory=list, max_length=256)


class TranscodeStatsView(BaseModel):
    # Encode tasks only; subtitle OCR tasks are counted on their own.
    tasks_by_status: dict[str, int]
    total_tasks: int
    subtitle_ocr_tasks_by_status: dict[str, int] = Field(default_factory=dict)
    gpus_total: int
    gpus_available: int
    max_parallel: int
    # QUEUED tasks that need an encoder, as opposed to a passthrough move.
    # On a ripper-only deployment these are held until transcoding returns.
    queued_encode_tasks: int = 0


class GpuView(BaseModel):
    """One row of the DB-authoritative GPU inventory (Settings > GPUs)."""

    id: str
    vendor: GpuVendor
    device_path: str
    encoder_kinds: list[str]
    status: GpuStatus
    enabled: bool
    claimed_by_task_id: str | None = None
    last_seen_at: datetime | None = None
    probed_at: datetime | None = None
    probe_error: str | None = None


class GpuUpdateRequest(BaseModel):
    """PATCH body for a GPU row - the enable/disable switch only. Vendor,
    path and encoder kinds describe hardware; they are re-seeded, not edited."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class GpuProbeScheduled(BaseModel):
    """202 body for a single-row re-probe: the probe runs in the background
    and the row updates when `gpu.probed` arrives on `transcode.events`."""

    scheduled: bool


class GpuProbeAllScheduled(BaseModel):
    """202 body for a re-probe of every enabled, idle row: the ids whose
    background probe was scheduled."""

    scheduled: list[str]


class EncoderAvailabilityView(BaseModel):
    """One `arm_common.encoders.ENCODERS` catalog entry, with availability
    computed server-side from the live `gpus` inventory (GET /api/encoders).

    `group` buckets `preset`/`cpu`/`any` kinds by themselves and `gpu` kinds
    by vendor, so the transcode preset picker can render sections without
    re-deriving the grouping client-side. `available` is always true for
    `preset`/`cpu`/`any` (an `any_*` encoder falls back to CPU at dispatch
    time); for a vendor-pinned `gpu` encoder it reflects whether any enabled
    device's probe currently verifies that vendor/codec. `reason` explains
    an unavailable `gpu` entry, or an `any_*` entry that would currently run
    on the CPU for lack of a verified GPU; it is `None` otherwise.
    """

    id: str
    label: str
    group: Literal["preset", "cpu", "any", "qsv", "nvenc", "vaapi"]
    engine: str
    kind: str
    vendor: GpuVendor | None
    codec: str | None
    available: bool
    reason: str | None


class TranscodeWorkerView(BaseModel):
    task_id: str
    kind: TranscodeTaskKind = TranscodeTaskKind.ENCODE
    claimed_by: str | None = None
    progress_pct: int
    claim_heartbeat_at: datetime | None = None
    gpu_id: str | None = None
    source_track_id: str
    output_path: str | None = None
