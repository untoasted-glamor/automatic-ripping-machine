"""TheDiscDB identity source: duration parse, SourceFile join, claim build."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_backend.identity.sources.thediscdb import (  # noqa: E402
    build_claims,
    external_imdb_id,
    parse_duration,
    role_for_disc_type,
)
from arm_backend.identity.sources.thediscdb_snapshot import DiscMatch  # noqa: E402
from arm_common import DiscType  # noqa: E402
from arm_common.enums import TrackRole  # noqa: E402
from arm_common.schemas import ScanResult, ScanTitle  # noqa: E402

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


def _match(titles: list[dict[str, Any]], kind: str = "movie") -> DiscMatch:
    return DiscMatch(
        kind=kind,
        title_slug="round-midnight-1986",
        release_slug="2022-criterion-blu-ray",
        disc={"ContentHash": "2D61282D8DA5EAC2CA87B451BCE9A055", "Titles": titles},
        metadata={"Title": "Round Midnight", "Year": 1986, "ExternalIds": {"Imdb": "tt0090557", "Tmdb": "14670"}},
        release={"Slug": "2022-criterion-blu-ray"},
    )


def test_parse_duration() -> None:
    assert parse_duration("2:11:34") == 2 * 3600 + 11 * 60 + 34
    assert parse_duration("56:03") == 56 * 60 + 3
    assert parse_duration("") is None
    assert parse_duration("garbage") is None


def test_parse_duration_non_numeric_component_returns_none() -> None:
    # Right shape (2-3 colon-separated parts) but non-numeric -> int() raises
    # ValueError inside the try, caught and treated as unparseable.
    assert parse_duration("aa:bb") is None


def test_role_for_disc_type() -> None:
    assert role_for_disc_type("MainMovie") is TrackRole.MAIN
    assert role_for_disc_type("Episode") is TrackRole.EPISODE
    assert role_for_disc_type("Trailer") is TrackRole.TRAILER
    assert role_for_disc_type("Featurette") is TrackRole.EXTRA
    assert role_for_disc_type("Nonsense") is TrackRole.OTHER


def test_build_claims_joins_by_source_playlist() -> None:
    match = _match(
        [
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Comment": "Main.mkv",
                "Item": {"Title": "Round Midnight", "Type": "MainMovie"},
            },
            {
                "SourceFile": "00011.mpls",
                "Duration": "0:12:00",
                "Comment": "Making Of.mkv",
                "Item": {"Title": "The Making Of", "Type": "Featurette"},
            },
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[
            ScanTitle(index=0, duration_seconds=7894, source_playlist="00001.mpls"),
            ScanTitle(index=1, duration_seconds=720, source_playlist="00011.mpls"),
            ScanTitle(index=2, duration_seconds=30, source_playlist="00029.mpls"),  # not in db
        ],
    )
    result = build_claims(match, scan, now=NOW)
    assert result.extra["release_slug"] == match.release_slug
    assert result.tracks["0"].role is TrackRole.MAIN
    assert result.tracks["0"].episode_name == "Round Midnight"
    assert result.tracks["0"].season is None
    assert result.tracks["0"].episode is None
    assert result.tracks["0"].filename == "Main.mkv"
    assert result.tracks["0"].selected is True
    assert result.tracks["1"].role is TrackRole.EXTRA
    assert "selected" not in result.tracks["1"].model_fields_set
    assert "2" not in result.tracks  # unmatched scan title untouched


def test_build_claims_series_episode_fields() -> None:
    match = _match(
        [
            {
                "SourceFile": "00800.mpls",
                "Duration": "1:06:55",
                "Comment": "1883 S01E01.mkv",
                "Item": {"Title": "1883", "Type": "Episode", "Season": "1", "Episode": "1"},
            }
        ],
        kind="series",
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=5, duration_seconds=4015, source_playlist="00800.mpls")],
    )
    result = build_claims(match, scan, now=NOW)
    assert result.tracks["5"].role is TrackRole.EPISODE
    assert result.tracks["5"].episode_name == "1883"
    assert result.tracks["5"].season == 1
    assert result.tracks["5"].episode == 1
    assert result.tracks["5"].filename == "1883 S01E01.mkv"
    assert result.tracks["5"].selected is True


def test_build_claims_duration_fallback_when_no_playlist() -> None:
    # DVD scans have no playlist (TINFO 16 is Blu-ray only); duration within +-2s joins.
    match = _match(
        [
            {
                "SourceFile": "VTS_01_1.VOB",
                "Duration": "1:30:00",
                "Comment": "Movie.mkv",
                "Item": {"Title": "Movie", "Type": "MainMovie"},
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=0, duration_seconds=5401, source_playlist=None)],
    )
    result = build_claims(match, scan, now=NOW)
    assert result.tracks["0"].role is TrackRole.MAIN


def test_build_claims_ambiguous_duration_no_join() -> None:
    # Two scan titles inside the window and no playlist -> ambiguous, skip.
    match = _match(
        [
            {
                "SourceFile": "VTS_01_1.VOB",
                "Duration": "1:30:00",
                "Comment": "Movie.mkv",
                "Item": {"Title": "Movie", "Type": "MainMovie"},
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[
            ScanTitle(index=0, duration_seconds=5400, source_playlist=None),
            ScanTitle(index=1, duration_seconds=5401, source_playlist=None),
        ],
    )
    assert build_claims(match, scan, now=NOW).tracks == {}


def test_build_claims_duration_fallback_skips_title_with_unknown_duration() -> None:
    # A scan title with no duration can't be matched by duration; the one
    # title that can still joins.
    match = _match(
        [
            {
                "SourceFile": "VTS_01_1.VOB",
                "Duration": "1:30:00",
                "Comment": "Movie.mkv",
                "Item": {"Title": "Movie", "Type": "MainMovie"},
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[
            ScanTitle(index=0, duration_seconds=None, source_playlist=None),
            ScanTitle(index=1, duration_seconds=5401, source_playlist=None),
        ],
    )
    assert list(build_claims(match, scan, now=NOW).tracks) == ["1"]


def test_build_claims_duration_fallback_ignores_a_title_with_a_playlist() -> None:
    # Duration fallback only considers scan titles with no playlist. A title
    # WITH a playlist that isn't in the disc map is a different title, even
    # inside the duration window.
    match = _match(
        [
            {
                "SourceFile": "VTS_01_1.VOB",
                "Duration": "1:30:00",
                "Comment": "Movie.mkv",
                "Item": {"Title": "Movie", "Type": "MainMovie"},
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[
            # This has a playlist, so duration fallback should skip it.
            ScanTitle(index=0, duration_seconds=5401, source_playlist="00002.mpls"),
        ],
    )
    result = build_claims(match, scan, now=NOW)
    # No join by duration fallback since the title has a playlist.
    assert result.tracks == {}


def test_build_claims_skips_non_dict_title_entry() -> None:
    # Malformed third-party JSON: a non-dict entry in Titles must be skipped,
    # not raise (AttributeError on entry.get would otherwise 500 identify).
    match = _match(
        [
            "not a dict",
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Comment": "Main.mkv",
                "Item": {"Title": "Round Midnight", "Type": "MainMovie"},
            },
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=0, duration_seconds=7894, source_playlist="00001.mpls")],
    )
    result = build_claims(match, scan, now=NOW)
    assert result.tracks["0"].role is TrackRole.MAIN


def test_build_claims_non_dict_item_treated_as_missing() -> None:
    # Item present but not a dict (upstream layout drift) -> treated as {}.
    match = _match(
        [
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Comment": "Main.mkv",
                "Item": "also not a dict",
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=0, duration_seconds=7894, source_playlist="00001.mpls")],
    )
    result = build_claims(match, scan, now=NOW)
    assert result.tracks["0"].role is TrackRole.OTHER
    assert result.tracks["0"].episode_name is None


def test_build_claims_selects_only_main_and_episodes() -> None:
    # Reuse the featurette fixture from the old test_apply_map_featurette_not_selected:
    # a Featurette entry must produce a claim WITHOUT `selected` in model_fields_set.
    match = _match(
        [
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Comment": "bonus.mkv",
                "Item": {"Title": "Behind the Scenes", "Type": "Featurette"},
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=0, duration_seconds=7894, source_playlist="00001.mpls")],
    )
    result = build_claims(match, scan, now=NOW)
    assert result.tracks["0"].role is TrackRole.EXTRA
    assert "selected" not in result.tracks["0"].model_fields_set


def test_build_claims_malformed_season_episode_ignored() -> None:
    # Malformed Season/Episode (e.g. non-numeric) must not raise; the field
    # is simply absent from the claim. No Comment key here either, exercising
    # the "no filename" path.
    match = _match(
        [
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Item": {
                    "Title": "Round Midnight",
                    "Type": "MainMovie",
                    "Season": "garbage",
                    "Episode": "also_garbage",
                },
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=0, duration_seconds=7894, source_playlist="00001.mpls")],
    )
    result = build_claims(match, scan, now=NOW)
    assert "season" not in result.tracks["0"].model_fields_set
    assert "episode" not in result.tracks["0"].model_fields_set
    assert "filename" not in result.tracks["0"].model_fields_set


def test_build_claims_duration_fallback_unparseable_duration_no_join() -> None:
    # No SourceFile match, and the disc entry's own Duration is unparseable
    # -> the duration-fallback branch bails before it can even look for
    # candidates.
    match = _match(
        [
            {
                "SourceFile": "nomatch.mpls",
                "Duration": "garbage",
                "Comment": "Movie.mkv",
                "Item": {"Title": "Movie", "Type": "MainMovie"},
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[ScanTitle(index=0, duration_seconds=5401, source_playlist="other.mpls")],
    )
    assert build_claims(match, scan, now=NOW).tracks == {}


def test_build_claims_duplicate_ref_first_entry_wins() -> None:
    # Two disc entries joining to the same scan title (e.g. a duplicated
    # SourceFile in TheDiscDB data) -> the first entry's claim wins; the
    # second is skipped rather than flip-flopping the claim.
    match = _match(
        [
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Comment": "Main.mkv",
                "Item": {"Title": "Round Midnight", "Type": "MainMovie"},
            },
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Comment": "Duplicate.mkv",
                "Item": {"Title": "Duplicate", "Type": "Episode"},
            },
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[ScanTitle(index=0, duration_seconds=7894, source_playlist="00001.mpls")],
    )
    result = build_claims(match, scan, now=NOW)
    assert len(result.tracks) == 1
    assert result.tracks["0"].role is TrackRole.MAIN
    assert result.tracks["0"].episode_name == "Round Midnight"


def test_external_imdb_id_returns_id() -> None:
    match = _match([])
    assert external_imdb_id(match) == "tt0090557"


def test_external_imdb_id_malformed_external_ids_returns_none() -> None:
    # ExternalIds as a non-dict (malformed third-party JSON) must not raise.
    match = DiscMatch(
        kind="movie",
        title_slug="x",
        release_slug="y",
        disc={"ContentHash": "X", "Titles": []},
        metadata={"ExternalIds": "garbage"},
        release={},
    )
    assert external_imdb_id(match) is None


# A real scan: source_file is TINFO 27, MakeMKV's planned output name, and the
# playlist is TINFO 16 (Blu-ray) or absent (DVD).


def test_build_claims_joins_a_real_bluray_scan_by_playlist() -> None:
    match = _match(
        [
            {
                "SourceFile": "00001.mpls",
                "Duration": "2:11:34",
                "Comment": "Main.mkv",
                "Item": {"Title": "Round Midnight", "Type": "MainMovie"},
            },
            {
                "SourceFile": "00011.MPLS",
                "Duration": "0:12:00",
                "Comment": "Making Of.mkv",
                "Item": {"Title": "The Making Of", "Type": "Featurette"},
            },
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.BLURAY,
        titles=[
            ScanTitle(
                index=0, duration_seconds=7894, source_file="ROUND_MIDNIGHT_t00.mkv", source_playlist="00001.mpls"
            ),
            ScanTitle(
                index=1, duration_seconds=720, source_file="ROUND_MIDNIGHT_t01.mkv", source_playlist="00011.mpls"
            ),
        ],
    )
    result = build_claims(match, scan, now=NOW)
    assert result.tracks["0"].role is TrackRole.MAIN
    assert result.tracks["0"].filename == "Main.mkv"
    assert result.tracks["1"].role is TrackRole.EXTRA


def test_build_claims_joins_a_real_dvd_scan_by_duration() -> None:
    """A DVD scan has an output name and no playlist: the duration fallback
    joins it."""
    match = _match(
        [
            {
                "SourceFile": "VTS_01_1.VOB",
                "Duration": "1:30:00",
                "Comment": "Movie.mkv",
                "Item": {"Title": "Movie", "Type": "MainMovie"},
            }
        ]
    )
    scan = ScanResult(
        disc_type=DiscType.DVD,
        titles=[
            ScanTitle(index=0, duration_seconds=5401, source_file="B1_t00.mkv", original_title_id=1),
            ScanTitle(index=1, duration_seconds=300, source_file="A1_t01.mkv", original_title_id=2),
        ],
    )
    result = build_claims(match, scan, now=NOW)
    assert list(result.tracks) == ["0"]
    assert result.tracks["0"].role is TrackRole.MAIN
