"""The recorded-output makemkvcon fake (S6) against the recordings it models.

The fake derives every title table from one `info` dump at `--minlength=0`.
These tests check that what it produces at other minimums, and for single
title remuxes, backups and failures, matches what the real binary printed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests._makemkvcon_fake import ACCA_DVD, FIXTURES, HANAKO_BD, SINTEL_ISO, file_size, install


async def _run(*argv: str) -> tuple[list[str], str, int]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    assert proc.stdout is not None and proc.stderr is not None
    lines = []
    while raw := await proc.stdout.readline():
        lines.append(raw.decode().rstrip("\n"))
    stderr = (await proc.stderr.read()).decode()
    return lines, stderr, await proc.wait()


def _table(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith(("TCOUNT:", "CINFO:", "TINFO:", "SINFO:"))]


def _recorded_table(name: str) -> list[str]:
    return _table((FIXTURES / name).read_text().splitlines())


@pytest.mark.parametrize(
    ("source", "minlength", "recording"),
    [
        ("dev:/dev/sr0", None, "makemkvcon_bd_hanako_info.txt"),
        ("iso:{iso}", None, "makemkvcon_iso_sintel_info.txt"),
        ("iso:{iso}", "600", "makemkvcon_iso_sintel_info_minlength600.txt"),
        ("iso:{iso}", "0", "makemkvcon_iso_sintel_info_minlength0.txt"),
    ],
)
async def test_info_at_a_minimum_matches_the_recording(monkeypatch, tmp_path, source, minlength, recording):
    iso = str(tmp_path / "sintel.iso")
    install(monkeypatch, disc=HANAKO_BD, isos={iso: SINTEL_ISO})
    flags = [] if minlength is None else [f"--minlength={minlength}"]

    lines, _, rc = await _run("makemkvcon", "-r", "--cache=1", *flags, "info", source.format(iso=iso))

    assert rc == 0
    assert _table(lines) == _recorded_table(recording)


async def test_backup_reads_back_with_its_own_numbering(monkeypatch, tmp_path):
    """Backup of the Hanako-kun BD, then `info file:` at 120 s and 600 s:
    the backup's recorded tables, where id 0 is the play-all (disc id 7)."""
    fake = install(monkeypatch, disc=HANAKO_BD)
    bk = tmp_path / "bk"

    lines, _, rc = await _run("makemkvcon", "-r", "--progress=-stdout", "backup", "disc:0", str(bk))

    assert rc == 0
    assert lines[-2:] == [
        'MSG:5070,128,0,"Backup done","Backup done"',
        'MSG:5081,260,0,"Backup done.","Backup done."',
    ]
    assert any(f"file://{bk}" in line for line in lines if line.startswith("MSG:5072,"))
    assert (bk / "BDMV" / "index.bdmv").is_file()
    assert (bk / "CERTIFICATE").is_dir()
    for minlength, recording in (
        ("120", "makemkvcon_bd_hanako_info_file_backup.txt"),
        ("600", "makemkvcon_bd_hanako_info_file_backup_minlength600.txt"),
    ):
        info, _, rc = await _run("makemkvcon", "-r", "--noscan", f"--minlength={minlength}", "info", f"file:{bk}")
        assert rc == 0
        assert _table(info) == _recorded_table(recording)
    assert [inv.command for inv in fake.drive_reads] == ["backup"]


async def test_dvd_backup_is_one_iso_image_read_back_with_iso(monkeypatch, tmp_path):
    """Recorded on ACCA_CS_D1: `backup disc:0 <path>` writes one image file at
    `<path>`; `iso:` reads it with the disc's own tables, at 0 s and 120 s."""
    fake = install(monkeypatch, disc=ACCA_DVD)
    image = tmp_path / "bk" / "disc.iso"

    lines, _, rc = await _run("makemkvcon", "-r", "--progress=-stdout", "backup", "disc:0", str(image))

    assert rc == 0
    assert lines[-2:] == [
        'MSG:5070,128,0,"Backup done","Backup done"',
        'MSG:5081,260,0,"Backup done.","Backup done."',
    ]
    assert any(f'\\"file://{image}\\"' in line for line in lines if line.startswith("MSG:5072,"))
    assert image.is_file() and image.stat().st_size > 0
    for flags, recording in (
        (["--minlength=0"], "makemkvcon_dvd_acca_info_iso_backup_minlength0_noscan_tray_open.txt"),
        ([], "makemkvcon_dvd_acca_info_iso_backup_noscan_tray_open.txt"),
    ):
        info, _, rc = await _run("makemkvcon", "-r", "--noscan", *flags, "info", f"iso:{image}")
        assert rc == 0
        assert _table(info) == _recorded_table(recording)
    assert [inv.command for inv in fake.drive_reads] == ["backup"]


async def test_a_file_read_of_a_dvd_image_fails_silently(monkeypatch, tmp_path):
    """Recorded: rc 10, no MSG after the start-up line, nothing on stderr."""
    install(monkeypatch, disc=ACCA_DVD)
    image = tmp_path / "disc.iso"
    await _run("makemkvcon", "-r", "backup", "disc:0", str(image))

    lines, stderr, rc = await _run("makemkvcon", "-r", "--noscan", "--minlength=0", "info", f"file:{image}")

    assert rc == 10
    assert stderr == ""
    assert [line for line in lines if line.startswith("MSG:")] == [lines[0]]
    assert lines[0].startswith("MSG:1005,")
    assert _table(lines) == []


async def test_dvd_remux_from_the_image_replays_the_recording(monkeypatch, tmp_path):
    """Recorded: `--noscan --minlength=0 mkv iso:<image> 3` → B1_t03.mkv (2:49:02)."""
    fake = install(monkeypatch, disc=ACCA_DVD)
    image, out = tmp_path / "disc.iso", tmp_path / "out"
    await _run("makemkvcon", "-r", "backup", "disc:0", str(image))

    lines, _, rc = await _run(
        "makemkvcon", "-r", "--progress=-stdout", "--noscan", "--minlength=0", "mkv", f"iso:{image}", "3", str(out)
    )

    recorded = (FIXTURES / "makemkvcon_dvd_acca_mkv_iso_backup_minlength0_t3_orig11.txt").read_text().splitlines()
    assert rc == 0
    assert [p.name for p in out.iterdir()] == ["B1_t03.mkv"]
    assert (out / "B1_t03.mkv").stat().st_size == file_size(8_090_341_376)
    assert [line for line in lines if not line.startswith("MSG:5014,")] == [
        line for line in recorded if not line.startswith("MSG:5014,")
    ]
    assert any(f"file://{out}" in line for line in lines if line.startswith("MSG:5014,"))
    assert fake.invocations[-1].probes_drive is False


async def test_a_drive_index_lookup_reads_no_disc(monkeypatch):
    fake = install(monkeypatch, disc=HANAKO_BD)

    await _run("makemkvcon", "-r", "--cache=1", "info", "disc:9999")

    assert fake.drive_reads == []


async def test_backup_refuses_a_dev_source(monkeypatch, tmp_path):
    install(monkeypatch, disc=HANAKO_BD)

    _, stderr, rc = await _run("makemkvcon", "-r", "backup", "dev:/dev/sr0", str(tmp_path / "bk"))

    assert rc == 10
    assert 'Backup source must start with "disc:"' in stderr
    assert not (tmp_path / "bk").exists()


async def test_backup_needs_the_drive_index_of_the_device(monkeypatch, tmp_path):
    install(monkeypatch, disc=HANAKO_BD, drive_index=2)

    info, _, _ = await _run("makemkvcon", "-r", "info", "disc:9999")
    _, _, wrong = await _run("makemkvcon", "-r", "backup", "disc:0", str(tmp_path / "a"))
    _, _, right = await _run("makemkvcon", "-r", "backup", "disc:2", str(tmp_path / "b"))

    assert [line for line in info if line.endswith('"/dev/sr0"')][0].startswith("DRV:2,2,999,12,")
    assert wrong != 0
    assert right == 0


async def test_a_decrypted_blu_ray_backup_segfaults_every_later_read(monkeypatch, tmp_path):
    install(monkeypatch, disc=HANAKO_BD)
    bk = tmp_path / "bk"

    _, _, backup_rc = await _run("makemkvcon", "-r", "backup", "--decrypt", "disc:0", str(bk))
    _, _, info_rc = await _run("makemkvcon", "-r", "info", f"file:{bk}")
    _, _, mkv_rc = await _run("makemkvcon", "-r", "mkv", f"file:{bk}", "1", str(tmp_path / "out"))

    assert (backup_rc, info_rc, mkv_rc) == (0, 139, 139)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("minlength", "title", "name"),
    [(None, "0", "C1_t00.mkv"), (None, "1", "A1_t01.mkv"), ("600", "2", "B1_t02.mkv"), ("0", "3", "B3_t03.mkv")],
)
async def test_single_title_mkv_writes_the_recorded_name(monkeypatch, tmp_path, minlength, title, name):
    """Recorded: title 0 → C1_t00.mkv, 1 → A1_t01.mkv, and at 600 s title 2
    (the 58-minute one) → B1_t02.mkv; the prefix is TINFO 49, per title."""
    iso = str(tmp_path / "sintel.iso")
    install(monkeypatch, isos={iso: SINTEL_ISO})
    flags = [] if minlength is None else [f"--minlength={minlength}"]
    out = tmp_path / "out"

    lines, _, rc = await _run("makemkvcon", "-r", "--progress=-stdout", *flags, "mkv", f"iso:{iso}", title, str(out))

    assert rc == 0
    assert [p.name for p in out.iterdir()] == [name]
    assert 'PRGC:5017,0,"Saving to MKV file"' in lines
    assert lines[-1] == 'MSG:5036,260,1,"Copy complete. 1 titles saved.","Copy complete. %1 titles saved.","1"'


async def test_mkv_of_a_missing_title_exits_12_silently(monkeypatch, tmp_path):
    iso = str(tmp_path / "sintel.iso")
    install(monkeypatch, isos={iso: SINTEL_ISO})

    lines, _, rc = await _run("makemkvcon", "-r", "mkv", f"iso:{iso}", "9", str(tmp_path / "out"))

    assert rc == 12
    assert lines[-1].startswith("MSG:5011,")
    assert not any(line.startswith("MSG:5003,") for line in lines)
    assert not (tmp_path / "out").exists()


async def test_backup_remux_names_the_file_by_the_backup_id(monkeypatch, tmp_path):
    """Recorded: backup id 7 is 00010.mpls (disc id 1) and writes
    `ToiletBound HanakoKun D1_t07.mkv`."""
    install(monkeypatch, disc=HANAKO_BD)
    bk, out = tmp_path / "bk", tmp_path / "out"
    await _run("makemkvcon", "-r", "backup", "disc:0", str(bk))

    _, _, rc = await _run("makemkvcon", "-r", "--noscan", "mkv", f"file:{bk}", "7", str(out))

    assert rc == 0
    (written,) = out.iterdir()
    assert written.name == "ToiletBound HanakoKun D1_t07.mkv"
    assert written.stat().st_size == file_size(6_567_585_792)


async def test_mkv_all_writes_every_title_at_the_minimum(monkeypatch, tmp_path):
    iso = str(tmp_path / "sintel.iso")
    install(monkeypatch, isos={iso: SINTEL_ISO})
    out = tmp_path / "out"

    lines, _, rc = await _run("makemkvcon", "mkv", "-r", "--minlength=300", f"iso:{iso}", "all", str(out))

    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == ["A1_t01.mkv", "B1_t03.mkv", "B3_t02.mkv", "C1_t00.mkv"]
    assert [line for line in lines if line.startswith("PRGC:5017,")] == [
        f'PRGC:5017,{n},"Saving to MKV file"' for n in range(4)
    ]


async def test_failing_one_title_of_mkv_all(monkeypatch, tmp_path):
    iso = str(tmp_path / "sintel.iso")
    fake = install(monkeypatch, isos={iso: SINTEL_ISO})
    fake.fail("mkv", title="02")
    fake.fail("mkv", title="04", silent=True)
    out = tmp_path / "out"

    lines, _, rc = await _run("makemkvcon", "-r", "mkv", f"iso:{iso}", "all", str(out))

    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == ["B1_t04.mkv", "B4_t03.mkv", "C1_t00.mkv"]
    assert any(line.startswith('MSG:5003,0,2,"Failed to save title 1 to file') for line in lines)
    assert lines[-1].startswith('MSG:5037,516,2,"Copy complete. 3 titles saved, 1 failed."')


@pytest.mark.parametrize("command", ["info", "mkv", "backup"])
async def test_failing_a_whole_invocation(monkeypatch, tmp_path, command):
    fake = install(monkeypatch, disc=HANAKO_BD)
    fake.fail(command, message="boom", returncode=3)
    args = {
        "info": ["dev:/dev/sr0"],
        "mkv": ["dev:/dev/sr0", "all", str(tmp_path / "o")],
        "backup": ["disc:0", str(tmp_path / "o")],
    }

    lines, stderr, rc = await _run("makemkvcon", "-r", command, *args[command])

    assert rc == 3
    assert "boom" in stderr and any("boom" in line for line in lines)
    assert not (tmp_path / "o").exists()


async def test_noscan_lists_the_drive_without_reading_its_disc(monkeypatch, tmp_path):
    """Recorded with the BD seated: the DRV line has the model but no disc label."""
    iso = str(tmp_path / "sintel.iso")
    fake = install(monkeypatch, disc=HANAKO_BD, isos={iso: SINTEL_ISO})

    scanned, _, _ = await _run("makemkvcon", "-r", "info", f"iso:{iso}")
    quiet, _, _ = await _run("makemkvcon", "-r", "--noscan", "info", f"iso:{iso}")

    recorded = (FIXTURES / "makemkvcon_iso_sintel_info_drive_seated.txt").read_text().splitlines()
    recorded_noscan = (FIXTURES / "makemkvcon_iso_sintel_info_noscan_drive_seated.txt").read_text().splitlines()
    assert [line for line in scanned if line.startswith("DRV:0,")] == [r for r in recorded if r.startswith("DRV:0,")]
    assert [line for line in quiet if line.startswith("DRV:0,")] == [
        r for r in recorded_noscan if r.startswith("DRV:0,")
    ]
    assert [inv.probes_drive for inv in fake.invocations] == [True, False]
    assert fake.drive_reads == []


async def test_no_drive_and_unknown_sources(monkeypatch, tmp_path):
    fake = install(monkeypatch, device=None)

    lines, _, rc = await _run("makemkvcon", "-r", "info", "dev:/dev/sr0")

    assert rc != 0
    assert any(line.startswith("MSG:5042,") for line in lines)
    assert fake.invocations[0].probes_drive is False


async def test_other_programs_are_refused(monkeypatch):
    install(monkeypatch)

    with pytest.raises(AssertionError, match="unexpected subprocess"):
        await asyncio.create_subprocess_exec("eject", "/dev/sr0")


def test_fixture_dir_is_the_recordings() -> None:
    assert (FIXTURES / HANAKO_BD.info_min0).is_file()
    assert Path(FIXTURES / SINTEL_ISO.info_min0).is_file()
