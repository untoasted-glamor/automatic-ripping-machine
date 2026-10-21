"""`remux_title` and `list_source_titles`, the per-title half of
backup-then-remux (track-select plan S7.1), against the recorded-output
makemkvcon fake: a Hanako-kun Blu-ray backup (every title id differs from
the disc's) and the Sintel ISO."""

from __future__ import annotations

from pathlib import Path

import pytest

from arm_ripper.rip.makemkv_backup import rip_backup
from arm_ripper.rip.makemkv_rip import remux_title
from arm_ripper.scan.makemkv import ScanError, list_source_titles
from tests._makemkvcon_fake import HANAKO_BD, SINTEL_ISO, FakeMakemkvcon, file_size, install


async def _hanako_backup(monkeypatch, tmp_path: Path) -> tuple[FakeMakemkvcon, str]:
    fake = install(monkeypatch, disc=HANAKO_BD)
    target = tmp_path / ".backup" / "disc"
    result = await rip_backup("disc:0", target, decrypt=True)
    assert result.ok, result.error
    return fake, f"file:{target / 'BDMV'}"


async def test_a_remuxed_title_is_named_by_its_disc_index_and_sized_as_its_title(monkeypatch, tmp_path):
    """Disc title 0 (`00009.mpls`) is backup title 8: the file written for
    backup id 8 is moved out of scratch and named `_t00`."""
    fake, url = await _hanako_backup(monkeypatch, tmp_path)
    titles = {t.source_playlist: t for t in await list_source_titles(url)}
    episode = titles["00009.mpls"]
    out = tmp_path / "job"
    ticks: list[float] = []

    async def on_progress(fraction: float) -> None:
        ticks.append(fraction)

    result = await remux_title(
        url, episode.index, scratch_dir=out / ".remux" / "0", output_dir=out, output_index=0, on_progress=on_progress
    )

    assert episode.index == 8
    assert result.ok is True, result.error
    assert result.output_path == out / "ToiletBound HanakoKun D1_t00.mkv"
    assert result.size_bytes == result.output_path.stat().st_size == file_size(episode.size_bytes or 0)
    assert list((out / ".remux" / "0").iterdir()) == []
    assert ticks and ticks[-1] == 1.0
    remux = fake.invocations[-1]
    assert (remux.command, remux.probes_drive, remux.reads_drive) == ("mkv", False, False)


async def test_a_title_id_the_source_lacks_fails_with_no_file(monkeypatch, tmp_path):
    """Recorded: a missing id exits 12 with no message and writes nothing."""
    _, url = await _hanako_backup(monkeypatch, tmp_path)
    out = tmp_path / "job"

    result = await remux_title(url, 99, scratch_dir=out / ".remux" / "3", output_dir=out, output_index=3)

    assert result.ok is False
    assert "exit=12" in (result.error or "")
    assert list(out.glob("*.mkv")) == []


async def test_a_title_makemkv_fails_to_save_fails_the_remux(monkeypatch, tmp_path):
    fake, url = await _hanako_backup(monkeypatch, tmp_path)
    fake.fail("mkv", title="00010.mpls")
    titles = {t.source_playlist: t for t in await list_source_titles(url)}
    out = tmp_path / "job"

    result = await remux_title(
        url, titles["00010.mpls"].index, scratch_dir=out / ".remux" / "1", output_dir=out, output_index=1
    )

    assert result.ok is False
    assert "produced no .mkv for title 9" in (result.error or "")
    assert "Failed to save title 9" in (result.error or "")
    assert list(out.glob("*.mkv")) == []


async def test_an_iso_source_lists_every_title_and_remuxes_one(monkeypatch, tmp_path):
    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")
    fake = install(monkeypatch, isos={str(iso): SINTEL_ISO})
    out = tmp_path / "job"

    titles = await list_source_titles(f"iso:{iso}")
    result = await remux_title(f"iso:{iso}", 6, scratch_dir=out / ".remux" / "6", output_dir=out, output_index=6)

    assert [t.duration_seconds for t in titles] == [898, 897, 119, 365, 216, 107, 3504]
    assert result.ok is True, result.error
    assert result.output_path == out / "B1_t06.mkv"
    assert result.size_bytes == file_size(titles[6].size_bytes or 0)
    assert all(not inv.probes_drive for inv in fake.invocations)


async def test_listing_a_source_that_does_not_open_raises(monkeypatch, tmp_path):
    install(monkeypatch, disc=HANAKO_BD)

    with pytest.raises(ScanError, match="exited 1"):
        await list_source_titles(f"file:{tmp_path / 'nothing' / 'BDMV'}")


async def test_listing_a_source_that_fails_raises(monkeypatch, tmp_path):
    fake, url = await _hanako_backup(monkeypatch, tmp_path)
    fake.fail("info", message="cannot read the backup")

    with pytest.raises(ScanError, match="cannot read the backup"):
        await list_source_titles(url)
