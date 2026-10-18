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
  `<dir>` and streams the save phase (`PRGT:5024`, `MSG:5014`, one
  `PRGC:5017,<position>` per title, `PRGV`, `MSG:5005`/`5036`, or `MSG:5037`
  when a title failed). An id with no title exits 12 and writes nothing, with
  no error message (recorded). `mkv all` itself was never recorded; its save
  phase is the recorded single-title shape repeated.
- **`backup disc:<N> <dir>`** replays the recorded BD backup stream, writes the
  recorded folder layout (empty files) and makes `<dir>` readable as
  `file:<dir>`, with the backup's own title numbering when the source has a
  backup recording (on the Hanako-kun BD every id differs from the disc's).
  `N` must be the index of the `DRV:` line for the device. `backup dev:…`
  exits 10 (recorded). A `--decrypt` BD backup is written, but every later
  `file:` read of it exits 139 (recorded segfault).
- **Drive lines.** Every invocation lists the drives first. The configured
  device gets a `DRV:` line at `drive_index`, with the seated disc's label,
  unless `--noscan` is passed: then the drive is listed but its disc isn't
  read (recorded).
- **Invocations** are recorded with `reads_drive` (handed a `dev:` or `disc:`
  source: the drive-access contract, testing rule 5) and `probes_drive`
  (started without `--noscan` while a drive is attached).

Not recorded, so guessed and marked as such: a source that can't be opened
(`MSG:5010`, exit 1), a DVD backup (the BD stream with a `VIDEO_TS/` layout,
read back with the disc's own numbering), and how a single title fails to save
(`MSG:5003`, no file, exit 0 unless the test says otherwise).

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

    `backup` is how a `backup` of it reads back via `file:`; None means
    "with the same numbering as the source" (assumed, not recorded).
    """

    info_min0: str
    backup: Recording | None = None


SINTEL_ISO = Recording("makemkvcon_iso_sintel_info_minlength0.txt")
HANAKO_BD = Recording(
    "makemkvcon_bd_hanako_info_minlength0.txt",
    backup=Recording("makemkvcon_bd_hanako_info_file_backup_minlength0.txt"),
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
    returncode: int | None = None

    @property
    def reads_drive(self) -> bool:
        """Handed the optical drive itself as its source."""
        return self.source.startswith(("dev:", "disc:"))

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


@dataclass
class FakeMakemkvcon:
    device: str | None = "/dev/sr0"
    disc: Recording | None = None
    drive_index: int = 0
    isos: dict[str, Recording] = field(default_factory=dict)
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
        inv = Invocation(
            argv=tuple(argv),
            command=command,
            source=positionals[1],
            minlength=minlength,
            noscan="--noscan" in flags,
            decrypt="--decrypt" in flags,
            drive_attached=self.device is not None,
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

    def _open(self, source: str) -> tuple[Recording, bool] | None:
        """(recording, is a decrypted BD backup) for a source, or None."""
        scheme, _, rest = source.partition(":")
        if scheme == "dev" and self.disc is not None and rest == self.device:
            return self.disc, False
        if scheme == "disc" and self.disc is not None and self.device is not None and rest == str(self.drive_index):
            return self.disc, False
        if scheme == "iso" and rest in self.isos:
            return self.isos[rest], False
        if scheme == "file":
            backup = self._backups.get(Path(rest).resolve())
            if backup is not None:
                return backup.recording, backup.decrypted and backup.blu_ray
        return None

    def _failure(self, inv: Invocation, *, title: str | None = None) -> _Failure | None:
        return next((f for f in self._failures if f.command == inv.command and f.title == title), None)

    def _run(self, inv: Invocation, args: list[str]) -> tuple[list[_Step], list[str], int]:
        if inv.command == "backup" and not inv.source.startswith("disc:"):
            recorded = _fixture_lines("makemkvcon_bd_hanako_backup_dev_source_rejected_rc10.txt")
            return list(self._with_drives(recorded[:-1], inv.noscan)), [recorded[-1]], 10

        opened = self._open(inv.source)
        if opened is None:
            text = f"Failed to open disc {inv.source} (fake: no such source)"
            msg = f'MSG:5010,0,0,"{text}","{text}"'
            return [_STARTED_MSG, *self._drv_block(inv.noscan), msg], [text], 1
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
        return self._mkv(table, header, inv, title_arg=args[0], out_dir=Path(args[1]))

    def _kept(self, table: _Table, minlength: int) -> list[_Title]:
        return [t for t in table.titles if t.duration >= minlength]

    def _info(self, table: _Table, header: list[str], minlength: int) -> list[_Step]:
        kept = self._kept(table, minlength)
        lines: list[_Step] = [*header, f"TCOUNT:{len(kept)}", *table.cinfo]
        for new, title in enumerate(kept):
            lines.extend(_renumber(title, new))
        return lines

    def _backup(self, recording: Recording, table: _Table, out_dir: Path, inv: Invocation) -> list[_Step]:
        name = (
            "makemkvcon_bd_hanako_backup_decrypt_prgv_thinned.txt"
            if inv.decrypt
            else "makemkvcon_bd_hanako_backup_encrypted_prgv_thinned.txt"
        )
        recorded_dir = re.compile(r"file:///raw/_spike/bd/bk\w*")
        lines: list[_Step] = []
        for line in self._with_drives(_fixture_lines(name), inv.noscan):
            if line.startswith("MSG:5070,"):
                lines.append(functools.partial(self._write_backup, recording, table, out_dir, inv.decrypt))
            lines.append(recorded_dir.sub(f"file://{out_dir}", line))
        return lines

    def _write_backup(self, recording: Recording, table: _Table, out_dir: Path, decrypted: bool) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        if table.blu_ray:
            layout = _fixture_lines("makemkvcon_bd_hanako_backup_encrypted_layout.txt")
            paths = [p.removeprefix("./") for p in layout if p not in (".", "")]
            dirs = {str(Path(p).parent) for p in paths}
            for rel in paths:
                target = out_dir / rel
                if rel in dirs:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
        else:
            (out_dir / "VIDEO_TS").mkdir(exist_ok=True)
            (out_dir / "VIDEO_TS" / "VIDEO_TS.IFO").touch()
        self._backups[out_dir.resolve()] = _Backup(recording.backup or recording, table.blu_ray, decrypted)

    def _mkv(
        self, table: _Table, header: list[str], inv: Invocation, *, title_arg: str, out_dir: Path
    ) -> tuple[list[_Step], list[str], int]:
        kept = self._kept(table, inv.minlength)
        numbered = list(enumerate(kept))
        if title_arg != "all":
            wanted = int(title_arg)
            if not 0 <= wanted < len(kept):
                # Recorded: exit 12, no MSG error, no save phase, no file.
                return list(header), [], 12
            numbered = [(wanted, kept[wanted])]

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
) -> FakeMakemkvcon:
    """Route every `asyncio.create_subprocess_exec` to a fake `makemkvcon`.

    `disc` is seated in `device` (`None` = empty tray; `device=None` = no
    drive at all); `isos` maps `.iso` paths to their recordings. Any other
    program spawned during the test fails the test.
    """
    fake = FakeMakemkvcon(device=device, disc=disc, drive_index=drive_index, isos=dict(isos or {}))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)
    return fake
