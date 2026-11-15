import asyncio
import csv
import logging
from typing import Iterable, NamedTuple

from arm_common import DiscType
from arm_common.schemas import DiscFingerprintInput, ScanResult, ScanTitle
from arm_ripper.scan.disc_probe import DiscProbe, probe_disc
from arm_ripper.scan.makemkv_info import parse_makemkvcon_info
from arm_ripper.source import is_file_source, makemkv_source_url

logger = logging.getLogger("arm_ripper.scan.makemkv")

SCAN_TIMEOUT_SECONDS = 300.0

# MakeMKV emits this when its hard-coded 60-day beta kill-switch has fired.
# No registration key overrides it; the binary refuses all protected-disc
# work and the only fix is rebuilding against a fresher upstream tarball.
# See docs/user/MakeMKV-Ripper.md § Failure modes.
MAKEMKV_EXPIRED_PREFIX = b"MSG:5021,"


class ScanError(Exception):
    pass


class MakemkvBinaryExpiredError(ScanError):
    """Distinct subtype so callers / operators can match this specifically
    and route to the upstream-blocked playbook instead of a generic retry."""


_EMPTY_SCAN_MAX_MESSAGES = 15


def summarize_empty_scan(lines: Iterable[str], *, drives: bool = True) -> str:
    """Condense makemkvcon's robot output into something an operator can act
    on when a scan found no titles: MakeMKV's own MSG texts (deduped, capped)
    and, for a drive source, the drives it could see (DRV lines with a name).

    Robot-mode formats (quoted fields may contain commas):
      MSG:code,flags,count,"message","format",params...
      DRV:index,visible,enabled,flags,"drive name","disc name","device path"
    No named drive at all means MakeMKV couldn't open the drive: it finds
    drives through their /dev/sgN node, so that node is missing or unreadable.
    """
    messages: list[str] = []
    seen: list[str] = []
    for raw in lines:
        msg_type, _, rest = raw.strip().partition(":")
        if msg_type not in ("MSG", "DRV") or not rest:
            continue
        fields = next(csv.reader([rest]), [])
        if msg_type == "MSG" and len(fields) >= 4:
            text = f"[{fields[0]}] {fields[3]}"
            if text not in messages:
                messages.append(text)
        elif msg_type == "DRV" and len(fields) >= 7 and fields[4]:
            disc = f' disc="{fields[5]}"' if fields[5] else ""
            seen.append(f'"{fields[4]}" dev={fields[6] or "?"}{disc}')

    shown = messages[:_EMPTY_SCAN_MAX_MESSAGES]
    if len(messages) > len(shown):
        shown.append(f"... {len(messages) - len(shown)} more")
    said = f"makemkv said: {' | '.join(shown) if shown else '(no MSG output)'}"
    if not drives:
        return said
    drive_text = (
        "; ".join(seen)
        if seen
        else "none (MakeMKV could not open any drive: is the drive's /dev/sgN node in this container?)"
    )
    return f"drives seen: {drive_text}; {said}"


def _classify_from_titles(titles: list[ScanTitle]) -> DiscType:
    """Last-resort fallback used only when MakeMKV's CINFO:1 disc-type
    string was missing. Title size > 4.7GB is unreliable (DVD-9s exceed it
    routinely) but better than UNKNOWN.
    """
    if not titles:
        return DiscType.UNKNOWN
    # Every duration unknown (a cut-short scan): fall back on DVD, as for a
    # disc with no long title, rather than fail on max() of nothing.
    longest = max((t.duration_seconds for t in titles if t.duration_seconds is not None), default=0)
    return (
        DiscType.BLURAY
        if longest >= 60 * 60 * 1.5 and any(t.size_bytes is not None and t.size_bytes > 4_700_000_000 for t in titles)
        else DiscType.DVD
    )


async def _read_info_stdout(proc: asyncio.subprocess.Process) -> tuple[bytes, bool]:
    """Read makemkvcon's stdout to EOF; return it and whether MSG:5021 (expired binary) appeared.

    The first MSG:5021 line kills makemkvcon; the pipe is still drained to EOF.
    """
    assert proc.stdout is not None
    chunks: list[bytes] = []
    expired = False
    async for raw in proc.stdout:
        chunks.append(raw)
        if not expired and raw.startswith(MAKEMKV_EXPIRED_PREFIX):
            expired = True
            proc.kill()
            # Keep draining the pipe so the kernel doesn't block on
            # buffered writes between SIGKILL and process teardown.
    return b"".join(chunks), expired


async def _read_info_stderr(proc: asyncio.subprocess.Process) -> bytes:
    """Read makemkvcon's stderr to EOF."""
    assert proc.stderr is not None
    chunks: list[bytes] = []
    async for chunk in proc.stderr:
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_makemkvcon_stream(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes, bool]:
    """Drain stdout + stderr concurrently. Returns (stdout, stderr, expired).

    Watches stdout line-by-line; on `MSG:5021,` (binary kill-switch fired)
    kills the subprocess so the caller short-circuits the SCAN_TIMEOUT_SECONDS
    ceiling. With an expired binary makemkvcon can either exit instantly
    (`info disc:9999`) or spin against the ISO/disc structure for the full
    5 minutes (`info iso:/big.iso`) — the early-kill turns the slow path
    into a deterministic ~few-second failure.
    """
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_task = asyncio.create_task(_read_info_stdout(proc))
    stderr_task = asyncio.create_task(_read_info_stderr(proc))
    stdout_bytes, expired = await stdout_task
    stderr_bytes = await stderr_task
    return stdout_bytes, stderr_bytes, expired


class _InfoRun(NamedTuple):
    """What one `makemkvcon info` run printed, and how it exited."""

    stdout: bytes
    stderr: bytes
    expired: bool
    returncode: int | None


async def _run_info(device_path: str) -> _InfoRun:
    """Run `makemkvcon info` on the device under the scan timeout, at
    `--minlength=0` (see `scan_disc`).

    Raises ScanError when makemkvcon is not on PATH or times out.
    """
    cmd = ["makemkvcon", "-r", "--cache=1", "--minlength=0", "info", makemkv_source_url(device_path)]
    logger.info("makemkvcon info device=%s", device_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ScanError("makemkvcon binary not found on PATH") from e

    try:
        stdout, stderr, expired = await asyncio.wait_for(_read_makemkvcon_stream(proc), timeout=SCAN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ScanError(f"makemkvcon timed out after {SCAN_TIMEOUT_SECONDS}s") from None

    await proc.wait()
    return _InfoRun(stdout, stderr, expired, proc.returncode)


def _raise_for_failure(run: _InfoRun) -> None:
    """Raise when the info run failed.

    MakemkvBinaryExpiredError for an expired binary, else ScanError on a non-zero exit.
    """
    if run.expired:
        raise MakemkvBinaryExpiredError(
            "makemkvcon refused: binary is past its hard-coded expiry "
            "(MSG:5021 'application version is too old'). MakeMKV beta "
            "binaries carry a 60-day kill-switch from release date that "
            "no registration key overrides; the only fix is rebuilding the "
            "ripper image after upstream ships a fresher tarball. See "
            "docs/user/MakeMKV-Ripper.md § Failure modes."
        )

    if run.returncode != 0:
        msg = run.stderr.decode(errors="replace").strip() or run.stdout.decode(errors="replace")[:200]
        raise ScanError(f"makemkvcon exited {run.returncode}: {msg}")


def _fingerprints(probe: DiscProbe) -> list[DiscFingerprintInput]:
    """The disc fingerprints the probe found, in the order crc64, thediscdb, matrix256; empty values are skipped."""
    fingerprints: list[DiscFingerprintInput] = []
    if probe.crc64:
        fingerprints.append(DiscFingerprintInput(algo="crc64", value=probe.crc64))
    if probe.thediscdb:
        fingerprints.append(DiscFingerprintInput(algo="thediscdb", value=probe.thediscdb))
    if probe.matrix256:
        fingerprints.append(DiscFingerprintInput(algo="matrix256", value=probe.matrix256))
    return fingerprints


async def scan_disc(device_path: str) -> ScanResult:
    """List every title on the disc, however short.

    Runs `info` at `--minlength=0`: MakeMKV numbers titles densely over the
    ones that pass its minimum, so a scan at its built-in 120 s would number
    titles differently from a rip run at any other minimum (a session
    override). Scanning everything lets the rip's minimum be applied
    afterwards, to titles whose ids are known. The caller records which
    titles are worth a Track row in `ScanResult.min_length_seconds`.
    """
    run = await _run_info(device_path)

    _raise_for_failure(run)

    lines = run.stdout.decode(errors="replace").splitlines()
    volume_label, titles, mkv_disc_type = parse_makemkvcon_info(lines)
    if not titles:
        # Without this MakeMKV's reason (no drive found, AACS failure, read
        # errors) was discarded and the operator only ever saw "0 titles".
        logger.warning(
            "makemkvcon found 0 titles device=%s: %s",
            device_path,
            summarize_empty_scan(lines, drives=not is_file_source(device_path)),
        )

    # MakeMKV's CINFO:1 is the authoritative disc-type signal — works on
    # region-locked discs that the kernel refuses to mount, and on UDF
    # quirks; when CINFO:1 is missing we fall back to a title-size heuristic.
    # Classify before probing so the probe knows whether to bother opening
    # the disc a third time for the Blu-ray-only BDMT read.
    if mkv_disc_type is not None:
        disc_type = mkv_disc_type
    else:
        disc_type = _classify_from_titles(titles)
    probe = await probe_disc(
        device_path, bluray=disc_type == DiscType.BLURAY, menus=disc_type in (DiscType.DVD, DiscType.BLURAY)
    )

    return ScanResult(
        disc_type=disc_type,
        volume_label=volume_label,
        titles=titles,
        fingerprints=_fingerprints(probe),
        bd_meta=probe.bd_meta,
        menu_kind=probe.menu_kind,
    )


async def list_source_titles(source_url: str, *, minlength_seconds: int = 0) -> list[SourceTitle]:
    """The titles of a disc backup, an ISO or a disc folder, numbered as `mkv`
    at the same `--minlength` numbers them (backup-then-remux).

    `--noscan`: the drive is listed but its disc is not read; the titles come
    from `source_url`. Raises ScanError when makemkvcon fails or lists
    nothing (MakeMKV 2.0.0 exits 10 with no message reading a DVD backup
    image as `file:`).
    """
    cmd = ["makemkvcon", "-r", "--cache=1", "--noscan", f"--minlength={minlength_seconds}", "info", source_url]
    logger.info("makemkvcon info source=%s minlength=%ds", source_url, minlength_seconds)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise ScanError("makemkvcon binary not found on PATH") from e
    try:
        stdout, stderr, expired = await asyncio.wait_for(_read_makemkvcon_stream(proc), timeout=SCAN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ScanError(f"makemkvcon timed out after {SCAN_TIMEOUT_SECONDS}s") from None
    await proc.wait()
    if expired:
        raise MakemkvBinaryExpiredError("makemkvcon refused: binary is past its hard-coded expiry (MSG:5021)")
    lines = stdout.decode(errors="replace").splitlines()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or summarize_empty_scan(lines)
        raise ScanError(f"makemkvcon exited {proc.returncode} reading {source_url}: {detail}")
    _, titles, _ = parse_makemkvcon_info(lines)
    if not titles:
        raise ScanError(f"makemkvcon listed no titles in {source_url}: {summarize_empty_scan(lines)}")
    equal = parse_equal_playlists(lines)
    return [
        SourceTitle.model_validate(
            {**t.model_dump(), "equal_playlists": tuple(sorted(equal.get(t.source_playlist or "", ())))}
        )
        for t in titles
    ]


class SourceTitle(ScanTitle):
    """A title of a disc backup, an ISO or a disc folder (`list_source_titles`).

    `equal_playlists` are the playlists MakeMKV found equal to this title's
    and skipped (`MSG:3309`). Which playlist of an equal pair MakeMKV keeps
    isn't stable between a disc and its backup: on Princess Tutu BD-1
    (1.18.4) the disc scan kept 00007, 00010 and 00011.mpls and the backup
    their twins 00017, 00020 and 00021.mpls, so the disc's playlists weren't
    in the backup's listing at all.
    """

    equal_playlists: tuple[str, ...] = ()


def parse_equal_playlists(lines: Iterable[str]) -> dict[str, frozenset[str]]:
    """Map each playlist MakeMKV called equal to another to all the others it
    is equal to, from `MSG:3309,…,"Title %1 is equal to title %2 and was
    skipped","00007.mpls","00017.mpls"`. Equality is transitive: MakeMKV can
    keep a playlist and later skip it as equal to a third (00057 ≡ 00012).
    """
    parent: dict[str, str] = {}

    def root(name: str) -> str:
        while (up := parent.setdefault(name, name)) != name:
            parent[name] = parent.setdefault(up, up)
            name = up
        return name

    for raw in lines:
        msg_type, _, rest = raw.strip().partition(":")
        if msg_type != "MSG" or not rest.startswith("3309,"):
            continue
        fields = next(csv.reader([rest]), [])
        if len(fields) >= 7 and fields[5] and fields[6]:
            parent[root(fields[5])] = root(fields[6])

    groups: dict[str, set[str]] = {}
    for name in list(parent):
        groups.setdefault(root(name), set()).add(name)
    return {name: frozenset(members - {name}) for members in groups.values() for name in members}
