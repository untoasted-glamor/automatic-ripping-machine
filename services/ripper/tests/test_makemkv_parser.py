from pathlib import Path

from arm_common import DiscType
from arm_ripper.scan.makemkv import parse_makemkvcon_info

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_dvd_with_two_titles():
    lines = (FIXTURES / "makemkvcon_dvd_short.txt").read_text().splitlines()
    volume_label, titles, disc_type = parse_makemkvcon_info(lines)

    assert volume_label == "THE_MATRIX_1999"  # CINFO:2 wins (first match)
    assert disc_type == DiscType.DVD  # CINFO:1,"DVD"
    assert len(titles) == 2

    feature = titles[0]
    assert feature.index == 0
    assert feature.duration_seconds == 2 * 3600 + 16 * 60 + 17
    assert feature.chapter_count == 32
    assert feature.size_bytes == 7340032000
    assert feature.source_file == "title00.mkv"

    extra = titles[1]
    assert extra.index == 1
    assert extra.duration_seconds == 113


def test_handles_disc_with_no_titles():
    lines = (FIXTURES / "makemkvcon_no_titles.txt").read_text().splitlines()
    volume_label, titles, disc_type = parse_makemkvcon_info(lines)

    assert volume_label is None
    assert titles == []
    assert disc_type is None


def test_skips_titles_without_duration():
    lines = ['TINFO:5,8,0,"4"', 'TINFO:5,27,0,"title05.mkv"']
    _, titles, _ = parse_makemkvcon_info(lines)
    assert titles == []


def test_warns_when_tcount_exceeds_parsed_titles(caplog):
    # Title 0 has a duration and survives; title 1 is seen (TCOUNT counts it)
    # but never gets a usable TINFO:t,9 duration line, so it's dropped. This
    # is the "stragglers" bug: MakeMKV may still rip title 1, producing an
    # output file with no corresponding scanned/DB title — the cross-check
    # must surface that instead of staying silent.
    lines = [
        "TCOUNT:2",
        'TINFO:0,9,0,"1:30:00"',
        'TINFO:1,8,0,"4"',
        'TINFO:1,27,0,"title01.mkv"',
    ]
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert len(titles) == 1
    assert titles[0].index == 0
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "TCOUNT=2" in message
    assert "1 parsed" in message
    assert "not parsed: [1]" in message


def test_warning_names_titles_with_no_tinfo_at_all(caplog):
    # e.g. truncated stdout: TCOUNT says 3 but title 2 never appears.
    lines = ["TCOUNT:3", 'TINFO:0,9,0,"1:30:00"', 'TINFO:1,9,0,"0:05:00"']
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert len(titles) == 2
    assert len(caplog.records) == 1
    assert "not parsed: [2]" in caplog.records[0].getMessage()


def test_warning_when_tcount_below_parsed_titles(caplog):
    lines = ["TCOUNT:1", 'TINFO:0,9,0,"1:30:00"', 'TINFO:1,9,0,"0:05:00"']
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert len(titles) == 2
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "TCOUNT=1 does not match 2 parsed" in message
    assert "only" not in message


def test_no_warning_when_tcount_matches_parsed_titles(caplog):
    lines = ["TCOUNT:1", 'TINFO:0,9,0,"1:30:00"']
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert len(titles) == 1
    assert caplog.records == []


def test_ignores_unknown_message_types():
    lines = ['MSG:1005,0,1,"hello"', "DRV:0,2,...", "BOGUS:nope"]
    volume_label, titles, disc_type = parse_makemkvcon_info(lines)
    assert volume_label is None
    assert titles == []
    assert disc_type is None


def test_classifies_blu_ray_from_cinfo():
    # CINFO:1,N,"Blu-ray disc" is the upstream value for BD-Video discs.
    lines = ['CINFO:1,6210,"Blu-ray disc"', 'CINFO:2,0,"GUARDIANS_OF_THE_GALAXY"']
    volume_label, _, disc_type = parse_makemkvcon_info(lines)
    assert volume_label == "GUARDIANS_OF_THE_GALAXY"
    assert disc_type == DiscType.BLURAY


def test_classifies_audio_cd_from_cinfo():
    lines = ['CINFO:1,6201,"Audio CD"']
    _, _, disc_type = parse_makemkvcon_info(lines)
    assert disc_type == DiscType.CD


def test_classifies_unknown_cinfo_string_as_none():
    lines = ['CINFO:1,9999,"Mystery format"']
    _, _, disc_type = parse_makemkvcon_info(lines)
    assert disc_type is None


def test_classifies_dvd_disc_suffix_from_cinfo():
    # MakeMKV v1.18 emits "DVD disc" rather than the "DVD" string v1.17 emitted.
    # Real example from a region-locked Blood Diamond DVD-9 we saw in prod.
    lines = ['CINFO:1,6206,"DVD disc"', 'CINFO:2,0,"BLOOD DIAMOND"']
    volume_label, _, disc_type = parse_makemkvcon_info(lines)
    assert volume_label == "BLOOD DIAMOND"
    assert disc_type == DiscType.DVD


def test_hd_dvd_does_not_match_dvd_branch():
    # "HD-DVD" contains "dvd" — the HD-DVD branch must be checked first
    # so we don't silently treat HD-DVD as a regular DVD.
    lines = ['CINFO:1,6207,"HD-DVD"']
    _, _, disc_type = parse_makemkvcon_info(lines)
    assert disc_type is None  # no enum yet — caller falls back to probe/heuristic
