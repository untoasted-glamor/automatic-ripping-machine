"""ISO 639-2 language codes for transcode track selection.

A transcode preset names the audio and subtitle languages it keeps as ISO
639-2 codes (`eng`, `jpn`). ISO 639-2 has two forms for 20 languages: the
bibliographic code (`ger`, `fre`), which Matroska track tags and MakeMKV use,
and the terminology code (`deu`, `fra`). Both forms are accepted and stored
as the bibliographic code, so a preset that lists `deu` matches a track
tagged `ger`. Jellyfin reads either form in a sidecar filename.

`und` (undetermined) is a valid entry: it matches tracks with no language tag.
"""

from __future__ import annotations

import re

UNDETERMINED = "und"

# Terminology code -> bibliographic code (ISO 639-2/T -> ISO 639-2/B).
_TERMINOLOGY_TO_BIBLIOGRAPHIC: dict[str, str] = {
    "bod": "tib",
    "ces": "cze",
    "cym": "wel",
    "deu": "ger",
    "ell": "gre",
    "eus": "baq",
    "fas": "per",
    "fra": "fre",
    "hye": "arm",
    "isl": "ice",
    "kat": "geo",
    "mkd": "mac",
    "mri": "mao",
    "msa": "may",
    "mya": "bur",
    "nld": "dut",
    "ron": "rum",
    "slk": "slo",
    "sqi": "alb",
    "zho": "chi",
}

_CODE_RE = re.compile(r"^[a-z]{3}$")

# Upper bound on a preset's language list; a longer list is a typo or abuse.
MAX_LANGUAGES = 32


def canonical_language(code: str | None) -> str:
    """Normalise a track's language tag for matching and file naming.

    Lower-cases, maps a terminology code to its bibliographic form, and turns
    a missing or malformed tag into `und`.
    """
    value = (code or "").strip().lower()
    if not _CODE_RE.match(value):
        return UNDETERMINED
    return _TERMINOLOGY_TO_BIBLIOGRAPHIC.get(value, value)


def normalize_language_list(codes: list[str]) -> list[str]:
    """Validate a preset's ordered language list and return it canonicalised.

    Raises ValueError on an entry that is not a three-letter code or on a
    list longer than MAX_LANGUAGES. Duplicates (including a `deu`/`ger`
    pair) are dropped, keeping the first position.
    """
    if len(codes) > MAX_LANGUAGES:
        raise ValueError(f"at most {MAX_LANGUAGES} languages")
    out: list[str] = []
    for raw in codes:
        value = raw.strip().lower()
        if not _CODE_RE.match(value):
            raise ValueError(f"{raw!r} is not an ISO 639-2 code (three letters, e.g. eng)")
        canonical = _TERMINOLOGY_TO_BIBLIOGRAPHIC.get(value, value)
        if canonical not in out:
            out.append(canonical)
    return out
