"""Parse `makemkvcon -r info` robot output into the volume label, titles and disc type."""

import logging
import re
from typing import Callable, Iterable

from arm_common import DiscType
from arm_common.languages import canonical_language
from arm_common.schemas import ScanStream, ScanTitle

logger = logging.getLogger("arm_ripper.scan.makemkv_info")

_DURATION_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")
_QUOTED_RE = re.compile(r'^"(.*)"$')


def _strip_quotes(value: str) -> str:
    m = _QUOTED_RE.match(value.strip())
    return m.group(1) if m else value.strip()


def _parse_duration(value: str) -> int | None:
    m = _DURATION_RE.match(value.strip())
    if not m:
        return None
    h, mn, s = (int(g) for g in m.groups())
    return h * 3600 + mn * 60 + s


def _to_int(value: str) -> int | None:
    """The value as an int, or None when it isn't one."""
    try:
        return int(value)
    except ValueError:
        return None


# TINFO attribute code -> (title field, parser). A field is set only when its parser returns a value.
_TINFO_FIELDS: dict[int, tuple[str, Callable[[str], int | str | None]]] = {
    8: ("chapter_count", _to_int),
    9: ("duration_seconds", _parse_duration),
    11: ("size_bytes", _to_int),
    16: ("source_playlist", str),
    24: ("original_title_id", _to_int),
    25: ("segment_count", _to_int),
    26: ("segment_map", str),
    27: ("source_file", str),
}


def _classify_from_cinfo(disc_type_text: str) -> DiscType | None:
    """Map MakeMKV's `CINFO:1` text value to our DiscType enum.

    Observed values vary across MakeMKV versions:
      - "DVD" (v1.17.x) and "DVD disc" (v1.18.x) for DVD-Video
      - "Blu-ray disc" for BD-Video
      - "Audio CD" for CDDA
      - "HD-DVD" / "HD DVD" for HD-DVD (no v3 enum yet — return None)
    Match HD-DVD before DVD because "hd-dvd" contains "dvd".
    """
    s = disc_type_text.strip().lower()
    if "blu-ray" in s or "bluray" in s or s == "bd":
        return DiscType.BLURAY
    if "hd-dvd" in s or "hd dvd" in s:
        return None
    if "dvd" in s:
        return DiscType.DVD
    if "audio cd" in s or s == "cd":
        return DiscType.CD
    return None


# SINFO attribute ids (MakeMKV apdefs.h `ap_ia*`), as seen in the recordings
# under tests/fixtures:
_SINFO_TYPE = 1  # code 6201 Video, 6202 Audio, 6203 Subtitles (the text is localised)
_SINFO_LANG_CODE = 3  # "eng", "jpn"
_SINFO_LANG_NAME = 4  # "English"
_SINFO_CODEC_ID = 5  # "A_TRUEHD", "S_HDMV/PGS", "S_VOBSUB"
_SINFO_CODEC_SHORT = 6  # "TrueHD", "DD", "PGS" ("" for VobSub)
_SINFO_CHANNELS = 14  # "6"
_SINFO_FLAGS = 22  # stream flags bitmask, see below
_SINFO_TREE_INFO = 30  # "TrueHD Surround 5.1 English", "PGS English  (forced only)"
_SINFO_MKV_FLAGS = 38  # "d" = default track
_STREAM_KINDS = {6202: "audio", 6203: "subtitle"}
# AP_AVStreamFlag_*: Hanako's TrueHD tracks carry 1024 (has core audio), their
# AC-3 cores 2304 (derived + core audio), the forced-only PGS tracks 6144
# (derived + forced subtitles).
_FLAG_DERIVED = 2048
_FLAG_FORCED_SUBTITLES = 4096


def _stream_from_sinfo(index: int, attrs: dict[int, tuple[int, str]]) -> ScanStream | None:
    """Build a ScanStream from one stream's SINFO attributes; None for video
    and anything that is not an audio or subtitle stream."""
    type_code = attrs.get(_SINFO_TYPE, (0, ""))[0]
    kind = _STREAM_KINDS.get(type_code)
    if kind is None:
        return None

    def text(attr: int) -> str | None:
        value = " ".join(attrs.get(attr, (0, ""))[1].split())
        return value or None

    def number(attr: int) -> int | None:
        value = text(attr)
        return int(value) if value is not None and value.isdigit() else None

    flags = number(_SINFO_FLAGS) or 0
    return ScanStream(
        index=index,
        kind="audio" if kind == "audio" else "subtitle",
        language=canonical_language(text(_SINFO_LANG_CODE)),
        language_name=text(_SINFO_LANG_NAME),
        codec_id=text(_SINFO_CODEC_ID),
        codec=text(_SINFO_CODEC_SHORT),
        description=text(_SINFO_TREE_INFO),
        channels=number(_SINFO_CHANNELS),
        forced=bool(flags & _FLAG_FORCED_SUBTITLES),
        derived=bool(flags & _FLAG_DERIVED),
        default="d" in (text(_SINFO_MKV_FLAGS) or ""),
    )


def _int_field(entry: dict[str, object], key: str) -> int | None:
    value = entry.get(key)
    return value if isinstance(value, int) else None


def _str_field(entry: dict[str, object], key: str) -> str | None:
    value = entry.get(key)
    return value if isinstance(value, str) else None


def _split_robot_line(raw: str) -> tuple[str, list[str]] | None:
    """A robot-mode line as (message type, stripped comma fields); None for a blank or colonless line."""
    line = raw.strip()
    if not line or ":" not in line:
        return None
    msg_type, _, rest = line.partition(":")
    return msg_type, [f.strip() for f in rest.split(",")]


def _scan_title(
    idx: int, entry: dict[str, object], sinfo: dict[int, dict[int, tuple[int, str]]] | None = None
) -> ScanTitle:
    """The ScanTitle for one title's collected TINFO fields and SINFO streams.

    Fork: a title with no usable duration is kept with duration_seconds=None
    (see `parse_makemkvcon_info`)."""
    return ScanTitle(
        index=idx,
        duration_seconds=_int_field(entry, "duration_seconds"),
        chapter_count=_int_field(entry, "chapter_count"),
        size_bytes=_int_field(entry, "size_bytes"),
        source_file=_str_field(entry, "source_file"),
        source_playlist=_str_field(entry, "source_playlist"),
        original_title_id=_int_field(entry, "original_title_id"),
        segment_count=_int_field(entry, "segment_count"),
        segment_map=_str_field(entry, "segment_map"),
        streams=[
            stream
            for stream_idx, attrs in sorted((sinfo or {}).items())
            if (stream := _stream_from_sinfo(stream_idx, attrs)) is not None
        ],
    )


class _InfoParser:
    """Accumulates the disc label, the disc type and each title's TINFO fields from robot-mode lines.

    Fork: also MakeMKV's TCOUNT and each title's SINFO stream attributes."""

    def __init__(self) -> None:
        self.volume_label: str | None = None
        self.mkv_disc_type: DiscType | None = None
        self.titles: dict[int, dict[str, object]] = {}
        self.tcount: int | None = None
        self.sinfo: dict[int, dict[int, dict[int, tuple[int, str]]]] = {}

    def feed(self, raw: str) -> None:
        """Route one line to its handler; other lines are ignored."""
        split = _split_robot_line(raw)
        if split is None:
            return
        msg_type, fields = split
        if msg_type == "TCOUNT":
            self.tcount = _to_int(fields[0])
        elif msg_type == "CINFO" and len(fields) >= 3:
            self.on_cinfo(fields)
        elif msg_type == "TINFO" and len(fields) >= 4:
            self.on_tinfo(fields)
        elif msg_type == "SINFO" and len(fields) >= 5:
            self.on_sinfo(fields)

    def on_cinfo(self, fields: list[str]) -> None:
        try:
            code = int(fields[0])
        except ValueError:
            return
        if code == 1 and self.mkv_disc_type is None:
            self.mkv_disc_type = _classify_from_cinfo(_strip_quotes(",".join(fields[2:])))
        elif code in (2, 30) and not self.volume_label:
            self.volume_label = _strip_quotes(",".join(fields[2:]))

    def on_tinfo(self, fields: list[str]) -> None:
        try:
            track_idx = int(fields[0])
            code = int(fields[1])
        except ValueError:
            return
        value = _strip_quotes(",".join(fields[3:]))
        entry = self.titles.setdefault(track_idx, {})
        field = _TINFO_FIELDS.get(code)
        if field is not None:
            name, parse = field
            field_value = parse(value)
            if field_value is not None:
                entry[name] = field_value

    def on_sinfo(self, fields: list[str]) -> None:
        try:
            title_idx, stream_idx, attr, value_code = (int(f) for f in fields[:4])
        except ValueError:
            return
        streams = self.sinfo.setdefault(title_idx, {})
        streams.setdefault(stream_idx, {})[attr] = (value_code, _strip_quotes(",".join(fields[4:])))

    def scan_titles(self) -> list[ScanTitle]:
        """Every title MakeMKV described, in index order (fork: untimed ones too)."""
        return [_scan_title(idx, self.titles[idx], self.sinfo.get(idx)) for idx in sorted(self.titles)]


def parse_makemkvcon_info(
    lines: Iterable[str],
) -> tuple[str | None, list[ScanTitle], DiscType | None]:
    """Parse the robot-mode output of `makemkvcon -r info`.

    Returns (volume_label, titles, mkv_disc_type).

    MakeMKV codes used:
    - CINFO:1,...    — disc-type text ("DVD" / "Blu-ray disc" / "HD-DVD" /
                       "Audio CD") — authoritative signal regardless of
                       region-locks or kernel-mount failures
    - CINFO:2,...    — disc name (volume label)
    - CINFO:30,...   — disc tree info (sometimes the only place with the label)
    - TINFO:t,9,...  — title duration (HH:MM:SS)
    - TINFO:t,8,...  — chapter count
    - TINFO:t,11,... — title size in bytes
    - TINFO:t,16,... — source playlist/clip on Blu-ray (e.g. 00800.mpls,
                       or 00002.m2ts for a bare clip); not emitted on DVD.
                       The stable key for matching a title across sources.
    - TINFO:t,24,... — original title number on DVD (e.g. "07"); not
                       emitted on Blu-ray. DVD's stable key.
    - TINFO:t,25,... — segment count
    - TINFO:t,26,... — segment map, kept verbatim: a clip list on Blu-ray
                       ("14,15,16"), a cell range on DVD ("1-7")
    - TINFO:t,27,... — planned *output* filename (e.g. title_t00.mkv),
                       numbered by this invocation's own title ids; despite
                       the field name it is not a source file
    - SINFO:t,s,...  — per-stream attributes of title t, stream s; the audio
                       and subtitle streams become `ScanTitle.streams` (see
                       `_stream_from_sinfo` for the attribute ids)
    - TCOUNT:n       — MakeMKV's own title count, only cross-checked against
                       the titles parsed (a mismatch is logged); it never
                       makes a title on its own.

    A title that emitted any TINFO line but no usable TINFO:t,9 duration is
    kept with duration_seconds=None rather than dropped: MakeMKV can still rip
    it, and a dropped title leaves an output file no Track accounts for.

    Reference: https://github.com/automatic-ripping-machine/automatic-ripping-machine/wiki/MakeMKV-Codes
    """
    parser = _InfoParser()
    for raw in lines:
        parser.feed(raw)
    parsed = parser.scan_titles()
    tcount = parser.tcount
    missing_duration = [t.index for t in parsed if t.duration_seconds is None]
    if missing_duration:
        logger.warning(
            "makemkvcon gave no usable duration (TINFO:t,9) for %d title(s); kept with an unknown "
            "duration so each still gets a Track row; indices: %s",
            len(missing_duration),
            missing_duration,
        )
    # Only indices MakeMKV described become titles. An index TCOUNT counts but
    # no TINFO line names means the output was cut short, not that a title is
    # there: a made-up title would have no duration, so the rip would count it
    # eligible and it could never get a file of its own.
    if tcount is not None and len(parsed) < tcount:
        logger.warning(
            "makemkvcon TCOUNT=%d but only %d title(s) emitted TINFO output; the rest are missing "
            "from this scan and will not be ripped; observed indices: %s",
            tcount,
            len(parsed),
            [t.index for t in parsed],
        )
    elif tcount is not None and len(parsed) > tcount:
        logger.warning(
            "makemkvcon TCOUNT=%d is lower than the %d title(s) observed in TINFO output",
            tcount,
            len(parsed),
        )

    return parser.volume_label, parsed, parser.mkv_disc_type
