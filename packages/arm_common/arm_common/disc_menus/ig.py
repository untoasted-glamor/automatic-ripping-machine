"""Interactive graphics (IG): a Blu-ray menu's display sets, pages, buttons and button images.

An IG stream is a series of display sets, each an interactive composition segment (ICS: pages, button overlap
groups, buttons, their commands) plus palettes (PDS) and run-length coded objects (ODS). Layout after libbluray's
`ig_decode.c` and `pg_decode.c`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from arm_common.disc_menus import MenuParseError
from arm_common.disc_menus.binary import u8, u16, u24
from arm_common.disc_menus.hdmv import NO_ID, Command, commands

SEG_PDS, SEG_ODS, SEG_ICS, SEG_END = 0x14, 0x15, 0x18, 0x80


@dataclass(frozen=True)
class EffectSequence:
    windows: int
    effects: int


@dataclass(frozen=True)
class ButtonState:
    start: int  # first object id, NO_ID for none
    end: int  # last object id (an animation runs start..end)
    sound: int = 0xFF  # sound id, 0xFF for none
    repeat: bool = False

    def object_ids(self) -> list[int]:
        if self.start == NO_ID:
            return []
        end = self.end if self.end != NO_ID and self.end >= self.start else self.start
        return list(range(self.start, end + 1))


@dataclass(frozen=True)
class Button:
    id: int
    numeric: int
    auto_action: bool
    x: int
    y: int
    nav: dict[str, int]
    normal: ButtonState
    selected: ButtonState
    activated: ButtonState
    cmds: tuple[Command, ...]


@dataclass(frozen=True)
class ButtonGroup:
    default_valid: int
    buttons: tuple[Button, ...]


@dataclass(frozen=True)
class Page:
    id: int
    version: int
    in_effects: EffectSequence
    out_effects: EffectSequence
    anim_rate: int
    default_selected: int
    default_activated: int
    palette: int
    bogs: tuple[ButtonGroup, ...]


@dataclass(frozen=True)
class Composition:
    width: int
    height: int
    popup: bool
    preloaded: bool
    user_timeout: int  # 90 kHz ticks, 0 for none
    pages: tuple[Page, ...]

    def signature(self) -> tuple[tuple[int, tuple[str, ...]], ...]:
        """What a composition does (its buttons and their commands), to tell display sets apart."""
        return tuple((bt.id, tuple(c.hex for c in bt.cmds)) for pg in self.pages for g in pg.bogs for bt in g.buttons)


@dataclass
class DisplaySet:
    ics: Composition
    palettes: dict[int, dict[int, tuple[int, int, int, int]]] = field(default_factory=dict)  # id -> {index: YCrCbA}
    objects: dict[int, tuple[int, int, bytes]] = field(default_factory=dict)  # id -> (width, height, RLE)


def _effects(b: bytes, o: int) -> tuple[EffectSequence, int]:
    nw = u8(b, o)
    o += 1 + nw * 9
    ne = u8(b, o)
    o += 1
    for _ in range(ne):
        nco = u8(b, o + 4)
        o += 5
        for _ in range(nco):
            cropped = u8(b, o + 3) & 0x80
            o += 16 if cropped else 8
    return EffectSequence(nw, ne), o


def _state(b: bytes, o: int, *, with_flags: bool) -> ButtonState:
    return ButtonState(u16(b, o), u16(b, o + 2), repeat=bool(with_flags and u8(b, o + 4) & 0x80))


def parse_composition(head: bytes, data: bytes) -> Composition:
    """head: the ICS segment's first 9 bytes (video, composition and sequence descriptors);
    data: the reassembled interactive_composition (from its 24-bit length on)."""
    width, height = u16(head, 0), u16(head, 2)
    o = 3
    flags = u8(data, o)
    preloaded, popup = bool(flags >> 7), bool((flags >> 6) & 1)
    o += 1
    if not preloaded:
        o += 10  # composition and selection timeouts of a multiplexed stream
    user_timeout = u24(data, o)
    o += 3
    pages: list[Page] = []
    npages = u8(data, o)
    o += 1
    for _ in range(npages):
        pid, ver = u8(data, o), u8(data, o + 1)
        o += 10  # id, version, 8-byte user operation mask
        ine, o = _effects(data, o)
        oute, o = _effects(data, o)
        anim, dsel, dact, pal, nbog = u8(data, o), u16(data, o + 1), u16(data, o + 3), u8(data, o + 5), u8(data, o + 6)
        o += 7
        bogs: list[ButtonGroup] = []
        for _ in range(nbog):
            dv, nb = u16(data, o), u8(data, o + 2)
            o += 3
            buttons: list[Button] = []
            for _ in range(nb):
                ncmd = u16(data, o + 33)
                buttons.append(
                    Button(
                        id=u16(data, o),
                        numeric=u16(data, o + 2),
                        auto_action=bool(u8(data, o + 4) & 0x80),
                        x=u16(data, o + 5),
                        y=u16(data, o + 7),
                        nav={
                            "up": u16(data, o + 9),
                            "down": u16(data, o + 11),
                            "left": u16(data, o + 13),
                            "right": u16(data, o + 15),
                        },
                        normal=_state(data, o + 17, with_flags=True),
                        selected=ButtonState(
                            u16(data, o + 23), u16(data, o + 25), u8(data, o + 22), bool(u8(data, o + 27) & 0x80)
                        ),
                        activated=ButtonState(u16(data, o + 29), u16(data, o + 31), u8(data, o + 28)),
                        cmds=commands(data, o + 35, ncmd),
                    )
                )
                o += 35 + ncmd * 12
            bogs.append(ButtonGroup(dv, tuple(buttons)))
        pages.append(Page(pid, ver, ine, oute, anim, dsel, dact, pal, tuple(bogs)))
    return Composition(width, height, popup, preloaded, user_timeout, tuple(pages))


def display_sets(payloads: Iterable[bytes]) -> list[DisplaySet]:
    """The display sets of one IG PID, from its PES payloads in order."""
    sets: list[DisplaySet] = []
    cur: DisplaySet | None = None
    ics_head: bytes | None = None
    ics_buf = bytearray()
    ods_buf: dict[int, tuple[int, int, bytearray]] = {}
    for data in payloads:
        o = 0
        while o + 3 <= len(data):
            t, ln = data[o], int.from_bytes(data[o + 1 : o + 3], "big")
            seg = bytes(data[o + 3 : o + 3 + ln])
            o += 3 + ln
            if t == SEG_ICS and len(seg) >= 9:
                first, last = seg[8] & 0x80, seg[8] & 0x40
                if first:
                    ics_head, ics_buf = seg[:9], bytearray()
                ics_buf += seg[9:]
                if last and ics_head is not None:
                    try:
                        cur = DisplaySet(parse_composition(ics_head, bytes(ics_buf)))
                    except MenuParseError:
                        cur = None
                        continue
                    sets.append(cur)
            elif t == SEG_PDS and cur is not None and len(seg) >= 2:
                cur.palettes[seg[0]] = {
                    seg[i]: (seg[i + 1], seg[i + 2], seg[i + 3], seg[i + 4]) for i in range(2, len(seg) - 4, 5)
                }
            elif t == SEG_ODS and cur is not None and len(seg) >= 4:
                oid, flags = int.from_bytes(seg[0:2], "big"), seg[3]
                if flags & 0x80 and len(seg) >= 11:
                    ods_buf[oid] = (
                        int.from_bytes(seg[7:9], "big"),
                        int.from_bytes(seg[9:11], "big"),
                        bytearray(seg[11:]),
                    )
                elif oid in ods_buf:
                    ods_buf[oid][2].extend(seg[4:])
                if flags & 0x40 and oid in ods_buf:
                    w, h, rle = ods_buf.pop(oid)
                    cur.objects[oid] = (w, h, bytes(rle))
    return sets


def decode_rle(data: bytes, w: int, h: int) -> bytearray:
    """A PG/IG object's run-length code -> one palette index per pixel, row by row."""
    pix = bytearray(w * h)
    x = y = i = 0
    n = len(data)
    while i < n and y < h:
        b = data[i]
        i += 1
        if b:
            run, c = 1, b
        else:
            if i >= n:
                break
            b2 = data[i]
            i += 1
            if b2 == 0:
                x, y = 0, y + 1
                continue
            flag, run = b2 >> 6, b2 & 0x3F
            if flag & 1 and i < n:
                run = (run << 8) | data[i]
                i += 1
            c = 0
            if flag & 2 and i < n:
                c = data[i]
                i += 1
        if y < h and x < w:
            end = min(x + run, w)
            pix[y * w + x : y * w + end] = bytes([c]) * (end - x)
        x += run
    return pix


def ycrcb_to_rgba(y: int, cr: int, cb: int, a: int) -> tuple[int, int, int, int]:
    """One palette entry (BT.709, as HD menus use) as RGBA."""
    r = y + 1.5748 * (cr - 128)
    g = y - 0.1873 * (cb - 128) - 0.4681 * (cr - 128)
    b = y + 1.8556 * (cb - 128)
    return (max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b))), a)


def object_rgba(ds: DisplaySet, oid: int, palette_id: int) -> tuple[int, int, bytes] | None:
    """(width, height, RGBA bytes) of one object drawn with a palette, or None when the set lacks the object."""
    if oid not in ds.objects:
        return None
    w, h, rle = ds.objects[oid]
    pal = ds.palettes.get(palette_id) or next(iter(ds.palettes.values()), {})
    lut = [b"\x00\x00\x00\x00"] * 256
    for k, v in pal.items():
        lut[k] = bytes(ycrcb_to_rgba(*v))
    return w, h, b"".join(lut[p] for p in decode_rle(rle, w, h))


def no_id(v: int) -> int | None:
    return None if v == NO_ID else v
