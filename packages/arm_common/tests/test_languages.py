"""arm_common.languages and the transcode preset track-selection schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arm_common import AudioSelection, SubtitleOutput, SubtitleSelection
from arm_common.languages import MAX_LANGUAGES, canonical_language, normalize_language_list
from arm_common.schemas import TranscodePresetCreateRequest, TranscodePresetUpdateRequest


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("eng", "eng"), ("JPN", "jpn"), ("deu", "ger"), ("fra", "fre"), ("", "und"), (None, "und"), ("en", "und")],
)
def test_canonical_language(tag: str | None, expected: str) -> None:
    assert canonical_language(tag) == expected


def test_normalize_language_list_keeps_order_and_drops_duplicates() -> None:
    assert normalize_language_list(["JPN", " eng", "deu", "ger", "und"]) == ["jpn", "eng", "ger", "und"]
    assert normalize_language_list([]) == []


@pytest.mark.parametrize("bad", ["english", "en", "e1g", ""])
def test_normalize_language_list_rejects_non_codes(bad: str) -> None:
    with pytest.raises(ValueError, match="ISO 639-2"):
        normalize_language_list(["eng", bad])


def test_normalize_language_list_caps_the_length() -> None:
    with pytest.raises(ValueError, match="at most"):
        normalize_language_list(["eng"] * (MAX_LANGUAGES + 1))


_BASE = {"name": "x", "media_type": "movie", "tool": "handbrake", "container": "mkv"}


def test_create_request_defaults() -> None:
    req = TranscodePresetCreateRequest(**_BASE)
    assert req.audio_selection == AudioSelection.ALL
    assert req.subtitle_selection == SubtitleSelection.ALL
    assert req.subtitle_output == SubtitleOutput.SEPARATE


def test_create_request_languages_mode_needs_a_list() -> None:
    with pytest.raises(ValidationError, match="at least one audio language"):
        TranscodePresetCreateRequest(**_BASE, audio_selection="languages")
    req = TranscodePresetCreateRequest(**_BASE, subtitle_selection="languages", subtitle_languages=["FRA"])
    assert req.subtitle_languages == ["fre"]


def test_update_request_normalises_and_passes_none_through() -> None:
    assert TranscodePresetUpdateRequest(audio_languages=["JPN"]).audio_languages == ["jpn"]
    assert TranscodePresetUpdateRequest().audio_languages is None
    with pytest.raises(ValidationError):
        TranscodePresetUpdateRequest(subtitle_languages=["xx"])
