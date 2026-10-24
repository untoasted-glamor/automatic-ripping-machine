"""SINFO stream parsing (`parse_makemkvcon_info` -> `ScanTitle.streams`),
checked against the real makemkvcon recordings in tests/fixtures."""

from __future__ import annotations

from arm_common.schemas import ScanResult, ScanTitle
from arm_ripper.scan.makemkv import parse_makemkvcon_info
from tests._makemkvcon_fake import FIXTURES


def _titles(name: str) -> list[ScanTitle]:
    _, titles, _ = parse_makemkvcon_info((FIXTURES / name).read_text().splitlines())
    return titles


def test_hanako_bd_title_streams() -> None:
    title = _titles("makemkvcon_bd_hanako_info.txt")[0]
    audio = [s for s in title.streams if s.kind == "audio"]
    subs = [s for s in title.streams if s.kind == "subtitle"]
    assert [(s.index, s.language, s.codec, s.channels, s.derived) for s in audio] == [
        (1, "eng", "TrueHD", 6, False),
        (2, "eng", "DD", 6, True),  # the AC-3 core of the TrueHD track
        (3, "jpn", "TrueHD", 2, False),
        (4, "jpn", "DD", 2, True),
    ]
    assert audio[0].description == "TrueHD Surround 5.1 English"
    assert audio[0].codec_id == "A_TRUEHD"
    assert audio[0].language_name == "English"
    assert audio[0].default is True
    assert [(s.index, s.forced, s.derived) for s in subs] == [
        (5, False, False),
        (6, True, True),
        (7, False, False),
        (8, True, True),
    ]
    # Runs of spaces in MakeMKV's text are collapsed.
    assert subs[1].description == "PGS English (forced only)"
    # The video stream is not listed.
    assert all(s.index != 0 for s in title.streams)


def test_every_hanako_title_lists_its_streams() -> None:
    titles = _titles("makemkvcon_bd_hanako_info.txt")
    assert titles and all(t.streams for t in titles)


def test_acca_dvd_vobsub_streams() -> None:
    title = _titles("makemkvcon_dvd_acca_info.txt")[0]
    assert [(s.kind, s.language, s.codec_id, s.codec) for s in title.streams] == [
        ("audio", "eng", "A_AC3", "DD"),
        ("audio", "jpn", "A_AC3", "DD"),
        ("subtitle", "eng", "S_VOBSUB", None),  # MakeMKV gives VobSub no short codec name
        ("subtitle", "eng", "S_VOBSUB", None),
    ]
    assert title.streams[2].description == "English"


def test_sintel_iso_subtitle_languages() -> None:
    titles = _titles("makemkvcon_iso_sintel_info.txt")
    # The long title has eight English audio streams and no subtitles; others carry VobSub.
    assert {s.kind for s in titles[0].streams} == {"audio"}
    sub_languages = {s.language for t in titles for s in t.streams if s.kind == "subtitle"}
    assert {"eng", "dut", "spa"} <= sub_languages


def test_untagged_bad_and_unknown_stream_lines() -> None:
    lines = [
        'TINFO:0,9,0,"0:10:00"',
        'SINFO:0,1,1,6202,"Audio"',
        'SINFO:0,1,14,0,"two"',  # not a number
        'SINFO:0,2,1,6203,"Subtitles"',
        'SINFO:0,2,3,0,"xx"',  # not a language code
        'SINFO:0,2,22,0,"4096"',
        'SINFO:0,2,38,0,""',
        'SINFO:0,3,1,6299,"Something"',  # unknown stream type
        'SINFO:0,4,5,0,"A_AC3"',  # no type at all
        'SINFO:x,1,1,6202,"Audio"',  # malformed ids
        "SINFO:0,1,1",  # too few fields
        'SINFO:1,1,1,6202,"Audio"',  # title with no TINFO: not a title
    ]
    _, titles, _ = parse_makemkvcon_info(lines)
    assert len(titles) == 1
    audio, sub = titles[0].streams
    assert (audio.kind, audio.language, audio.channels, audio.description) == ("audio", "und", None, None)
    assert (sub.kind, sub.language, sub.forced, sub.derived, sub.default) == ("subtitle", "und", True, False, False)


def test_a_stored_scan_without_streams_still_loads() -> None:
    scan = ScanResult.model_validate(
        {"disc_type": "bluray", "titles": [{"index": 0, "duration_seconds": 1447, "source_file": "t00.mkv"}]}
    )
    assert scan.titles[0].streams == []
