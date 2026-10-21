"""Single-invocation MakeMKV rip.

`rip_disc` shells out to `makemkvcon mkv ... all <outdir>` exactly once
per disc — the v2 pattern. The drive stays open for the entire rip, so
the between-titles SCSI NOT_READY / USB-autosuspend gap that plagued
the per-title implementation simply doesn't exist anymore.

Per-title attribution is reconstructed from the robot stream:
  - PRGC:5017 ("Saving to MKV file") lines fire once at the start of
    every title save; the second field is the *output position* (0-based)
    that matches the `_tNN.mkv` filename suffix and indexes into
    `eligible_source_indexes` to recover the source title index. This
    is the primary per-title milestone in `mkv all` mode (confirmed
    empirically against MakeMKV 1.18.3 — the older PRGT "Saving title
    #N" milestone never fires in this mode).
  - PRGV gives [0..1] fractional progress; `current/max` is per-title
    (drives `on_title_progress`), `total/max` is disc-overall (used as
    a fallback `on_disc_progress` for the brief window before the first
    PRGC:5017 arrives).
  - MSG:5003 lines tell us a title failed (with reason). We capture the
    title index from the args.
  - Post-exit, we walk `output_dir` for `title_tNN.mkv` files. The file
    `_tNN.mkv` belongs to `eligible_source_indexes[NN]`: matched by the
    suffix's value, never by its rank among the files found, so a title
    MakeMKV skipped can't shift its neighbours onto the wrong tracks. A
    file that exists for an attempted title means success; an attempted
    title with no file of its own number means failure (reason from
    MSG:5003 if any, otherwise "no .mkv produced").

Tracks that were *not* attempted by makemkvcon (below `--minlength`)
never appear in the result map; the dispatcher decides what to do
with them (typically: mark FAILED with "skipped: below minlength").

When only some titles are kept, `mkv` can't be told which (it takes one
title id or `all`), and one `mkv dev:` per title is banned (the drive idles
between titles; USB-BD drives then autosuspend). Backup-then-remux reads the
drive once (`makemkv_backup.rip_backup`) and `remux_title` writes each kept
title from that backup, or from an ISO or folder source, with `--noscan` so
no remux reads the drive's disc.
"""

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from arm_ripper.rip.hashing import sha256_file
from arm_ripper.source import makemkv_source_url

logger = logging.getLogger("arm_ripper.rip.makemkv")

RIP_TIMEOUT_SECONDS = 6 * 60 * 60  # 6 hours: worst-case BD
# The error a rip reports when its caller set `stop` (the drive dropped off the
# bus mid-rip, arm_ripper.job_controller): makemkvcon was killed, not failed.
STOPPED_ERROR = "makemkvcon stopped: the drive disconnected"

OnTitleStart = Callable[[int], Awaitable[None]]
OnTitleProgress = Callable[[int, float], Awaitable[None]]
# Fired the instant title N's save phase is known to be complete:
# either when PRGC:5017,N+1 transitions us off title N (MakeMKV has
# moved to the next title — N's file is finalised) or, for the last
# title in the rip, when makemkvcon exits cleanly. The dispatcher uses
# this to PATCH each Track DONE with size + output_path as it finishes
# instead of waiting for the whole `mkv all` invocation to end.
OnTitleDone = Callable[[int], Awaitable[None]]
# Fired with the disc-overall PRGV `total/max` fraction whenever no
# per-title PRGT has identified the title currently being written.
# `mkv all` mode is opaque per-title (a single "Saving all titles to
# MKV files" PRGT and that's it), so this callback is the only signal
# the dashboard ever gets during the rip phase. See dispatcher's
# `_on_disc_progress` for attribution to a track id.
OnDiscProgress = Callable[[float], Awaitable[None]]

_PRGV_RE = re.compile(r"^PRGV:(\d+),(\d+),(\d+)$")
_PRGT_RE = re.compile(r'^PRGT:(\d+),(\d+),"(.*)"$')
# PRGC:5017,N,"Saving to MKV file" fires once per title save start in
# `mkv all` mode. N is the 0-based *output position* (matches the
# `_tNN.mkv` filename suffix and `eligible_source_indexes[N]`).
_PRGC_SAVE_TITLE_RE = re.compile(r'^PRGC:5017,(\d+),"')
_MSG_HEADER_RE = re.compile(r"^MSG:(\d+),(\d+),(\d+),")
_QUOTED_ARG_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# PRGT text for the "now saving title N" milestone. Older MakeMKV builds
# in per-title invocation mode emit "Saving title #N to MKV file"; in
# `mkv all` mode on 1.18.3+ this never fires (PRGC:5017 is the per-title
# signal there). The PRGT path is kept for defensive compatibility.
_PRGT_SAVING_TITLE_RE = re.compile(r"\btitle\s+#?(\d+)\b", re.IGNORECASE)

# MakeMKV codes worth surfacing in disc-level error text.
#  1002 — LIBMKV_TRACE Exception (e.g. "Error while reading input")
#  3032 — drive/disc region mismatch
#  5003 — Failed to save title N to file ... (per-title)
#  5037 — Copy complete. X titles saved, Y failed (final summary)
# Reference: https://github.com/automatic-ripping-machine/automatic-ripping-machine/wiki/MakeMKV-Codes
_DIAGNOSTIC_MSG_CODES = frozenset({1002, 3032, 5003, 5037})

# MSG codes we recognise for per-title outcomes. 5003 carries the
# failure reason; we don't need a success code because file existence
# in the output dir is the source of truth for "title saved ok".
_TITLE_FAIL_CODE = 5003


@dataclass
class RipResult:
    ok: bool
    output_path: Path | None = None
    size_bytes: int | None = None
    duration_seconds: int | None = None
    sha256: str | None = None
    error: str | None = None


@dataclass
class _TitleState:
    """Per-title state reconstructed from the stream."""

    fail_reason: str | None = None
    started_emitted: bool = False
    done_emitted: bool = False


@dataclass
class _ParserState:
    """Shared mutable state for the stream parser. Callbacks fire from
    inside the streamer task; the dispatcher resolves title_index →
    TrackView upstream of us."""

    titles: dict[int, _TitleState] = field(default_factory=dict)
    current_title: int | None = None
    diagnostics: list[str] = field(default_factory=list)
    # Flips True when MakeMKV emits PRGT:5024 "Saving all titles to MKV
    # files" (the save phase boundary). Pre-rip PRGV events have a
    # `total/max` channel that reaches ~70-80% during analyse/decrypt
    # but isn't *rip* progress; firing the disc-overall fallback for
    # those would publish a misleading 70-80% to the dashboard then
    # snap to 0% when the first PRGC:5017 fires, breaking the ETA
    # baseline (rips store sees a sustained negative pctDelta and
    # holds ETA null until per-title progress climbs back past the
    # pre-rip peak — ~hours for a long main feature).
    save_phase_started: bool = False
    # Set when MakeMKV emits MSG:5021 (the binary's hard-coded 60-day
    # beta kill-switch). The streamer kills the subprocess as soon as
    # this is seen so a 6-hour RIP_TIMEOUT_SECONDS doesn't run out.
    # See docs/user/MakeMKV-Ripper.md § Failure modes.
    binary_expired: bool = False


def parse_progress_line(line: str) -> float | None:
    """Return the per-operation fractional progress [0, 1] from a PRGV
    line (`current/max`), or None.

    Used to drive per-title progress when a "Saving title #N" PRGT
    has previously identified the in-flight title. In `mkv all` mode
    the per-title PRGT never fires; see `parse_progress_totals` for
    the disc-overall fallback.
    """
    m = _PRGV_RE.match(line.strip())
    if not m:
        return None
    _current, _total, max_ = (int(g) for g in m.groups())
    if max_ <= 0:
        return None
    return min(1.0, max(0.0, _current / max_))


def parse_progress_totals(line: str) -> tuple[float, float] | None:
    """Return (current/max, total/max) from a PRGV line, or None.

    `current/max` is per-operation progress (resets between titles in
    `mkv ... all` mode); `total/max` is disc-overall progress that
    advances monotonically across the whole rip. The streamer uses the
    `total` channel as the disc-level signal when no per-title PRGT
    has set `current_title`.
    """
    m = _PRGV_RE.match(line.strip())
    if not m:
        return None
    current, total, max_ = (int(g) for g in m.groups())
    if max_ <= 0:
        return None
    cur_frac = min(1.0, max(0.0, current / max_))
    tot_frac = min(1.0, max(0.0, total / max_))
    return cur_frac, tot_frac


def parse_msg_args(line: str) -> tuple[int, list[str]] | None:
    """Parse a MSG: line into (code, args). args is the list of trailing
    quoted strings *after* the rendered text and format string — i.e.
    the substitution arguments. Returns None if the line isn't a MSG.
    """
    header = _MSG_HEADER_RE.match(line.strip())
    if not header:
        return None
    code = int(header.group(1))
    quoted = [m.group(1) for m in _QUOTED_ARG_RE.finditer(line)]
    # args[0] = rendered text, args[1] = format string, args[2:] = substitution values.
    args = [a.replace('\\"', '"') for a in quoted[2:]]
    return code, args


def parse_diagnostic_msg(line: str) -> tuple[int, str] | None:
    """If `line` is a MSG: line whose code is in _DIAGNOSTIC_MSG_CODES,
    return (code, rendered_text). Otherwise None.

    Used by `_compose_error` to surface MakeMKV-side reasons when the
    overall rip exits non-zero (or exits 0 but produces no files).
    """
    header = _MSG_HEADER_RE.match(line.strip())
    if not header:
        return None
    code = int(header.group(1))
    if code not in _DIAGNOSTIC_MSG_CODES:
        return None
    quoted = _QUOTED_ARG_RE.search(line)
    if quoted is None:
        return None
    text = quoted.group(1).replace('\\"', '"')
    return code, text


def parse_prgt_title(line: str) -> int | None:
    """If `line` is a PRGT whose text announces "Saving title N to
    MKV file", return N. Otherwise None.

    Defensive — empirically silent in `mkv all` mode on MakeMKV 1.18.3
    (PRGC:5017 is the actual per-title signal there; see
    `parse_prgc_save_position`). Kept for older versions / per-title
    invocation modes that may still emit this milestone.
    """
    m = _PRGT_RE.match(line.strip())
    if not m:
        return None
    text = m.group(3)
    # Filter on "saving" to avoid matching e.g. "Reading information for title 3".
    if "saving" not in text.lower():
        return None
    title_match = _PRGT_SAVING_TITLE_RE.search(text)
    if not title_match:
        return None
    return int(title_match.group(1))


def parse_prgc_save_position(line: str) -> int | None:
    """If `line` is a PRGC:5017 ("Saving to MKV file") line, return the
    0-based output position from its second field. Otherwise None.

    In `mkv all` mode MakeMKV emits one PRGC:5017 per title save start,
    with the second field stepping 0, 1, 2, … through the eligible
    titles in rip order. That position matches the `_tNN.mkv` filename
    suffix and indexes into the dispatcher's `eligible_source_indexes`.
    Reference v2's parser at arm/ui/jobs/json_api.py:116 which uses the
    same field.
    """
    m = _PRGC_SAVE_TITLE_RE.match(line.strip())
    if not m:
        return None
    return int(m.group(1))


def _extract_title_index_from_msg5003(args: list[str]) -> int | None:
    """MSG:5003 args vary slightly across MakeMKV versions but always
    include the title index as one of the integer-string arguments.
    Heuristic: take the first arg that parses cleanly as an int."""
    for a in args:
        try:
            return int(a)
        except ValueError:
            continue
    return None


async def _emit_title_start(
    state: _ParserState,
    title_idx: int,
    on_title_start: OnTitleStart | None,
) -> None:
    """Set current_title and fire on_title_start exactly once per title."""
    state.current_title = title_idx
    ts = state.titles.setdefault(title_idx, _TitleState())
    if ts.started_emitted:
        return
    ts.started_emitted = True
    if on_title_start is not None:
        try:
            await on_title_start(title_idx)
        except Exception as exc:
            logger.debug("title start callback raised: %s", exc)


async def _emit_title_done(
    state: _ParserState,
    title_idx: int,
    on_title_done: OnTitleDone | None,
) -> None:
    """Fire on_title_done exactly once per title."""
    ts = state.titles.setdefault(title_idx, _TitleState())
    if ts.done_emitted:
        return
    ts.done_emitted = True
    if on_title_done is not None:
        try:
            await on_title_done(title_idx)
        except Exception as exc:
            logger.debug("title done callback raised: %s", exc)


async def _stream_output(
    proc: asyncio.subprocess.Process,
    state: _ParserState,
    on_title_start: OnTitleStart | None,
    on_title_progress: OnTitleProgress | None,
    on_disc_progress: OnDiscProgress | None,
    on_title_done: OnTitleDone | None = None,
    eligible_source_indexes: list[int] | None = None,
    gate_disc_progress: bool = True,
) -> None:
    assert proc.stdout is not None
    eligible = eligible_source_indexes or []
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            return
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        # Raw makemkvcon line → DEBUG service log, then per-job log via
        # the backend's tailer. ~30 k lines per BD rip; gated on
        # ARM_LOG_LEVEL=debug so it's free in INFO-mode operation.
        logger.debug("makemkv-raw: %s", line)

        # Hard-coded 60-day kill-switch in the makemkv-bin blob. Kill the
        # subprocess so the 6-hour RIP_TIMEOUT_SECONDS doesn't fire — the
        # caller checks state.binary_expired to surface a distinct error.
        # See docs/user/MakeMKV-Ripper.md § Failure modes.
        if not state.binary_expired and line.startswith("MSG:5021,"):
            state.binary_expired = True
            state.diagnostics.append(line)
            proc.kill()
            continue

        # PRGV — two channels in one line:
        #   `current/max` advances per-operation (resets per title)
        #   `total/max`   advances disc-overall (monotonic *within
        #                 each makemkvcon phase*; pre-rip analyse can
        #                 itself reach 70-80% before the save phase
        #                 starts and resets it back to 0)
        # When PRGC:5017 has identified the current title, drive the
        # per-title callback with `current/max`. Otherwise — only
        # if we've entered the save phase — fall back to the
        # disc-overall callback so the dashboard bar still advances
        # in the brief window before the first PRGC:5017 (or for the
        # whole save phase on hypothetical MakeMKV builds that don't
        # emit PRGC:5017). Pre-rip PRGV is silently dropped to keep
        # the rips-store ETA baseline anchored at per-title 0%.
        totals = parse_progress_totals(line)
        if totals is not None:
            cur_frac, tot_frac = totals
            if state.current_title is not None and on_title_progress is not None:
                try:
                    await on_title_progress(state.current_title, cur_frac)
                except Exception as exc:
                    logger.debug("title progress callback raised: %s", exc)
            elif (state.save_phase_started or not gate_disc_progress) and on_disc_progress is not None:
                try:
                    await on_disc_progress(tot_frac)
                except Exception as exc:
                    logger.debug("disc progress callback raised: %s", exc)
            continue

        # PRGC:5017 — primary per-title signal in `mkv all` mode. The
        # second field is the output position (0-based) which we map
        # back to a source title index via the eligible list.
        out_pos = parse_prgc_save_position(line)
        if out_pos is not None:
            if 0 <= out_pos < len(eligible):
                source_idx = eligible[out_pos]
                # Transition signal: the previous title (if any) is now
                # finalised on disk — MakeMKV has closed its file and
                # moved on. Fire on_title_done before the next start so
                # the dispatcher can PATCH N=DONE before N+1 goes
                # IN_PROGRESS, keeping the lifecycle ordering clean.
                if state.current_title is not None and state.current_title != source_idx:
                    await _emit_title_done(state, state.current_title, on_title_done)
                await _emit_title_start(state, source_idx, on_title_start)
            else:
                logger.warning(
                    "PRGC:5017 output position %d out of range (eligible has %d entries)",
                    out_pos,
                    len(eligible),
                )
            continue

        # PRGT — defensive fallback for older MakeMKV / per-title mode
        # which sometimes emits "Saving title N" instead of (or in
        # addition to) PRGC:5017. Guarded by `started_emitted` so a
        # late PRGT doesn't reset progress on a title PRGC already
        # opened.
        title_idx = parse_prgt_title(line)
        if title_idx is not None:
            await _emit_title_start(state, title_idx, on_title_start)
            continue

        # Generic PRGT (other milestones) — log; also detect the save
        # phase boundary (PRGT:5024 "Saving all titles to MKV files")
        # which gates the disc-overall PRGV fallback.
        prgt = _PRGT_RE.match(line.strip())
        if prgt:
            if prgt.group(1) == "5024":
                state.save_phase_started = True
            logger.info("makemkvcon milestone: %s", prgt.group(3))
            continue

        if line.startswith("MSG:"):
            diag = parse_diagnostic_msg(line)
            if diag is not None:
                state.diagnostics.append(diag[1])
            parsed = parse_msg_args(line)
            if parsed is not None:
                code, args = parsed
                if code == _TITLE_FAIL_CODE:
                    fail_idx = _extract_title_index_from_msg5003(args)
                    rendered = next(iter(_QUOTED_ARG_RE.finditer(line)), None)
                    reason = rendered.group(1).replace('\\"', '"') if rendered else "save failed"
                    if fail_idx is not None:
                        ts = state.titles.setdefault(fail_idx, _TitleState())
                        ts.fail_reason = reason
                    else:
                        # Unattributable fail — append to overall diagnostics
                        # so the disc-level error captures it.
                        logger.warning("MSG:5003 without parseable title index: %s", line)
            # MSG lines are MakeMKV's own account of the rip (a few dozen per
            # disc, unlike the ~30k PRGV lines) and the only record of why a
            # title was skipped or failed, so they reach the job log at INFO.
            logger.info("makemkvcon: %s", line)


def _compose_error(prefix: str, diagnostics: list[str]) -> str:
    """Stitch the diagnostic MakeMKV messages onto the failure summary so
    the dispatcher can surface the actual cause (e.g. "Error while reading
    input", "Failed to save title 2 to file ...") instead of just the
    generic exit-code wrapper."""
    if not diagnostics:
        return prefix
    seen: list[str] = []
    for d in diagnostics:
        if not seen or seen[-1] != d:
            seen.append(d)
    return f"{prefix}: {'; '.join(seen)}"


_OUTPUT_FILE_RE = re.compile(r"_t(\d+)\.mkv$", re.IGNORECASE)


def _output_files_by_position(output_dir: Path) -> dict[int, Path]:
    """Map the value of each produced `.mkv`'s `_tNN` suffix (its output
    position in this rip) to the file.

    MakeMKV in `mkv ... all <outdir>` mode writes files named
    `<volume_label>_tNN.mkv` (or `<custom_template>_tNN.mkv` if the
    user has tweaked their MakeMKV profile). The suffix `NN` is the
    *position in the eligible-rip output*, not the source title index.
    Example: a Blu-ray with source titles {0, 1, 2, ...} where only
    titles 0 and 2 met `--minlength` produces `<label>_t00.mkv`
    (= source 0) and `<label>_t01.mkv` (= source 2).

    The position comes from the name, not from the file's rank in the
    directory: when MakeMKV skips a title mid-rip (no `_t03.mkv`), the
    files after the gap keep their own numbers (`_t04.mkv` is still
    position 4), so callers index `eligible_source_indexes[NN]` with it.

    Files without a parseable `_tNN.mkv` suffix (legacy strays, partial
    writes from a crashed earlier run, etc.) are skipped. If two files
    share a suffix (a stray from an earlier run under another label),
    the first by name wins and the other stays on disk unclaimed.
    """
    by_position: dict[int, Path] = {}
    for f in sorted(output_dir.glob("*.mkv")):
        m = _OUTPUT_FILE_RE.search(f.name)
        if not m:
            continue
        position = int(m.group(1))
        if position in by_position:
            logger.warning(
                "two output files claim position %d: keeping %s, ignoring %s",
                position,
                by_position[position].name,
                f.name,
            )
            continue
        by_position[position] = f
    return by_position


@dataclass
class RipDiscResult:
    """Outcome of one `rip_disc` invocation.

    `overall_error` is set when makemkvcon itself failed (non-zero exit,
    timeout, exec error). When `overall_error` is None the rip ran to
    completion; per-title outcomes live in `titles` and the dispatcher
    looks them up to attribute success/failure to track records.
    """

    overall_error: str | None
    titles: dict[int, RipResult]


async def _run_makemkvcon(
    cmd: list[str],
    state: _ParserState,
    *,
    on_title_start: OnTitleStart | None = None,
    on_title_progress: OnTitleProgress | None = None,
    on_disc_progress: OnDiscProgress | None = None,
    on_title_done: OnTitleDone | None = None,
    eligible_source_indexes: list[int] | None = None,
    gate_disc_progress: bool = True,
    stop: asyncio.Event | None = None,
) -> str | None:
    """Run one makemkvcon invocation to completion, streaming its robot output
    into `state`. Returns None on a clean exit, else the error text: not on
    PATH, timed out, the expired-binary kill switch, a non-zero exit, or
    STOPPED_ERROR once `stop` is set (the process is killed at once rather
    than left to fail every remaining title against a dead device node).
    Cancel-safe: a cancelled caller kills the subprocess before unwinding."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return f"makemkvcon not on PATH: {e}"

    streamer = asyncio.create_task(
        _stream_output(
            proc,
            state,
            on_title_start,
            on_title_progress,
            on_disc_progress,
            on_title_done=on_title_done,
            eligible_source_indexes=eligible_source_indexes,
            gate_disc_progress=gate_disc_progress,
        )
    )
    stopped = False
    try:
        if stop is None:
            await asyncio.wait_for(proc.wait(), timeout=RIP_TIMEOUT_SECONDS)
        else:
            exited = asyncio.create_task(proc.wait())
            stop_seen = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    {exited, stop_seen}, timeout=RIP_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                stop_seen.cancel()
                exited.cancel()
            if not done:
                raise asyncio.TimeoutError
            stopped = exited not in done
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        streamer.cancel()
        return _compose_error(f"makemkvcon timed out after {RIP_TIMEOUT_SECONDS}s", state.diagnostics)
    finally:
        # Cancel-safe cleanup: parent abandon → CancelledError. Kill the
        # subprocess so /raw can be wiped without orphan fds.
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except BaseException:
                pass
        streamer.cancel()

    # Drain the streamer so every line after EOF is processed before callers read `state`.
    try:
        await streamer
    except asyncio.CancelledError:
        pass

    if stopped:
        return STOPPED_ERROR

    if state.binary_expired:
        return _compose_error(
            "makemkvcon refused: binary is past its hard-coded expiry "
            "(MSG:5021). MakeMKV beta binaries carry a 60-day kill-switch; "
            "rebuild the ripper image after upstream ships a fresher tarball. "
            "See docs/user/MakeMKV-Ripper.md § Failure modes.",
            state.diagnostics,
        )

    if proc.returncode != 0:
        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        msg = stderr.decode(errors="replace").strip()[:400] or f"exit={proc.returncode}"
        return _compose_error(f"makemkvcon failed: {msg}", state.diagnostics)
    return None


async def rip_disc(
    device_path: str,
    output_dir: Path,
    *,
    minlength_seconds: int = 120,
    eligible_source_indexes: list[int] | None = None,
    on_title_start: OnTitleStart | None = None,
    on_title_progress: OnTitleProgress | None = None,
    on_disc_progress: OnDiscProgress | None = None,
    on_title_done: OnTitleDone | None = None,
    hash_outputs: bool = True,
    stop: asyncio.Event | None = None,
) -> RipDiscResult:
    """Rip every title with duration ≥ `minlength_seconds` from the disc
    in `device_path` to `output_dir`. Single makemkvcon invocation; the
    drive stays open for the duration of the rip.

    `eligible_source_indexes` is the list of *source* title indexes
    the dispatcher expects MakeMKV to rip — i.e. those whose duration
    is ≥ `minlength_seconds`, sorted ascending. Required for correct
    attribution because MakeMKV's `mkv all` output filenames carry an
    output-position suffix (`_t00`, `_t01`, ...) rather than the
    source title index, and the robot stream emits no per-title MSG /
    PRGT we can rely on (empirically silent on MakeMKV 1.17.5+ in
    `mkv all` mode). The file whose suffix is `_t{i:02d}` belongs to
    `eligible_source_indexes[i]`: paired by the suffix's value, not by
    the order files are found, so a title MakeMKV skipped comes back
    FAILED instead of shifting every later file onto its neighbour's
    track. Pass `None` (or `[]`) only if the caller will handle
    attribution itself; the result will have an empty `titles` dict.

    Not matched against the scan's TINFO 27 (planned output filename):
    the scan runs at `--minlength=0`, so its `_tNN` numbering differs
    from this rip's whenever `--minlength` drops a scanned title.

    `stop`, when set mid-rip, kills makemkvcon and returns STOPPED_ERROR as
    the overall error (see _run_makemkvcon).

    `hash_outputs=False` leaves `sha256` unset on every result: the
    dispatcher hashes each title itself as soon as it is written, so the
    pass overlaps the rip instead of re-reading every file after it.

    Returns a `RipDiscResult` whose `titles` dict maps source title
    index to a RipResult. The dispatcher cross-references this with
    the user's track selection: titles in the dict with ok=True got
    their files; titles absent (despite being eligible) didn't render
    and are FAILED.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "makemkvcon",
        "mkv",
        "--robot",
        "--progress=-stdout",
        f"--minlength={minlength_seconds}",
        makemkv_source_url(device_path),
        "all",
        str(output_dir),
    ]
    logger.info(
        "makemkvcon mkv all device=%s outdir=%s minlength=%ds",
        device_path,
        output_dir,
        minlength_seconds,
    )

    state = _ParserState()
    error = await _run_makemkvcon(
        cmd,
        state,
        on_title_start=on_title_start,
        on_title_progress=on_title_progress,
        on_disc_progress=on_disc_progress,
        on_title_done=on_title_done,
        eligible_source_indexes=eligible_source_indexes,
        stop=stop,
    )
    if error is not None:
        return RipDiscResult(overall_error=error, titles={})

    # Final title's on_title_done — the streamer fires done at PRGC
    # transitions, but the last title in the rip never gets a follow-up
    # PRGC:5017,N+1. Catch any started-but-not-yet-done title here so
    # the dispatcher PATCHes it DONE before rip_disc returns.
    for source_idx in sorted(state.titles):
        ts = state.titles[source_idx]
        if ts.started_emitted and not ts.done_emitted:
            await _emit_title_done(state, source_idx, on_title_done)

    # Attribute output files to source title indexes.
    #
    # MakeMKV's `mkv all` mode is opaque per-title — the stream emits
    # only overall PRGT/MSG (5014 start, 5005/5036 summary). We get
    # back to per-title outcomes from the files' `_tNN` suffixes: file
    # NN belongs to `eligible[NN]`. A suffix past the eligible list is a
    # straggler (e.g. user lowered minlength mid-disc, MakeMKV picked up
    # extras we didn't expect) and we simply don't claim it — it
    # remains on disk for inspection.
    titles_out: dict[int, RipResult] = {}
    eligible = eligible_source_indexes or []

    for position, file_path in sorted(_output_files_by_position(output_dir).items()):
        if position >= len(eligible):
            logger.warning("output file %s has no eligible title (eligible has %d)", file_path.name, len(eligible))
            continue
        source_idx = eligible[position]
        size = file_path.stat().st_size
        digest = await sha256_file(file_path) if hash_outputs else None
        titles_out[source_idx] = RipResult(
            ok=True,
            output_path=file_path,
            size_bytes=size,
            sha256=digest,
        )

    # If MSG:5003 captured a per-title failure reason and we didn't
    # produce a file for it, surface that reason via a FAILED entry. Cheap belt-and-braces — most of the time
    # state.titles is empty in `mkv all` mode.
    for title_idx, ts in state.titles.items():
        if title_idx in titles_out:
            continue
        if ts.fail_reason:
            titles_out[title_idx] = RipResult(ok=False, error=ts.fail_reason)

    # Eligible source indexes that didn't get a file *and* don't have
    # a captured failure reason are surfaced as a generic "produced no
    # .mkv" failure so the dispatcher can mark the track FAILED rather
    # than silently dropping it.
    for source_idx in eligible:
        if source_idx in titles_out:
            continue
        titles_out[source_idx] = RipResult(
            ok=False,
            error="makemkvcon exited 0 but produced no .mkv for this title",
        )

    return RipDiscResult(overall_error=None, titles=titles_out)


# Backup-then-remux lists and remuxes every source at this minimum: all its
# titles, numbered by their rank among all of them, whatever the rip's minimum.
REMUX_MIN_LENGTH_SECONDS = 0


async def remux_title(
    source_url: str,
    title_id: int,
    *,
    scratch_dir: Path,
    output_dir: Path,
    output_index: int,
    on_progress: OnDiscProgress | None = None,
) -> RipResult:
    """Write title `title_id` of `source_url` (a disc backup, an ISO or a disc
    folder) and move it to `output_dir` as `<prefix>_t{output_index:02d}.mkv`.

    One `makemkvcon --noscan --minlength=0 mkv <source> <id> <scratch_dir>`:
    `title_id` is in the source's own numbering at minlength 0 (MakeMKV
    renumbers titles per minimum), so it comes from `list_source_titles` of
    the same source. `--noscan` keeps MakeMKV from reading the drive's disc
    at start-up; the title data comes from disk.

    MakeMKV names the file `<prefix>_t<title_id>.mkv` (the prefix differs per
    title on DVD), so the one `.mkv` that lands in `scratch_dir` is the title.
    A non-zero exit, no file or more than one is a failure: a missing id
    exits 12 with no message at all, and `mkv` into a directory that doesn't
    exist exits 0 with no file (1.18.4), so `scratch_dir` is made first. No
    sha256: the dispatcher hashes each file like an `mkv all` one.
    """
    shutil.rmtree(scratch_dir, ignore_errors=True)
    scratch_dir.mkdir(parents=True)
    cmd = [
        "makemkvcon",
        "mkv",
        "--robot",
        "--progress=-stdout",
        "--noscan",
        f"--minlength={REMUX_MIN_LENGTH_SECONDS}",
        source_url,
        str(title_id),
        str(scratch_dir),
    ]
    logger.info("makemkvcon mkv source=%s title=%d", source_url, title_id)

    state = _ParserState()
    # The one save is announced as PRGC:5017 position 0; naming it as the only
    # eligible title keeps the streamer from warning about it, and with no
    # per-title callback its progress comes through the disc-level channel.
    error = await _run_makemkvcon(cmd, state, on_disc_progress=on_progress, eligible_source_indexes=[title_id])
    if error is not None:
        return RipResult(ok=False, error=error)
    produced = sorted(scratch_dir.glob("*.mkv"))
    if len(produced) != 1:
        what = "no .mkv" if not produced else f"{len(produced)} .mkv files"
        return RipResult(
            ok=False,
            error=_compose_error(f"makemkvcon exited 0 but produced {what} for title {title_id}", state.diagnostics),
        )
    # Named by the disc-scan index, like the `mkv all` files: `B1_t00.mkv`
    # from a backup whose id for disc title 3 is 0 becomes `B1_t03.mkv`.
    name = produced[0].name
    if _OUTPUT_FILE_RE.search(name):
        renamed = _OUTPUT_FILE_RE.sub(f"_t{output_index:02d}.mkv", name)
    else:
        renamed = f"{produced[0].stem}_t{output_index:02d}.mkv"
    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / renamed
    os.replace(produced[0], final)
    return RipResult(ok=True, output_path=final, size_bytes=final.stat().st_size)
