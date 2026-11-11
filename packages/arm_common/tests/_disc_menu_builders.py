"""Disc menu test inputs, built at test time (fork-only).

No disc data is committed (design v5, "No test files"): these helpers write the few bytes of each structure a test
needs, with `struct`, so the parsers, the ripper's probe and copy, and the pack builders test without a disc.
Imported as `tests._disc_menu_builders` from every service's tests.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from pathlib import Path

# ------------------------------------------------------------------------------------------ HDMV commands


def hdmv_cmd(
    grp: int,
    sub: int,
    *,
    op_cnt: int = 0,
    br: int = 0,
    cmp: int = 0,
    setop: int = 0,
    dst: int = 0,
    src: int = 0,
    imm1: bool = True,
    imm2: bool = True,
) -> bytes:
    w = (op_cnt << 29) | (grp << 27) | (sub << 24) | (int(imm1) << 23) | (int(imm2) << 22)
    w |= (br << 16) | (cmp << 8) | setop
    return struct.pack(">III", w, dst, src)


def play_pl(n: int) -> bytes:
    return hdmv_cmd(0, 2, op_cnt=1, br=0, dst=n)


def jump_object(n: int) -> bytes:
    return hdmv_cmd(0, 1, op_cnt=1, br=0, dst=n)


def set_gpr(reg: int, value: int) -> bytes:
    return hdmv_cmd(2, 0, op_cnt=2, setop=1, dst=reg, src=value, imm1=False)


# ------------------------------------------------------------------------------------------ index.bdmv, MovieObject


def _index_obj(kind: str, ref: int | str, access: int = 0) -> bytes:
    if kind == "hdmv":
        assert isinstance(ref, int)
        return bytes([1 << 6 | access << 4, 0, 0, 0, 0x40, 0]) + struct.pack(">H", ref) + bytes(4)
    assert isinstance(ref, str)
    return bytes([2 << 6 | access << 4, 0, 0, 0, 0x40]) + ref.encode("latin-1")[:5] + bytes(2)


def index_bdmv(
    first_play: tuple[str, int | str] = ("hdmv", 0),
    top_menu: tuple[str, int | str] = ("hdmv", 1),
    titles: Sequence[tuple[str, int | str]] = (("hdmv", 2),),
) -> bytes:
    body = _index_obj(*first_play) + _index_obj(*top_menu) + struct.pack(">H", len(titles))
    body += b"".join(_index_obj(*t) for t in titles)
    head = b"INDX0200" + struct.pack(">II", 40, 0) + bytes(24)
    return head + struct.pack(">I", len(body)) + body


def movie_objects(objects: Sequence[Sequence[bytes]], flags: int = 0) -> bytes:
    body = struct.pack(">IH", 0, len(objects))
    for cmds in objects:
        body += struct.pack(">HH", flags, len(cmds)) + b"".join(cmds)
    head = b"MOBJ0200" + struct.pack(">I", 0) + bytes(28)
    return head + struct.pack(">I", len(body)) + body


# ------------------------------------------------------------------------------------------ playlists, clip info

Stream = tuple[int, int, str]  # (pid, coding, lang3)


def _stream_entry(pid: int, subpath: int | None) -> bytes:
    if subpath is None:
        body = bytes([1]) + struct.pack(">H", pid)
    else:
        body = bytes([3, subpath]) + struct.pack(">H", pid)
    return bytes([9]) + body.ljust(9, b"\x00")


def _stream_attr(coding: int, lang3: str) -> bytes:
    if coding in (0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xA1, 0xA2):
        body = bytes([coding, 3 << 4 | 1]) + lang3.encode()
    elif coding in (0x90, 0x91):
        body = bytes([coding]) + lang3.encode()
    else:
        body = bytes([coding, 0x61])
    return bytes([5]) + body.ljust(5, b"\x00")


def _stn(audio: Sequence[Stream], pg: Sequence[Stream], ig: Sequence[tuple[int, str, int | None]]) -> bytes:
    body = bytes(2) + bytes([1, len(audio), len(pg), len(ig), 0, 0, 0]) + bytes(5)
    body += _stream_entry(0x1011, None) + _stream_attr(0x1B, "")
    for pid, coding, lang in [*audio, *pg]:
        body += _stream_entry(pid, None) + _stream_attr(coding, lang)
    for pid, lang, subpath in ig:
        body += _stream_entry(pid, subpath) + _stream_attr(0x91, lang)
    return struct.pack(">H", len(body)) + body


def mpls(
    items: Sequence[tuple[str, int, int]],
    *,
    marks: Sequence[tuple[int, int, int]] = (),
    audio: Sequence[Stream] = ((0x1100, 0x81, "eng"),),
    pg: Sequence[Stream] = (),
    ig: Sequence[tuple[int, str, int | None]] = (),
    subpaths: Sequence[tuple[int, Sequence[str]]] = (),
) -> bytes:
    """items: (clip, in, out) in 45 kHz; marks: (type, item, time); ig: (pid, lang3, subpath index or None)."""
    pl = struct.pack(">HHH", 0, len(items), len(subpaths))
    stn = _stn(audio, pg, ig)
    for clip, tin, tout in items:
        rest = clip.encode() + b"M2TS" + struct.pack(">HBII", 1, 0, tin, tout) + bytes(8) + bytes([0, 0, 0, 0]) + stn
        pl += struct.pack(">H", len(rest)) + rest
    for sp_type, clips in subpaths:
        sp = bytes([0, sp_type, 0, 0, 0, len(clips)])
        for c in clips:
            spi = c.encode() + b"M2TS" + bytes(15)
            sp += struct.pack(">H", len(spi)) + spi
        pl += struct.pack(">I", len(sp)) + sp
    mk = struct.pack(">H", len(marks))
    for typ, item, t in marks:
        mk += struct.pack(">BBHIHI", 0, typ, item, t, 0xFFFF, 0)
    pl_start = 40
    mk_start = pl_start + 4 + len(pl)
    head = b"MPLS0200" + struct.pack(">III", pl_start, mk_start, 0) + bytes(20)
    return head + struct.pack(">I", len(pl)) + pl + struct.pack(">I", len(mk)) + mk


def clpi(streams: Sequence[Stream], pmt: int = 0x100) -> bytes:
    prog = bytes([0, 1]) + struct.pack(">IHBB", 0, pmt, len(streams), 0)
    for pid, coding, lang in streams:
        attr = _stream_attr(coding, lang)
        prog += struct.pack(">H", pid) + attr
    head = b"HDMV0200" + struct.pack(">II", 0, 40) + bytes(24)
    return head + struct.pack(">I", len(prog)) + prog


# ------------------------------------------------------------------------------------------ IG and PG segments


def segment(t: int, body: bytes) -> bytes:
    return bytes([t]) + struct.pack(">H", len(body)) + body


def rle(rows: Sequence[Sequence[int]]) -> bytes:
    """PG/IG run-length code for rows of palette indexes."""
    out = bytearray()
    for row in rows:
        x = 0
        while x < len(row):
            c, n = row[x], 1
            while x + n < len(row) and row[x + n] == c and n < 0x3FFF:
                n += 1
            if c and n == 1:
                out.append(c)
            else:
                flag = (0x80 if c else 0) | (0x40 if n > 63 else 0)
                out += bytes([0, flag | (n >> 8 if n > 63 else n)])
                if n > 63:
                    out.append(n & 0xFF)
                if c:
                    out.append(c)
            x += n
        out += b"\x00\x00"
    return bytes(out)


def ods(oid: int, rows: Sequence[Sequence[int]]) -> bytes:
    data = rle(rows)
    w, h = len(rows[0]), len(rows)
    body = struct.pack(">HBB", oid, 0, 0xC0) + (len(data) + 4).to_bytes(3, "big") + struct.pack(">HH", w, h) + data
    return segment(0x15, body)


def pds(pid: int, entries: dict[int, tuple[int, int, int, int]]) -> bytes:
    body = bytes([pid, 0]) + b"".join(bytes([k, *v]) for k, v in sorted(entries.items()))
    return segment(0x14, body)


def ig_button(
    bid: int, x: int, y: int, *, normal: int, selected: int, activated: int, cmds: Sequence[bytes] = (), nav: int = 0
) -> bytes:
    n = nav or bid
    return (
        struct.pack(">HHB", bid, 0xFFFF, 0)
        + struct.pack(">HH", x, y)
        + struct.pack(">HHHH", n, n, n, n)
        + struct.pack(">HHB", normal, normal, 0)
        + bytes([0xFF])
        + struct.pack(">HHB", selected, selected, 0)
        + bytes([0xFF])
        + struct.pack(">HH", activated, activated)
        + struct.pack(">H", len(cmds))
        + b"".join(cmds)
    )


def ics(
    buttons: Sequence[bytes],
    *,
    width: int = 1920,
    height: int = 1080,
    popup: bool = False,
    default_selected: int = 0xFFFF,
    palette: int = 0,
) -> bytes:
    page = bytes([0, 0]) + bytes(8) + bytes([0, 0]) + bytes([0, 0])  # id, version, UO mask, no in/out effects
    page += bytes([0]) + struct.pack(">HHBB", default_selected, 0xFFFF, palette, 1)
    page += struct.pack(">HB", 0xFFFF, len(buttons)) + b"".join(buttons)
    comp = bytes([0x80 | (0x40 if popup else 0)]) + (0).to_bytes(3, "big") + bytes([1]) + page
    head = struct.pack(">HHB", width, height, 0x10) + struct.pack(">HB", 0, 0x80) + bytes([0xC0])
    return segment(0x18, head + len(comp).to_bytes(3, "big") + comp)


def pcs(objects: int) -> bytes:
    return segment(0x16, struct.pack(">HHBHBBBB", 1920, 1080, 0x10, 0, 0x80, 0, 0, objects) + bytes(8 * objects))


def sup(events: int, *, blank: int = 0) -> bytes:
    """A `.sup` file: `events` shown compositions and `blank` empty ones (which clear the screen)."""
    out = b""
    for objects in [1] * events + [0] * blank:
        seg = pcs(objects)
        out += b"PG" + bytes(8) + seg
    return out


# ------------------------------------------------------------------------------------------ transport stream


def pes(payload: bytes, pts: int | None = 0, stream_id: int = 0xBD) -> bytes:
    if pts is None:
        head = bytes([0x80, 0, 0])
    else:
        p = bytes([
            0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE), (pts >> 7) & 0xFF,
            0x01 | ((pts << 1) & 0xFE),
        ])  # fmt: skip
        head = bytes([0x80, 0x80, 5]) + p
    body = head + payload
    return b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", min(len(body), 0xFFFF)) + body


def ts_packets(pid: int, data: bytes) -> bytes:
    """One PES split into 192-byte Blu-ray packets on `pid`."""
    out = bytearray()
    first = True
    while data or first:
        chunk = data[:184]
        data = data[184:]
        b1 = (0x40 if first else 0) | (pid >> 8)
        if len(chunk) < 184:
            stuff = 184 - len(chunk) - 1
            af = bytes([stuff]) + (bytes([0]) + b"\xff" * (stuff - 1) if stuff else b"")
            pk = bytes([0x47, b1, pid & 0xFF, 0x30]) + af + chunk
        else:
            pk = bytes([0x47, b1, pid & 0xFF, 0x10]) + chunk
        out += bytes(4) + pk
        first = False
    return bytes(out)


def ig_clip(pid: int, segments: Sequence[bytes]) -> bytes:
    return ts_packets(pid, pes(b"".join(segments)))


def pg_clip(streams: dict[int, int], pad_pid: int = 0x1011, pad_packets: int = 0) -> bytes:
    """A clip with `events` shown PG compositions per PID (one PES each), plus optional filler packets."""
    out = b""
    for pid, events in streams.items():
        for _ in range(events):
            out += ts_packets(pid, pes(pcs(1)))
    return out + ts_packets(pad_pid, pes(bytes(100), stream_id=0xE0)) * pad_packets


# ------------------------------------------------------------------------------------------ DVD

SECTOR = 2048


def dvd_cmd(fields: dict[tuple[int, int], int]) -> bytes:
    """An 8-byte DVD VM command from (start bit, bit count) -> value, numbered as libdvdnav's `vm_getbits`
    (bit 63 is the first bit)."""
    v = 0
    for (start, count), val in fields.items():
        v |= (val & ((1 << count) - 1)) << (start - count + 1)
    return v.to_bytes(8, "big")


def dvd_cmd_link_pgcn(pgcn: int) -> bytes:
    """`LinkPGCN <pgcn>` (type 1, link op 4)."""
    v = (1 << 61) | (4 << 48) | pgcn
    return v.to_bytes(8, "big")


def dvd_cmd_jump_tt(tt: int) -> bytes:
    """`JumpTT <tt>` (type 1, the jump flag, jump op 2)."""
    v = (1 << 61) | (1 << 60) | (2 << 48) | (tt << 16)
    return v.to_bytes(8, "big")


def bcd(secs: float, fps_code: int = 3) -> bytes:
    def b(n: int) -> int:
        return ((n // 10) << 4) | (n % 10)

    s = int(secs)
    frames = round((secs - s) * 29.97)
    return bytes([b(s // 3600), b(s // 60 % 60), b(s % 60), fps_code << 6 | b(frames)])


def pgc(
    *,
    pre: Sequence[bytes] = (),
    post: Sequence[bytes] = (),
    cells: Sequence[tuple[int, int, float]] = (),
    still: int = 0,
    palette: Sequence[tuple[int, int, int]] = (),
    subp_ctl0: int = 0,
    programs: int | None = None,
) -> bytes:
    """A program chain: cells are (first sector, last sector, seconds)."""
    nprog = len(cells) if programs is None else programs
    b = bytearray(0xEC)
    b[2], b[3] = nprog, len(cells)
    b[4:8] = bcd(sum(c[2] for c in cells))
    struct.pack_into(">I", b, 0x1C, subp_ctl0)
    b[0xA2] = still
    for i, (y, cr, cb) in enumerate(palette[:16]):
        b[0xA4 + i * 4 : 0xA8 + i * 4] = bytes([0, y, cr, cb])
    off = 0xEC
    cmds = b""
    if pre or post:
        cmds = (
            struct.pack(">HHHH", len(pre), len(post), 0, 7 + 8 * (len(pre) + len(post)))
            + b"".join(pre)
            + b"".join(post)
        )
        struct.pack_into(">H", b, 0xE4, off)
        off += len(cmds)
    pmap = bytes(range(1, nprog + 1))
    if nprog:
        struct.pack_into(">H", b, 0xE6, off)
        off += len(pmap) + (len(pmap) & 1)
    cellb = b""
    for first, last, secs in cells:
        cellb += bytes([0, 0, still, 0]) + bcd(secs) + struct.pack(">IIII", first, first, first, last)
    if cells:
        struct.pack_into(">H", b, 0xE8, off)
    return bytes(b) + cmds + (pmap + (b"\x00" if len(pmap) & 1 else b"") if nprog else b"") + cellb


def _pgci_ut(menus: dict[str, Sequence[tuple[int, bytes]]]) -> bytes:
    """Menu PGC unit table: per language, (menu type, PGC bytes) list."""
    lus = []
    for _lang, pgcs in menus.items():
        lu = struct.pack(">HHI", len(pgcs), 0, 0)
        srp, body = b"", b""
        base = 8 + 8 * len(pgcs)
        for mtype, p in pgcs:
            srp += bytes([0x80 | mtype, 0]) + struct.pack(">HI", 0, base + len(body))
            body += p
        lus.append(lu + srp + body)
    head = struct.pack(">HHI", len(menus), 0, 0)
    entries, off = b"", 8 + 8 * len(menus)
    for lang, lu in zip(menus, lus, strict=True):
        entries += lang.encode() + bytes([0, 0x80]) + struct.pack(">I", off)
        off += len(lu)
    return head + entries + b"".join(lus)


def _sectors(*blobs: bytes) -> list[bytes]:
    return [blob + bytes(-len(blob) % SECTOR) for blob in blobs]


def _video_attr(aspect169: bool = True) -> bytes:
    return bytes([(0 << 4) | ((3 if aspect169 else 0) << 2), 0])


def vmg_ifo(
    *,
    nvts: int,
    titles: Sequence[tuple[int, int, int]] = (),
    menus: dict[str, Sequence[tuple[int, bytes]]] | None = None,
    first_play: bytes | None = None,
) -> bytes:
    """titles: (vts, vts title number, chapters)."""
    head = bytearray(SECTOR)
    head[:12] = b"DVDVIDEO-VMG"
    struct.pack_into(">H", head, 0x3E, nvts)
    fp = first_play if first_play is not None else pgc()
    struct.pack_into(">I", head, 0x84, 0x400)
    head[0x400 : 0x400 + len(fp)] = fp
    head[0x100:0x102] = _video_attr()
    tt = struct.pack(">HHI", len(titles), 0, 0) + b"".join(
        bytes([0, 1]) + struct.pack(">HH", ch, 0) + bytes([vts, ttn]) + bytes(4) for vts, ttn, ch in titles
    )
    secs = [bytearray(s) for s in _sectors(bytes(head), tt)]
    struct.pack_into(">I", secs[0], 0xC4, 1)
    if menus:
        secs.append(bytearray(_sectors(_pgci_ut(menus))[0]))
        struct.pack_into(">I", secs[0], 0xC8, 2)
    return b"".join(bytes(s) for s in secs)


def vts_ifo(
    *,
    title_pgcs: Sequence[bytes] = (),
    chapters: Sequence[Sequence[tuple[int, int]]] = (),
    menus: dict[str, Sequence[tuple[int, bytes]]] | None = None,
    audio: Sequence[tuple[int, str, int]] = ((0, "en", 2),),
    subp: Sequence[str] = (),
    aspect169: bool = True,
) -> bytes:
    """audio: (coding index, lang, channels); chapters: per title, (pgcn, pgn) list."""
    head = bytearray(SECTOR)
    head[:12] = b"DVDVIDEO-VTS"
    head[0x100:0x102] = _video_attr(aspect169)
    head[0x200:0x202] = _video_attr(aspect169)
    struct.pack_into(">H", head, 0x202, len(audio))
    for i, (coding, lang, ch) in enumerate(audio):
        head[0x204 + i * 8 : 0x208 + i * 8] = bytes([coding << 5, (ch - 1) & 7]) + lang.encode()
    struct.pack_into(">H", head, 0x254, len(subp))
    for i, lang in enumerate(subp):
        head[0x256 + i * 6 + 2 : 0x256 + i * 6 + 4] = lang.encode()
    ptt_off = 8 + 4 * len(chapters)
    ptt_entries = b"".join(struct.pack(">HH", p, g) for t in chapters for p, g in t)
    offs, o = b"", ptt_off
    for t in chapters:
        offs += struct.pack(">I", o)
        o += 4 * len(t)
    ptt = struct.pack(">HHI", len(chapters), 0, ptt_off + len(ptt_entries) - 1) + offs + ptt_entries
    srp, pbody = b"", b""
    base = 8 + 8 * len(title_pgcs)
    for i, p in enumerate(title_pgcs):
        srp += bytes([0x80 | (i + 1), 0]) + struct.pack(">HI", 0, base + len(pbody))
        pbody += p
    pgci = struct.pack(">HHI", len(title_pgcs), 0, 0) + srp + pbody
    tables = [ptt, pgci] + ([_pgci_ut(menus)] if menus else [])
    blobs = _sectors(*tables)
    at, n = [], 1
    for blob in blobs:
        at.append(n)
        n += len(blob) // SECTOR
    for field_offset, sector in zip((0xC8, 0xCC, 0xD0), at, strict=False):
        struct.pack_into(">I", head, field_offset, sector)
    return bytes(head) + b"".join(blobs)


def _pack(*pes_packets: bytes) -> bytes:
    pk = b"\x00\x00\x01\xba" + bytes([0x44, 0, 4, 0, 4, 1, 1, 0x89, 0xC3, 0xF8])
    for p in pes_packets:
        pk += p
    left = SECTOR - len(pk)
    assert left >= 6 or left == 0, left
    if left:
        pk += b"\x00\x00\x01\xbe" + struct.pack(">H", left - 6) + b"\xff" * (left - 6)
    return pk


def _ps_pes(stream_id: int, body: bytes) -> bytes:
    return b"\x00\x00\x01" + bytes([stream_id]) + struct.pack(">H", len(body)) + body


DvdButton = tuple[tuple[int, int, int, int], tuple[int, int, int, int], bytes]  # rect, nav (u/d/l/r), cmd


def nav_pack(buttons: Sequence[DvdButton] = (), *, colors: int = 0xFEDC9876) -> bytes:
    """A NAV pack whose PCI shows `buttons` (none: no highlight)."""
    pci = bytearray(979)
    if buttons:
        struct.pack_into(">H", pci, 0x60, 1)
        struct.pack_into(">H", pci, 0x6E, 1 << 12)  # one button group
        pci[0x71] = len(buttons)
        for i in range(3):
            struct.pack_into(">II", pci, 0x76 + i * 8, colors, colors ^ 0x0F0F0000)
        for i, ((x0, y0, x1, y1), nav, cmd) in enumerate(buttons):
            o = 0x8E + i * 18
            a = (1 << 22) | (x0 << 12) | x1
            c = (y0 << 12) | y1
            pci[o : o + 3] = a.to_bytes(3, "big")
            pci[o + 3 : o + 6] = c.to_bytes(3, "big")
            pci[o + 6 : o + 10] = bytes(nav)
            pci[o + 10 : o + 18] = cmd
    dsi = bytes(1017)
    return _pack(_ps_pes(0xBF, b"\x00" + bytes(pci)), _ps_pes(0xBF, b"\x01" + dsi[:1000]))


def video_pack(scrambled: bool = False) -> bytes:
    flags = 0x80 | (0x10 if scrambled else 0)
    return _pack(_ps_pes(0xE0, bytes([flags, 0, 0]) + bytes(1900)))


def audio_pack() -> bytes:
    return _pack(_ps_pes(0xBD, bytes([0x80, 0, 0, 0x80]) + bytes(1900)))


def spu_unit(area: tuple[int, int, int, int], rows: Sequence[Sequence[int]], *, colors: int = 0x3210,
             contrast: int = 0xFFF0) -> bytes:  # fmt: skip
    """One subpicture unit: 2-bit pixels (0-3) in `rows` filling `area` (x0, y0, x1, y1)."""

    def field(lines: Sequence[Sequence[int]]) -> bytes:
        out = bytearray()
        for row in lines:
            x = 0
            while x < len(row):
                c, n = row[x], 1
                while x + n < len(row) and row[x + n] == c and n < 255:
                    n += 1
                out += struct.pack(">H", n << 2 | c)
                x += n
        return bytes(out)

    f0, f1 = field(rows[0::2]), field(rows[1::2])
    x0, y0, x1, y1 = area
    off0 = 4
    off1 = off0 + len(f0)
    ctrl = off1 + len(f1)
    seq = struct.pack(">HH", 0, ctrl) + bytes([0x01, 0x03]) + struct.pack(">H", colors)
    seq += bytes([0x04]) + struct.pack(">H", contrast)
    seq += bytes([0x05, x0 >> 4, (x0 & 0xF) << 4 | x1 >> 8, x1 & 0xFF, y0 >> 4, (y0 & 0xF) << 4 | y1 >> 8, y1 & 0xFF])
    seq += bytes([0x06]) + struct.pack(">HH", off0, off1) + b"\xff"
    unit = f0 + f1 + seq
    return struct.pack(">HH", 4 + len(unit), ctrl) + unit


def spu_pack(stream: int, data: bytes, *, pts: int | None = 0) -> bytes:
    """One pack carrying (part of) a subpicture unit on `stream` (0-31)."""
    if pts is None:
        head = bytes([0x81, 0, 0])
    else:
        head = bytes([0x81, 0x80, 5]) + pes(b"", pts)[9:14]
    return _pack(_ps_pes(0xBD, head + bytes([0x20 + stream]) + data))


def write_files(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root
