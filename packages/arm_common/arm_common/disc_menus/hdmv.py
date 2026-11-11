"""HDMV Blu-ray structures: `index.bdmv`, `MovieObject.bdmv`, playlists, clip info and navigation commands.

Layouts follow libbluray (`indx_parse.c`, `mobj_parse.c`, `mpls_parse.c`, `clpi_parse.c`, `mobj_print.c`). None of
these files is encrypted on a disc, so they can be read off the device at scan time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arm_common.disc_menus import MenuParseError
from arm_common.disc_menus.binary import need, text, u8, u16, u32

NO_ID = 0xFFFF
"""The 16-bit "none" value (no movie object, no default button, no numeric key)."""

TICKS_PER_SECOND = 45000
"""Playlist and clip times are in 45 kHz ticks."""


def _magic(b: bytes, want: str) -> None:
    if len(b) < 8 or b[:4].decode("latin-1") != want:
        raise MenuParseError(f"not a {want} file")


# ------------------------------------------------------------------------------------------ index.bdmv


@dataclass(frozen=True)
class IndexObject:
    """First play, top menu or one title: an HDMV movie object or a BD-J object."""

    kind: str  # "hdmv", "bdj" or "unknown"
    object_id: int | None = None  # HDMV movie object number
    bdj_name: str | None = None  # BD-J object file (`BDJO/<name>.bdjo`)

    @property
    def is_bdj(self) -> bool:
        return self.kind == "bdj"


@dataclass(frozen=True)
class BdIndex:
    first_play: IndexObject
    top_menu: IndexObject
    titles: tuple[IndexObject, ...]


def _index_object(b: bytes, o: int) -> IndexObject:
    need(b, o, 12)
    t = b[o] >> 6
    if t == 1:
        oid = u16(b, o + 6)
        return IndexObject("hdmv", None if oid == NO_ID else oid)
    if t == 2:
        return IndexObject("bdj", bdj_name=text(b, o + 5, 5))
    return IndexObject("unknown")


def parse_index(b: bytes) -> BdIndex:
    _magic(b, "INDX")
    o = u32(b, 8) + 4  # skip the indexes() length field
    titles = tuple(_index_object(b, o + 26 + i * 12) for i in range(u16(b, o + 24)))
    return BdIndex(_index_object(b, o), _index_object(b, o + 12), titles)


# ------------------------------------------------------------------------------------------ navigation commands

_BRANCH = {
    0: {0: "nop", 1: "goto", 2: "break"},
    1: {0: "jump_object", 1: "jump_title", 2: "call_object", 3: "call_title", 4: "resume"},
    2: {0: "play_pl", 1: "play_pl_pi", 2: "play_pl_pm", 3: "terminate_pl", 4: "link_pi", 5: "link_mk"},
}
_CMP = {1: "bc", 2: "==", 3: "!=", 4: ">=", 5: ">", 6: "<=", 7: "<"}
_SETOP = {
    1: "=", 2: "<->", 3: "+=", 4: "-=", 5: "*=", 6: "/=", 7: "%=", 8: "rnd", 9: "&=", 10: "|=", 11: "^=",
    12: "bitset", 13: "bitclr", 14: "<<=", 15: ">>=",
}  # fmt: skip
_SETSYS = {
    1: "set_stream", 2: "set_nv_timer", 3: "set_button_page", 4: "enable_button", 5: "disable_button",
    6: "set_sec_stream", 7: "popup_off", 8: "still_on", 9: "still_off", 10: "set_output_mode",
    11: "set_stream_ss", 16: "setsystem_0x10",
}  # fmt: skip


def _operand(v: int, imm: int) -> str:
    if imm:
        return f"0x{v:x}"
    if v & 0x80000000:
        return f"PSR{v & 0x7F}"
    return f"GPR{v & 0xFFF}"


def _reg_or_value(v: int, imm: int, mask: int) -> str:
    return f"0x{v & mask:x}" if imm else f"GPR{v & 0xFFF}"


def _stream_number(v: int, imm: int) -> str:
    return f"{v & 0xFFF}" if imm else f"GPR{v & 0xFFF}"


def command_text(raw: bytes) -> str:
    """One 12-byte HDMV command as text, for reading only (the player runs the bytes)."""
    if len(raw) != 12:
        raise MenuParseError("an HDMV command is 12 bytes")
    w, dst, src = u32(raw, 0), u32(raw, 4), u32(raw, 8)
    op_cnt, grp, sub = w >> 29, (w >> 27) & 3, (w >> 24) & 7
    imm1, imm2 = (w >> 23) & 1, (w >> 22) & 1
    br, cmp_, setop = (w >> 16) & 0xF, (w >> 8) & 0xF, w & 0x1F
    a = _operand(dst, imm1) if op_cnt >= 1 else ""
    b = _operand(src, imm2) if op_cnt >= 2 else ""
    args = ", ".join(x for x in (a, b) if x)
    if grp == 0:
        name = _BRANCH.get(sub, {}).get(br)
        return f"{name} {args}".strip() if name else f"?branch {sub}/{br} {raw.hex()}"
    if grp == 1:
        return f"if ({a} {_CMP.get(cmp_, f'?cmp{cmp_}')} {b})" if b else f"?cmp {raw.hex()}"
    if grp == 2 and sub == 0:
        return f"{a} {_SETOP.get(setop, f'?set{setop}')} {b}".strip()
    if grp == 2 and sub == 1:
        name = _SETSYS.get(setop, f"?setsys{setop}")
        parts: list[str] = []
        if setop == 3:  # set_button_page: bit 31 = button flag (dst) / page flag (src), bit 30 = no out effects
            if dst & 0x80000000:
                parts.append(f"button={_reg_or_value(dst, imm1, 0xFFFF)}")
            if src & 0x80000000:
                parts.append(f"page={_reg_or_value(src, imm2, 0xFF)}" + (" no_effects" if src & 0x40000000 else ""))
            return f"{name} " + " ".join(parts)
        if setop in (1, 11):  # set_stream: a flag bit per half, a value or register in the low 12 bits
            if dst & 0x80000000:
                parts.append(f"audio={_stream_number(dst >> 16, imm1)}")
            if dst & 0x8000:
                parts.append(f"pg={_stream_number(dst, imm1)}" + (" display" if dst & 0x4000 else ""))
            if src & 0x80000000:
                parts.append(f"ig={_stream_number(src >> 16, imm2)}")
            if src & 0x8000:
                parts.append(f"angle={_stream_number(src, imm2)}")
            return f"{name} " + " ".join(parts)
        return f"{name} {args}".strip()
    return f"?grp{grp} {raw.hex()}"


@dataclass(frozen=True)
class Command:
    raw: bytes

    @property
    def hex(self) -> str:
        return self.raw.hex()

    @property
    def text(self) -> str:
        return command_text(self.raw)


def commands(b: bytes, o: int, n: int) -> tuple[Command, ...]:
    need(b, o, n * 12)
    return tuple(Command(bytes(b[o + i * 12 : o + i * 12 + 12])) for i in range(n))


# ------------------------------------------------------------------------------------------ MovieObject.bdmv


@dataclass(frozen=True)
class MovieObject:
    resume: bool
    menu_call_mask: bool
    title_search_mask: bool
    cmds: tuple[Command, ...]


def parse_movie_objects(b: bytes) -> tuple[MovieObject, ...]:
    _magic(b, "MOBJ")
    o = 40  # MovieObjects() starts after the 40-byte header
    n = u16(b, o + 8)
    p = o + 10
    out: list[MovieObject] = []
    for _ in range(n):
        flags, nc = u16(b, p), u16(b, p + 2)
        out.append(
            MovieObject(bool(flags & 0x8000), bool(flags & 0x4000), bool(flags & 0x2000), commands(b, p + 4, nc))
        )
        p += 4 + nc * 12
    return tuple(out)


# ------------------------------------------------------------------------------------------ playlists

CODING = {
    0x02: "mpeg2", 0x1B: "h264", 0xEA: "vc1", 0x24: "hevc", 0x80: "lpcm", 0x81: "ac3", 0x82: "dts",
    0x83: "truehd", 0x84: "eac3", 0x85: "dts-hd", 0x86: "dts-ma", 0xA1: "eac3", 0xA2: "dts-hd", 0x90: "pgs",
    0x91: "ig", 0x92: "textst",
}  # fmt: skip
AUDIO_CODINGS = frozenset({0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xA1, 0xA2})
PG_CODING, IG_CODING, TEXTST_CODING = 0x90, 0x91, 0x92
CHANNELS = {1: 1, 3: 2, 6: 6, 12: 2}
"""Audio presentation type -> channels (mono, stereo, multi-channel, stereo + multi-channel)."""

SUBPATH_TYPES = {
    2: "audio_browsable", 3: "ig_menu", 4: "textst", 5: "out_of_mux_pg_ig", 6: "out_of_mux_audio",
    7: "out_of_mux_video",
}  # fmt: skip


@dataclass(frozen=True)
class StreamEntry:
    """One entry of a play item's stream table (STN), numbered from 1 within its kind."""

    n: int
    coding: int
    pid: int
    ref: str  # "item" (the play item's clip) or "subpath"
    subpath: int = 0
    subclip: int = 0
    lang3: str = ""
    audio_format: int = 0

    @property
    def codec(self) -> str:
        return CODING.get(self.coding, hex(self.coding))


@dataclass(frozen=True)
class PlayItem:
    clip: str
    in_time: int
    out_time: int

    @property
    def length(self) -> int:
        return self.out_time - self.in_time


@dataclass(frozen=True)
class PlaylistMark:
    type: str  # "entry" or "link"
    item: int
    time: int  # clip time
    at: int | None  # playlist time: the items before it, then time - in


@dataclass(frozen=True)
class SubPath:
    type: str
    clips: tuple[str, ...]


@dataclass(frozen=True)
class Playlist:
    items: tuple[PlayItem, ...]
    marks: tuple[PlaylistMark, ...]
    subpaths: tuple[SubPath, ...]
    streams: dict[str, tuple[StreamEntry, ...]] = field(default_factory=dict)  # play item 1's STN

    @property
    def starts(self) -> list[int]:
        """Each play item's start in playlist time."""
        out, t = [], 0
        for it in self.items:
            out.append(t)
            t += it.length
        return out

    @property
    def length(self) -> int:
        return sum(it.length for it in self.items)


def _stn(b: bytes, p: int) -> dict[str, tuple[StreamEntry, ...]]:
    counts = (u8(b, p + 4), u8(b, p + 5), u8(b, p + 6), u8(b, p + 7))  # video, audio, pg, ig
    q = p + 16
    out: dict[str, tuple[StreamEntry, ...]] = {}
    for kind, n in zip(("video", "audio", "pg", "ig"), counts, strict=True):
        lst: list[StreamEntry] = []
        for i in range(n):
            eln, et = u8(b, q), u8(b, q + 1)
            ref, sp, sc, pid = "item", 0, 0, 0
            if et == 1:
                pid = u16(b, q + 2)
            elif et == 2:
                ref, sp, sc, pid = "subpath", u8(b, q + 2), u8(b, q + 3), u16(b, q + 4)
            elif et in (3, 4):
                ref, sp, pid = "subpath", u8(b, q + 2), u16(b, q + 3)
            q += 1 + eln
            aln, ct = u8(b, q), u8(b, q + 1)
            lang3, fmt = "", 0
            if ct in AUDIO_CODINGS:
                fmt, lang3 = u8(b, q + 2) >> 4, text(b, q + 3, 3)
            elif ct in (PG_CODING, IG_CODING):
                lang3 = text(b, q + 2, 3)
            elif ct == TEXTST_CODING:
                lang3 = text(b, q + 3, 3)
            q += 1 + aln
            lst.append(StreamEntry(i + 1, ct, pid, ref, sp, sc, lang3, fmt))
        out[kind] = tuple(lst)
    return out


def parse_playlist(b: bytes) -> Playlist:
    _magic(b, "MPLS")
    o = u32(b, 8)
    n_items, n_sub = u16(b, o + 6), u16(b, o + 8)
    p = o + 10
    items: list[PlayItem] = []
    stn: dict[str, tuple[StreamEntry, ...]] | None = None
    for _ in range(n_items):
        ln = u16(b, p)
        clip = text(b, p + 2, 5)
        multi = (u16(b, p + 11) >> 4) & 1
        s = p + 34
        if multi:
            s += 2 + (u8(b, s) - 1) * 10
        if stn is None:
            stn = _stn(b, s)
        items.append(PlayItem(clip, u32(b, p + 14), u32(b, p + 18)))
        p += 2 + ln
    subpaths: list[SubPath] = []
    for _ in range(n_sub):
        ln = u32(b, p)
        sp_type, n_spi = u8(b, p + 5), u8(b, p + 9)
        q = p + 10
        clips: list[str] = []
        for _ in range(n_spi):
            clips.append(text(b, q + 2, 5))
            q += 2 + u16(b, q)
        subpaths.append(SubPath(SUBPATH_TYPES.get(sp_type, f"type{sp_type}"), tuple(clips)))
        p += 4 + ln
    starts, t = [], 0
    for it in items:
        starts.append(t)
        t += it.length
    mo = u32(b, 12)
    marks: list[PlaylistMark] = []
    for i in range(u16(b, mo + 4)):
        m = mo + 6 + i * 14
        typ, item, ts = u8(b, m + 1), u16(b, m + 2), u32(b, m + 4)
        at = starts[item] + ts - items[item].in_time if item < len(items) else None
        marks.append(PlaylistMark({1: "entry", 2: "link"}.get(typ, f"type{typ}"), item, ts, at))
    return Playlist(tuple(items), tuple(marks), tuple(subpaths), stn or {})


# ------------------------------------------------------------------------------------------ clip info


@dataclass(frozen=True)
class ClipStream:
    pid: int
    coding: int
    lang3: str

    @property
    def codec(self) -> str:
        return CODING.get(self.coding, hex(self.coding))


@dataclass(frozen=True)
class ClipInfo:
    pmt_pids: tuple[int, ...]
    streams: tuple[ClipStream, ...]

    def pids(self, coding: int) -> list[int]:
        return sorted(s.pid for s in self.streams if s.coding == coding)


def parse_clip_info(b: bytes) -> ClipInfo:
    _magic(b, "HDMV")
    o = u32(b, 12)
    nprog = u8(b, o + 5)
    p = o + 6
    pmts: list[int] = []
    streams: list[ClipStream] = []
    for _ in range(nprog):
        pmts.append(u16(b, p + 4))
        nst = u8(b, p + 6)
        p += 8
        for _ in range(nst):
            pid, ln, ct = u16(b, p), u8(b, p + 2), u8(b, p + 3)
            lang = ""
            if ct in (PG_CODING, IG_CODING):
                lang = text(b, p + 4, 3)
            elif ct in AUDIO_CODINGS:
                lang = text(b, p + 5, 3)
            elif ct == TEXTST_CODING:
                lang = text(b, p + 5, 3)
            streams.append(ClipStream(pid, ct, lang))
            p += 3 + ln
    return ClipInfo(tuple(pmts), tuple(streams))
