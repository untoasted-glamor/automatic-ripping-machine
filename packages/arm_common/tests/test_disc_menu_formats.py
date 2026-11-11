"""The disc menu readers (`arm_common.disc_menus`), on structures built at test time (fork-only)."""

from __future__ import annotations

import io

import pytest
from arm_common.disc_menus import MenuParseError, binary, dvd, hdmv, ig, pg, ts

from tests import _disc_menu_builders as b

# ------------------------------------------------------------------------------------------ HDMV


def test_index_reads_hdmv_and_bdj_objects() -> None:
    idx = hdmv.parse_index(b.index_bdmv(("hdmv", 0), ("hdmv", 1), [("hdmv", 2), ("bdj", "00001")]))
    assert idx.first_play == hdmv.IndexObject("hdmv", 0)
    assert idx.top_menu.object_id == 1 and not idx.top_menu.is_bdj
    assert idx.titles[0].object_id == 2
    assert idx.titles[1].is_bdj and idx.titles[1].bdj_name == "00001"


def test_index_with_no_top_menu_and_unknown_objects() -> None:
    raw = bytearray(b.index_bdmv(("hdmv", 0xFFFF), ("hdmv", 1), []))
    raw[44 + 12] = 3 << 6  # top menu: an object type that isn't HDMV or BD-J
    idx = hdmv.parse_index(bytes(raw))
    assert idx.first_play.object_id is None
    assert idx.top_menu.kind == "unknown" and idx.titles == ()


@pytest.mark.parametrize("bad", [b"", b"MOBJ0200" + bytes(40), b"INDX0200" + bytes(4)])
def test_index_refuses_other_or_short_files(bad: bytes) -> None:
    with pytest.raises(MenuParseError):
        hdmv.parse_index(bad)


def test_movie_objects_and_command_text() -> None:
    mobj = hdmv.parse_movie_objects(b.movie_objects([[b.set_gpr(1, 0xFFFF), b.play_pl(1)], [b.jump_object(0)]], 0xE000))
    assert len(mobj) == 2
    assert mobj[0].resume and mobj[0].menu_call_mask and mobj[0].title_search_mask
    assert [c.text for c in mobj[0].cmds] == ["GPR1 = 0xffff", "play_pl 0x1"]
    assert mobj[1].cmds[0].text == "jump_object 0x0"
    assert mobj[0].cmds[1].hex == b.play_pl(1).hex()


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (b.hdmv_cmd(0, 0, br=0), "nop"),
        (b.hdmv_cmd(0, 1, op_cnt=1, br=1, dst=3), "jump_title 0x3"),
        (b.hdmv_cmd(0, 2, op_cnt=2, br=5, dst=0, src=0x80000004, imm2=False), "link_mk 0x0, PSR4"),
        (b.hdmv_cmd(0, 3, br=9), "?branch 3/9 " + b.hdmv_cmd(0, 3, br=9).hex()),
        (b.hdmv_cmd(1, 0, op_cnt=2, cmp=2, dst=1, src=5, imm1=False), "if (GPR1 == 0x5)"),
        (b.hdmv_cmd(1, 0, op_cnt=1, cmp=2, dst=1), "?cmp " + b.hdmv_cmd(1, 0, op_cnt=1, cmp=2, dst=1).hex()),
        (b.hdmv_cmd(2, 0, op_cnt=2, setop=3, dst=2, src=1, imm1=False), "GPR2 += 0x1"),
        (
            b.hdmv_cmd(2, 1, op_cnt=2, setop=3, dst=0x80000005, src=0xC0000002),
            "set_button_page button=0x5 page=0x2 no_effects",
        ),
        (
            b.hdmv_cmd(2, 1, op_cnt=2, setop=1, dst=0x8002C003, src=0x80018001),
            "set_stream audio=2 pg=3 display ig=1 angle=1",
        ),
        (b.hdmv_cmd(2, 1, op_cnt=1, setop=4, dst=7), "enable_button 0x7"),
        (b.hdmv_cmd(3, 0), "?grp3 " + b.hdmv_cmd(3, 0).hex()),
    ],
)
def test_command_text(raw: bytes, text: str) -> None:
    assert hdmv.command_text(raw) == text


def test_command_text_needs_12_bytes() -> None:
    with pytest.raises(MenuParseError):
        hdmv.command_text(bytes(8))


def test_playlist_items_marks_streams_and_subpaths() -> None:
    raw = b.mpls(
        [("00014", 0, 90000), ("00015", 45000, 135000)],
        marks=[(1, 0, 0), (1, 1, 90000), (2, 1, 45000)],
        audio=[(0x1100, 0x83, "jpn")],
        pg=[(0x1200, 0x90, "eng"), (0x1201, 0x90, "eng")],
        ig=[(0x1400, "eng", 0)],
        subpaths=[(3, ["00046"])],
    )
    pl = hdmv.parse_playlist(raw)
    assert [(i.clip, i.in_time, i.out_time) for i in pl.items] == [("00014", 0, 90000), ("00015", 45000, 135000)]
    assert pl.starts == [0, 90000] and pl.length == 180000
    assert [(m.type, m.item, m.at) for m in pl.marks] == [("entry", 0, 0), ("entry", 1, 135000), ("link", 1, 90000)]
    assert pl.subpaths == (hdmv.SubPath("ig_menu", ("00046",)),)
    (audio,) = pl.streams["audio"]
    assert (audio.pid, audio.codec, audio.lang3, hdmv.CHANNELS[audio.audio_format]) == (0x1100, "truehd", "jpn", 2)
    assert [(s.n, s.pid) for s in pl.streams["pg"]] == [(1, 0x1200), (2, 0x1201)]
    (menu,) = pl.streams["ig"]
    assert (menu.ref, menu.subpath, menu.pid, menu.lang3) == ("subpath", 0, 0x1400, "eng")


def test_playlist_mark_past_the_items_has_no_playlist_time() -> None:
    pl = hdmv.parse_playlist(b.mpls([("00001", 0, 10)], marks=[(1, 5, 0)]))
    assert pl.marks[0].at is None


def test_clip_info_streams_and_pids() -> None:
    ci = hdmv.parse_clip_info(b.clpi([(0x1011, 0x1B, ""), (0x1100, 0x81, "eng"), (0x1200, 0x90, "eng"),
                                      (0x1400, 0x91, "jpn"), (0x1401, 0x91, "eng")], pmt=0x100))  # fmt: skip
    assert ci.pmt_pids == (0x100,)
    assert ci.pids(hdmv.IG_CODING) == [0x1400, 0x1401]
    assert [s.codec for s in ci.streams] == ["h264", "ac3", "pgs", "ig", "ig"]
    assert [s.lang3 for s in ci.streams] == ["", "eng", "eng", "jpn", "eng"]


def test_binary_readers_refuse_short_reads() -> None:
    assert binary.u24(b"\x01\x02\x03", 0) == 0x010203
    with pytest.raises(MenuParseError):
        binary.u32(b"\x00\x01", 0)


# ------------------------------------------------------------------------------------------ IG, TS, PG


def _menu_segments() -> list[bytes]:
    pal = {1: (235, 128, 128, 255), 2: (16, 128, 128, 128)}
    return [
        b.ics(
            [
                b.ig_button(1, 100, 200, normal=10, selected=11, activated=11, cmds=[b.play_pl(8)], nav=2),
                b.ig_button(2, 100, 300, normal=12, selected=13, activated=13),
            ],
            default_selected=1,
        ),
        b.pds(0, pal),
        b.ods(10, [[1] * 70, [2, 2, 0, 1]]),
        b.ods(11, [[2] * 4]),
        bytes([0x80, 0, 0]),  # end of display set
    ]


def test_ig_stream_round_trip() -> None:
    clip = b.ig_clip(0x1400, _menu_segments())
    payloads = [d for pid, _pts, d in ts.pes_payloads(ts.packets(io.BytesIO(clip)), {0x1400}) if pid == 0x1400]
    (ds,) = ig.display_sets(payloads)
    comp = ds.ics
    assert (comp.width, comp.height, comp.popup, comp.preloaded, comp.user_timeout) == (1920, 1080, False, True, 0)
    (page,) = comp.pages
    assert ig.no_id(page.default_selected) == 1 and ig.no_id(page.default_activated) is None
    b1, b2 = page.bogs[0].buttons
    assert (b1.id, b1.x, b1.y, b1.nav["up"], b1.cmds[0].text) == (1, 100, 200, 2, "play_pl 0x8")
    assert b1.normal.object_ids() == [10] and b2.activated.object_ids() == [13]
    assert ig.ButtonState(0xFFFF, 0xFFFF).object_ids() == []
    assert ig.ButtonState(4, 2).object_ids() == [4]
    assert comp.signature()[0] == (1, (b.play_pl(8).hex(),))
    w, h, rgba = ig.object_rgba(ds, 10, 0) or (0, 0, b"")
    assert (w, h) == (70, 2)
    assert rgba[:4] == bytes(ig.ycrcb_to_rgba(235, 128, 128, 255))
    assert rgba[70 * 4 + 2 * 4 : 70 * 4 + 3 * 4] == b"\x00\x00\x00\x00"  # index 0: transparent
    assert ig.object_rgba(ds, 99, 0) is None


def test_rle_decoder_long_runs_and_truncation() -> None:
    rows = [[0] * 100 + [3] * 70, [5]]
    data = b.rle(rows)
    pix = ig.decode_rle(data, 170, 2)
    assert bytes(pix[:170]) == bytes([0] * 100 + [3] * 70)
    assert pix[170] == 5
    assert bytes(ig.decode_rle(b"\x00", 4, 1)) == bytes(4)


def test_display_sets_skip_a_broken_composition_and_orphans() -> None:
    broken = b.segment(0x18, bytes(9) + b"\x00\x00\x05\x80")  # claims pages it doesn't hold
    orphans = b.pds(0, {1: (1, 2, 3, 4)}) + b.ods(1, [[1]])
    assert ig.display_sets([broken + orphans]) == []


def test_multipart_ods_and_ics() -> None:
    seg = b.ics([b.ig_button(1, 0, 0, normal=1, selected=1, activated=1)])
    body = seg[3:]
    first = b.segment(0x18, body[:8] + bytes([0x80]) + body[9:20])
    last = b.segment(0x18, body[:8] + bytes([0x40]) + body[20:])
    ods_full = b.ods(1, [[1, 1, 1, 1]])[3:]
    ods_a = b.segment(0x15, ods_full[:3] + bytes([0x80]) + ods_full[4:13])
    ods_b = b.segment(0x15, ods_full[:3] + bytes([0x40]) + ods_full[13:])
    (ds,) = ig.display_sets([first, last + ods_a, ods_b])
    assert ds.objects[1][:2] == (4, 1)


def test_packets_and_pid_filter() -> None:
    clip = b.ig_clip(0x1400, _menu_segments()) + b.ts_packets(0x1011, b.pes(bytes(500), stream_id=0xE0))
    src, dst = io.BytesIO(clip + b"\x00" * 10), io.BytesIO()
    n = ts.filter_pids(src, dst, {0x1400})
    assert n == len(b.ig_clip(0x1400, _menu_segments())) // ts.PACKET
    assert all(ts.pid_of(dst.getvalue()[i : i + 192]) == 0x1400 for i in range(0, len(dst.getvalue()), 192))
    bad = bytearray(clip[:192])
    bad[4] = 0
    assert ts.pid_of(bytes(bad)) is None
    assert list(ts.packets(io.BytesIO(clip), max_bytes=1)) != []


def test_pes_without_pts_and_non_pes_starts() -> None:
    pkts = list(ts.packets(io.BytesIO(b.ts_packets(0x1200, b.pes(b.pcs(1), pts=None)))))
    garbage = bytearray(pkts[0])
    garbage[8:11] = b"\x01\x02\x03"  # payload unit start without a PES start code
    out = list(ts.pes_payloads([bytes(garbage), *pkts], {0x1200}))
    assert [(pid, pts) for pid, pts, _ in out] == [(0x1200, None)]
    adapt_only = bytearray(pkts[0])
    adapt_only[7] = 0x20
    assert list(ts.pes_payloads([bytes(adapt_only)], {0x1200})) == []


def test_pg_counts_in_sup_and_payloads() -> None:
    assert pg.count_sup(io.BytesIO(b.sup(3, blank=2))) == 3
    clip = b.pg_clip({0x1200: 4, 0x1201: 1})
    payloads: dict[int, list[bytes]] = {0x1200: [], 0x1201: []}
    for pid, _pts, d in ts.pes_payloads(ts.packets(io.BytesIO(clip)), set(payloads)):
        payloads[pid].append(d)
    assert {p: pg.count_payloads(v) for p, v in payloads.items()} == {0x1200: 4, 0x1201: 1}
    assert pg.count_payloads([b.pcs(0)]) == 0


# ------------------------------------------------------------------------------------------ DVD


def _palette() -> list[tuple[int, int, int]]:
    return [(16 + i * 10, 128, 128) for i in range(16)]


def _vmg(**kw: object) -> bytes:
    root = b.pgc(pre=[b.dvd_cmd_link_pgcn(2)], cells=[(0, 3, 30.0)], palette=_palette(), subp_ctl0=0x80010000)
    return b.vmg_ifo(nvts=1, titles=[(1, 1, 2)], menus={"en": [(3, root)]}, **kw)  # type: ignore[arg-type]


def test_vmg_and_vts() -> None:
    vmg = dvd.parse_vmg(_vmg())
    assert vmg.nvts == 1 and vmg.menu_video.aspect == "16:9" and vmg.menu_video.size == (720, 480)
    assert vmg.titles == (dvd.TitleEntry(1, 1, 2, 1, 1),)
    (root,) = vmg.menus["en"]
    assert root.entry == "root" and root.pre[0].text == "LinkPGCN 2"
    assert root.cells[0].first == 0 and root.cells[0].last == 3 and root.cells[0].duration == 30.0
    assert root.palette[1] == (26, 128, 128) and root.subp_ctl[0] == 0x80010000
    assert dvd.has_menu_pgcs(vmg.menus) and not dvd.has_menu_pgcs({"en": ()})

    title = b.pgc(cells=[(0, 10, 60.0), (11, 20, 60.5)], post=[b.dvd_cmd_jump_tt(1)])
    vts = dvd.parse_vts(b.vts_ifo(title_pgcs=[title], chapters=[[(1, 1), (1, 2)]], audio=[(0, "ja", 6)],
                                  subp=["en", "en"]), 1)  # fmt: skip
    assert vts.audio == (dvd.AudioAttr("ac3", "ja", 6),) and vts.subp == ("en", "en")
    assert vts.title_pgcs[0].title == 1 and vts.title_pgcs[0].post[0].text == "JumpTT 1"
    assert vts.ptt == (((1, 1), (1, 2)),)
    assert vts.menus == {}
    assert dvd.lang3("ja") == "jpn" and dvd.lang3("xx") == "xx"


def test_ifo_magic() -> None:
    with pytest.raises(MenuParseError):
        dvd.parse_vmg(bytes(4096))
    with pytest.raises(MenuParseError):
        dvd.parse_vts(_vmg(), 1)


def test_bcd_time_and_video_attr() -> None:
    assert dvd.bcd_time(bytes([0x01, 0x02, 0x03, 0x40 | 0x25]), 0) == 3723 + 1.0
    assert dvd.bcd_time(bytes([0, 0, 5, 0]), 0) == 5.0
    assert dvd.video_attr(bytes([0x10, 0x08]), 0) == dvd.VideoAttr("PAL", "4:3", (704, 576))


_T, _CMP, _OP, _SOP, _IMM, _IMM55 = (63, 3), (54, 3), (51, 4), (59, 4), (60, 1), (55, 1)


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        ({_T: 0, _OP: 0}, "Nop"),
        ({_T: 0, _OP: 1, (7, 8): 5}, "Goto 5"),
        ({_T: 0, _OP: 2}, "Break"),
        ({_T: 0, _OP: 3, (11, 4): 2, (7, 8): 4}, "SetTmpPML 2, Goto 4"),
        ({_T: 0, _OP: 9}, "?special9"),
        ({_T: 0, _CMP: 2, (39, 8): 0, _IMM55: 1, (31, 16): 5, _OP: 1, (7, 8): 7}, "if (g[0] == 0x5) Goto 7"),
        ({_T: 0, _CMP: 7, (39, 8): 1, (23, 8): 20, _OP: 2}, "if (g[1] < ?reg20) Break"),
        ({_T: 1, _IMM: 1, _OP: 1}, "Exit"),
        ({_T: 1, _IMM: 1, _OP: 2, (22, 7): 2}, "JumpTT 2"),
        ({_T: 1, _IMM: 1, _OP: 3, (22, 7): 2}, "JumpVTS_TT 2"),
        ({_T: 1, _IMM: 1, _OP: 5, (22, 7): 2, (41, 10): 3}, "JumpVTS_PTT 2:3"),
        ({_T: 1, _IMM: 1, _OP: 6, (23, 2): 0}, "JumpSS FP"),
        ({_T: 1, _IMM: 1, _OP: 6, (23, 2): 1, (19, 4): 3}, "JumpSS VMGM (menu 3)"),
        (
            {_T: 1, _IMM: 1, _OP: 6, (23, 2): 2, (30, 7): 1, (38, 7): 1, (19, 4): 3},
            "JumpSS VTSM (vts 1, title 1, menu 3)",
        ),
        ({_T: 1, _IMM: 1, _OP: 6, (23, 2): 3, (46, 15): 4}, "JumpSS VMGM (pgc 4)"),
        ({_T: 1, _IMM: 1, _OP: 8, (23, 2): 1, (19, 4): 3, (31, 8): 2}, "CallSS VMGM (menu 3, rsm_cell 2)"),
        ({_T: 1, _IMM: 1, _OP: 9}, "?jump9"),
        ({_T: 1, _IMM: 1, _CMP: 2, (15, 8): 0x83, (7, 8): 0x84, _OP: 1}, "if (SPRM[3:angle] == SPRM[4:TTN]) Exit"),
        ({_T: 1, _OP: 1, (7, 8): 6, (15, 6): 2}, "LinkNextPG (button 2)"),
        ({_T: 1, _OP: 1, (7, 8): 99}, "?linksub99"),
        ({_T: 1, _OP: 4, (14, 15): 3}, "LinkPGCN 3"),
        ({_T: 1, _OP: 5, (9, 10): 7, (15, 6): 2}, "LinkPTT 7 (button 2)"),
        ({_T: 1, _OP: 6, (6, 7): 3}, "LinkPGN 3"),
        ({_T: 1, _OP: 7, (7, 8): 2}, "LinkCN 2"),
        ({_T: 1, _OP: 15}, "?link15"),
        ({_T: 2, _SOP: 1, _IMM: 1, (39, 1): 1, (38, 7): 8}, "SPRM[1:audio] = 0x8"),
        ({_T: 2, _SOP: 1, (23, 1): 1, (19, 4): 4}, "SPRM[3:angle] = g[4]"),
        ({_T: 2, _SOP: 1}, "Nop"),
        ({_T: 2, _SOP: 2, _IMM: 1, (47, 16): 5}, "SPRM[9:nav_timer] = 0x5; SPRM[10:timer_PGCN] = 0"),
        ({_T: 2, _SOP: 3, _IMM: 1, (19, 4): 2, (47, 16): 7}, "SetMode Register g[2] = 0x7"),
        ({_T: 2, _SOP: 3, (23, 1): 1, (19, 4): 2, (39, 8): 0x81}, "SetMode Counter g[2] = SPRM[1:audio]"),
        ({_T: 2, _SOP: 6, _IMM: 1, (31, 16): 3 << 10}, "SPRM[8:HL_BTNN] = 0xc00 (button 3)"),
        ({_T: 2, _SOP: 6, (19, 4): 3}, "SPRM[8:HL_BTNN] = g[3]"),
        ({_T: 2, _SOP: 6, (19, 4): 3, _OP: 4, (14, 15): 3}, "SPRM[8:HL_BTNN] = g[3], LinkPGCN 3"),
        ({_T: 2, _SOP: 7}, "?sysset7"),
        ({_T: 3, _SOP: 1, (35, 4): 0, _IMM: 1, (31, 16): 7, _OP: 4, (14, 15): 3}, "g[0] = 0x7, LinkPGCN 3"),
        (
            {_T: 3, _CMP: 3, (43, 4): 1, _IMM55: 1, (15, 16): 2, _SOP: 3, (35, 4): 2, _IMM: 1, (31, 16): 1},
            "if (g[1] != 0x2) g[2] += 0x1",
        ),
        ({_T: 3}, "Nop"),
        ({_T: 4, _SOP: 1, _IMM: 1, (47, 16): 1, (7, 8): 1}, "g[0] = 0x1, LinkTopC"),
        ({_T: 4, _CMP: 2, _IMM55: 1, (31, 16): 4, (7, 8): 1}, "Nop, if (g[0] == 0x4) LinkTopC"),
        ({_T: 5, _SOP: 1, _IMM: 1, (47, 16): 1, (7, 8): 1}, "{ g[0] = 0x1, LinkTopC }"),
        ({_T: 6, _SOP: 1, _IMM: 1, (47, 16): 1, (7, 8): 1}, "{ g[0] = 0x1 } LinkTopC"),
        (
            {_T: 5, _CMP: 2, _IMM: 1, (31, 8): 2, (23, 8): 0x81, _SOP: 1, (47, 16): 1, (7, 8): 1},
            "if (g[2] == SPRM[1:audio]) { g[0] = 0x1, LinkTopC }",
        ),
        (
            {_T: 6, _CMP: 4, (39, 8): 3, _IMM55: 1, (31, 16): 9, _SOP: 1, (47, 8): 0x82, (7, 8): 1},
            "if (g[3] >= 0x9) { g[0] = SPRM[2:subp] } LinkTopC",
        ),
        ({_T: 7}, "?type7"),
    ],
)
def test_dvd_command_text(fields: dict[tuple[int, int], int], text: str) -> None:
    assert dvd.DvdCommand(b.dvd_cmd(fields)).text == text


def test_dvd_command_is_8_bytes() -> None:
    with pytest.raises(MenuParseError):
        _ = dvd.DvdCommand(b"\x30").text


def test_nav_pack_highlight_and_pack_payloads() -> None:
    nav = b.nav_pack([((10, 20, 110, 60), (2, 2, 1, 1), b.dvd_cmd_link_pgcn(3))])
    h = dvd.highlight_of(nav)
    assert h is not None and h.nbuttons == 1 and h.groups == 1
    (btn,) = h.buttons
    assert (btn.rect, btn.nav, btn.color, btn.cmd.text) == (
        (10, 20, 110, 60), {"up": 2, "down": 2, "left": 1, "right": 1}, 1, "LinkPGCN 3",
    )  # fmt: skip
    assert h.colors[0][0][0] == (0xF, 0xE, 0xD, 0xC)
    assert h.signature() == (((10, 20, 110, 60), (2, 2, 1, 1), b.dvd_cmd_link_pgcn(3).hex()),)
    assert dvd.highlight_of(b.nav_pack()) is None
    assert dvd.highlight_of(b.video_pack()) is None
    assert dvd.parse_pci(bytes(10)) is None
    kinds = [
        p.kind for pk in (b.video_pack(True), b.audio_pack(), b.spu_pack(1, b"\x00\x04")) for p in dvd.parse_pack(pk)
    ]
    assert kinds == ["video", "audio", "spu"]
    assert dvd.parse_pack(b.video_pack(True))[0].scrambled and not dvd.parse_pack(b.video_pack())[0].scrambled
    assert dvd.parse_pack(bytes(2048)) == []
    mpeg_audio = bytearray(b.video_pack())
    mpeg_audio[17] = 0xC0
    assert dvd.parse_pack(bytes(mpeg_audio))[0].kind == "audio"


def test_sectors_and_subpicture_counts() -> None:
    vob = b.spu_pack(0, b"\x00") + b.spu_pack(0, b"\x00", pts=None) + b.spu_pack(1, b"\x00") + b.video_pack()
    assert len(list(dvd.sectors(vob, 0, 9))) == 4
    assert dvd.count_subpictures(dvd.sectors(vob, 0, 3)) == {0: 1, 1: 1}


def test_spu_decode() -> None:
    rows = [[0, 1, 2, 3], [3, 3, 3, 3], [1, 1, 0, 0]]
    spu = dvd.decode_spu(b.spu_unit((5, 6, 8, 8), rows, colors=0x4321, contrast=0xF0F0))
    assert spu is not None
    assert spu.area == (5, 6, 8, 8) and spu.size == (4, 3)
    assert spu.colors == (4, 3, 2, 1) and spu.contrast == (15, 0, 15, 0) and not spu.forced
    assert [list(r) for r in spu.pixels] == rows
    assert dvd.decode_spu(b"\x00") is None
    assert dvd.decode_spu(b"\x00\x08\x00\x04\xff\xff\xff\xff") is None  # no area


def test_spu_decode_forced_and_end_of_line_runs() -> None:
    unit = bytearray(b.spu_unit((0, 0, 3, 1), [[2, 2, 2, 2], [1, 1, 1, 1]]))
    ctrl = int.from_bytes(unit[2:4], "big")
    unit[ctrl + 4] = 0x00  # forced start instead of start
    unit[4:6] = (0x0002).to_bytes(2, "big")  # row 0: one code, run 0 (to end of line), colour 2
    spu = dvd.decode_spu(bytes(unit))
    assert spu is not None and spu.forced and list(spu.pixels[0]) == [2, 2, 2, 2]
