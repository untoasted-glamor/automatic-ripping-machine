"""arm_backend.sidecars: which reported sidecars are accepted, and removal."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from arm_backend.sidecars import accepted_sidecars, is_sidecar_of, remove_sidecars, sidecar_files

VIDEO = "Show/Season 01/Show - S01E01.mkv"


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("Show/Season 01/Show - S01E01.eng.sup", True),
        ("Show/Season 01/Show - S01E01.jpn.2.forced.mks", True),
        ("Show/Season 01/Show - S01E01.mkv", False),  # the video itself
        ("Show/Season 01/Show - S01E02.eng.sup", False),  # another episode
        ("Show/Season 02/Show - S01E01.eng.sup", False),  # another folder
        ("/Show/Season 01/Show - S01E01.eng.sup", False),  # absolute
        ("Show/Season 01/../Season 01/Show - S01E01.eng.sup", False),
    ],
)
def test_is_sidecar_of(candidate: str, expected: bool) -> None:
    assert is_sidecar_of(VIDEO, candidate) is expected


def test_accepted_sidecars_drops_foreign_paths_and_duplicates(caplog: pytest.LogCaptureFixture) -> None:
    ok = "Show/Season 01/Show - S01E01.eng.sup"
    with caplog.at_level(logging.WARNING, logger="arm_backend.sidecars"):
        kept = accepted_sidecars(VIDEO, [ok, "../etc/passwd", ok])
    assert kept == [ok]
    assert "../etc/passwd" in caplog.text


def test_sidecar_files_and_remove_stay_inside_the_media_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "media"
    (media / "M").mkdir(parents=True)
    keep = tmp_path / "outside.sup"
    keep.write_text("x")
    present = media / "M" / "M.eng.sup"
    present.write_text("x")
    locked = media / "M" / "M.jpn.sup"
    locked.write_text("x")
    recorded = ["M/M.eng.sup", "M/M.ger.sup", "M/M.jpn.sup", "../outside.sup"]

    with caplog.at_level(logging.WARNING, logger="arm_backend.sidecars"):
        assert sidecar_files(media, recorded) == [media / "M/M.eng.sup", media / "M/M.ger.sup", media / "M/M.jpn.sup"]
    assert "outside the media root" in caplog.text
    assert sidecar_files(media, None) == []

    real_unlink = Path.unlink

    def _unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "M.jpn.sup":
            raise PermissionError("busy")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", _unlink)
    # One removed, one already gone, one that cannot be removed, one outside.
    assert remove_sidecars(media, recorded) == 1
    assert not present.exists()
    assert locked.exists() and keep.exists()
    assert "could not remove sidecar" in caplog.text
