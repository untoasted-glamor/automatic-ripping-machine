"""DVD-Video menu structures: IFOs, program chains (PGCs), DVD VM commands, NAV packs and subpictures.

Layouts follow libdvdread (`ifo_read.c`, `nav_read.c`) and libdvdnav (`vmcmd.c`). IFOs aren't encrypted; CSS
scrambles VOB sectors, which `scrambled` tells from a PES header.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from arm_common.disc_menus import MenuParseError
from arm_common.disc_menus.binary import text, u8, u16, u32

SECTOR = 2048
MENU_TYPE = {2: "title", 3: "root", 4: "subpicture", 5: "audio", 6: "angle", 7: "chapter"}
AUDIO_CODEC = ["ac3", "?", "mpeg1", "mpeg2", "lpcm", "?", "dts", "?"]
LANG3 = {
    "en": "eng", "ja": "jpn", "fr": "fra", "de": "deu", "es": "spa", "it": "ita", "nl": "nld", "pt": "por",
    "pl": "pol", "zh": "zho", "ko": "kor", "ru": "rus", "fy": "fry", "sv": "swe", "da": "dan", "fi": "fin",
    "no": "nor", "cs": "ces", "hu": "hun", "el": "ell", "he": "heb", "ar": "ara", "tr": "tur", "th": "tha",
}  # fmt: skip


def lang3(code: str) -> str:
    """An IFO's two-letter language code as the three-letter code Jellyfin and ARM use."""
    return LANG3.get(code, code)


# ------------------------------------------------------------------------------------------ commands

_CMP = {1: "&", 2: "==", 3: "!=", 4: ">=", 5: ">", 6: "<=", 7: "<"}
_SET = {1: "=", 2: "<->", 3: "+=", 4: "-=", 5: "*=", 6: "/=", 7: "%=", 8: "rnd", 9: "&=", 10: "|=", 11: "^="}
_LINK = {
    0: "LinkNoLink", 1: "LinkTopC", 2: "LinkNextC", 3: "LinkPrevC", 5: "LinkTopPG", 6: "LinkNextPG",
    7: "LinkPrevPG", 9: "LinkTopPGC", 10: "LinkNextPGC", 11: "LinkPrevPGC", 12: "LinkGoUpPGC", 13: "LinkTailPGC",
    16: "RSM",
}  # fmt: skip
_SPRM = {
    0: "menu_lang", 1: "audio", 2: "subp", 3: "angle", 4: "TTN", 5: "VTS_TTN", 6: "TT_PGCN", 7: "PTTN",
    8: "HL_BTNN", 9: "nav_timer", 10: "timer_PGCN", 11: "karaoke", 12: "country", 13: "parental",
    14: "player_cfg", 15: "audio_caps", 16: "audio_lang", 17: "audio_lang_ext", 18: "subp_lang",
    19: "subp_lang_ext", 20: "region",
}  # fmt: skip


class _Text:
    """Text of one 8-byte DVD VM command, after libdvdnav's `vmcmd.c`. Reading only: the player runs the bytes."""

    def __init__(self, raw: bytes) -> None:
        self.v = int.from_bytes(raw, "big")

    def b(self, start: int, count: int) -> int:
        return (self.v >> (start - count + 1)) & ((1 << count) - 1)

    def reg(self, r: int) -> str:
        if r & 0x80:
            return f"SPRM[{r & 0x7F}:{_SPRM.get(r & 0x7F, '?')}]"
        return f"g[{r}]" if r < 16 else f"?reg{r}"

    def rod(self, imm: int, start: int) -> str:
        return f"0x{self.b(start, 16):x}" if imm else self.reg(self.b(start - 8, 8))

    def rod2(self, imm: int, start: int) -> str:
        return f"0x{self.b(start - 1, 7):x}" if imm else f"g[{self.b(start - 4, 4)}]"

    def rod3(self, imm: int, start: int) -> str:
        return f"0x{self.b(start, 16):x}" if imm else self.reg(self.b(start, 8))

    def cmp(self, op: int) -> str:
        return _CMP.get(op, f"?cmp{op}")

    def if1(self) -> str:
        op = self.b(54, 3)
        return f"if (g[{self.b(39, 8)}] {self.cmp(op)} {self.rod(self.b(55, 1), 31)}) " if op else ""

    def if2(self) -> str:
        op = self.b(54, 3)
        return f"if ({self.reg(self.b(15, 8))} {self.cmp(op)} {self.reg(self.b(7, 8))}) " if op else ""

    def if3(self) -> str:
        op = self.b(54, 3)
        return f"if (g[{self.b(43, 4)}] {self.cmp(op)} {self.rod(self.b(55, 1), 15)}) " if op else ""

    def if4(self) -> str:
        op = self.b(54, 3)
        return f"if (g[{self.b(51, 4)}] {self.cmp(op)} {self.rod(self.b(55, 1), 31)}) " if op else ""

    def if5(self) -> str:
        op = self.b(54, 3)
        if not op:
            return ""
        if self.b(60, 1):
            return f"if (g[{self.b(31, 8)}] {self.cmp(op)} {self.reg(self.b(23, 8))}) "
        return f"if (g[{self.b(39, 8)}] {self.cmp(op)} {self.rod(self.b(55, 1), 31)}) "

    def special(self) -> str:
        op = self.b(51, 4)
        return {
            0: "Nop",
            1: f"Goto {self.b(7, 8)}",
            2: "Break",
            3: f"SetTmpPML {self.b(11, 4)}, Goto {self.b(7, 8)}",
        }.get(op, f"?special{op}")

    def linksub(self) -> str:
        op, btn = self.b(7, 8), self.b(15, 6)
        return _LINK.get(op, f"?linksub{op}") + (f" (button {btn})" if btn else "")

    def link(self, optional: bool) -> str:
        op = self.b(51, 4)
        btn = self.b(15, 6)
        bs = f" (button {btn})" if btn else ""
        s = {
            0: "",
            1: self.linksub(),
            4: f"LinkPGCN {self.b(14, 15)}",
            5: f"LinkPTT {self.b(9, 10)}{bs}",
            6: f"LinkPGN {self.b(6, 7)}{bs}",
            7: f"LinkCN {self.b(7, 8)}{bs}",
        }.get(op, f"?link{op}")
        return (", " if optional and op and s else "") + s

    def jump(self) -> str:
        op = self.b(51, 4)
        k = self.b(23, 2)
        if op == 1:
            return "Exit"
        if op == 2:
            return f"JumpTT {self.b(22, 7)}"
        if op == 3:
            return f"JumpVTS_TT {self.b(22, 7)}"
        if op == 5:
            return f"JumpVTS_PTT {self.b(22, 7)}:{self.b(41, 10)}"
        if op == 6:
            return [
                "JumpSS FP",
                f"JumpSS VMGM (menu {self.b(19, 4)})",
                f"JumpSS VTSM (vts {self.b(30, 7)}, title {self.b(38, 7)}, menu {self.b(19, 4)})",
                f"JumpSS VMGM (pgc {self.b(46, 15)})",
            ][k]
        if op == 8:
            r = self.b(31, 8)
            return [
                f"CallSS FP (rsm_cell {r})",
                f"CallSS VMGM (menu {self.b(19, 4)}, rsm_cell {r})",
                f"CallSS VTSM (menu {self.b(19, 4)}, rsm_cell {r})",
                f"CallSS VMGM (pgc {self.b(46, 15)}, rsm_cell {r})",
            ][k]
        return f"?jump{op}"

    def sysset(self) -> str:
        op, imm = self.b(59, 4), self.b(60, 1)
        if op == 1:
            parts = [f"SPRM[{i}:{_SPRM[i]}] = {self.rod2(imm, 47 - i * 8)}" for i in (1, 2, 3) if self.b(47 - i * 8, 1)]
            return "; ".join(parts) or "Nop"
        if op == 2:
            return f"SPRM[9:nav_timer] = {self.rod(imm, 47)}; SPRM[10:timer_PGCN] = {self.b(30, 15)}"
        if op == 3:
            mode = "Counter" if self.b(23, 1) else "Register"
            return f"SetMode {mode} g[{self.b(19, 4)}] = {self.rod(imm, 47)}"
        if op == 6:
            if imm:
                return f"SPRM[8:HL_BTNN] = 0x{self.b(31, 16):x} (button {self.b(31, 6)})"
            return f"SPRM[8:HL_BTNN] = g[{self.b(19, 4)}]"
        return f"?sysset{op}"

    def set_(self, dst: str, op: int, src: str) -> str:
        return f"{dst} {_SET.get(op, f'?set{op}')} {src}" if op else ""

    def text(self) -> str:
        t = self.b(63, 3)
        if t == 0:
            return self.if1() + self.special()
        if t == 1:
            return self.if2() + self.jump() if self.b(60, 1) else self.if1() + self.link(False)
        if t == 2:
            return self.if2() + self.sysset() + self.link(True)
        if t == 3:
            s = self.set_(f"g[{self.b(35, 4)}]", self.b(59, 4), self.rod(self.b(60, 1), 31))
            return self.if3() + (s or "Nop") + self.link(True)
        if t == 4:
            s = self.set_(f"g[{self.b(51, 4)}]", self.b(59, 4), self.rod(self.b(60, 1), 47))
            return (s or "Nop") + ", " + self.if4() + self.linksub()
        if t in (5, 6):
            s = self.set_(f"g[{self.b(51, 4)}]", self.b(59, 4), self.rod3(self.b(60, 1), 47))
            if t == 5:
                return self.if5() + "{ " + s + ", " + self.linksub() + " }"
            return self.if5() + "{ " + s + " } " + self.linksub()
        return f"?type{t}"


@dataclass(frozen=True)
class DvdCommand:
    raw: bytes

    @property
    def hex(self) -> str:
        return self.raw.hex()

    @property
    def text(self) -> str:
        if len(self.raw) != 8:
            raise MenuParseError("a DVD VM command is 8 bytes")
        return _Text(self.raw).text()


def _cmds(b: bytes, o: int, n: int) -> tuple[DvdCommand, ...]:
    if o + n * 8 > len(b):
        raise MenuParseError("PGC command table runs past the IFO")
    return tuple(DvdCommand(bytes(b[o + i * 8 : o + i * 8 + 8])) for i in range(n))


# ------------------------------------------------------------------------------------------ IFOs


def bcd_time(b: bytes, o: int) -> float:
    def d(x: int) -> int:
        return (x >> 4) * 10 + (x & 0xF)

    h, m, s, f = u8(b, o), u8(b, o + 1), u8(b, o + 2), u8(b, o + 3)
    fps = {1: 25.0, 3: 29.97}.get(f >> 6, 0.0)
    return round(d(h) * 3600 + d(m) * 60 + d(s) + (d(f & 0x3F) / fps if fps else 0), 3)


@dataclass(frozen=True)
class VideoAttr:
    standard: str
    aspect: str
    size: tuple[int, int]


def video_attr(b: bytes, o: int) -> VideoAttr:
    a0, a1 = u8(b, o), u8(b, o + 1)
    std = ["NTSC", "PAL", "?", "?"][(a0 >> 4) & 3]
    h = 480 if std == "NTSC" else 576
    size = [(720, h), (704, h), (352, h), (352, h // 2)][(a1 >> 3) & 3]
    return VideoAttr(std, {0: "4:3", 3: "16:9"}.get((a0 >> 2) & 3, "?"), size)


@dataclass(frozen=True)
class Cell:
    block: int
    still: int
    cmd: int
    duration: float
    first: int  # sector, relative to the domain's VOBs
    last_vobu_start: int
    last: int


@dataclass(frozen=True)
class Pgc:
    programs: int
    duration: float
    next: int
    prev: int
    goup: int
    still: int
    palette: tuple[tuple[int, int, int], ...]  # 16 x (Y, Cr, Cb)
    subp_ctl: tuple[int, ...]  # 32 entries
    pre: tuple[DvdCommand, ...]
    post: tuple[DvdCommand, ...]
    cellcmds: tuple[DvdCommand, ...]
    cells: tuple[Cell, ...]
    program_map: tuple[int, ...]
    entry: str | None = None  # a menu PGC's menu type (root, title, ...)
    title: int | None = None  # a title PGC's VTS title number

    def all_commands(self) -> tuple[DvdCommand, ...]:
        return self.pre + self.post + self.cellcmds


def parse_pgc(b: bytes, o: int, *, entry: str | None = None, title: int | None = None) -> Pgc:
    nprog, ncell = u8(b, o + 2), u8(b, o + 3)
    palette = tuple((u8(b, o + 0xA5 + i * 4), u8(b, o + 0xA6 + i * 4), u8(b, o + 0xA7 + i * 4)) for i in range(16))
    subp = tuple(u32(b, o + 0x1C + i * 4) for i in range(32))
    pre: tuple[DvdCommand, ...] = ()
    post: tuple[DvdCommand, ...] = ()
    cellc: tuple[DvdCommand, ...] = ()
    cmd_off, pmap_off, cpb_off = u16(b, o + 0xE4), u16(b, o + 0xE6), u16(b, o + 0xE8)
    if cmd_off:
        c = o + cmd_off
        npre, npost, ncellc = u16(b, c), u16(b, c + 2), u16(b, c + 4)
        allc = _cmds(b, c + 8, npre + npost + ncellc)
        pre, post, cellc = allc[:npre], allc[npre : npre + npost], allc[npre + npost :]
    pmap = tuple(u8(b, o + pmap_off + i) for i in range(nprog)) if pmap_off else ()
    cells: list[Cell] = []
    if cpb_off:
        for i in range(ncell):
            c = o + cpb_off + i * 24
            cells.append(
                Cell(u8(b, c) >> 4, u8(b, c + 2), u8(b, c + 3), bcd_time(b, c + 4), u32(b, c + 8), u32(b, c + 16),
                     u32(b, c + 20))
            )  # fmt: skip
    return Pgc(
        nprog, bcd_time(b, o + 4), u16(b, o + 0x9C), u16(b, o + 0x9E), u16(b, o + 0xA0), u8(b, o + 0xA2), palette,
        subp, pre, post, cellc, tuple(cells), pmap, entry, title,
    )  # fmt: skip


def parse_menu_pgcs(b: bytes, sector: int) -> dict[str, tuple[Pgc, ...]]:
    """A domain's menu PGCs (the PGCI unit table at `sector`), by language unit."""
    if not sector:
        return {}
    base = sector * SECTOR
    out: dict[str, tuple[Pgc, ...]] = {}
    for i in range(u16(b, base)):
        e = base + 8 + i * 8
        lu = base + u32(b, e + 4)
        pgcs: list[Pgc] = []
        for j in range(u16(b, lu)):
            s = lu + 8 + j * 8
            ent = u8(b, s)
            pgcs.append(parse_pgc(b, lu + u32(b, s + 4), entry=MENU_TYPE.get(ent & 0x0F) if ent & 0x80 else None))
        out[text(b, e, 2)] = tuple(pgcs)
    return out


def parse_title_pgcs(b: bytes, sector: int) -> tuple[Pgc, ...]:
    base = sector * SECTOR
    out: list[Pgc] = []
    for j in range(u16(b, base)):
        s = base + 8 + j * 8
        t = u8(b, s)
        out.append(parse_pgc(b, base + u32(b, s + 4), title=t & 0x7F if t & 0x80 else None))
    return tuple(out)


def parse_ptt(b: bytes, sector: int, ntitles: int) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Per VTS title, its chapters as (PGC number, program number)."""
    base = sector * SECTOR
    end = u32(b, base + 4) + 1
    offs = [u32(b, base + 8 + i * 4) for i in range(ntitles)] + [end]
    return tuple(
        tuple((u16(b, base + o), u16(b, base + o + 2)) for o in range(offs[i], offs[i + 1], 4)) for i in range(ntitles)
    )


@dataclass(frozen=True)
class TitleEntry:
    n: int
    angles: int
    chapters: int
    vts: int
    vts_ttn: int


@dataclass(frozen=True)
class Vmg:
    nvts: int
    menu_video: VideoAttr
    first_play: Pgc
    titles: tuple[TitleEntry, ...]
    menus: dict[str, tuple[Pgc, ...]]


def parse_vmg(b: bytes) -> Vmg:
    if b[:12] != b"DVDVIDEO-VMG":
        raise MenuParseError("not a VIDEO_TS.IFO")
    tt = u32(b, 0xC4) * SECTOR
    titles = []
    for i in range(u16(b, tt) if tt else 0):
        e = tt + 8 + i * 12
        titles.append(TitleEntry(i + 1, u8(b, e + 1), u16(b, e + 2), u8(b, e + 6), u8(b, e + 7)))
    return Vmg(
        u16(b, 0x3E), video_attr(b, 0x100), parse_pgc(b, u32(b, 0x84)), tuple(titles), parse_menu_pgcs(b, u32(b, 0xC8))
    )


@dataclass(frozen=True)
class AudioAttr:
    codec: str
    lang: str
    channels: int


@dataclass(frozen=True)
class Vts:
    n: int
    menu_video: VideoAttr
    title_video: VideoAttr
    audio: tuple[AudioAttr, ...]
    subp: tuple[str, ...]  # two-letter languages, in stream order
    title_pgcs: tuple[Pgc, ...]
    ptt: tuple[tuple[tuple[int, int], ...], ...]
    menus: dict[str, tuple[Pgc, ...]] = field(default_factory=dict)


def parse_vts(b: bytes, n: int) -> Vts:
    if b[:12] != b"DVDVIDEO-VTS":
        raise MenuParseError(f"not a VTS_{n:02d}_0.IFO")
    tpgcs = parse_title_pgcs(b, u32(b, 0xCC))
    ptt = parse_ptt(b, u32(b, 0xC8), len([p for p in tpgcs if p.title])) if u32(b, 0xC8) else ()
    audio = tuple(
        AudioAttr(
            AUDIO_CODEC[u8(b, 0x204 + a * 8) >> 5],
            text(b, 0x206 + a * 8, 2).strip("\x00"),
            (u8(b, 0x205 + a * 8) & 7) + 1,
        )
        for a in range(min(u16(b, 0x202), 8))
    )
    subp = tuple(text(b, 0x256 + s * 6 + 2, 2).strip("\x00") for s in range(min(u16(b, 0x254), 32)))
    return Vts(n, video_attr(b, 0x100), video_attr(b, 0x200), audio, subp, tpgcs, ptt, parse_menu_pgcs(b, u32(b, 0xD0)))


def has_menu_pgcs(menus: dict[str, tuple[Pgc, ...]]) -> bool:
    return any(pgcs for pgcs in menus.values())


# ------------------------------------------------------------------------------------------ NAV packs (buttons)


@dataclass(frozen=True)
class HlButton:
    group: int
    n: int
    color: int
    rect: tuple[int, int, int, int]  # x0, y0, x1, y1
    auto_action: int
    nav: dict[str, int]
    cmd: DvdCommand


@dataclass(frozen=True)
class Highlight:
    """The highlight information (HLI) of one NAV pack's PCI: a menu's buttons."""

    status: int
    groups: int
    nbuttons: int
    forced_select: int
    forced_action: int
    colors: tuple[tuple[tuple[tuple[int, ...], tuple[int, ...]], tuple[tuple[int, ...], tuple[int, ...]]], ...]
    buttons: tuple[HlButton, ...]

    def signature(self) -> tuple[tuple[tuple[int, int, int, int], tuple[int, ...], str], ...]:
        return tuple((b.rect, tuple(b.nav.values()), b.cmd.hex) for b in self.buttons)


def _btn_colors(v: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """(colours, contrasts) for e2, e1, pattern, background."""
    return tuple((v >> s) & 0xF for s in (28, 24, 20, 16)), tuple((v >> s) & 0xF for s in (12, 8, 4, 0))


def parse_pci(p: bytes) -> Highlight | None:
    """p = the PCI payload (after the substream id byte). None when the pack has no highlight."""
    hli = 0x60
    if len(p) < 0x8E:
        return None
    ss = u16(p, hli) & 3
    if not ss:
        return None
    btn_md = u16(p, hli + 0x0E)
    ngroups, nbtn = (btn_md >> 12) & 3, u8(p, hli + 0x11)
    colors = tuple((_btn_colors(u32(p, 0x76 + i * 8)), _btn_colors(u32(p, 0x76 + i * 8 + 4))) for i in range(3))
    per_group = 36 // max(ngroups, 1)
    buttons: list[HlButton] = []
    for g in range(max(ngroups, 1)):
        for i in range(min(nbtn, per_group)):
            o = 0x8E + (g * per_group + i) * 18
            if o + 18 > len(p):
                break
            a, c = int.from_bytes(p[o : o + 3], "big"), int.from_bytes(p[o + 3 : o + 6], "big")
            nav = {k: p[o + 6 + j] & 0x3F for j, k in enumerate(("up", "down", "left", "right"))}
            rect = ((a >> 12) & 0x3FF, (c >> 12) & 0x3FF, a & 0x3FF, c & 0x3FF)
            buttons.append(
                HlButton(g + 1, i + 1, a >> 22, rect, (c >> 22) & 3, nav, DvdCommand(bytes(p[o + 10 : o + 18])))
            )
    return Highlight(ss, ngroups, nbtn, u8(p, hli + 0x14), u8(p, hli + 0x15), colors, tuple(buttons))


@dataclass(frozen=True)
class PackPayload:
    kind: str  # "pci", "dsi", "video", "audio" or "spu"
    data: bytes  # the payload; for "spu" the subpicture data after the substream byte
    stream: int = 0  # subpicture stream (0-31)
    pts: int | None = None
    scrambled: bool = False


def scrambled(pes_body: bytes) -> bool:
    """PES_scrambling_control of an MPEG-2 PES header (the body after the 6-byte packet start)."""
    return bool(pes_body and (pes_body[0] & 0xC0) == 0x80 and (pes_body[0] >> 4) & 3)


def parse_pack(pk: bytes) -> list[PackPayload]:
    """The PES packets in one 2048-byte program stream pack."""
    if len(pk) < SECTOR or pk[:4] != b"\x00\x00\x01\xba":
        return []
    out: list[PackPayload] = []
    o = 14 + (pk[13] & 7)
    while o + 6 <= SECTOR and pk[o : o + 3] == b"\x00\x00\x01":
        sid, ln = pk[o + 3], int.from_bytes(pk[o + 4 : o + 6], "big")
        body = pk[o + 6 : o + 6 + ln]
        if sid == 0xBF and body[:1] == b"\x00":
            out.append(PackPayload("pci", body[1:]))
        elif sid == 0xBF and body[:1] == b"\x01":
            out.append(PackPayload("dsi", body[1:]))
        elif sid == 0xE0:
            out.append(PackPayload("video", b"", scrambled=scrambled(body)))
        elif sid == 0xBD and len(body) > 3 and len(body) > 3 + body[2]:
            data = body[3 + body[2] :]
            sub = data[0]
            pts = None
            if body[1] & 0x80 and len(body) >= 8:
                h = body[3:8]
                pts = (
                    ((h[0] >> 1) & 7) << 30
                    | (int.from_bytes(h[1:3], "big") >> 1) << 15
                    | (int.from_bytes(h[3:5], "big") >> 1)
                )
            if 0x20 <= sub <= 0x3F:
                out.append(PackPayload("spu", data[1:], sub - 0x20, pts, scrambled(body)))
            else:
                out.append(PackPayload("audio", b"", scrambled=scrambled(body)))
        elif 0xC0 <= sid <= 0xDF:
            out.append(PackPayload("audio", b"", scrambled=scrambled(body)))
        o += 6 + ln
    return out


def sectors(buf: bytes, first: int, last: int) -> Iterator[bytes]:
    for s in range(first, last + 1):
        pk = buf[s * SECTOR : (s + 1) * SECTOR]
        if len(pk) < SECTOR:
            return
        yield pk


def highlight_of(pk: bytes) -> Highlight | None:
    """The highlight of a NAV pack (a cell's first sector is one), or None."""
    for p in parse_pack(pk):
        if p.kind == "pci":
            return parse_pci(p.data)
    return None


def count_subpictures(packs: Iterable[bytes]) -> dict[int, int]:
    """Subpicture units started (a PES with a PTS) per subpicture stream: the DVD side of sidecar pairing."""
    counts: dict[int, int] = {}
    for pk in packs:
        for p in parse_pack(pk):
            if p.kind == "spu" and p.pts is not None:
                counts[p.stream] = counts.get(p.stream, 0) + 1
    return counts


# ------------------------------------------------------------------------------------------ subpictures


@dataclass(frozen=True)
class Spu:
    area: tuple[int, int, int, int]  # x0, y0, x1, y1
    colors: tuple[int, ...]  # e2, e1, pattern, background palette indexes
    contrast: tuple[int, ...]
    forced: bool
    pixels: tuple[bytes, ...]  # one 2-bit value per pixel (0 bg, 1 pattern, 2 e1, 3 e2), row by row

    @property
    def size(self) -> tuple[int, int]:
        return self.area[2] - self.area[0] + 1, self.area[3] - self.area[1] + 1


def decode_spu(pkt: bytes) -> Spu | None:
    """One whole subpicture unit -> its area, colours and pixels, or None when it can't be drawn."""
    if len(pkt) < 4:
        return None
    size, ctrl = u16(pkt, 0), u16(pkt, 2)
    if size > len(pkt) or ctrl >= size:
        return None
    colors: tuple[int, ...] = (0, 0, 0, 0)
    contrast: tuple[int, ...] = (0, 0, 0, 0)
    area: tuple[int, int, int, int] | None = None
    offsets: tuple[int, int] | None = None
    forced = False
    seq, seen = ctrl, set()
    while seq not in seen and seq + 4 <= size:
        seen.add(seq)
        nxt = u16(pkt, seq + 2)
        i = seq + 4
        while i < size:
            c = pkt[i]
            i += 1
            if c == 0x00:
                forced = True
            elif c in (0x01, 0x02):
                pass
            elif c == 0x03 and i + 2 <= size:
                v = u16(pkt, i)
                colors = tuple((v >> s) & 0xF for s in (12, 8, 4, 0))
                i += 2
            elif c == 0x04 and i + 2 <= size:
                v = u16(pkt, i)
                contrast = tuple((v >> s) & 0xF for s in (12, 8, 4, 0))
                i += 2
            elif c == 0x05 and i + 6 <= size:
                d = pkt[i : i + 6]
                area = ((d[0] << 4) | (d[1] >> 4), (d[3] << 4) | (d[4] >> 4), ((d[1] & 0xF) << 8) | d[2],
                        ((d[4] & 0xF) << 8) | d[5])  # fmt: skip
                i += 6
            elif c == 0x06 and i + 4 <= size:
                offsets = (u16(pkt, i), u16(pkt, i + 2))
                i += 4
            elif c == 0x07 and i + 2 <= size:
                i += u16(pkt, i)  # CHG_COLCON: skipped
            else:
                break
        if nxt == seq:
            break
        seq = nxt
    if area is None or offsets is None or area[2] < area[0] or area[3] < area[1]:
        return None
    w, h = area[2] - area[0] + 1, area[3] - area[1] + 1
    rows = [bytearray(w) for _ in range(h)]
    end = ctrl * 2
    for fld in (0, 1):
        pos = offsets[fld] * 2  # nibble position

        def nib() -> int:
            nonlocal pos
            if pos >> 1 >= len(pkt):
                pos += 1
                return 0
            v = (pkt[pos >> 1] >> (0 if pos & 1 else 4)) & 0xF
            pos += 1
            return v

        for y in range(fld, h, 2):
            x = 0
            while x < w and pos < end:
                v = nib()
                if v < 0x4:
                    v = (v << 4) | nib()
                    if v < 0x10:
                        v = (v << 4) | nib()
                        if v < 0x40:
                            v = (v << 4) | nib()
                n, col = v >> 2, v & 3
                if n == 0:
                    n = w - x
                stop = min(x + n, w)
                rows[y][x:stop] = bytes([col]) * (stop - x)
                x += n
            if pos & 1:
                pos += 1
    return Spu(area, colors, contrast, forced, tuple(bytes(r) for r in rows))
