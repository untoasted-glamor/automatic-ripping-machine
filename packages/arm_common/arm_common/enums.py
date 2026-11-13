from enum import StrEnum


class DiscType(StrEnum):
    DVD = "dvd"
    BLURAY = "bluray"
    CD = "cd"
    DATA = "data"
    UNKNOWN = "unknown"


class DriveStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    RIPPING = "ripping"
    ERROR = "error"


class DriveMediaStatus(StrEnum):
    """What the drive thinks is in its tray. Reported by each ripper
    on a heartbeat so the backend can fail manual-trigger requests
    fast when the user clicks Start without loading a disc."""

    LOADED = "loaded"  # CDS_DISC_OK
    NO_DISC = "no_disc"  # CDS_NO_DISC
    TRAY_OPEN = "tray_open"  # CDS_TRAY_OPEN
    NOT_READY = "not_ready"  # CDS_DRIVE_NOT_READY (medium spinning up)
    # The device node exists but open() was refused (EPERM/EACCES — cgroup
    # rule not honoured, wrong group). Present-but-misconfigured; distinct
    # from DETACHED so the UI can say "fix permissions", not "plug it in".
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"  # CDS_NO_INFO, or ioctl unsupported on a probed device
    # No hardware behind this drive's identity right now (ENOENT/ENXIO/ENODEV):
    # unplugged, or not yet enumerated. The ripper keeps heartbeating so the
    # row stays visible; the backend derives DriveStatus.OFFLINE from this.
    DETACHED = "detached"


class DriveMode(StrEnum):
    AUTO = "auto"  # ripper auto-rips on disc insert
    MANUAL = "manual"  # ripper waits for an explicit manual.trigger


class DriveLifecycle(StrEnum):
    """Operator-owned state of a physical optical drive the backend has seen.
    Presence (plugged in right now) is a separate, orthogonal fact."""

    DETECTED = "detected"  # seen by the scanner, no decision yet
    IGNORED = "ignored"  # operator said "not ARM's" — never nag, never prune
    ENROLLED = "enrolled"  # operator said "ARM's" — a ripper serves it (Plan 3 spawns it)
    RETIRED = "retired"  # a virtual (ISO) drive whose one-shot rip has ended


class DriveKind(StrEnum):
    """What a Drive row represents. OPTICAL is a physical drive the scanner
    found; VIRTUAL is an ephemeral per-ISO-rip drive row (source_kind /
    source_path identify the ISO), created enrolled and retired when its one
    rip ends."""

    OPTICAL = "optical"
    VIRTUAL = "virtual"


class DriveSourceKind(StrEnum):
    """What a virtual drive's source is: an .iso image, or a disc folder (a
    BDMV / VIDEO_TS tree MakeMKV reads directly)."""

    ISO = "iso"
    FOLDER = "folder"


class IsoPreparePhase(StrEnum):
    """What an ISO ripper is doing before its job exists: scanning the image
    (or its extracted folder) or unpacking it for MakeMKV."""

    SCANNING = "scanning"
    EXTRACTING = "extracting"


class DriveIdentityKind(StrEnum):
    """What a Drive row's identity is keyed on. BY_ID is the udev
    /dev/disk/by-id link name (stable across replug and renumbering); PORT
    is the sysfs device path — the degraded fallback for drives that expose
    no serial, and the UI says so."""

    BY_ID = "by_id"
    PORT = "port"


class JobStatus(StrEnum):
    CREATED = "created"
    AWAITING_USER_ID = "awaiting_user_id"
    IDENTIFIED = "identified"
    RIPPING = "ripping"
    RIPPED = "ripped"
    RIPPED_PARTIAL = "ripped_partial"
    # A scanned + identified disc held for operator review (timed review gate).
    # The rip pipeline parks here, counting down `manual_wait_seconds`; on expiry
    # it auto-starts (unless globally paused), or the operator Starts/Cancels.
    # Distinct from AWAITING_USER_ID ("could not identify — needs operator ID").
    # Resolvable (PRESERVE) for identity edits; non-terminal. Review Track rows
    # may already exist (identify persists them for the review card), but the
    # disc is not ripped: an applied session always parks and fans out at
    # rip-complete.
    AWAITING_REVIEW = "awaiting_review"
    # Set by rip-complete when a placeholder disc rips successfully but
    # identification never landed (metadata_json["unidentified"]): transcode
    # is gated on identity, so the after-rip hooks wait for resolve, which
    # promotes this to RIPPED (not IDENTIFIED). Resolvable via the
    # /resolve endpoint just like AWAITING_USER_ID.
    RIPPED_AWAITING_IDENTIFY = "ripped_awaiting_identify"
    ABANDONED = "abandoned"
    FAILED = "failed"


# Canonical JobStatus groupings. Single source of truth — routers/services must
# import these rather than re-defining local copies.
TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.RIPPED,
        JobStatus.RIPPED_PARTIAL,
        JobStatus.RIPPED_AWAITING_IDENTIFY,
        JobStatus.ABANDONED,
        JobStatus.FAILED,
    }
)
# Pre-rip = disc present, not yet committed to a rip (reusable on re-identify).
PRE_RIP_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.CREATED,
        JobStatus.AWAITING_USER_ID,
        JobStatus.IDENTIFIED,
        JobStatus.AWAITING_REVIEW,
    }
)
NON_TERMINAL_JOB_STATUSES: frozenset[JobStatus] = PRE_RIP_JOB_STATUSES | frozenset({JobStatus.RIPPING})

# Identity edits (POST /jobs/{id}/resolve). PROMOTE flips status (to IDENTIFIED,
# or RIPPED for a ripped placeholder); PRESERVE edits identity in place.
RESOLVABLE_PROMOTE_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.AWAITING_USER_ID, JobStatus.RIPPED_AWAITING_IDENTIFY}
)
RESOLVABLE_PRESERVE_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.IDENTIFIED,
        JobStatus.RIPPED,
        JobStatus.RIPPED_PARTIAL,
        # A held review-gate disc accepts identity edits WITHOUT flipping status —
        # resolve = "I've identified it"; the separate Start = "begin ripping".
        # PRESERVE (not PROMOTE) keeps it in AWAITING_REVIEW after an edit.
        JobStatus.AWAITING_REVIEW,
    }
)
RESOLVABLE_JOB_STATUSES: frozenset[JobStatus] = RESOLVABLE_PROMOTE_JOB_STATUSES | RESOLVABLE_PRESERVE_JOB_STATUSES
# Session apply (POST /jobs/{id}/transcode). OK = resolve outputs now (or park
# as no_tracks pre-rip); PARK = park as waiting_identify until identity lands.
APPLY_OK_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.IDENTIFIED, JobStatus.AWAITING_REVIEW, JobStatus.RIPPED, JobStatus.RIPPED_PARTIAL}
)
APPLY_PARK_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.AWAITING_USER_ID, JobStatus.RIPPED_AWAITING_IDENTIFY}
)
# Rip finished (successfully or as a placeholder); Track rows exist.
POST_RIP_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.RIPPED, JobStatus.RIPPED_PARTIAL, JobStatus.RIPPED_AWAITING_IDENTIFY}
)
# The ripper has taken the job's keep/drop set (`Track.excluded`) at rip-start,
# so re-including a title that has no file can no longer get it ripped.
RIP_STARTED_JOB_STATUSES: frozenset[JobStatus] = POST_RIP_JOB_STATUSES | frozenset({JobStatus.RIPPING})
# Jobs whose parked applications are re-drained when transcoding is re-enabled:
# the rip is done and identity is known, so the only thing that held an encode
# application parked was the toggle. Pre-rip jobs (identified, awaiting review)
# are left out: their applications stay parked until rip-complete drains them.
REDRAIN_JOB_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.RIPPED, JobStatus.RIPPED_PARTIAL})


class TrackStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    # The disc menu only: it can't be ripped (Java menus, no menu, no menu
    # files in the backup), with the reason in `last_error`. Counts as
    # finished: rip-complete's job status ignores it and retry-failed never
    # picks it up.
    SKIPPED = "skipped"


class TrackKind(StrEnum):
    VIDEO_TITLE = "video_title"
    AUDIO_TRACK = "audio_track"
    DATA_DUMP = "data_dump"
    DISC_BACKUP = "disc_backup"
    # The disc's menus, copied into `<job raw>/menu/` (fork only). At most one
    # per DVD or Blu-ray job, `source_ref` "menu", index -1 so it sorts first.
    # Not a title: `arm_common.tracks.is_title` says so for every caller.
    DISC_MENU = "disc_menu"


class MenuKind(StrEnum):
    """What a disc's menu is, found at scan time (`arm_ripper.menus.probe`,
    fork only): `ScanResult.menu_kind`."""

    HDMV = "hdmv"  # a Blu-ray with HDMV (IG) menus: a pack can be built
    BDJ = "bdj"  # a Blu-ray whose first play or top menu is BD-J: no pack
    DVD = "dvd"  # a DVD with menu program chains
    NONE = "none"  # a disc with no menu
    UNKNOWN = "unknown"  # nothing could be read


class TrackRole(StrEnum):
    """What one title on a disc is. Set by identity sources via the resolver
    (arm_backend.identity) or by the operator; replaces the old free-text
    role and the per-track video_type."""

    MAIN = "main"
    EPISODE = "episode"
    EXTRA = "extra"
    TRAILER = "trailer"
    OTHER = "other"
    # Plays other titles back to back. Set by the review gate's detector
    # (arm_backend.track_selection) or the operator; starts left out.
    PLAY_ALL = "play_all"
    # A special: a bonus Jellyfin files as season 0 (`Season 00/`), numbered by
    # TMDB's season 0 in `episode_number` (fork only, disc menus plan v5,
    # decision 13). The episode stage leaves it alone.
    SPECIAL = "special"


class MediaType(StrEnum):
    MOVIE = "movie"
    TV = "tv"
    MUSIC = "music"
    DATA = "data"
    ISO = "iso"


class TrackSelection(StrEnum):
    MAIN_FEATURE = "main_feature"
    ALL_TRACKS = "all_tracks"
    ARCHIVE = "archive"
    CUSTOM = "custom"


class IdentificationMode(StrEnum):
    REQUIRED = "required"
    SKIP = "skip"
    DEFERRED_PLACEHOLDER = "deferred_placeholder"


class OutputMode(StrEnum):
    TRACKS = "tracks"
    ISO = "iso"
    DATA_COPY = "data_copy"
    FOLDER = "folder"


class TranscodeTool(StrEnum):
    HANDBRAKE = "handbrake"
    ABCDE = "abcde"
    NONE = "none"


class AudioSelection(StrEnum):
    """Which source audio tracks a transcode preset keeps.

    `preset` leaves the choice to the engine (HandBrake: the preset's own
    rule, which keeps the first track; ffmpeg VAAPI: every track). `all`
    keeps every track in source order; `languages` keeps the tracks whose
    language is in the preset's ordered `audio_languages` list.
    """

    PRESET = "preset"
    ALL = "all"
    LANGUAGES = "languages"


class AudioFormat(StrEnum):
    """What each kept audio track becomes (when ARM picks the audio tracks).

    `preset` leaves it to the engine (HandBrake: the preset's audio encoders;
    ffmpeg VAAPI: every track copied). `aac_best` keeps one track per
    language: its best track (lossless TrueHD first) converted to AAC with up
    to 5.1 channels. `ac3_else_aac51` keeps one track per language: an
    untouched copy of its AC-3 (Dolby Digital) track, else its best track
    converted to AAC with up to 5.1 channels. `aac_stereo`
    converts every kept track to AAC stereo, `aac_51` to AAC with up to 5.1
    channels, and `copy` keeps every kept track untouched.
    """

    PRESET = "preset"
    AAC_BEST = "aac_best"
    AC3_ELSE_AAC51 = "ac3_else_aac51"
    AAC_STEREO = "aac_stereo"
    AAC_51 = "aac_51"
    COPY = "copy"


class SubtitleSelection(StrEnum):
    """Which source subtitle tracks a transcode preset keeps (see AudioSelection)."""

    PRESET = "preset"
    ALL = "all"
    LANGUAGES = "languages"
    NONE = "none"


class SubtitleOutput(StrEnum):
    """Where kept subtitle tracks go: muxed into the video, written as
    separate files next to it (Jellyfin/Plex sidecars), or both."""

    EMBEDDED = "embedded"
    SEPARATE = "separate"
    BOTH = "both"


class ContainerFormat(StrEnum):
    MKV = "mkv"
    MP4 = "mp4"
    WEBM = "webm"
    FLAC = "flac"
    MP3 = "mp3"
    OGG = "ogg"
    ISO = "iso"
    NONE = "none"


class PreviouslyRippedAction(StrEnum):
    """What identify does with a disc whose fingerprint matches an earlier rip
    (a job in POST_RIP_JOB_STATUSES, on any drive)."""

    # Park the new job in AWAITING_REVIEW with no countdown until the operator
    # chooses Rip again or Discard.
    HOLD = "hold"
    # Create no job; record a rip.duplicate_rejected event and eject the disc.
    REJECT = "reject"
    # Rip it again without asking.
    RIP = "rip"


class RetentionPolicy(StrEnum):
    KEEP_FOREVER = "keep_forever"
    PRUNE_AFTER_SESSION = "prune_after_session"
    CUSTOM = "custom"


class SessionApplicationStatus(StrEnum):
    WAITING_IDENTIFY = "waiting_identify"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DONE_PARTIAL = "done_partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TranscodeTaskStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class MenuProposalStatus(StrEnum):
    """Where a name proposed from a disc menu button stands (fork only, disc
    menus plan v5, M2-M4).

    `proposed`: made by a menu preview, not yet looked at. `accepted`: the
    owner took it (maybe edited) and its encode isn't finished, so the rename
    waits for it. `applied`: the track carries the name and its finished
    encode has been renamed. `rejected`: the owner turned it down. `info`:
    nothing to apply (an episode button that confirms or contradicts identity,
    a play-all, a title that wasn't ripped)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    REJECTED = "rejected"
    INFO = "info"


class TranscodeTaskKind(StrEnum):
    """What a `transcode_tasks` row runs in its transcoder container.

    `encode` writes a video (or audio) file from a raw rip. `subtitle_ocr`
    reads the image subtitle sidecars its parent encode task wrote (`.sup`)
    and writes a text subtitle (`.srt`) beside each; it needs no GPU, no
    preset and no raw rip, and never changes its session application's
    status."""

    ENCODE = "encode"
    SUBTITLE_OCR = "subtitle_ocr"
    # Reads the job's copied disc menu (`<job raw>/menu/`) and writes a preview
    # into `<job raw>/menu/preview/`: each reachable menu page as an image,
    # each visible button's label read by OCR, and what every button plays
    # (fork only, disc menus plan v5, M1). Runs in the menu preview image, no
    # GPU, no preset; its source track is the job's `disc_menu` track.
    MENU_PREVIEW = "menu_preview"
    # Builds the job's disc menu pack for the Jellyfin Disc Menu plugin
    # (fork only, disc menus plan v5, A3): reads `<job raw>/menu/` and the
    # job's encodes, writes `<show folder>/.discmenus/<disc>/`. Runs in the
    # transcode image (ffmpeg for backgrounds), no GPU, no preset; its source
    # track is the job's `disc_menu` track, and `/register` hands it the map
    # of which encode each disc title is (`MenuPackPlan`).
    MENU_PACK = "menu_pack"


class GpuVendor(StrEnum):
    VAAPI = "vaapi"
    NVENC = "nvenc"
    QSV = "qsv"


class GpuStatus(StrEnum):
    AVAILABLE = "available"
    BUSY = "busy"


class VideoCodec(StrEnum):
    H264 = "h264"
    H265 = "h265"
    AV1 = "av1"


class MakemkvKeyState(StrEnum):
    """Outcome of the ripper's disc-free `makemkvcon info disc:9999` probe.
    Stored on the Config singleton and read by the key check, preflight and config view.

    VALID                   — clean probe, key accepted.
    UNREGISTERED_OR_EXPIRED — MSG:5052/5055 (evaluation expired / no valid key).
    BINARY_EXPIRED          — MSG:5021 (60-day kill-switch; no key overrides it).
    FORMAT_INVALID          — configured key fails the M-/T- serial regex (pre-probe).
    PROBE_FAILED            — binary missing / timeout / undeterminable (→ valid=null).
    """

    VALID = "valid"
    UNREGISTERED_OR_EXPIRED = "unregistered_or_expired"
    BINARY_EXPIRED = "binary_expired"
    FORMAT_INVALID = "format_invalid"
    PROBE_FAILED = "probe_failed"


class KeydbState(StrEnum):
    """Outcome of the ripper's community-keydb fetch (`update_keydb.sh`),
    stored on the Config singleton and surfaced by /api/system/preflight.

    OK              — downloaded, filtered, installed; vuk_count set.
    DISABLED        — community keydb gated off via config.
    FRESH_KEPT      — age-gated skip; existing keydb retained (age_days set).
    DOWNLOAD_FAILED — all download retries failed; existing keydb retained.
    EMPTY           — fetched file not a ZIP / no keydb.cfg / no VUK entries.
    PROBE_FAILED    — wrapper could not run or parse the script.
    """

    OK = "ok"
    DISABLED = "disabled"
    FRESH_KEPT = "fresh_kept"
    DOWNLOAD_FAILED = "download_failed"
    EMPTY = "empty"
    PROBE_FAILED = "probe_failed"


class MakemkvSdfState(StrEnum):
    """Outcome of the ripper's MakeMKV SDF fetch (`update_sdf.sh`), stored on
    the Config singleton and surfaced by /api/system/preflight.

    UPDATED         — downloaded (official or mirror) and installed.
    FRESH_KEPT      — age-gated skip; existing SDF retained (age_days set).
    DISABLED        — SDF refresh gated off via config.
    DOWNLOAD_FAILED — all sources failed; baked/existing SDF retained.
    PROBE_FAILED    — wrapper could not run or parse the script.
    """

    UPDATED = "updated"
    FRESH_KEPT = "fresh_kept"
    DISABLED = "disabled"
    DOWNLOAD_FAILED = "download_failed"
    PROBE_FAILED = "probe_failed"


class UserRole(StrEnum):
    """Fixed two-account model (spec 2026-07-12-basic-user-management-design).

    ADMIN — the single writer; the only account that can hold a session.
    GUEST — read-everything/write-nothing; acquired by having no token at all
            rather than by logging in, and gated by the `disabled` flag on its
            row.

    Stored as VARCHAR via `enum_column`, never a Postgres CREATE TYPE.
    """

    ADMIN = "admin"
    GUEST = "guest"


class SetupStep(StrEnum):
    """First-run setup walkthrough steps, in walkthrough order (setup spec 2026-10-01).

    Stored as VARCHAR keys inside Config.setup_progress JSON, validated in the app.
    """

    ACCOUNT = "account"
    SYSTEM = "system"
    DRIVES = "drives"
    MAKEMKV = "makemkv"
    METADATA = "metadata"
    DISCS = "discs"
    TRANSCODING = "transcoding"
    NOTIFICATIONS = "notifications"
    FINISH = "finish"


SETUP_STEP_ORDER: tuple[SetupStep, ...] = tuple(SetupStep)


class SetupStepState(StrEnum):
    DONE = "done"
    SKIPPED = "skipped"
    ATTENTION = "attention"
