"""A `makemkvcon` fake built from recorded robot output (track-select plan, S6).

`install(monkeypatch, ...)` replaces `asyncio.create_subprocess_exec` with a
fake that reads its argv the way the real `makemkvcon` does and answers from
the recordings in `fixtures/`. Tests then check what came out (files in
`tmp_path`, what each track was reported as), not which arguments went in.

What is modelled, and where it comes from (S0 "Spike results"):

- **Title tables.** Each source is described by its `info` dump at
  `--minlength=0`. At any other minimum MakeMKV keeps the titles at or above
  it and numbers them densely, in the same order (the "rank rule", confirmed
  on the Sintel DVD and the Hanako-kun BD, disc and backup). With no
  `--minlength` the built-in default is 120 s. A title's planned output name
  (TINFO 27, `<prefix>_tNN.mkv`) is renumbered with it.
- **`info <src>`** replays that table.
- **`mkv <src> <id|all> <dir>`** writes `<TINFO 27 name>` per saved title into
  `<dir>`. A single-title `mkv` that was recorded (`Recording.remuxes`: same
  source, `--minlength` and id) replays that recording; anything else streams
  a save phase built from the recorded shape (`PRGT:5024`, `MSG:5014`, one
  `PRGC:5017,<position>` per title, `PRGV`, `MSG:5005`/`5036`, or `MSG:5037`
  when a title failed). An id with no title exits 12 and writes nothing, with
  no error message (recorded). `mkv all` itself was never recorded; its save
  phase is the recorded single-title shape repeated.
- **`backup <dev:<device>|disc:<N>> <path>`** replays the recorded backup
  stream of that disc type and writes what the real tool wrote:
  - Blu-ray: a folder at `<path>` with the recorded layout (empty files),
    read back as `file:<path>`.
  - DVD: one ISO image file at `<path>` (sparse), read back as `iso:<path>`.
    A `file:` read of the image exits 10 with nothing but the drive list
    (recorded on ACCA_CS_D1).

  Read back, a backup has its own title numbering when the source has a
  backup recording (on the Hanako-kun BD every id differs from the disc's;
  on the ACCA DVD they match). `N` must be the index of the `DRV:` line for
  the device. A backup folder also reads back as `file:<path>/BDMV`, the URL
  `source.makemkv_source_url` builds for a disc folder.
- **MakeMKV version.** Every recording was made on 2.0.0; the ripper image
  runs 1.18.4 (2.0.0 segfaults on unencrypted Blu-rays). `makemkv_version`
  picks which behaviour the fake has where the two are known to differ:
  - `"2.0.0"` (recorded): `backup dev:…` exits 10 and wants `disc:<N>`; a
    `--decrypt` BD backup is written, but every later `file:` read of it
    exits 139; `file:` on a DVD backup image exits 10.
  - `"1.18.4"` (the default): `file:` on a DVD backup image reads it like
    `iso:` (recorded, S17 17.3). `backup dev:<device>` works, as upstream's
    `rip_backup` calls it, and a decrypted BD backup reads back like an
    encrypted one: *assumed from upstream's FOLDER preset, not recorded on
    this drive yet (S17 17.4).*
  Title tables, numbering and output names are the same on both: the
  Sintel ISO and ACCA image tables at 0/120/600 s are byte-identical on
  1.18.4 (`*_v1184.txt`).
- **Disc folders.** `folders` maps a folder path (with `BDMV/` or `VIDEO_TS/`
  under it) to its recording, read as `file:<folder>/BDMV` (or the folder
  itself), like a Rip-from-folder source.
- **Drive lines.** Every invocation lists the drives first. The configured
  device gets a `DRV:` line at `drive_index`, with the seated disc's label,
  unless `--noscan` is passed: then the drive is listed but its disc isn't
  read (recorded).
- **Invocations** are recorded with `reads_drive` (handed the drive itself,
  `dev:<device>` or `disc:<its index>`: the drive-access contract, testing
  rule 5; a `disc:9999` lookup reads nothing) and `probes_drive` (started
  without `--noscan` while a drive is attached).

Not recorded, so guessed and marked as such: a source that can't be opened
(`MSG:5010`, exit 1), `iso:` on a Blu-ray backup folder (treated the same),
and how a single title fails to save (`MSG:5003`, no file, exit 0 unless the
test says otherwise).

`.mkv` files are sparse and `TINFO 11 // SIZE_DIVISOR` bytes long, so each
title's file has a size of its own and the suite never writes gigabytes.
"""

from __future__ import annotations

import asyncio
import functools
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Bytes in the source per byte written by the fake.
SIZE_DIVISOR = 1000

BUILTIN_MIN_LENGTH_SECONDS = 120
_DRIVE_SLOTS = 16
_DRIVE_MODEL = "BD-RE HL-DT-ST BD-RE BU40N 1.03 912HS003567"
_NO_DRIVES_MSG = (
    'MSG:5042,0,0,"The program can\'t find any usable optical drives.",'
    '"The program can\'t find any usable optical drives."'
)
_STARTED_MSG = (
    'MSG:1005,0,1,"MakeMKV v2.0.0 linux(x64-release) started","%1 started","MakeMKV v2.0.0 linux(x64-release)"'
)
_DURATION_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")
_OUTPUT_SUFFIX_RE = re.compile(r"_t\d+(?=\.mkv\"?$)")
_KNOWN_FLAGS = frozenset({"-r", "--robot", "--noscan", "--decrypt"})
_KNOWN_OPTION_PREFIXES = ("--progress=", "--minlength=", "--cache=", "--messages=", "--directio=")


@dataclass(frozen=True)
class Recording:
    """A source as `makemkvcon -r info` described it at `--minlength=0`.

    `backup` is how a `backup` of it reads back; None means "with the same
    numbering as the source" (assumed, not recorded). `remuxes` are recorded
    single-title `mkv` runs of this source, as `(minlength, id, fixture)`.
    """

    info_min0: str
    backup: Recording | None = None
    remuxes: tuple[tuple[int, int, str], ...] = ()


SINTEL_ISO = Recording(
    "makemkvcon_iso_sintel_info_minlength0.txt",
    remuxes=(
        (120, 0, "makemkvcon_iso_sintel_mkv_t0.txt"),
        (120, 1, "makemkvcon_iso_sintel_mkv_t1.txt"),
        (120, 3, "makemkvcon_iso_sintel_mkv_noscan_t3_drive_seated.txt"),
        (600, 2, "makemkvcon_iso_sintel_mkv_minlength600_t2.txt"),
    ),
)
HANAKO_BD = Recording(
    "makemkvcon_bd_hanako_info_minlength0.txt",
    backup=Recording(
        "makemkvcon_bd_hanako_info_file_backup_minlength0.txt",
        remuxes=(
            (120, 0, "makemkvcon_bd_hanako_mkv_file_backup_t0_playall_drive_not_ready.txt"),
            (120, 1, "makemkvcon_bd_hanako_mkv_file_backup_t1.txt"),
            (120, 7, "makemkvcon_bd_hanako_mkv_file_backup_t7_no_drive.txt"),
            (600, 1, "makemkvcon_bd_hanako_mkv_file_backup_minlength600_t1.txt"),
        ),
    ),
)
# ACCA_CS_D1: one 2:49:02 title plus three short clips at minlength 0. Its
# backup is an ISO image whose tables are byte-identical to the disc's.
ACCA_DVD = Recording(
    "makemkvcon_dvd_acca_info_minlength0.txt",
    backup=Recording(
        "makemkvcon_dvd_acca_info_iso_backup_minlength0_noscan_tray_open.txt",
        remuxes=(
            (0, 2, "makemkvcon_dvd_acca_mkv_iso_backup_minlength0_t2_orig04.txt"),
            (0, 3, "makemkvcon_dvd_acca_mkv_iso_backup_minlength0_t3_orig11.txt"),
        ),
    ),
)


@dataclass(frozen=True)
class _Title:
    index: int  # id at --minlength=0
    lines: tuple[str, ...]
    duration: int
    size_bytes: int
    key: str | None  # TINFO 16 (BD playlist) or TINFO 24 (DVD title number), as recorded
    output_name: str  # TINFO 27 at --minlength=0


@dataclass(frozen=True)
class _Table:
    header: tuple[str, ...]  # pre-TCOUNT lines; "" marks where the DRV block goes
    cinfo: tuple[str, ...]
    titles: tuple[_Title, ...]
    blu_ray: bool
    volume_id: str


def _quoted(line: str) -> str:
    """The value of a CINFO (`code,flag,value`) or TINFO (`title,code,flag,value`) line."""
    fields = 3 if line.startswith("CINFO:") else 4
    return line.split(",", fields - 1)[fields - 1].strip().strip('"')


@functools.cache
def _load(name: str) -> _Table:
    header: list[str] = []
    cinfo: list[str] = []
    by_index: dict[int, list[str]] = {}
    seen_tcount = False
    for line in (FIXTURES / name).read_text().splitlines():
        kind = line.partition(":")[0]
        if kind == "TCOUNT":
            seen_tcount = True
        elif kind == "CINFO":
            cinfo.append(line)
        elif kind in ("TINFO", "SINFO"):
            by_index.setdefault(int(line[6:].split(",", 1)[0]), []).append(line)
        elif not seen_tcount:
            if kind == "DRV" or line.startswith("MSG:5042,"):
                if "" not in header:
                    header.append("")
                continue
            header.append(line)
    if "" not in header:
        header.insert(1, "")

    titles: list[_Title] = []
    for idx, lines in sorted(by_index.items()):
        info: dict[int, str] = {}
        for line in lines:
            if line.startswith("TINFO:"):
                code = int(line.split(",", 2)[1])
                info[code] = _quoted(line)
        m = _DURATION_RE.match(info[9])
        assert m is not None, (name, idx, info[9])
        h, mn, s = (int(g) for g in m.groups())
        titles.append(
            _Title(
                index=idx,
                lines=tuple(lines),
                duration=h * 3600 + mn * 60 + s,
                size_bytes=int(info[11]),
                key=info.get(16, info.get(24)),
                output_name=info[27],
            )
        )
    disc_type = next((_quoted(c) for c in cinfo if c.startswith("CINFO:1,")), "")
    volume_id = next((_quoted(c) for c in cinfo if c.startswith("CINFO:32,")), "")
    return _Table(tuple(header), tuple(cinfo), tuple(titles), "blu-ray" in disc_type.lower(), volume_id)


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def _renumber(title: _Title, new: int) -> list[str]:
    out: list[str] = []
    for line in title.lines:
        kind, _, rest = line.partition(":")
        rest = f"{new}," + rest.split(",", 1)[1]
        if kind == "TINFO" and rest.split(",", 2)[1] == "27":
            rest = _OUTPUT_SUFFIX_RE.sub(f"_t{new:02d}", rest)
        out.append(f"{kind}:{rest}")
    return out


def output_name(title: _Title, new: int) -> str:
    return _OUTPUT_SUFFIX_RE.sub(f"_t{new:02d}", title.output_name)


def file_size(size_bytes: int) -> int:
    """Size of the `.mkv` the fake writes for a title of `size_bytes` (TINFO 11)."""
    return max(1, size_bytes // SIZE_DIVISOR)


@dataclass
class Invocation:
    argv: tuple[str, ...]
    command: str
    source: str
    minlength: int
    noscan: bool
    decrypt: bool
    drive_attached: bool
    # Handed the optical drive itself as its source (`dev:<device>` or
    # `disc:<its DRV index>`).
    reads_drive: bool = False
    returncode: int | None = None

    @property
    def probes_drive(self) -> bool:
        """Opened the drive's disc at start-up (every run does, unless `--noscan`)."""
        return self.drive_attached and not self.noscan


@dataclass
class _Failure:
    command: str
    title: str | None
    returncode: int | None
    message: str
    silent: bool


_Step = str | Callable[[], None]


class _Pipe:
    """One of the fake process's output streams. Steps are lines, or side
    effects (writing a file) that run when the reader gets that far, as the
    real tool's output and its files appear together."""

    def __init__(self, steps: list[_Step]) -> None:
        self._steps: deque[_Step] = deque(steps)

    @property
    def drained(self) -> bool:
        return not self._steps

    def close(self) -> None:
        self._steps.clear()

    def _next(self) -> bytes:
        while self._steps:
            step = self._steps.popleft()
            if callable(step):
                step()
                continue
            return (step + "\n").encode()
        return b""

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return self._next()

    async def read(self) -> bytes:
        await asyncio.sleep(0)
        chunks = []
        while chunk := self._next():
            chunks.append(chunk)
        return b"".join(chunks)

    def __aiter__(self) -> _Pipe:
        return self

    async def __anext__(self) -> bytes:
        line = await self.readline()
        if not line:
            raise StopAsyncIteration
        return line


class _Process:
    def __init__(self, invocation: Invocation, stdout: list[_Step], stderr: list[str], returncode: int) -> None:
        self._invocation = invocation
        self._exit = returncode
        self.stdout = _Pipe(stdout)
        self.stderr = _Pipe(list(stderr))
        self.returncode: int | None = None

    async def wait(self) -> int:
        # The real process exits once its output is written; ours once it's read.
        while self.returncode is None and not self.stdout.drained:
            await asyncio.sleep(0)
        if self.returncode is None:
            self.returncode = self._exit
        self._invocation.returncode = self.returncode
        return self.returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        stdout = await self.stdout.read()
        stderr = await self.stderr.read()
        await self.wait()
        return stdout, stderr

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9
            self._invocation.returncode = -9
        self.stdout.close()


@dataclass
class _Backup:
    recording: Recording
    blu_ray: bool
    decrypted: bool

    @property
    def image(self) -> bool:
        """A DVD backup is one ISO image file; a Blu-ray backup a folder."""
        return not self.blu_ray


# Recorded paths the replayed streams name, swapped for the test's own.
_RECORDED_URL_RE = re.compile(r'file://(?:/raw/_spike|/raw/_fixtures|/spike|/tmp)[^\\"]*')


@dataclass
class FakeMakemkvcon:
    device: str | None = "/dev/sr0"
    disc: Recording | None = None
    drive_index: int = 0
    isos: dict[str, Recording] = field(default_factory=dict)
    folders: dict[str, Recording] = field(default_factory=dict)
    makemkv_version: str = "1.18.4"
    invocations: list[Invocation] = field(default_factory=list)
    _failures: list[_Failure] = field(default_factory=list)
    _backups: dict[Path, _Backup] = field(default_factory=dict)

    # --- test-facing API ---------------------------------------------------

    @property
    def drive_reads(self) -> list[Invocation]:
        return [inv for inv in self.invocations if inv.reads_drive]

    def fail(
        self,
        command: str,
        *,
        title: str | None = None,
        returncode: int | None = None,
        message: str = "fake makemkvcon failure",
        silent: bool = False,
    ) -> None:
        """Make every later `command` (`info`, `mkv`, `backup`) fail.

        With `title` (a TINFO 16 playlist like `"00010.mpls"`, or a DVD's
        TINFO 24 like `"07"`, as recorded), only that title fails to save in
        an `mkv`: `MSG:5003` and no file, or with `silent=True` no message
        and no `PRGC:5017` either (MakeMKV skipping it, as in the S14
        incident). The exit code is `returncode`, default 0. Without `title`
        the whole invocation fails: `message` as a MSG and on stderr, no
        output, exit `returncode` (default 1).
        """
        assert command in ("info", "mkv", "backup"), command
        assert title is None or command == "mkv", "only an mkv fails per title"
        self._failures.append(_Failure(command, title, returncode, message, silent))

    def clear_failures(self) -> None:
        """Undo every `fail`: later invocations succeed again (a retry)."""
        self._failures.clear()

    # --- the fake process --------------------------------------------------

    async def create_subprocess_exec(self, *argv: str, **_kwargs: Any) -> _Process:
        assert argv and Path(argv[0]).name == "makemkvcon", f"unexpected subprocess in a makemkvcon test: {argv}"
        flags = [a for a in argv[1:] if a.startswith("-")]
        positionals = [a for a in argv[1:] if not a.startswith("-")]
        for flag in flags:
            assert flag in _KNOWN_FLAGS or flag.startswith(_KNOWN_OPTION_PREFIXES), f"unknown option {flag!r}"
        assert "-r" in flags or "--robot" in flags, "the fake only speaks robot mode"
        assert positionals, argv
        command = positionals[0]
        arity = {"info": 2, "mkv": 4, "backup": 3}
        assert command in arity, f"unmodelled command {command!r}"
        assert len(positionals) == arity[command], f"{command} takes {arity[command] - 1} arguments: {argv}"
        minlength = BUILTIN_MIN_LENGTH_SECONDS
        for flag in flags:
            if flag.startswith("--minlength="):
                minlength = int(flag.split("=", 1)[1])
        source = positionals[1]
        inv = Invocation(
            argv=tuple(argv),
            command=command,
            source=source,
            minlength=minlength,
            noscan="--noscan" in flags,
            decrypt="--decrypt" in flags,
            drive_attached=self.device is not None,
            reads_drive=self.device is not None and source in (f"dev:{self.device}", f"disc:{self.drive_index}"),
        )
        self.invocations.append(inv)
        stdout, stderr, returncode = self._run(inv, positionals[2:])
        return _Process(inv, stdout, stderr, returncode)

    def _drv_block(self, noscan: bool) -> list[str]:
        lines = [] if self.device is not None else [_NO_DRIVES_MSG]
        for i in range(_DRIVE_SLOTS):
            if self.device is not None and i == self.drive_index:
                if noscan or self.disc is None:
                    lines.append(f'DRV:{i},0,999,0,"{_DRIVE_MODEL}","","{self.device}"')
                else:
                    label = _load(self.disc.info_min0).volume_id
                    lines.append(f'DRV:{i},2,999,12,"{_DRIVE_MODEL}","{label}","{self.device}"')
            else:
                lines.append(f'DRV:{i},256,999,0,"","",""')
        return lines

    def _with_drives(self, lines: list[str] | tuple[str, ...], noscan: bool) -> list[str]:
        """Replace a recording's own DRV block (marked "" by `_load`, or its
        DRV lines) with this fake's drives."""
        out: list[str] = []
        placed = False
        for line in lines:
            if line == "" or line.startswith("DRV:") or line.startswith("MSG:5042,"):
                if not placed:
                    out.extend(self._drv_block(noscan))
                    placed = True
                continue
            out.append(line)
        return out

    def _open(self, source: str) -> tuple[Recording, bool] | _Backup | None:
        """(recording, is a decrypted BD backup) for a source that opens, the
        backup for a 2.0.0 `file:` read of a DVD image (fails silently), or None."""
        scheme, _, rest = source.partition(":")
        if scheme == "dev" and self.disc is not None and rest == self.device:
            return self.disc, False
        if scheme == "disc" and self.disc is not None and self.device is not None and rest == str(self.drive_index):
            return self.disc, False
        if scheme == "iso" and rest in self.isos:
            return self.isos[rest], False
        if scheme == "file":
            folder = _disc_folder(Path(rest))
            for path, recording in self.folders.items():
                if Path(path).resolve() == folder:
                    return recording, False
            rest = str(folder)
        backup = self._backups.get(Path(rest).resolve()) if scheme in ("file", "iso") else None
        if backup is None:
            return None
        if backup.image:
            if scheme == "iso" or self.makemkv_version != "2.0.0":
                return backup.recording, False
            return backup
        if scheme == "file":
            return backup.recording, backup.decrypted and self.makemkv_version == "2.0.0"
        return None

    def _failure(self, inv: Invocation, *, title: str | None = None) -> _Failure | None:
        return next((f for f in self._failures if f.command == inv.command and f.title == title), None)

    def _run(self, inv: Invocation, args: list[str]) -> tuple[list[_Step], list[str], int]:
        dev_backup = self.makemkv_version != "2.0.0" and inv.source.startswith("dev:")
        if inv.command == "backup" and not (inv.source.startswith("disc:") or dev_backup):
            recorded = _fixture_lines("makemkvcon_bd_hanako_backup_dev_source_rejected_rc10.txt")
            return list(self._with_drives(recorded[:-1], inv.noscan)), [recorded[-1]], 10

        opened = self._open(inv.source)
        if opened is None:
            text = f"Failed to open disc {inv.source} (fake: no such source)"
            msg = f'MSG:5010,0,0,"{text}","{text}"'
            return [_STARTED_MSG, *self._drv_block(inv.noscan), msg], [text], 1
        if isinstance(opened, _Backup):
            # Recorded: `file:` on a DVD backup image exits 10 after listing
            # the drives, with no MSG and nothing on stderr.
            recorded = _fixture_lines("makemkvcon_dvd_acca_info_file_on_iso_backup_rc10_noscan_after_backup.txt")
            return list(self._with_drives(recorded, inv.noscan)), [], 10
        recording, segfaults = opened
        table = _load(recording.info_min0)
        header = self._with_drives(table.header, inv.noscan)

        if segfaults:
            recorded = _fixture_lines("makemkvcon_bd_hanako_info_file_decrypted_backup_segfault_rc139.txt")
            source_dir = inv.source.partition(":")[2]
            lines = [line.replace("file:///raw/_spike/bd/bk", f"file://{source_dir}") for line in recorded]
            return self._with_drives(lines, inv.noscan), [], 139

        whole = self._failure(inv)
        if whole is not None:
            msg = f'MSG:5010,0,0,"{whole.message}","{whole.message}"'
            return [*header, msg], [whole.message], 1 if whole.returncode is None else whole.returncode

        if inv.command == "info":
            return self._info(table, header, inv.minlength), [], 0
        if inv.command == "backup":
            return self._backup(recording, table, Path(args[0]), inv), [], 0
        return self._mkv(recording, table, header, inv, title_arg=args[0], out_dir=Path(args[1]))

    def _kept(self, table: _Table, minlength: int) -> list[_Title]:
        return [t for t in table.titles if t.duration >= minlength]

    def _info(self, table: _Table, header: list[str], minlength: int) -> list[_Step]:
        kept = self._kept(table, minlength)
        lines: list[_Step] = [*header, f"TCOUNT:{len(kept)}", *table.cinfo]
        for new, title in enumerate(kept):
            lines.extend(_renumber(title, new))
        return lines

    def _backup(self, recording: Recording, table: _Table, out_path: Path, inv: Invocation) -> list[_Step]:
        if not table.blu_ray:
            name = "makemkvcon_dvd_acca_backup_iso_image_prgv_thinned.txt"
        elif inv.decrypt:
            name = "makemkvcon_bd_hanako_backup_decrypt_prgv_thinned.txt"
        else:
            name = "makemkvcon_bd_hanako_backup_encrypted_prgv_thinned.txt"
        lines: list[_Step] = []
        for line in self._with_drives(_fixture_lines(name), inv.noscan):
            if line.startswith("MSG:5070,"):
                lines.append(functools.partial(self._write_backup, recording, table, out_path, inv.decrypt))
            lines.append(_RECORDED_URL_RE.sub(f"file://{out_path}", line))
        return lines

    def _write_backup(self, recording: Recording, table: _Table, out_path: Path, decrypted: bool) -> None:
        if table.blu_ray:
            out_path.mkdir(parents=True, exist_ok=True)
            layout = _fixture_lines("makemkvcon_bd_hanako_backup_encrypted_layout.txt")
            paths = [p.removeprefix("./") for p in layout if p not in (".", "")]
            dirs = {str(Path(p).parent) for p in paths}
            for rel in paths:
                target = out_path / rel
                if rel in dirs:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
        else:
            # Recorded on ACCA_CS_D1: one ISO image at exactly <path>, about
            # the size of the disc's used space.
            out_path.parent.mkdir(parents=True, exist_ok=True)
            _write_sparse(out_path, file_size(sum(t.size_bytes for t in table.titles)))
        self._backups[out_path.resolve()] = _Backup(recording.backup or recording, table.blu_ray, decrypted)

    def _replay_remux(
        self, recording: Recording, inv: Invocation, *, wanted: int, title: _Title, out_dir: Path
    ) -> list[_Step] | None:
        """The recorded stream of this exact single-title `mkv`, if there is one."""
        fixture = next((f for m, i, f in recording.remuxes if (m, i) == (inv.minlength, wanted)), None)
        if fixture is None or any(f.command == "mkv" for f in self._failures):
            return None
        lines: list[_Step] = []
        for line in self._with_drives(_fixture_lines(fixture), inv.noscan):
            if not inv.probes_drive and line.startswith(("MSG:2003,", "MSG:1012,")):
                continue  # the drive's disc is only read (and only errors) when probed
            if line.startswith("MSG:5014,"):
                line = _RECORDED_URL_RE.sub(f"file://{out_dir}", line)
            elif line.startswith("MSG:3006,"):
                line = _RECORDED_URL_RE.sub(f"file://{inv.source.partition(':')[2]}", line)
            elif line.startswith("MSG:5005,"):
                lines.append(
                    functools.partial(_write_sparse, out_dir / output_name(title, wanted), file_size(title.size_bytes))
                )
            lines.append(line)
        return lines

    def _mkv(
        self, recording: Recording, table: _Table, header: list[str], inv: Invocation, *, title_arg: str, out_dir: Path
    ) -> tuple[list[_Step], list[str], int]:
        kept = self._kept(table, inv.minlength)
        numbered = list(enumerate(kept))
        if title_arg != "all":
            wanted = int(title_arg)
            if not 0 <= wanted < len(kept):
                # Recorded: exit 12, no MSG error, no save phase, no file.
                return list(header), [], 12
            numbered = [(wanted, kept[wanted])]
            replayed = self._replay_remux(recording, inv, wanted=wanted, title=kept[wanted], out_dir=out_dir)
            if replayed is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
                return replayed, [], 0

        out_dir.mkdir(parents=True, exist_ok=True)
        url = f"file://{out_dir}"
        lines: list[_Step] = [
            *header,
            'PRGT:5024,0,"Saving all titles to MKV files"',
            f'MSG:5014,131072,2,"Saving {len(numbered)} titles into directory {url}",'
            f'"Saving %1 titles into directory %2","{len(numbered)}","{url}"',
            'PRGC:5057,0,"Analyzing seamless segments"',
        ]
        total = sum(t.size_bytes for _, t in numbered) or 1
        done_bytes = 0
        saved = failed = 0
        returncode = 0
        for position, (new, title) in enumerate(numbered):
            failure = self._failure(inv, title=title.key)
            if failure is not None and failure.returncode is not None:
                returncode = failure.returncode
            if failure is not None and failure.silent:
                continue
            before = done_bytes * 65536 // total
            done_bytes += title.size_bytes
            after = done_bytes * 65536 // total
            target = out_dir / output_name(title, new)
            lines += [f'PRGC:5017,{position},"Saving to MKV file"', f"PRGV:0,{before},65536"]
            if failure is not None:
                failed += 1
                text = f"Failed to save title {new} to file {target}"
                lines.append(f'MSG:5003,0,2,"{text}","Failed to save title %1 to file %2","{new}","{target}"')
                continue
            saved += 1
            lines += [
                functools.partial(_write_sparse, target, file_size(title.size_bytes)),
                f"PRGV:65536,{after},65536",
            ]
        if failed:
            lines.append(
                f'MSG:5037,516,2,"Copy complete. {saved} titles saved, {failed} failed.",'
                f'"Copy complete. %1 titles saved, %2 failed.","{saved}","{failed}"'
            )
        else:
            lines += [
                f'MSG:5005,128,1,"{saved} titles saved","%1 titles saved","{saved}"',
                f'MSG:5036,260,1,"Copy complete. {saved} titles saved.","Copy complete. %1 titles saved.","{saved}"',
            ]
        return lines, [], returncode


def _disc_folder(path: Path) -> Path:
    """The disc folder a `file:` URL names: MakeMKV is pointed at its `BDMV`
    or `VIDEO_TS` directory (`source._disc_tree`), or at the folder itself."""
    resolved = path.resolve()
    return resolved.parent if resolved.name in ("BDMV", "VIDEO_TS") else resolved


def _write_sparse(path: Path, size: int) -> None:
    with path.open("wb") as f:
        f.truncate(size)


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    disc: Recording | None = None,
    device: str | None = "/dev/sr0",
    drive_index: int = 0,
    isos: dict[str, Recording] | None = None,
    folders: dict[str, Recording] | None = None,
    makemkv_version: str = "1.18.4",
) -> FakeMakemkvcon:
    """Route every `asyncio.create_subprocess_exec` to a fake `makemkvcon`.

    `disc` is seated in `device` (`None` = empty tray; `device=None` = no
    drive at all); `isos` maps `.iso` paths and `folders` disc-folder paths
    to their recordings; `makemkv_version` is `"1.18.4"` (the image's) or
    `"2.0.0"` (the recordings'). Any other program spawned during the test
    fails the test.
    """
    assert makemkv_version in ("1.18.4", "2.0.0"), makemkv_version
    fake = FakeMakemkvcon(
        device=device,
        disc=disc,
        drive_index=drive_index,
        isos=dict(isos or {}),
        folders=dict(folders or {}),
        makemkv_version=makemkv_version,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)
    return fake
