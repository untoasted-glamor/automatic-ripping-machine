"""`rip_backup` against a faked makemkvcon (same harness as test_makemkv_rip_disc),
and backup-then-remux's backup choice against the recorded-output fake."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import arm_ripper.rip.makemkv_rip as makemkv_rip
from arm_common import DiscType
from arm_ripper.rip.makemkv_backup import (
    BACKUP_DIRNAME,
    BackupSourceError,
    backup_source,
    parse_drive_index,
    rip_backup,
    selection_backup,
)
from arm_ripper.scan.makemkv import list_source_titles, parse_makemkvcon_info
from tests._makemkvcon_fake import ACCA_DVD, FIXTURES, HANAKO_BD, install


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self.stdout = _FakeStream([(ln + "\n").encode() for ln in lines])
        self.stderr = _FakeStream([])
        self.returncode: int | None = None
        self._rc = returncode
        self.killed = False

    async def wait(self) -> int:
        await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _stub(monkeypatch, proc: _FakeProc, calls: list[tuple[Any, ...]], *, writes_to: Path | None = None) -> None:
    """`writes_to`: the fake makemkvcon writes a backup there when it runs."""

    async def fake_exec(*args: Any, **_kwargs: Any) -> _FakeProc:
        calls.append(args)
        if writes_to is not None:
            _write_backup(writes_to)
        return proc

    monkeypatch.setattr(makemkv_rip.asyncio, "create_subprocess_exec", fake_exec)


def _write_backup(output_dir: Path) -> None:
    bdmv = output_dir / BACKUP_DIRNAME / "BDMV" / "STREAM"
    bdmv.mkdir(parents=True)
    (bdmv.parent / "index.bdmv").write_bytes(b"i" * 10)
    (bdmv / "00000.m2ts").write_bytes(b"v" * 90)


async def test_rip_backup_runs_one_backup_decrypt_command(monkeypatch, tmp_path):
    calls: list[tuple[Any, ...]] = []
    _stub(monkeypatch, _FakeProc([]), calls, writes_to=tmp_path)
    await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True)
    assert calls == [
        (
            "makemkvcon",
            "backup",
            "--decrypt",
            "--robot",
            "--progress=-stdout",
            "disc:0",
            str(tmp_path / BACKUP_DIRNAME),
        )
    ]


async def test_rip_backup_success_reports_the_folder_size_and_no_sha(monkeypatch, tmp_path):
    _stub(monkeypatch, _FakeProc([]), [], writes_to=tmp_path)
    result = await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True)
    assert (result.ok, result.output_path, result.size_bytes, result.sha256) == (
        True,
        tmp_path / BACKUP_DIRNAME,
        100,
        None,
    )


async def test_rip_backup_reports_disc_progress_without_a_save_phase_marker(monkeypatch, tmp_path):
    _stub(monkeypatch, _FakeProc(["PRGV:0,2500,10000", "PRGV:0,10000,10000"]), [], writes_to=tmp_path)
    ticks: list[float] = []

    async def on_progress(fraction: float) -> None:
        ticks.append(fraction)

    await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True, on_progress=on_progress)
    assert ticks == [0.25, 1.0]


async def test_rip_backup_exit_zero_without_index_bdmv_fails(monkeypatch, tmp_path):
    _stub(monkeypatch, _FakeProc([]), [])
    (tmp_path / BACKUP_DIRNAME).mkdir()
    result = await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True)
    assert result.ok is False
    assert result.error is not None and "produced no BDMV" in result.error


async def test_rip_backup_never_passes_on_a_stale_backup(monkeypatch, tmp_path):
    """A backup left by an earlier run is cleared first, so its index.bdmv
    cannot pass the success check for a run that wrote nothing."""
    _write_backup(tmp_path)
    _stub(monkeypatch, _FakeProc([]), [])
    result = await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True)
    assert result.ok is False
    assert result.error is not None and "produced no BDMV" in result.error


async def test_rip_backup_nonzero_exit_fails_with_diagnostics(monkeypatch, tmp_path):
    _stub(monkeypatch, _FakeProc(['MSG:1002,0,1,"Error while reading input","%1","x"'], returncode=1), [])
    result = await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True)
    assert result.ok is False
    assert result.error is not None and "Error while reading input" in result.error


async def test_rip_backup_expired_binary_kills_and_fails(monkeypatch, tmp_path):
    proc = _FakeProc(['MSG:5021,0,0,"expired","%1"'])
    _stub(monkeypatch, proc, [])
    result = await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True)
    assert proc.killed
    assert result.ok is False and "MSG:5021" in (result.error or "")


async def test_rip_backup_cancelled_kills_makemkvcon(monkeypatch, tmp_path):
    """Abandoning a job mid-backup must not leave makemkvcon running."""

    class _Hanging(_FakeProc):
        async def wait(self) -> int:
            if self.returncode is None:
                await asyncio.sleep(3600)
            return self.returncode  # type: ignore[return-value]

    proc = _Hanging([])
    _stub(monkeypatch, proc, [])
    task = asyncio.create_task(rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=True))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


async def test_rip_backup_without_decrypt_leaves_the_flag_out(monkeypatch, tmp_path):
    calls: list[tuple[Any, ...]] = []
    _stub(monkeypatch, _FakeProc([]), calls, writes_to=tmp_path)
    await rip_backup("disc:0", tmp_path / BACKUP_DIRNAME, decrypt=False)
    assert "--decrypt" not in calls[0]
    assert calls[0][-2:] == ("disc:0", str(tmp_path / BACKUP_DIRNAME))


async def test_rip_backup_never_passes_on_a_stale_image(monkeypatch, tmp_path):
    """A DVD image left by an earlier run is deleted first, like a stale folder."""
    target = tmp_path / "disc.iso"
    target.write_bytes(b"old image")
    _stub(monkeypatch, _FakeProc([]), [])
    result = await rip_backup("disc:0", target, decrypt=True)
    assert result.ok is False
    assert not target.exists()


# --- backup-then-remux's backup (track-select S7.1) ---------------------------
#
# `selection_backup` is the one choice the real-drive checks (S17 17.4, 17.6)
# may change: today `dev:`, decrypted, a folder for a Blu-ray and one image
# for a DVD. Each variant is checked end to end: the backup is written and
# its read-back URL lists the backup's titles.


def _recorded(name: str) -> list[tuple[int, int | None, str | None, int | None]]:
    _, titles, _ = parse_makemkvcon_info((FIXTURES / name).read_text().splitlines())
    return [(t.index, t.duration_seconds, t.source_playlist, t.original_title_id) for t in titles]


async def test_a_blu_ray_selection_backup_is_a_decrypted_folder_read_back_as_file(monkeypatch, tmp_path):
    fake = install(monkeypatch, disc=HANAKO_BD)

    choice = await selection_backup(DiscType.BLURAY, "/dev/sr0", tmp_path / ".backup")
    result = await rip_backup(choice.source, choice.target, decrypt=choice.decrypt)
    titles = await list_source_titles(choice.read_back_url())

    assert result.ok is True, result.error
    assert result.output_path == tmp_path / ".backup" / "disc"
    assert choice.read_back_url() == f"file:{tmp_path / '.backup' / 'disc' / 'BDMV'}"
    assert [(inv.command, inv.decrypt) for inv in fake.drive_reads] == [("backup", True)]
    assert [(t.index, t.duration_seconds, t.source_playlist, t.original_title_id) for t in titles] == _recorded(
        "makemkvcon_bd_hanako_info_file_backup_minlength0.txt"
    )


async def test_a_dvd_selection_backup_is_one_image_read_back_as_iso(monkeypatch, tmp_path):
    fake = install(monkeypatch, disc=ACCA_DVD)

    choice = await selection_backup(DiscType.DVD, "/dev/sr0", tmp_path / ".backup")
    result = await rip_backup(choice.source, choice.target, decrypt=choice.decrypt)
    titles = await list_source_titles(choice.read_back_url())

    assert result.ok is True, result.error
    assert result.output_path == tmp_path / ".backup" / "disc.iso"
    assert choice.target.is_file() and result.size_bytes == choice.target.stat().st_size
    assert choice.read_back_url() == f"iso:{tmp_path / '.backup' / 'disc.iso'}"
    assert [(inv.command, inv.decrypt) for inv in fake.drive_reads] == [("backup", True)]
    assert [(t.index, t.duration_seconds, t.source_playlist, t.original_title_id) for t in titles] == _recorded(
        "makemkvcon_dvd_acca_info_iso_backup_minlength0_noscan_tray_open.txt"
    )


async def test_a_dvd_backup_written_as_a_folder_reads_back_as_video_ts(monkeypatch, tmp_path):
    """If MakeMKV 1.18.4 writes a DVD backup as a `VIDEO_TS/` folder (S17
    17.6 decides), the same target passes the success check and reads back
    through `file:`."""
    install(monkeypatch, disc=ACCA_DVD)
    choice = await selection_backup(DiscType.DVD, "/dev/sr0", tmp_path / ".backup")

    async def fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        (choice.target / "VIDEO_TS").mkdir(parents=True)
        (choice.target / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"i" * 12)
        return _FakeProc([])

    monkeypatch.setattr(makemkv_rip.asyncio, "create_subprocess_exec", fake_exec)
    result = await rip_backup(choice.source, choice.target, decrypt=choice.decrypt)

    assert (result.ok, result.size_bytes) == (True, 12)
    assert choice.read_back_url() == f"file:{choice.target / 'VIDEO_TS'}"


# --- which `disc:<N>` the drive is (MakeMKV's `backup` refuses `dev:`) --------


_DRV_LINES = [
    'DRV:0,256,999,0,"","",""',
    'DRV:1,0,999,0,"BD-RE HL-DT-ST BD-RE BU40N 1.03 912HS003567","","/dev/sr1"',
    'DRV:2,0,999,0,"DVD-RW","","/dev/sr0"',
]


def test_parse_drive_index_reads_the_drv_line_naming_the_device() -> None:
    assert parse_drive_index(_DRV_LINES, "/dev/sr0") == 2
    assert parse_drive_index(_DRV_LINES, "/dev/sr1") == 1
    assert parse_drive_index(_DRV_LINES, "/dev/sr7") is None


def test_parse_drive_index_follows_a_symlinked_device(tmp_path) -> None:
    node = tmp_path / "sr0"
    node.write_bytes(b"")
    link = tmp_path / "by-id-cdrom"
    link.symlink_to(node)
    lines = [f'DRV:3,0,999,0,"BD","","{node}"']

    assert parse_drive_index(lines, str(link)) == 3


@pytest.mark.parametrize("drive_index", [0, 4])
async def test_a_selection_backup_names_the_drive_by_its_disc_index(monkeypatch, tmp_path, drive_index):
    """The lookup reads no disc (`--noscan info disc:9999`) and the backup
    reads the drive as `disc:<N>`."""
    fake = install(monkeypatch, disc=HANAKO_BD, drive_index=drive_index)

    choice = await selection_backup(DiscType.BLURAY, "/dev/sr0", tmp_path / ".backup")

    assert choice.source == f"disc:{drive_index}"
    (lookup,) = fake.invocations
    assert (lookup.command, lookup.source, lookup.probes_drive, lookup.reads_drive) == (
        "info",
        "disc:9999",
        False,
        False,
    )


async def test_no_drv_line_for_the_device_fails_the_lookup(monkeypatch, tmp_path):
    install(monkeypatch, disc=HANAKO_BD, device="/dev/sr3")

    with pytest.raises(BackupSourceError, match="lists no drive at /dev/sr0"):
        await selection_backup(DiscType.BLURAY, "/dev/sr0", tmp_path / ".backup")


async def test_the_lookup_fails_when_makemkvcon_is_missing(monkeypatch, tmp_path):
    async def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("makemkvcon")

    monkeypatch.setattr(makemkv_rip.asyncio, "create_subprocess_exec", missing)

    with pytest.raises(BackupSourceError):
        await selection_backup(DiscType.BLURAY, "/dev/sr0", tmp_path / ".backup")


async def test_a_file_source_keeps_its_own_url(tmp_path):
    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")

    assert await backup_source(str(iso)) == f"iso:{iso}"
