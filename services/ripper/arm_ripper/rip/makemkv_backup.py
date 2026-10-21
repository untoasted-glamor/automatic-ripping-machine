"""Whole-disc backup: one `makemkvcon backup` per disc.

Two callers:

- Upstream's FOLDER preset (`TrackKind.DISC_BACKUP`, Blu-ray): a decrypted
  `BDMV/` + `CERTIFICATE/` tree under `<job>/backup`, a finished output that
  passthrough files into the library.

Both read a drive as `disc:<N>` (`backup_source`): `backup` refuses `dev:`.
- Backup-then-remux (track-select plan, D1): one read of the disc into
  `<job>/.backup/`, scratch the kept titles are remuxed from. How that backup
  is taken lives in `selection_backup` alone.

There are no titles and no `.mkv` files, so only the mode-independent half of
the `mkv all` stream handling is shared (`makemkv_rip._run_makemkvcon`): PRGV
progress, MSG diagnostics, the MSG:5021 kill switch, the timeout, `stop` and
cancel-safe cleanup. Progress is the disc-level PRGV `total/max` from the
first line, since backup mode's phase markers are not the `mkv all` save
marker (PRGT:5024).
"""

import asyncio
import csv
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from arm_common import DiscType
from arm_common.fileops import tree_size

from arm_ripper.rip.makemkv_rip import OnDiscProgress, RipResult, _compose_error, _ParserState, _run_makemkvcon
from arm_ripper.source import is_file_source, makemkv_source_url

logger = logging.getLogger("arm_ripper.rip.makemkv_backup")

BACKUP_DIRNAME = "backup"
DRIVE_LOOKUP_TIMEOUT_SECONDS = 120.0


class BackupSourceError(Exception):
    """MakeMKV can't be pointed at the drive to back it up."""


def parse_drive_index(lines: list[str], device_path: str) -> int | None:
    """The index of the `DRV:` line whose last field is `device_path`.

    `DRV:<index>,<state>,<?>,<flags>,"<model>","<disc label>","<device>"`;
    the index is MakeMKV's own numbering of the drives it can see.
    """
    wanted = {device_path, os.path.realpath(device_path)}
    for line in lines:
        if not line.startswith("DRV:"):
            continue
        fields = next(csv.reader([line[4:]]))
        if len(fields) < 7 or not fields[-1]:
            continue
        if fields[-1] in wanted or os.path.realpath(fields[-1]) in wanted:
            try:
                return int(fields[0])
            except ValueError:
                return None
    return None


async def find_drive_index(device_path: str) -> int | None:
    """Ask MakeMKV which `disc:<N>` is `device_path`.

    Runs `--noscan info disc:9999`: there is no such drive, so no disc is
    opened as a source, and with `--noscan` the drives are listed without
    their discs being read, device paths included (recorded). None if
    makemkvcon can't run or doesn't list the device.
    """
    cmd = ["makemkvcon", "-r", "--cache=1", "--noscan", "info", "disc:9999"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=DRIVE_LOOKUP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return parse_drive_index(stdout.decode(errors="replace").splitlines(), device_path)


async def backup_source(device_path: str) -> str:
    """The source `makemkvcon backup` reads `device_path` as.

    A drive must be named `disc:<N>`: `backup` refuses `dev:<device>` (exit
    10, `Backup source must start with "disc:"`, nothing written; MakeMKV
    2.0.0 and 1.18.4 on a real drive, ledger T18). An ISO or a disc folder
    keeps its `iso:` / `file:` URL. Raises BackupSourceError when MakeMKV
    doesn't list the drive.
    """
    if is_file_source(device_path):
        return makemkv_source_url(device_path)
    index = await find_drive_index(device_path)
    if index is None:
        raise BackupSourceError(f"makemkvcon lists no drive at {device_path}, so it can't back the disc up")
    return f"disc:{index}"


@dataclass(frozen=True)
class SelectionBackup:
    """How backup-then-remux backs a disc up, and how it reads the copy back."""

    # What `makemkvcon backup` reads.
    source: str
    decrypt: bool
    # Where it writes: a folder (Blu-ray), or a folder or one image (DVD).
    target: Path

    def read_back_url(self) -> str:
        """The URL that reads the finished backup: `file:<target>/BDMV` (or
        `/VIDEO_TS`) for a folder, `iso:<target>` for an image."""
        return makemkv_source_url(str(self.target))


async def selection_backup(disc_type: DiscType, device_path: str, backup_dir: Path) -> SelectionBackup:
    """The one choice MakeMKV's behaviour on a real drive decides (track-select
    plan S17 17.4 and 17.6); nothing else in backup-then-remux depends on it.

    `disc:<N>` for the drive (`backup_source`: MakeMKV refuses `dev:`),
    decrypted (1.18.4 reads a decrypted Blu-ray backup back through `file:`,
    ledger T34), into `<backup_dir>/disc` for a Blu-ray (a folder) and
    `<backup_dir>/disc.iso` for a DVD. MakeMKV 2.0.0 wrote a DVD backup as
    one ISO image; the `.iso` name is what makes `source.makemkv_source_url`
    read an image as `iso:`, and a folder written there still reads back as
    `file:<…>/VIDEO_TS` (1.18.4 unchecked, 17.6). Raises BackupSourceError.
    """
    name = "disc.iso" if disc_type == DiscType.DVD else "disc"
    return SelectionBackup(source=await backup_source(device_path), decrypt=True, target=backup_dir / name)


def _clear(target: Path) -> None:
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)


def _backup_written(target: Path) -> bool:
    """A Blu-ray folder with its `index.bdmv`, a DVD folder with a
    `VIDEO_TS/` that holds something, or a non-empty image file."""
    if (target / "BDMV" / "index.bdmv").is_file():
        return True
    video_ts = target / "VIDEO_TS"
    if video_ts.is_dir() and any(video_ts.iterdir()):
        return True
    return target.is_file() and target.stat().st_size > 0


async def rip_backup(
    source: str,
    target: Path,
    *,
    decrypt: bool,
    on_progress: OnDiscProgress | None = None,
    stop: asyncio.Event | None = None,
) -> RipResult:
    """Back up the disc `source` (a MakeMKV URL, `disc:<N>` for a drive) to `target`.

    Success needs a clean exit and a backup at `target` (`_backup_written`).
    The result's `size_bytes` is the backup's file total; there is no sha256
    (MakeMKV checks the content as it decrypts, and re-reading up to 100 GB
    to hash it would double the time). `stop` kills makemkvcon when set (the
    drive dropped off the bus).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    # A backup left by an earlier run must never pass this run's success check.
    _clear(target)
    cmd = ["makemkvcon", "backup"]
    if decrypt:
        cmd.append("--decrypt")
    cmd += ["--robot", "--progress=-stdout", source, str(target)]
    logger.info("makemkvcon backup source=%s target=%s decrypt=%s", source, target, decrypt)

    state = _ParserState()
    error = await _run_makemkvcon(cmd, state, on_disc_progress=on_progress, gate_disc_progress=False, stop=stop)
    if error is not None:
        return RipResult(ok=False, error=error)
    if not _backup_written(target):
        return RipResult(
            ok=False,
            error=_compose_error("makemkvcon backup produced no BDMV, VIDEO_TS or disc image", state.diagnostics),
        )
    size = await asyncio.to_thread(tree_size, target) if target.is_dir() else target.stat().st_size
    return RipResult(ok=True, output_path=target, size_bytes=size)
