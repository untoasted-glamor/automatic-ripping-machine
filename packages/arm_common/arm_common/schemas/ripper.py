from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from arm_common.enums import DiscType, DriveMediaStatus, KeydbState, MakemkvKeyState, MakemkvSdfState, TrackStatus


class MakemkvKeyStatusReport(BaseModel):
    """Body of POST /api/ripper/makemkv-key-status — the ripper's disc-free
    `makemkvcon` probe outcome. Global (not per-drive); the backend writes it
    to the Config singleton. `detail` is a human-readable reason string."""

    state: MakemkvKeyState
    detail: str | None = None
    # Reporting drive, for setup's "checked by" (setup spec §6.7). Optional so
    # rippers older than the backend still validate.
    drive_id: str | None = None


class KeydbStatusReport(BaseModel):
    """Body of POST /api/ripper/keydb-status — the ripper's community-keydb
    fetch outcome. Global (not per-drive); the backend writes it to the Config
    singleton. `vuk_count` / `age_days` are present on ok / fresh_kept.
    Both fields are None on all other states."""

    state: KeydbState
    vuk_count: int | None = None
    age_days: int | None = None


class SdfStatusReport(BaseModel):
    """Body of POST /api/ripper/sdf-status — the ripper's MakeMKV SDF fetch
    outcome. Global (not per-drive); the backend writes it to the Config
    singleton. `age_days` is present on fresh_kept; None on all other states."""

    state: MakemkvSdfState
    age_days: int | None = None


class RegisterRequest(BaseModel):
    """POST /api/ripper/register. Keyed on the Drive row the backend handed
    this container (ARM_DRIVE_ID); `by_id_name` is the udev link the ripper
    is bound to (None for a port-identity drive) and must match the row."""

    drive_id: str
    hostname: str
    device_path: str
    ripper_version: str
    hw_caps: dict[str, Any] = Field(default_factory=dict)
    by_id_name: str | None = None


class RipperScanActivity(BaseModel):
    """A disc scan in progress on the reporting ripper's drive.

    The scan runs before identify creates a job and can take minutes across
    not-ready retries, so this is the only sign the drive is busy.
    `started_at` is when the first attempt began (elapsed time spans
    retries); `attempt` is the 1-based attempt running or being backed off
    before."""

    started_at: datetime
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)


class RipperHeartbeatRequest(BaseModel):
    """POST /api/ripper/heartbeat body. Each ripper sends one of these
    every HEARTBEAT_INTERVAL_SECONDS so the backend can refuse manual-
    trigger requests against drives whose tray is open / empty without
    waiting for a doomed identify. It also sends one at once when a disc
    scan starts, moves to a retry, or ends.

    Named distinctly from the transcoder HeartbeatRequest because the
    two services post completely different payloads to different paths.
    """

    drive_id: str
    media_status: DriveMediaStatus
    # None / omitted = not scanning (also what an older ripper sends).
    scan: RipperScanActivity | None = None


class ScanStream(BaseModel):
    """One audio or subtitle stream of a scanned title (MakeMKV `SINFO`).

    What the disc offers, for the track picker; the transcoder still probes
    the ripped file, since MakeMKV's own stream selection can leave a stream
    (e.g. a derived forced-only subtitle) out of the rip.
    """

    # MakeMKV's stream id within the title (0 is usually the video).
    index: int
    kind: Literal["audio", "subtitle"]
    # ISO 639-2 code as stored for track selection (`und` when untagged).
    language: str = "und"
    language_name: str | None = None
    # Matroska codec id (`A_TRUEHD`, `S_HDMV/PGS`) and MakeMKV's short name.
    codec_id: str | None = None
    codec: str | None = None
    # MakeMKV's one-line description, e.g. `TrueHD Surround 5.1 English`.
    description: str | None = None
    channels: int | None = None
    # Stream flags: forced-only subtitles, a stream derived from another
    # (the AC-3 core of a TrueHD track, a forced-only subtitle split off a
    # full one), and MakeMKV's default-track flag.
    forced: bool = False
    derived: bool = False
    default: bool = False


class ScanTitle(BaseModel):
    index: int
    # None when makemkvcon described the title (some TINFO line) but gave no
    # usable TINFO:t,9 duration. The title is kept rather than dropped (see
    # parse_makemkvcon_info) so MakeMKV can't rip a file no Track accounts for.
    duration_seconds: int | None
    chapter_count: int | None = None
    size_bytes: int | None = None
    # TINFO 27: MakeMKV's planned *output* file name (`<label>_tNN.mkv`),
    # numbered by the scan's own title ids. Kept under this name for
    # compatibility with stored scans.
    source_file: str | None = None
    # TINFO 16 (Blu-ray only): the playlist or clip the title plays, e.g.
    # `00800.mpls`. Title ids are not stable between a disc and its backup;
    # this is.
    source_playlist: str | None = None
    # TINFO 24 (DVD only): the disc's own title number, DVD's stable key.
    original_title_id: int | None = None
    # TINFO 25 / 26: segment count and segment map, the map kept verbatim
    # (a clip list like `14,15,16` on Blu-ray, a cell range like `1-7` on DVD).
    segment_count: int | None = None
    segment_map: str | None = None
    # Audio and subtitle streams (SINFO). Empty on scans stored before the
    # scan read them.
    streams: list[ScanStream] = Field(default_factory=list)


class DiscFingerprintInput(BaseModel):
    """One (algo, value) pair on a scanned disc.

    Canonical algo names: `crc64` (pydvdid DVD), `aacs` (Blu-ray AACS
    Disc ID), `musicbrainz` (CD disc id), `matrix256` (matrix256v1
    structural hash), `thediscdb` (TheDiscDB content hash).
    Free-form to permit new algos without a schema bump.
    """

    algo: str
    value: str


class BdDiscMeta(BaseModel):
    """Blu-ray disc title from BDMV/META/DL/bdmt_<lang>.xml (studio-authored).
    Optional on the wire: older rippers never send it."""

    name: str
    set_number: int | None = None
    num_sets: int | None = None
    language: str | None = None


class ScanResult(BaseModel):
    disc_type: DiscType
    volume_label: str | None = None
    titles: list[ScanTitle] = Field(default_factory=list)
    musicbrainz_disc_id: str | None = None
    # All disc fingerprints the scan was able to compute. Drives 1337server
    # lookup (crc64), and reverse "have we seen this disc before?" lookup
    # in future flows. Empty when nothing fingerprintable.
    fingerprints: list[DiscFingerprintInput] = Field(default_factory=list)
    # Studio disc title + set position from the Blu-ray BDMT file, when present.
    bd_meta: BdDiscMeta | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    # DVD/BD: the ripper's baseline minimum title length (ARM_MIN_LENGTH_SECONDS).
    # The scan itself lists every title (MakeMKV `--minlength=0`, so title ids
    # match a rip at any minimum); titles shorter than this get no Track row.
    # None on scans stored before the scan listed everything (MakeMKV had
    # already dropped titles under its 120 s default) and on CD/data scans.
    min_length_seconds: int | None = None


class IdentifyRequest(BaseModel):
    drive_id: str
    scan_result: ScanResult
    # Set by the ripper when it's running a manual-trigger flow; backend
    # stamps it into the job's `pending_session_id` column so
    # `maybe_auto_apply_session` can prefer it over the drive's persistent
    # default_session_id.
    pending_session_id: str | None = None


class TrackUpdateRequest(BaseModel):
    """PATCH /api/ripper/tracks/{track_id} body.

    Only fields that are non-None are written. Backend validates the
    state-machine transition implied by `status`.
    """

    status: TrackStatus
    output_path: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    duration_seconds: int | None = None
    last_error: str | None = None


class JobCompleteRequest(BaseModel):
    """POST /api/ripper/jobs/{job_id}/rip-complete body.

    Empty for now; backend computes the final job status from the track
    outcomes. Reserved for future flags (e.g. user-initiated abort).
    """


class RipperConfigView(BaseModel):
    """Subset of `Config` the ripper reads to gate its automatic behaviour.

    Polled on disc-insert before the pipeline kicks off; small enough to
    keep cheap. Does not include any UI-only fields.
    """

    auto_rip_on_insert: bool
    # Operator's MakeMKV key, when configured in the UI. The ripper writes it
    # into ~/.MakeMKV/settings.conf before each rip (overriding the legacy
    # MAKEMKV_KEY env var). `None` => fall back to that env var, then the
    # monthly forum beta-key scrape. Defaulted so older backends omitting it
    # still validate.
    makemkv_key: str | None = None
    # Operator toggle for the community-keydb (FindVUK) auto-fetch. Defaulted
    # True so older backends omitting it still validate and keep the feature on.
    community_keydb_enabled: bool = True
    makemkv_sdf_enabled: bool = True
    # Review gate: global pause suppresses the auto-start at countdown expiry;
    # manual_wait_seconds is the countdown duration; review_requires_confirm
    # (mandatory confirm) means the countdown never fires and only the
    # operator's Start proceeds. The ripper re-reads these while parked so a
    # mid-wait pause/duration/mode change is honoured. Defaulted so older
    # backends omitting them still validate (no hold, 60s, timed).
    ripping_paused: bool = False
    manual_wait_seconds: int = 60
    review_requires_confirm: bool = False
