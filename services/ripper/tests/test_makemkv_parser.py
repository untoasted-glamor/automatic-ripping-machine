from pathlib import Path

from arm_common import DiscType
from arm_common.schemas import ScanTitle
from arm_ripper.scan.makemkv import _classify_from_titles, parse_makemkvcon_info

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


def test_includes_titles_without_duration(caplog):
    # No TCOUNT here, so the only way title 5 is known at all is its TINFO
    # lines — it must still surface in the scan (duration_seconds=None)
    # rather than vanishing, so MakeMKV can't rip an untracked "straggler".
    lines = ['TINFO:5,8,0,"4"', 'TINFO:5,27,0,"title05.mkv"']
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert len(titles) == 1
    assert titles[0].index == 5
    assert titles[0].duration_seconds is None
    assert titles[0].chapter_count == 4
    assert titles[0].source_file == "title05.mkv"
    assert len(caplog.records) == 1
    assert "indices: [5]" in caplog.records[0].getMessage()


def test_warns_about_and_includes_title_missing_duration(caplog):
    # Title 0 has a duration; title 1 is seen (TCOUNT counts it, and it has
    # TINFO lines) but never gets a usable TINFO:t,9 duration line. This
    # used to be the "stragglers" bug: MakeMKV may still rip title 1,
    # producing an output file with no corresponding scanned/DB title.
    # It must now still appear in the scan, with duration_seconds=None.
    lines = [
        "TCOUNT:2",
        'TINFO:0,9,0,"1:30:00"',
        'TINFO:1,8,0,"4"',
        'TINFO:1,27,0,"title01.mkv"',
    ]
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert len(titles) == 2
    assert titles[0].index == 0
    assert titles[0].duration_seconds == 90 * 60
    assert titles[1].index == 1
    assert titles[1].duration_seconds is None
    assert titles[1].chapter_count == 4
    assert titles[1].source_file == "title01.mkv"
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "1 title(s)" in message
    assert "indices: [1]" in message


def test_does_not_invent_a_title_that_emitted_no_tinfo_at_all(caplog):
    # e.g. truncated stdout: TCOUNT says 3 but title 2 never appears in any
    # TINFO line. A TCOUNT-only index is evidence of a truncated parse, not of
    # a title: synthesising it produces a duration-less Track the rip
    # dispatcher counts as eligible, which shifts positional file attribution
    # onto the wrong Tracks (or, at best, gets PATCHed FAILED and downgrades a
    # clean rip to ripped_partial). Warn loudly and scan what we can see.
    lines = ["TCOUNT:3", 'TINFO:0,9,0,"1:30:00"', 'TINFO:1,9,0,"0:05:00"']
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert [t.index for t in titles] == [0, 1]
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "TCOUNT=3 but only 2 title(s) emitted TINFO output" in message


def test_warning_when_tcount_lower_than_observed_titles(caplog):
    lines = ["TCOUNT:1", 'TINFO:0,9,0,"1:30:00"', 'TINFO:1,9,0,"0:05:00"']
    with caplog.at_level("WARNING", logger="arm_ripper.scan.makemkv"):
        _, titles, _ = parse_makemkvcon_info(lines)

    assert len(titles) == 2
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "TCOUNT=1 is lower than the 2 title index(es)" in message


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


def test_classify_from_titles_falls_back_to_dvd_when_all_durations_unknown():
    # Degenerate scan: CINFO:1 missing AND no title has a usable duration.
    # Must not crash trying to max() an empty/None-only sequence.
    titles = [ScanTitle(index=0, duration_seconds=None), ScanTitle(index=1, duration_seconds=None)]
    assert _classify_from_titles(titles) == DiscType.DVD


def test_hd_dvd_does_not_match_dvd_branch():
    # "HD-DVD" contains "dvd" — the HD-DVD branch must be checked first
    # so we don't silently treat HD-DVD as a regular DVD.
    lines = ['CINFO:1,6207,"HD-DVD"']
    _, _, disc_type = parse_makemkvcon_info(lines)
    assert disc_type is None  # no enum yet — caller falls back to probe/heuristic
