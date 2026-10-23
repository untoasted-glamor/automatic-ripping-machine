"""Dispatcher tests.

Two surfaces:

(1) `probe_drive_media` — the heartbeat/manual-trigger pre-check probe.
    Pure ioctl, no read-verify — the per-title rip loop that needed
    verify-read no longer exists.

(2) `rip_all` for DVD/BD discs — the single-invocation v2-style flow.
    Faked `rip_disc` simulates per-title outcomes (a TitleStart hook
    fires, then a per-title RipResult comes back); the dispatcher
    fans the lifecycle hooks out to the right TrackView and handles
    skipped-by-minlength + disc-level failures.

(3) `rip_all` for CDs — a fake `abcde` that reads only the tracks on its
    command line, so excluded tracks are shown never to be read.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import shutil as shutil_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import arm_ripper.drive_status as drive_status_module
import arm_ripper.rip.abcde_rip as abcde_rip_module
import arm_ripper.rip.dispatcher as dispatcher_module
import arm_ripper.rip.makemkv_rip as makemkv_rip_module
import arm_ripper.scan.makemkv as scan_makemkv_module
from arm_common import DiscType, DriveMediaStatus
from arm_common.enums import TrackKind, TrackStatus
from arm_common.schemas import ScanResult, ScanTitle, TrackView

from arm_ripper.drive_status import probe_drive_media
from arm_ripper.rip.dispatcher import rip_all
from arm_ripper.rip.makemkv_rip import RipDiscResult, RipResult
from arm_ripper.scan.disc_probe import DiscProbe
from tests._makemkvcon_fake import ACCA_DVD, HANAKO_BD, SINTEL_ISO, Recording, file_size, install


_GB = 1_000_000_000


def _set_free_space(monkeypatch, free: int) -> None:
    """`/raw`'s free space as the OS reports it (S8's preflight reads it)."""

    def disk_usage(_path: Any) -> Any:
        return SimpleNamespace(total=10 * free, used=9 * free, free=free)

    monkeypatch.setattr(shutil_module, "disk_usage", disk_usage)


@pytest.fixture(autouse=True)
def _plenty_of_space(monkeypatch):
    """A roomy `/raw`, so no test depends on the host's disk (the recorded
    Blu-ray backs up ~46 GB); S8's tests set their own."""
    _set_free_space(monkeypatch, 10_000 * _GB)


def _track(idx: int, source_ref: str | None = None, duration_seconds: int | None = 1800) -> TrackView:
    """Default duration is 30 min so the dispatcher's eligibility
    filter (`duration_seconds >= min_length_seconds`) treats it as
    rippable. Pass an explicit duration to test below-minlength
    skipping."""
    return TrackView(
        id=f"trk_{idx}",
        job_id="job_test",
        kind=TrackKind.VIDEO_TITLE,
        index=idx,
        source_ref=str(idx) if source_ref is None else source_ref,
        status=TrackStatus.QUEUED,
        output_path=None,
        size_bytes=None,
        duration_seconds=duration_seconds,
        attempts=0,
        last_error=None,
    )


# --- probe_drive_media -------------------------------------------------------


def _patch_open_close_ioctl(monkeypatch, *, open_result, ioctl_result):
    """Stub os.open/os.close + fcntl.ioctl together so a single test
    can describe one probe's behaviour. *_result accepts a value or
    an Exception."""

    def fake_open(path: str, flags: int) -> int:
        if isinstance(open_result, Exception):
            raise open_result
        return open_result

    def fake_close(fd: int) -> None:
        return None

    def fake_ioctl(fd: int, request: int, arg: int) -> int:
        if isinstance(ioctl_result, Exception):
            raise ioctl_result
        return ioctl_result

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "close", fake_close)
    monkeypatch.setattr(drive_status_module.fcntl, "ioctl", fake_ioctl)


def test_probe_returns_loaded_when_disc_ok(monkeypatch):
    _patch_open_close_ioctl(monkeypatch, open_result=11, ioctl_result=4)  # CDS_DISC_OK
    status, reason = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.LOADED
    assert "loaded" in reason


def test_probe_returns_detached_when_open_says_the_drive_is_gone(monkeypatch):
    """ENODEV/ENOENT/ENXIO mean the drive is unplugged, not misconfigured —
    the heartbeat must carry DETACHED so the backend can derive OFFLINE."""
    _patch_open_close_ioctl(
        monkeypatch,
        open_result=OSError(errno.ENODEV, "No such device"),
        ioctl_result=4,
    )
    status, reason = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.DETACHED
    assert "errno=19" in reason


def test_probe_returns_detached_when_the_node_has_no_hardware_behind_it(monkeypatch):
    _patch_open_close_ioctl(
        monkeypatch,
        open_result=OSError(errno.ENXIO, "No such device or address"),
        ioctl_result=4,
    )
    status, _ = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.DETACHED


def test_probe_returns_unavailable_when_open_is_denied(monkeypatch):
    """EACCES: the node exists but the cgroup rule / CDROM_GID is wrong.
    That is an operator misconfiguration, not a detached drive."""
    _patch_open_close_ioctl(
        monkeypatch,
        open_result=OSError(errno.EACCES, "Permission denied"),
        ioctl_result=4,
    )
    status, reason = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.UNAVAILABLE
    assert "errno=13" in reason


def test_probe_returns_not_ready_when_disc_becoming_ready(monkeypatch):
    _patch_open_close_ioctl(monkeypatch, open_result=11, ioctl_result=3)  # CDS_DRIVE_NOT_READY
    status, _ = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.NOT_READY


def test_probe_returns_tray_open(monkeypatch):
    _patch_open_close_ioctl(monkeypatch, open_result=11, ioctl_result=2)  # CDS_TRAY_OPEN
    status, _ = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.TRAY_OPEN


def test_probe_returns_no_disc(monkeypatch):
    _patch_open_close_ioctl(monkeypatch, open_result=11, ioctl_result=1)  # CDS_NO_DISC
    status, _ = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.NO_DISC


def test_probe_returns_unknown_when_ioctl_unsupported(monkeypatch):
    _patch_open_close_ioctl(
        monkeypatch,
        open_result=11,
        ioctl_result=OSError(errno.ENOTTY, "Inappropriate ioctl for device"),
    )
    status, reason = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.UNKNOWN
    assert "ioctl unsupported" in reason


def test_probe_does_not_call_read(monkeypatch):
    """Probe is ioctl-only since the single-invocation rip removed
    the need for read-verify. Any os.read attempt is a regression."""

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("os.read must not be called by the probe")

    monkeypatch.setattr(os, "open", lambda path, flags: 11)
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(os, "read", boom)
    monkeypatch.setattr(drive_status_module.fcntl, "ioctl", lambda fd, req, arg: 4)

    status, _ = probe_drive_media("/dev/sr0")
    assert status is DriveMediaStatus.LOADED


# --- rip_all (DVD/BD single-invocation) --------------------------------------


def _stub_rip_disc(
    monkeypatch,
    fake_result: RipDiscResult,
    *,
    captured: dict[str, Any] | None = None,
    fire_per_title_start: bool = True,
    fire_per_title_done: bool = False,
    fire_disc_progress: list[float] | None = None,
):
    """Replace dispatcher's rip_disc with a fake that captures kwargs
    and returns the canned RipDiscResult. Optionally invokes
    on_title_start for each title in `fake_result.titles` so the
    dispatcher's lifecycle-hook plumbing is exercised.

    `fire_per_title_start=False` simulates MakeMKV's `mkv all` mode,
    where no per-title PRGT fires — the dispatcher must still drive
    the on_track_start → on_track_done sequence post-rip for every
    track the user selected.

    `fire_per_title_done=True` invokes on_title_done for each title in
    `fake_result.titles` — simulates the streamer's PRGC:5017
    transitions that finalise each title mid-rip. With this on, the
    dispatcher PATCHes DONE per-title via the live path and skips
    those tracks in its post-rip attribution loop.

    `fire_disc_progress=[0.1, 0.5, ...]` invokes on_disc_progress with
    each fraction in order, simulating the disc-overall PRGV stream
    that's the only progress signal in `mkv all` mode."""

    async def fake_rip_disc(
        *,
        device_path: str,
        output_dir: Path,
        minlength_seconds: int,
        eligible_source_indexes: list[int] | None = None,
        on_title_start: Any | None = None,
        on_title_progress: Any | None = None,
        on_disc_progress: Any | None = None,
        on_title_done: Any | None = None,
        hash_outputs: bool = True,
        stop: Any | None = None,
    ) -> RipDiscResult:
        if captured is not None:
            captured["hash_outputs"] = hash_outputs
            captured["device_path"] = device_path
            captured["output_dir"] = output_dir
            captured["minlength_seconds"] = minlength_seconds
            captured["eligible_source_indexes"] = eligible_source_indexes
        if fire_per_title_start and on_title_start is not None:
            for idx in fake_result.titles:
                await on_title_start(idx)
        # Per-title progress can only fire when the streamer has
        # established `current_title` from a "Saving title #N" PRGT —
        # i.e. the same condition that gates `fire_per_title_start`.
        # `mkv all` mode (fire_per_title_start=False) emits no
        # per-title PRGT and therefore no per-title progress; skipping
        # it here keeps the fake's behaviour aligned with the real
        # streamer in [makemkv_rip.py].
        if fire_per_title_start and on_title_progress is not None:
            for idx in fake_result.titles:
                await on_title_progress(idx, 0.5)
                await on_title_progress(idx, 1.0)
        if fire_disc_progress is not None and on_disc_progress is not None:
            for frac in fire_disc_progress:
                await on_disc_progress(frac)
        if fire_per_title_done and on_title_done is not None:
            for idx in fake_result.titles:
                await on_title_done(idx)
        return fake_result

    monkeypatch.setattr(dispatcher_module, "rip_disc", fake_rip_disc)


async def test_rip_all_dvd_single_invocation_per_disc(monkeypatch, tmp_path):
    """The DVD/BD branch invokes rip_disc exactly once for the whole
    disc. The lifecycle hooks fire per-title from the stream callbacks."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "title_t00.mkv", size_bytes=100, sha256="a"),
            1: RipResult(ok=True, output_path=tmp_path / "title_t01.mkv", size_bytes=200, sha256="b"),
            2: RipResult(ok=True, output_path=tmp_path / "title_t02.mkv", size_bytes=300, sha256="c"),
        },
    )
    captured: dict[str, Any] = {}
    _stub_rip_disc(monkeypatch, fake_result, captured=captured)

    started: list[str] = []
    done: list[tuple[str, bool, str | None]] = []
    progress: list[tuple[str, float]] = []

    async def on_start(track: TrackView) -> None:
        started.append(track.id)

    async def on_done(track: TrackView, result: RipResult) -> None:
        done.append((track.id, result.ok, result.error))

    async def on_progress(track: TrackView, fraction: float, stats: Any = None) -> None:
        if stats is not None and stats.get("phase") == "verifying":
            return  # the post-rip hash pass; covered by its own tests
        progress.append((track.id, fraction))

    tracks = [_track(0), _track(1), _track(2)]
    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        on_track_progress=on_progress,
        min_length_seconds=600,
    )

    assert captured["device_path"] == "/dev/sr0"
    assert captured["output_dir"] == tmp_path
    assert captured["minlength_seconds"] == 600
    assert started == ["trk_0", "trk_1", "trk_2"]
    assert [t for t, ok, _ in done] == ["trk_0", "trk_1", "trk_2"]
    assert all(ok for _, ok, _ in done)
    # Two progress samples per track (0.5 then 1.0) routed through the lookup.
    assert len(progress) == 6


async def test_rip_all_marks_skipped_titles_failed(monkeypatch, tmp_path):
    """A track the user selected whose duration is below `--minlength`
    never reaches MakeMKV's output. The dispatcher must mark it FAILED
    with a clear reason rather than silently dropping it."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "vol_t00.mkv", size_bytes=100, sha256="a"),
        },
    )
    _stub_rip_disc(monkeypatch, fake_result)

    done: list[tuple[str, bool, str | None]] = []

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def on_done(track: TrackView, result: RipResult) -> None:
        done.append((track.id, result.ok, result.error))

    # Track 1 is 60s — below the 600s minlength, so the dispatcher
    # filters it from the eligible list and reports it FAILED.
    short_track = _track(1, duration_seconds=60)
    await rip_all(
        disc_type=DiscType.DVD,
        device_path="/dev/sr0",
        tracks=[_track(0), short_track],
        output_dir=tmp_path,
        on_track_start=noop,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    assert [d for d in done if d[0] == "trk_0"] == [("trk_0", True, None)]
    failed = [d for d in done if d[0] == "trk_1"][0]
    assert failed[1] is False
    assert "below minlength=600s" in failed[2]


async def test_rip_all_passes_eligible_source_indexes_to_rip_disc(monkeypatch, tmp_path):
    """The dispatcher must compute the eligible-source-indexes list
    (those whose duration meets minlength) and pass it to rip_disc
    so attribution can pair output files positionally to source
    title indexes."""
    fake_result = RipDiscResult(overall_error=None, titles={})
    captured: dict[str, Any] = {}
    _stub_rip_disc(monkeypatch, fake_result, captured=captured)

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    tracks = [
        _track(0, duration_seconds=6708),
        _track(1, duration_seconds=314),  # below 600 → skipped
        _track(2, duration_seconds=852),
        _track(3, duration_seconds=315),  # below 600 → skipped
        _track(4, duration_seconds=1800),
    ]
    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        output_dir=tmp_path,
        on_track_start=noop,
        on_track_done=noop,
        min_length_seconds=600,
    )

    assert captured["eligible_source_indexes"] == [0, 2, 4]


async def test_rip_all_propagates_disc_level_error(monkeypatch, tmp_path):
    """rip_disc reporting overall_error → every selected track FAILED
    with that error string."""
    fake_result = RipDiscResult(
        overall_error="makemkvcon failed: SCSI error",
        titles={},
    )
    _stub_rip_disc(monkeypatch, fake_result)

    done: list[tuple[str, bool, str | None]] = []

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def on_done(track: TrackView, result: RipResult) -> None:
        done.append((track.id, result.ok, result.error))

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0), _track(1)],
        output_dir=tmp_path,
        on_track_start=noop,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    assert all(d[1] is False for d in done)
    assert all("SCSI error" in (d[2] or "") for d in done)


async def test_rip_all_invalid_source_ref_fails_fast(monkeypatch, tmp_path):
    """A track whose source_ref isn't an int never reaches makemkvcon."""
    fake_result = RipDiscResult(overall_error=None, titles={})
    _stub_rip_disc(monkeypatch, fake_result)

    done: list[tuple[str, bool, str | None]] = []

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def on_done(track: TrackView, result: RipResult) -> None:
        done.append((track.id, result.ok, result.error))

    bad = _track(5, source_ref="not-an-int")
    await rip_all(
        disc_type=DiscType.DVD,
        device_path="/dev/sr0",
        tracks=[bad],
        output_dir=tmp_path,
        on_track_start=noop,
        on_track_done=on_done,
    )

    assert done == [("trk_5", False, "invalid source_ref: 'not-an-int'")]


async def test_rip_all_synthesises_start_when_prgt_misses(monkeypatch, tmp_path):
    """In `mkv all` mode MakeMKV emits a single overall "Saving all
    titles to MKV files" PRGT instead of per-title milestones, so
    `on_title_start` never fires from the stream. The dispatcher must
    synthesise `on_track_start` before each `on_track_done` so the
    backend's QUEUED → IN_PROGRESS → DONE state machine sees a legal
    transition. This is the regression that broke job_01KQX2EE...:
    the post-rip PATCH `done` got 409'd and aborted the pipeline."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "title_t00.mkv", size_bytes=100, sha256="a"),
            2: RipResult(ok=True, output_path=tmp_path / "title_t02.mkv", size_bytes=200, sha256="b"),
        },
    )
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=False)

    events: list[tuple[str, str]] = []

    async def on_start(track: TrackView) -> None:
        events.append(("start", track.id))

    async def on_done(track: TrackView, result: RipResult) -> None:
        events.append(("done", track.id))

    # 8 selected tracks, only indexes 0 and 2 ripped (rest skipped by minlength).
    tracks = [_track(i) for i in range(8)]
    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    # Every track must see a `start` followed by a `done` in that order.
    by_track: dict[str, list[str]] = {}
    for kind, tid in events:
        by_track.setdefault(tid, []).append(kind)
    assert len(by_track) == 8
    for tid, seq in by_track.items():
        assert seq == ["start", "done"], f"track {tid} had bad lifecycle: {seq}"


async def test_rip_all_disc_progress_attributes_to_first_eligible_track(monkeypatch, tmp_path):
    """`mkv all` emits no per-title PRGT, so `on_title_progress` never
    fires from the stream. The dispatcher must surface PRGV's
    `total/max` (disc-overall) channel via `on_disc_progress`,
    attributed to the first eligible track. The UI's rips store keys
    live progress by job_id and renders one bar per disc, so the
    track_id chosen is purely a wire-format detail — what matters is
    that the bar advances 0 → 100 % across the rip."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "title_t00.mkv", size_bytes=100, sha256="a"),
            2: RipResult(ok=True, output_path=tmp_path / "title_t01.mkv", size_bytes=200, sha256="b"),
        },
    )
    _stub_rip_disc(
        monkeypatch,
        fake_result,
        fire_per_title_start=False,
        fire_disc_progress=[0.05, 0.4, 0.7, 1.0],
    )

    progress: list[tuple[str, float]] = []

    async def on_progress(track: TrackView, fraction: float, stats: Any = None) -> None:
        if stats is not None and stats.get("phase") == "verifying":
            return  # the post-rip hash pass; covered by its own tests
        progress.append((track.id, fraction))

    async def noop_start(_track: TrackView) -> None:
        return None

    async def noop_done(_track: TrackView, _result: RipResult) -> None:
        return None

    # 4 selected tracks; first eligible is track index 0 (`trk_0`).
    tracks = [_track(i) for i in range(4)]
    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        output_dir=tmp_path,
        on_track_start=noop_start,
        on_track_done=noop_done,
        on_track_progress=on_progress,
        min_length_seconds=600,
    )

    # Every disc-progress sample lands on the first eligible track,
    # in order, with the supplied fractions intact.
    assert progress == [("trk_0", 0.05), ("trk_0", 0.4), ("trk_0", 0.7), ("trk_0", 1.0)]


async def test_rip_all_disc_progress_no_op_when_no_progress_callback(monkeypatch, tmp_path):
    """If the caller doesn't wire `on_track_progress`, the dispatcher's
    disc-progress fallback must be a silent no-op rather than crashing
    on a None call."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "title_t00.mkv", size_bytes=100, sha256="a"),
        },
    )
    _stub_rip_disc(
        monkeypatch,
        fake_result,
        fire_per_title_start=False,
        fire_disc_progress=[0.5, 1.0],
    )

    async def noop_start(_track: TrackView) -> None:
        return None

    async def noop_done(_track: TrackView, _result: RipResult) -> None:
        return None

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0)],
        output_dir=tmp_path,
        on_track_start=noop_start,
        on_track_done=noop_done,
        on_track_progress=None,
        min_length_seconds=600,
    )
    # No assertions on progress because no callback was wired; reaching
    # here without raising is the success criterion.


async def test_rip_all_disc_error_path_drives_full_lifecycle(monkeypatch, tmp_path):
    """When rip_disc reports an overall_error every selected track must
    be FAILED — and each must transit IN_PROGRESS first per the
    backend state machine."""
    fake_result = RipDiscResult(overall_error="makemkvcon failed: SCSI error", titles={})
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=False)

    events: list[tuple[str, str]] = []

    async def on_start(track: TrackView) -> None:
        events.append(("start", track.id))

    async def on_done(track: TrackView, result: RipResult) -> None:
        events.append(("done", track.id))

    tracks = [_track(0), _track(1), _track(2)]
    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    by_track: dict[str, list[str]] = {}
    for kind, tid in events:
        by_track.setdefault(tid, []).append(kind)
    for tid in ("trk_0", "trk_1", "trk_2"):
        assert by_track[tid] == ["start", "done"]


async def test_rip_all_does_not_double_start_when_prgt_fires(monkeypatch, tmp_path):
    """If the per-title PRGT *did* fire and called on_track_start, the
    post-rip ensure-started shim must not call it again. Prevents a
    409 on the second IN_PROGRESS PATCH."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={0: RipResult(ok=True, output_path=tmp_path / "t.mkv", size_bytes=10, sha256="a")},
    )
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=True)

    starts = 0

    async def on_start(track: TrackView) -> None:
        nonlocal starts
        starts += 1

    async def on_done(track: TrackView, result: RipResult) -> None:
        pass

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0)],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    assert starts == 1


async def test_rip_all_carries_duration_from_track_view(monkeypatch, tmp_path):
    """rip_disc has no scan-side duration info; the dispatcher must
    forward TrackView.duration_seconds onto the success result so the
    backend track row gets populated."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "t.mkv", size_bytes=100, sha256="a"),
        },
    )
    _stub_rip_disc(monkeypatch, fake_result)

    seen_duration: list[int | None] = []

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def on_done(track: TrackView, result: RipResult) -> None:
        seen_duration.append(result.duration_seconds)

    track = TrackView(
        id="trk_0",
        job_id="j",
        kind=TrackKind.VIDEO_TITLE,
        index=0,
        source_ref="0",
        status=TrackStatus.QUEUED,
        output_path=None,
        size_bytes=None,
        duration_seconds=7321,
        attempts=0,
        last_error=None,
    )
    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[track],
        output_dir=tmp_path,
        on_track_start=noop,
        on_track_done=on_done,
    )

    assert seen_duration == [7321]


async def test_rip_all_falls_back_to_expected_duration(monkeypatch, tmp_path):
    """At rip-start, `track.duration_seconds` is null (it's the post-rip
    actual). `track.expected_duration_seconds` carries the scan-time
    estimate. The dispatcher must fall back to the latter so the
    backend's PATCH-DONE row gets a non-null duration. (This was the
    ripped-but-null bug surfaced by job_01KQXA9JM3RX2SV8SB7S4CP6NX.)"""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "t.mkv", size_bytes=100, sha256="a"),
        },
    )
    _stub_rip_disc(monkeypatch, fake_result)

    seen_duration: list[int | None] = []

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def on_done(track: TrackView, result: RipResult) -> None:
        seen_duration.append(result.duration_seconds)

    track = TrackView(
        id="trk_0",
        job_id="j",
        kind=TrackKind.VIDEO_TITLE,
        index=0,
        source_ref="0",
        status=TrackStatus.QUEUED,
        output_path=None,
        size_bytes=None,
        duration_seconds=None,
        expected_duration_seconds=6708,
        attempts=0,
        last_error=None,
    )
    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[track],
        output_dir=tmp_path,
        on_track_start=noop,
        on_track_done=on_done,
    )

    assert seen_duration == [6708]


async def test_rip_all_live_per_title_done_patches_via_streamer_callback(monkeypatch, tmp_path):
    """When the streamer fires on_title_done mid-rip (the new path),
    the dispatcher's `_on_title_done` stats the file and PATCHes the
    track DONE before rip_disc returns. The post-rip attribution loop
    must then skip those tracks rather than emitting a duplicate
    on_track_done."""
    # Files MakeMKV would have produced — positional `_tNN.mkv` suffix
    # matches eligible_source_indexes[N].
    (tmp_path / "Disc_t00.mkv").write_bytes(b"x" * 100)
    (tmp_path / "Disc_t01.mkv").write_bytes(b"y" * 200)

    fake_result = RipDiscResult(overall_error=None, titles={})
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=True, fire_per_title_done=True)

    events: list[tuple[str, str, int | None]] = []

    async def on_start(track: TrackView) -> None:
        events.append(("start", track.id, None))

    async def on_done(track: TrackView, result: RipResult) -> None:
        events.append(("done", track.id, result.size_bytes))

    # The fake fires on_title_done with the source idx (which our stub
    # passes from `fake_result.titles` keys). Seed those so the fake
    # has something to iterate.
    fake_result.titles[0] = RipResult(ok=True, output_path=None, size_bytes=None)
    fake_result.titles[1] = RipResult(ok=True, output_path=None, size_bytes=None)

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0), _track(1)],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    # Each track exactly one start + one done — no duplicate from the
    # post-rip attribution loop. Sizes come from the on-disk file
    # (stat'd by `_on_title_done`), not from `fake_result.titles`.
    by_track: dict[str, list[tuple[str, int | None]]] = {}
    for kind, tid, size in events:
        by_track.setdefault(tid, []).append((kind, size))
    assert by_track["trk_0"] == [("start", None), ("done", 100)]
    assert by_track["trk_1"] == [("start", None), ("done", 200)]


async def test_rip_all_disc_error_keeps_already_done_tracks_done(monkeypatch, tmp_path):
    """If the rip fails partway, tracks that already finalised via the
    live path keep their DONE status — partial success is still
    success for those files. Only the unfinished tracks transition to
    FAILED with the disc-level reason."""
    (tmp_path / "Disc_t00.mkv").write_bytes(b"x" * 100)

    # rip_disc failure path — but on_title_done was already called for
    # source idx 0 before the disc-level error fired.
    fake_result = RipDiscResult(
        overall_error="makemkvcon failed mid-rip",
        titles={0: RipResult(ok=True, output_path=None, size_bytes=None)},
    )
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=True, fire_per_title_done=True)

    events: list[tuple[str, str, str | None]] = []

    async def on_start(track: TrackView) -> None:
        events.append(("start", track.id, None))

    async def on_done(track: TrackView, result: RipResult) -> None:
        events.append(("done", track.id, result.error if not result.ok else None))

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0), _track(1)],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    # Track 0 was finalised via the live path and stays DONE.
    # Track 1 hadn't finalised; it gets the disc-level FAILED reason.
    assert ("done", "trk_0", None) in events
    failures = [e for e in events if e[0] == "done" and e[1] == "trk_1"]
    assert len(failures) == 1
    assert failures[0][2] == "makemkvcon failed mid-rip"


async def test_rip_all_live_done_handles_unselected_titles_gracefully(monkeypatch, tmp_path):
    """If MakeMKV's stream announces a title that isn't in the user's
    track selection (rare but possible if `mkv all` picks up extras),
    the dispatcher's `_on_title_done` no-ops cleanly without
    crashing or PATCHing a non-existent track."""
    fake_result = RipDiscResult(
        overall_error=None,
        titles={0: RipResult(ok=True), 5: RipResult(ok=True)},  # 5 not in track list
    )
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=True, fire_per_title_done=True)
    (tmp_path / "Disc_t00.mkv").write_bytes(b"x" * 50)

    events: list[str] = []

    async def on_start(track: TrackView) -> None:
        events.append(f"start-{track.id}")

    async def on_done(track: TrackView, result: RipResult) -> None:
        events.append(f"done-{track.id}")

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0)],  # only track 0 selected
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
    )

    # Track 0 went start → done. Title 5 was unselected; no events for it.
    assert events == ["start-trk_0", "done-trk_0"]


async def test_rip_all_dump_track_uses_data_rip_even_on_a_bluray_scan(monkeypatch, tmp_path):
    """The backend routes an ISO image MakeMKV could not open to the full-disc
    dump session: its single DATA_DUMP track must go through rip_data, never
    back into makemkvcon, even though the scan still says bluray."""
    data_calls: list[str] = []

    async def fake_rip_data(*, device_path, output_dir, on_progress=None):
        data_calls.append(device_path)
        if on_progress is not None:
            await on_progress(512, 1024)
        return RipResult(ok=True, output_path=output_dir / "dump.iso", size_bytes=1, sha256="x")

    async def fake_rip_disc(**_kwargs):
        raise AssertionError("makemkvcon must not be invoked for a dump track")

    monkeypatch.setattr(dispatcher_module, "rip_data", fake_rip_data)
    monkeypatch.setattr(dispatcher_module, "rip_disc", fake_rip_disc)
    dump = _track(0, source_ref="full")
    dump = dump.model_copy(update={"kind": TrackKind.DATA_DUMP})
    done: list[tuple[str, bool]] = []

    async def on_start(_t):
        pass

    async def on_done(t, result):
        done.append((t.id, result.ok))

    await dispatcher_module.rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/source/movie.iso",
        tracks=[dump],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
    )
    assert data_calls == ["/source/movie.iso"]
    assert done == [("trk_0", True)]


async def test_rip_all_dump_progress_carries_transfer_stats(monkeypatch, tmp_path):
    """The full-disc copy reports a fraction plus bytes done, total and rate."""

    async def fake_rip_data(*, device_path, output_dir, on_progress=None):
        await on_progress(256, 1024)
        await on_progress(1024, 1024)
        return RipResult(ok=True, output_path=output_dir / "dump.iso", size_bytes=1024, sha256="x")

    monkeypatch.setattr(dispatcher_module, "rip_data", fake_rip_data)
    dump = _track(0, source_ref="full").model_copy(update={"kind": TrackKind.DATA_DUMP})
    ticks: list[tuple[float, dict]] = []

    async def on_progress(_t, fraction, stats=None):
        ticks.append((fraction, stats))

    async def _noop(*_a):
        pass

    await dispatcher_module.rip_all(
        disc_type=DiscType.DATA,
        device_path="/source/movie.iso",
        tracks=[dump],
        output_dir=tmp_path,
        on_track_start=_noop,
        on_track_done=_noop,
        on_track_progress=on_progress,
    )
    assert [round(f, 2) for f, _s in ticks] == [0.25, 1.0]
    assert ticks[0][1]["bytes_done"] == 256 and ticks[0][1]["bytes_total"] == 1024
    assert ticks[-1][1]["rate_bps"] is None or ticks[-1][1]["rate_bps"] > 0


async def test_rip_all_disc_backup_track_uses_rip_backup(monkeypatch, tmp_path):
    backup_calls: list[tuple[str, Path, bool]] = []

    async def fake_rip_backup(source, target, *, decrypt, on_progress=None):
        backup_calls.append((source, target, decrypt))
        if on_progress is not None:
            await on_progress(0.5)
        return RipResult(ok=True, output_path=target, size_bytes=7)

    async def must_not_run(**_kwargs):
        raise AssertionError("a disc backup must not reach rip_disc or rip_data")

    monkeypatch.setattr(dispatcher_module, "rip_backup", fake_rip_backup)
    install(monkeypatch, disc=HANAKO_BD)
    monkeypatch.setattr(dispatcher_module, "rip_disc", must_not_run)
    monkeypatch.setattr(dispatcher_module, "rip_data", must_not_run)
    backup = _track(0, source_ref="full").model_copy(update={"kind": TrackKind.DISC_BACKUP})
    events: list[tuple[str, Any]] = []

    async def on_start(t):
        events.append(("start", t.id))

    async def on_progress(t, fraction, stats=None):
        events.append(("progress", (t.id, fraction, stats)))

    async def on_done(t, result):
        events.append(("done", (t.id, result.ok, result.size_bytes)))

    await dispatcher_module.rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[backup],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        on_track_progress=on_progress,
    )
    assert backup_calls == [("disc:0", tmp_path / "backup", True)]
    assert events == [
        ("start", "trk_0"),
        ("progress", ("trk_0", 0.5, None)),
        ("done", ("trk_0", True, 7)),
    ]


async def test_a_disc_backup_track_fails_when_makemkv_does_not_list_the_drive(monkeypatch, tmp_path):
    install(monkeypatch, disc=HANAKO_BD, device="/dev/sr3")
    backup = _track(0, source_ref="full").model_copy(update={"kind": TrackKind.DISC_BACKUP})

    reports, started = await _rip_tracks([backup], tmp_path / "job")

    (result,) = reports[backup.id]
    assert started == [backup.id]
    assert result.ok is False
    assert result.error == "makemkvcon lists no drive at /dev/sr0, so it can't back the disc up"
    assert not (tmp_path / "job" / "backup").exists()


async def test_rip_all_hashes_each_title_and_patches_done_with_its_sha256(monkeypatch, tmp_path):
    """D-125: the makemkv path hashes each title in the background as soon
    as it is written (rip_disc is told not to re-read everything after the
    rip) and PATCHes DONE with the digest, live titles and post-rip ones
    alike. Whatever is still hashing after the rip is reported as a
    "verifying" phase on the progress channel."""
    import hashlib

    (tmp_path / "title_t00.mkv").write_bytes(b"main feature")
    (tmp_path / "title_t01.mkv").write_bytes(b"extra")
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "title_t00.mkv", size_bytes=12),
            1: RipResult(ok=True, output_path=tmp_path / "title_t01.mkv", size_bytes=5),
        },
    )
    captured: dict[str, Any] = {}
    _stub_rip_disc(monkeypatch, fake_result, captured=captured, fire_per_title_start=False)

    # Title 0 finalises live (mid-rip); title 1 is attributed post-rip.
    real_stub = dispatcher_module.rip_disc

    async def live_then_post(**kwargs: Any) -> RipDiscResult:
        await kwargs["on_title_done"](0)
        return await real_stub(**kwargs)

    monkeypatch.setattr(dispatcher_module, "rip_disc", live_then_post)

    done: dict[str, str | None] = {}
    phases: list[str] = []

    async def noop_start(_track: TrackView) -> None:
        return None

    async def on_done(track: TrackView, result: RipResult) -> None:
        assert result.ok
        done[track.id] = result.sha256

    async def on_progress(_track: TrackView, _fraction: float, stats: Any = None) -> None:
        if stats is not None and "phase" in stats:
            phases.append(stats["phase"])

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0), _track(1)],
        output_dir=tmp_path,
        on_track_start=noop_start,
        on_track_done=on_done,
        on_track_progress=on_progress,
        min_length_seconds=600,
    )

    assert captured["hash_outputs"] is False
    assert done == {
        "trk_0": hashlib.sha256(b"main feature").hexdigest(),
        "trk_1": hashlib.sha256(b"extra").hexdigest(),
    }
    assert set(phases) <= {"verifying"}


async def test_title_hasher_reports_verify_progress_until_done(tmp_path):
    from arm_ripper.rip import title_hasher

    path = tmp_path / "t.mkv"
    path.write_bytes(b"x" * 10)
    gate = asyncio.Event()
    real = title_hasher.sha256_file

    async def slow(p, on_bytes=None):  # type: ignore[no-untyped-def]
        await gate.wait()
        return await real(p, on_bytes)

    title_hasher.sha256_file = slow  # type: ignore[assignment]
    try:
        hasher = title_hasher.TitleHasher()
        got: list[str | None] = []

        async def then(digest: str | None) -> None:
            got.append(digest)

        hasher.submit(path, 10, then)
        seen: list[tuple[int, int]] = []

        async def on_progress(done: int, total: int, _rate: float | None) -> None:
            seen.append((done, total))
            gate.set()

        await hasher.finish(on_progress)
    finally:
        title_hasher.sha256_file = real  # type: ignore[assignment]
    assert seen and seen[0] == (0, 10)
    assert got and got[0] is not None


async def test_title_hasher_passes_none_for_an_unreadable_file(tmp_path):
    from arm_ripper.rip.title_hasher import TitleHasher

    hasher = TitleHasher()
    got: list[str | None] = []

    async def then(digest: str | None) -> None:
        got.append(digest)

    hasher.submit(tmp_path / "missing.mkv", 1, then)
    await hasher.finish()
    assert got == [None]


async def test_rip_all_resume_pass_skips_titles_an_earlier_pass_settled(monkeypatch, tmp_path):
    """D-133: a resume after the drive dropped off the bus runs one more
    `mkv all`, which rips every title again. Titles already settled get no
    callbacks and their duplicate files are deleted; the rest attribute by
    position exactly as in a first pass."""
    for pos in range(3):
        (tmp_path / f"Disc_t{pos:02d}.mkv").write_bytes(b"x" * (10 + pos))
    fake_result = RipDiscResult(
        overall_error=None,
        titles={
            0: RipResult(ok=True, output_path=tmp_path / "Disc_t00.mkv", size_bytes=10),
            1: RipResult(ok=True, output_path=tmp_path / "Disc_t01.mkv", size_bytes=11),
            2: RipResult(ok=True, output_path=tmp_path / "Disc_t02.mkv", size_bytes=12),
        },
    )
    captured: dict[str, Any] = {}
    stop = asyncio.Event()
    _stub_rip_disc(monkeypatch, fake_result, captured=captured, fire_per_title_start=True, fire_per_title_done=True)

    events: list[tuple[str, str]] = []

    async def on_start(track: TrackView) -> None:
        events.append(("start", track.id))

    async def on_done(track: TrackView, result: RipResult) -> None:
        assert result.ok
        events.append(("done", track.id))

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr1",
        tracks=[_track(0), _track(1), _track(2)],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
        stop=stop,
        skip_track_ids={"trk_0"},
    )

    # Positions still line up with every eligible title, skipped or not.
    assert captured["eligible_source_indexes"] == [0, 1, 2]
    assert {e[1] for e in events} == {"trk_1", "trk_2"}
    assert sorted(f.name for f in tmp_path.glob("*.mkv")) == ["Disc_t01.mkv", "Disc_t02.mkv"]


async def test_rip_all_stopped_pass_reports_the_rest_without_touching_settled_tracks(monkeypatch, tmp_path):
    """A pass stopped by the lost drive reports its unfinished tracks with
    the stop error (the controller holds them for the resume), never the
    ones an earlier pass settled."""
    from arm_ripper.rip.makemkv_rip import STOPPED_ERROR

    _stub_rip_disc(monkeypatch, RipDiscResult(overall_error=STOPPED_ERROR, titles={}), fire_per_title_start=False)
    done: list[tuple[str, str | None]] = []

    async def on_start(track: TrackView) -> None:
        pass

    async def on_done(track: TrackView, result: RipResult) -> None:
        done.append((track.id, result.error))

    await rip_all(
        disc_type=DiscType.DVD,
        device_path="/dev/sr0",
        tracks=[_track(0), _track(1), _track(2, source_ref="bad")],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
        skip_track_ids={"trk_0", "trk_2"},
    )
    assert done == [("trk_1", STOPPED_ERROR)]


async def test_rip_all_does_not_finalise_a_title_the_drive_dropped_under(monkeypatch, tmp_path):
    """D-133: makemkvcon moves on from the title the drive dropped under (a
    PRGC transition) before the poll loop notices. That title's file is cut
    short, so it and the rest are reported stopped, never DONE, and `stop` is
    set to kill makemkvcon."""
    from arm_ripper.rip.makemkv_rip import STOPPED_ERROR

    for pos in range(3):
        (tmp_path / f"Disc_t{pos:02d}.mkv").write_bytes(b"x" * (10 + pos))
    fake_result = RipDiscResult(
        overall_error=None,
        titles={idx: RipResult(ok=True, output_path=tmp_path / f"Disc_t{idx:02d}.mkv") for idx in range(3)},
    )
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=True, fire_per_title_done=True)
    asked: list[int] = []

    def drive_lost() -> bool:
        # Still there when title 0 finishes; gone by the time title 1 does.
        asked.append(1)
        return len(asked) > 1

    stop = asyncio.Event()
    done: list[tuple[str, bool, str | None]] = []

    async def on_start(track: TrackView) -> None:
        pass

    async def on_done(track: TrackView, result: RipResult) -> None:
        done.append((track.id, result.ok, result.error))

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=[_track(0), _track(1), _track(2)],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
        stop=stop,
        drive_lost=drive_lost,
    )

    assert stop.is_set()
    assert sorted(done) == [("trk_0", True, None), ("trk_1", False, STOPPED_ERROR), ("trk_2", False, STOPPED_ERROR)]


async def test_rip_all_with_the_drive_present_finalises_every_title(monkeypatch, tmp_path):
    for pos in range(2):
        (tmp_path / f"Disc_t{pos:02d}.mkv").write_bytes(b"x" * (10 + pos))
    fake_result = RipDiscResult(
        overall_error=None,
        titles={idx: RipResult(ok=True, output_path=tmp_path / f"Disc_t{idx:02d}.mkv") for idx in range(2)},
    )
    _stub_rip_disc(monkeypatch, fake_result, fire_per_title_start=True, fire_per_title_done=True)
    stop = asyncio.Event()
    done: list[tuple[str, bool]] = []

    async def on_start(track: TrackView) -> None:
        pass

    async def on_done(track: TrackView, result: RipResult) -> None:
        done.append((track.id, result.ok))

    await rip_all(
        disc_type=DiscType.DVD,
        device_path="/dev/sr0",
        tracks=[_track(0), _track(1)],
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=600,
        stop=stop,
        drive_lost=lambda: False,
    )
    assert not stop.is_set()
    assert sorted(done) == [("trk_0", True), ("trk_1", True)]


# --- rip_all against a faked makemkvcon process -------------------------------
#
# These go through the real rip_disc and fake only the process boundary:
# `asyncio.create_subprocess_exec` returns a process that streams robot
# lines and writes `.mkv` files into the outdir it was handed, at the point
# in the stream where MakeMKV would. Assertions are on what each track was
# finally reported as (the PATCH the backend would receive).


class _ScriptedStdout:
    """Replays a script of robot lines; a `("write", NN)` step creates
    `<label>_tNN.mkv` in the outdir (size 1000 + NN, so a file's size names
    its title) at that point in the stream."""

    def __init__(self, script: list[str | tuple[str, int]], output_dir: Path) -> None:
        self._script = list(script)
        self._output_dir = output_dir

    async def readline(self) -> bytes:
        while self._script:
            step = self._script.pop(0)
            if isinstance(step, tuple):
                _, nn = step
                (self._output_dir / f"ACCA_CS_D2_t{nn:02d}.mkv").write_bytes(b"x" * (1000 + nn))
                continue
            await asyncio.sleep(0)
            return (step + "\n").encode()
        return b""


class _ScriptedMakemkvcon:
    def __init__(self, script: list[str | tuple[str, int]], output_dir: Path) -> None:
        self.stdout = _ScriptedStdout(script, output_dir)
        self.stderr = _ScriptedStdout([], output_dir)
        self.returncode: int | None = None

    async def wait(self) -> int:
        # Exit only once stdout is drained, like the real process.
        while self.stdout._script:
            await asyncio.sleep(0)
        self.returncode = 0
        return 0

    def kill(self) -> None:  # pragma: no cover - the scripted rips always exit
        self.returncode = -9


def _fake_makemkvcon(monkeypatch, script: list[str | tuple[str, int]]) -> list[list[str]]:
    """Every invocation replays `script`; returns each invocation's argv."""
    argvs: list[list[str]] = []

    async def create_subprocess_exec(*argv: str, **_kwargs: Any) -> _ScriptedMakemkvcon:
        argvs.append(list(argv))
        return _ScriptedMakemkvcon(script, Path(argv[-1]))

    monkeypatch.setattr(makemkv_rip_module.asyncio, "create_subprocess_exec", create_subprocess_exec)
    return argvs


def _mkv_all_script(saved: list[int], prgc_positions: list[int]) -> list[str | tuple[str, int]]:
    """A `mkv all` save phase: one PRGC:5017 per saved title, the title's
    file written while it saves, then MakeMKV's summary."""
    script: list[str | tuple[str, int]] = [
        'MSG:1005,0,1,"MakeMKV v1.18.3 linux(x64-release) started","%1 started","MakeMKV v1.18.3"',
        'PRGT:5024,0,"Saving all titles to MKV files"',
    ]
    for nn, pos in zip(saved, prgc_positions, strict=True):
        script += [f'PRGC:5017,{pos},"Saving to MKV file"', "PRGV:0,0,65536", ("write", nn), "PRGV:65536,0,65536"]
    script.append(
        f'MSG:5037,0,2,"Copy complete. {len(saved)} titles saved, 2 failed.",'
        f'"Copy complete. %1 titles saved, %2 failed.","{len(saved)}","2"'
    )
    return script


async def _rip_tracks(
    tracks: list[TrackView],
    tmp_path: Path,
    *,
    disc_type: DiscType = DiscType.BLURAY,
    device: str = "/dev/sr0",
    min_length_seconds: int = 120,
    **kwargs: Any,
) -> tuple[dict[str, list[RipResult]], list[str]]:
    """Run rip_all (a BD in /dev/sr0 unless told otherwise); return every
    terminal report per track id and the order of start reports."""
    reports: dict[str, list[RipResult]] = {t.id: [] for t in tracks}
    started: list[str] = []

    async def on_start(track: TrackView) -> None:
        started.append(track.id)

    async def on_done(track: TrackView, result: RipResult) -> None:
        reports[track.id].append(result)

    await rip_all(
        disc_type=disc_type,
        device_path=device,
        tracks=tracks,
        output_dir=tmp_path,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=min_length_seconds,
        **kwargs,
    )
    return reports, started


# MakeMKV skipped titles 3 and 4 of 12 (job_01M55M08QRCHJMD2QVV8D3H17G).
# Whether PRGC:5017 then names the title's own position (...2, 5, 6...) or
# keeps counting saves (...2, 3, 4...) isn't recorded, so both are covered.
_INCIDENT_SAVED = [0, 1, 2, 5, 6, 7, 8, 9, 10, 11]


@pytest.mark.parametrize(
    "prgc_positions",
    [_INCIDENT_SAVED, list(range(len(_INCIDENT_SAVED)))],
    ids=["prgc-names-title", "prgc-counts-saves"],
)
async def test_rip_all_skipped_titles_dont_shift_files_onto_neighbours(monkeypatch, tmp_path, prgc_positions):
    """12 eligible titles; makemkvcon writes `_t00`-`_t02` and `_t05`-`_t11`
    only. Tracks 3 and 4 are FAILED; every other track is DONE with the file
    bearing its own number, including 10 and 11, which order-pairing marked
    FAILED after crediting their files to tracks 8 and 9."""
    argvs = _fake_makemkvcon(monkeypatch, _mkv_all_script(_INCIDENT_SAVED, prgc_positions))
    tracks = [_track(i) for i in range(12)]

    reports, started = await _rip_tracks(tracks, tmp_path)

    # One makemkvcon for the whole disc.
    assert len(argvs) == 1 and argvs[0][-2] == "all"
    # The backend accepts one start and one terminal PATCH per track.
    assert sorted(started) == sorted(t.id for t in tracks)
    assert all(len(r) == 1 for r in reports.values()), reports
    final = {track_id: r[0] for track_id, r in reports.items()}
    for idx in (3, 4):
        assert final[f"trk_{idx}"].ok is False
        assert "no .mkv" in (final[f"trk_{idx}"].error or "")
    for idx in _INCIDENT_SAVED:
        result = final[f"trk_{idx}"]
        assert result.ok is True, (idx, result.error)
        assert result.output_path == tmp_path / f"ACCA_CS_D2_t{idx:02d}.mkv"
        assert result.size_bytes == 1000 + idx


async def test_rip_all_every_file_written_gives_each_track_its_own(monkeypatch, tmp_path):
    _fake_makemkvcon(monkeypatch, _mkv_all_script(list(range(4)), list(range(4))))

    reports, _ = await _rip_tracks([_track(i) for i in range(4)], tmp_path)

    for idx in range(4):
        (result,) = reports[f"trk_{idx}"]
        assert result.ok is True
        assert result.output_path == tmp_path / f"ACCA_CS_D2_t{idx:02d}.mkv"
        assert result.size_bytes == 1000 + idx


async def test_rip_all_short_title_between_eligible_ones(monkeypatch, tmp_path):
    """Scan titles 0, 1, 2 with 1 below minlength: makemkvcon (run with that
    minlength) numbers its output by eligible position, so `_t00` is source 0
    and `_t01` is source 2; source 1 is FAILED as below minlength."""
    _fake_makemkvcon(monkeypatch, _mkv_all_script([0, 1], [0, 1]))

    reports, _ = await _rip_tracks([_track(0), _track(1, duration_seconds=60), _track(2)], tmp_path)

    (t0,) = reports["trk_0"]
    (t1,) = reports["trk_1"]
    (t2,) = reports["trk_2"]
    assert t0.ok is True and t0.output_path == tmp_path / "ACCA_CS_D2_t00.mkv"
    assert t2.ok is True and t2.output_path == tmp_path / "ACCA_CS_D2_t01.mkv"
    assert t1.ok is False and "below minlength" in (t1.error or "")


async def test_rip_all_resume_pass_with_a_skipped_title_keeps_files_on_their_own_tracks(monkeypatch, tmp_path):
    """D-133 resume: the second `mkv all` rips every title again. Titles 0
    and 1 were settled by the first pass (no callbacks, their new files are
    deleted); MakeMKV skips title 3 this time. Track 2 and 4 get their own
    files and track 3 is FAILED, not handed title 4's file."""
    _fake_makemkvcon(monkeypatch, _mkv_all_script([0, 1, 2, 4], [0, 1, 2, 4]))
    tracks = [_track(i) for i in range(5)]

    reports, started = await _rip_tracks(
        tracks, tmp_path, stop=asyncio.Event(), skip_track_ids={"trk_0", "trk_1"}, drive_lost=lambda: False
    )

    assert reports["trk_0"] == [] and reports["trk_1"] == []
    assert "trk_0" not in started and "trk_1" not in started
    (t2,) = reports["trk_2"]
    (t3,) = reports["trk_3"]
    (t4,) = reports["trk_4"]
    assert t2.ok is True and t2.output_path == tmp_path / "ACCA_CS_D2_t02.mkv"
    assert t4.ok is True and t4.output_path == tmp_path / "ACCA_CS_D2_t04.mkv" and t4.size_bytes == 1004
    assert t3.ok is False and "no .mkv" in (t3.error or "")
    assert sorted(f.name for f in tmp_path.glob("*.mkv")) == ["ACCA_CS_D2_t02.mkv", "ACCA_CS_D2_t04.mkv"]


async def test_rip_all_leaves_a_file_past_the_eligible_list_unclaimed(monkeypatch, tmp_path, caplog):
    """A `_tNN` beyond the eligible list (MakeMKV ripped a title we didn't
    expect) is claimed by no track and stays on disk."""
    _fake_makemkvcon(monkeypatch, _mkv_all_script([0, 1], [0, 1]))
    caplog.set_level(logging.WARNING, logger="arm_ripper.rip.makemkv")

    reports, _ = await _rip_tracks([_track(0)], tmp_path)

    (t0,) = reports["trk_0"]
    assert t0.ok is True and t0.output_path == tmp_path / "ACCA_CS_D2_t00.mkv"
    assert (tmp_path / "ACCA_CS_D2_t01.mkv").exists()
    assert any("ACCA_CS_D2_t01.mkv has no eligible title" in r.getMessage() for r in caplog.records)


async def test_rip_all_two_files_with_one_suffix_credit_the_first_by_name(monkeypatch, tmp_path, caplog):
    """A stray from an earlier run under another label shares `_t00` with
    this rip's file: one of them is claimed, the other is left alone."""
    (tmp_path / "ZZZ_OLD_t00.mkv").write_bytes(b"stale")
    _fake_makemkvcon(monkeypatch, _mkv_all_script([0], [0]))
    caplog.set_level(logging.WARNING, logger="arm_ripper.rip.makemkv")

    reports, _ = await _rip_tracks([_track(0)], tmp_path)

    (t0,) = reports["trk_0"]
    assert t0.ok is True and t0.output_path == tmp_path / "ACCA_CS_D2_t00.mkv"
    assert any("two output files claim position 0" in r.getMessage() for r in caplog.records)


async def test_rip_all_logs_makemkv_messages_at_info(monkeypatch, tmp_path, caplog):
    """MakeMKV's MSG lines are the only record of why a title was skipped,
    so they reach the job log at INFO, not just the DEBUG raw stream."""
    script = _mkv_all_script([0], [0])
    script.insert(2, 'MSG:5010,0,0,"Failed to open disc title 1","Failed to open disc title %1","1"')
    _fake_makemkvcon(monkeypatch, script)
    caplog.set_level(logging.INFO)

    await _rip_tracks([_track(0)], tmp_path)

    info_text = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("Failed to open disc title 1" in m for m in info_text)


def _scanned_track(idx: int, expected_duration_seconds: int | None) -> TrackView:
    """A track as rip-start sends it: no measured duration yet, only the
    scan's estimate."""
    return TrackView(
        id=f"trk_{idx}",
        job_id="job_test",
        kind=TrackKind.VIDEO_TITLE,
        index=idx,
        source_ref=str(idx),
        status=TrackStatus.QUEUED,
        output_path=None,
        size_bytes=None,
        duration_seconds=None,
        expected_duration_seconds=expected_duration_seconds,
        attempts=0,
        last_error=None,
    )


async def test_rip_all_judges_minlength_by_the_scan_estimate_at_rip_start(monkeypatch, tmp_path):
    """At rip-start no track has a measured duration. Title 1 is scanned at
    60s, below the 120s minlength, so makemkvcon leaves it out and numbers
    title 2's file `_t01`: title 1 must not count as eligible, or title 2
    would look for `_t02` and fail."""
    _fake_makemkvcon(monkeypatch, _mkv_all_script([0, 1], [0, 1]))
    tracks = [_scanned_track(0, 1800), _scanned_track(1, 60), _scanned_track(2, 1800)]

    reports, _ = await _rip_tracks(tracks, tmp_path)

    (t0,) = reports["trk_0"]
    (t1,) = reports["trk_1"]
    (t2,) = reports["trk_2"]
    assert t0.ok is True and t0.output_path == tmp_path / "ACCA_CS_D2_t00.mkv"
    assert t2.ok is True and t2.output_path == tmp_path / "ACCA_CS_D2_t01.mkv"
    assert t1.ok is False and "track duration 60s below minlength=120s" in (t1.error or "")


async def test_rip_all_counts_a_title_with_no_known_duration_as_eligible(monkeypatch, tmp_path):
    captured: dict[str, Any] = {}
    _stub_rip_disc(monkeypatch, RipDiscResult(overall_error=None, titles={}), captured=captured)

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    await rip_all(
        disc_type=DiscType.DVD,
        device_path="/dev/sr0",
        tracks=[_scanned_track(0, None), _scanned_track(1, 30), _scanned_track(2, 600)],
        output_dir=tmp_path,
        on_track_start=noop,
        on_track_done=noop,
        min_length_seconds=120,
    )

    assert captured["eligible_source_indexes"] == [0, 2]


# --- rip_all against the recorded-output makemkvcon fake (S6, S16) -----------
#
# `tests._makemkvcon_fake` answers from real recordings: the Hanako-kun BD in
# /dev/sr0 (or as a disc folder) and the Sintel DVD as an ISO. The disc scan
# lists every title (`--minlength=0`); tracks exist for the titles the preset
# keeps at or above the ripper's 120 s baseline, as the backend makes them,
# and rip_all gets the stored scan's titles as rip-start sends them.

_BASELINE = 120


def _rip_start_tracks(scan: ScanResult, baseline: int = _BASELINE, keep: set[int] | None = None) -> list[TrackView]:
    """Track rows as rip-start hands them over (`select_tracks`): one per
    title at or above the baseline (ARCHIVE), or only `keep`'s titles (a
    preset that picks some), with the scan's duration and size as the
    *expected* values; `duration_seconds` stays unset until ripped."""
    return [
        _track(t.index, duration_seconds=None).model_copy(
            update={"expected_duration_seconds": t.duration_seconds, "expected_size_bytes": t.size_bytes}
        )
        for t in scan.titles
        if (t.duration_seconds or 0) >= baseline and (keep is None or t.index in keep)
    ]


async def _scan(monkeypatch, device: str) -> ScanResult:
    monkeypatch.setattr(scan_makemkv_module, "probe_disc", AsyncMock(return_value=DiscProbe(crc64=None)))
    return await scan_makemkv_module.scan_disc(device)


def _install_sources(monkeypatch, tmp_path: Path, source: str) -> tuple[Any, str]:
    """The fake with the Hanako-kun BD in /dev/sr0, the Sintel DVD as an ISO
    and the Hanako-kun BD as a disc folder; returns the device to rip."""
    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")
    folder = tmp_path / "hanako"
    (folder / "BDMV").mkdir(parents=True)
    fake = install(monkeypatch, disc=HANAKO_BD, isos={str(iso): SINTEL_ISO}, folders={str(folder): HANAKO_BD})
    device = {"bd-drive": "/dev/sr0", "dvd-iso": str(iso), "bd-folder": str(folder)}[source]
    return fake, device


def _assert_own_titles(reports: dict[str, list[RipResult]], tracks: list[TrackView], scan: ScanResult, out: Path):
    """Every track DONE once, with a file in `out` sized as its own title."""
    titles = {t.index: t for t in scan.titles}
    for track in tracks:
        (result,) = reports[track.id]
        title = titles[track.index]
        assert result.ok is True, (track.index, result.error)
        assert result.output_path is not None and result.output_path.parent == out
        assert result.size_bytes == result.output_path.stat().st_size == file_size(title.size_bytes or 0)
        assert result.duration_seconds == title.duration_seconds
    assert len({reports[t.id][0].output_path for t in tracks}) == len(tracks)


@pytest.mark.parametrize("source", ["bd-drive", "dvd-iso", "bd-folder"])
async def test_rip_all_full_selection_gives_every_track_its_own_title(monkeypatch, tmp_path, source):
    """Today's `mkv all` path (S6.2) with every track kept, ripped at the
    baseline: each track is DONE with a file sized as its own title, and the
    drive is read by one invocation for the whole rip."""
    fake, device = _install_sources(monkeypatch, tmp_path, source)
    scan = await _scan(monkeypatch, device)
    fake.invocations.clear()
    tracks = _rip_start_tracks(scan)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, disc_type=scan.disc_type, device=device, scan_titles=scan.titles)

    _assert_own_titles(reports, tracks, scan, out)
    assert len(list(out.iterdir())) == len(tracks)
    assert len(fake.drive_reads) == (1 if source == "bd-drive" else 0)


@pytest.mark.parametrize("source", ["bd-drive", "dvd-iso"])
async def test_a_preset_that_keeps_one_title_gets_that_titles_file(monkeypatch, tmp_path, source):
    """The `_tNN` bug outside the review gate: a preset kept one title (the
    longest, as MAIN_FEATURE does), so it is the only Track. That title alone
    is ripped (backup-then-remux, S7), and the track gets its own title's
    file, not `_t00`; the drive is read once and no scratch is left."""
    fake, device = _install_sources(monkeypatch, tmp_path, source)
    scan = await _scan(monkeypatch, device)
    fake.invocations.clear()
    longest = max(scan.titles, key=lambda t: t.duration_seconds or 0)
    tracks = _rip_start_tracks(scan, keep={longest.index})
    assert longest.index != min(t.index for t in scan.titles if (t.duration_seconds or 0) >= _BASELINE)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, disc_type=scan.disc_type, device=device, scan_titles=scan.titles)

    _assert_own_titles(reports, tracks, scan, out)
    assert [p.name for p in out.iterdir()] == [reports[tracks[0].id][0].output_path.name]
    assert len(fake.drive_reads) == (1 if source == "bd-drive" else 0)


async def _sintel_rip(monkeypatch, tmp_path, min_length_seconds: int) -> tuple[ScanResult, dict[str, RipResult], Path]:
    _, iso = _install_sources(monkeypatch, tmp_path, "dvd-iso")
    scan = await _scan(monkeypatch, iso)
    tracks = _rip_start_tracks(scan)
    out = tmp_path / "job"
    reports, _ = await _rip_tracks(
        tracks,
        out,
        disc_type=scan.disc_type,
        device=iso,
        scan_titles=scan.titles,
        min_length_seconds=min_length_seconds,
    )
    assert all(len(r) == 1 for r in reports.values()), reports
    return scan, {track_id: r[0] for track_id, r in reports.items()}, out


async def test_scan_lists_every_title_and_the_baseline_cuts_the_tracks(monkeypatch, tmp_path):
    _, iso = _install_sources(monkeypatch, tmp_path, "dvd-iso")
    scan = await _scan(monkeypatch, iso)

    assert [t.duration_seconds for t in scan.titles] == [898, 897, 119, 365, 216, 107, 3504]
    assert [t.index for t in _rip_start_tracks(scan)] == [0, 1, 3, 4, 6]


async def test_rip_minimum_below_the_baseline_keeps_files_on_their_own_titles(monkeypatch, tmp_path):
    """Sintel titles 0–6 (1:59 and 1:47 among them), tracks only for the
    ≥120 s titles, a 60 s rip minimum: `mkv all` would also rip the two
    short titles, so only the kept ones are remuxed (S7). Each track gets
    its own title's file; the short titles get none."""
    scan, final, out = await _sintel_rip(monkeypatch, tmp_path, min_length_seconds=60)

    sizes = {t.index: file_size(t.size_bytes or 0) for t in scan.titles}
    assert sorted(final) == ["trk_0", "trk_1", "trk_3", "trk_4", "trk_6"]
    for idx in (0, 1, 3, 4, 6):
        result = final[f"trk_{idx}"]
        assert result.ok is True, (idx, result.error)
        assert result.output_path is not None and result.output_path.name.endswith(f"_t{idx:02d}.mkv")
        assert result.size_bytes == sizes[idx]
    claimed = {r.output_path for r in final.values()}
    assert sorted(p for p in out.iterdir()) == sorted(claimed)


async def test_rip_minimum_above_a_kept_title_fails_only_that_title(monkeypatch, tmp_path):
    """Same disc ripped at 600 s: the 6:05 and 3:36 tracks are FAILED below
    minlength; the others get their own titles' files."""
    scan, final, _ = await _sintel_rip(monkeypatch, tmp_path, min_length_seconds=600)

    sizes = {t.index: file_size(t.size_bytes or 0) for t in scan.titles}
    for idx in (3, 4):
        assert final[f"trk_{idx}"].ok is False
        assert "below minlength=600s" in (final[f"trk_{idx}"].error or "")
    for idx in (0, 1, 6):
        assert final[f"trk_{idx}"].ok is True
        assert final[f"trk_{idx}"].size_bytes == sizes[idx]


@pytest.mark.parametrize("keep", [None, {1, 3, 5}], ids=["all-kept", "some-kept"])
async def test_rip_all_resume_pass_leaves_settled_tracks_alone(monkeypatch, tmp_path, keep):
    """A D-133 resume pass: `mkv all` rips the whole disc again, but the
    tracks an earlier pass settled get no callbacks and their new files are
    deleted (matched by position in the scan-based list), while the rest get
    their own titles' files. When the preset kept only some titles the pass
    backs the disc up and remuxes only the unsettled kept ones (S7)."""
    fake, device = _install_sources(monkeypatch, tmp_path, "bd-drive")
    scan = await _scan(monkeypatch, device)
    fake.invocations.clear()
    tracks = _rip_start_tracks(scan, keep=keep)
    settled = {tracks[0].id}
    out = tmp_path / "job"

    reports, started = await _rip_tracks(tracks, out, device=device, skip_track_ids=settled, scan_titles=scan.titles)

    by_index = {t.index: t for t in scan.titles}
    assert reports[tracks[0].id] == []
    assert not any(p.name.endswith(f"_t{_position(scan, tracks[0].index):02d}.mkv") for p in out.iterdir())
    rest = [t for t in tracks if t.id not in settled]
    _assert_own_titles(reports, rest, scan, out)
    assert all(by_index[t.index].size_bytes for t in rest)
    assert settled.isdisjoint(started)
    assert len(fake.drive_reads) == 1


def _position(scan: ScanResult, index: int) -> int:
    """A title's `_tNN` number in `mkv all` at the baseline."""
    eligible = sorted(t.index for t in scan.titles if (t.duration_seconds or 0) >= _BASELINE)
    return eligible.index(index)


async def test_rip_all_disc_backup_writes_the_decrypted_folder(monkeypatch, tmp_path):
    """Upstream's FOLDER preset against the fake (MakeMKV 1.18.4): one
    `backup --decrypt disc:<N>` of the drive (`dev:` is refused), and the
    track is DONE with `<job>/backup`, which reads back as the disc's backup."""
    fake = install(monkeypatch, disc=HANAKO_BD)
    out = tmp_path / "job"
    backup = _track(0, source_ref="full").model_copy(update={"kind": TrackKind.DISC_BACKUP})

    reports, started = await _rip_tracks([backup], out)

    (result,) = reports[backup.id]
    assert result.ok is True, result.error
    assert result.output_path == out / "backup"
    assert (out / "backup" / "BDMV" / "index.bdmv").is_file()
    assert started == [backup.id]
    assert [(inv.command, inv.decrypt) for inv in fake.drive_reads] == [("backup", True)]
    assert not (out / ".backup").exists()


# --- rip only the kept titles: backup-then-remux (S7) -------------------------
#
# A drive whose kept titles are fewer than `mkv all` would rip is read once by
# a `backup` into `<job>/.backup/`, and each kept title is remuxed from it with
# `--noscan`. ISO and folder sources are remuxed from directly. Excluded
# tracks get no callbacks at all (they stay `queued`).


def _work(fake: Any) -> list[Any]:
    """Every invocation but the `info disc:9999` lookup of the drive's `disc:<N>`."""
    return [inv for inv in fake.invocations if inv.source != "disc:9999"]


def _exclude(tracks: list[TrackView], *indexes: int) -> list[TrackView]:
    return [t.model_copy(update={"excluded": True}) if t.index in indexes else t for t in tracks]


async def _bd_job(monkeypatch, tmp_path: Path, *, same_ids: bool = False) -> tuple[Any, ScanResult]:
    """The Hanako-kun BD in /dev/sr0. Its 2.0.0 backup numbers every title
    differently from the disc (the play-all is disc 7, backup 0); with
    `same_ids` the backup keeps the disc's numbering, which a 1.18.4
    decrypted backup may (S17 17.4 decides)."""
    disc = Recording(HANAKO_BD.info_min0) if same_ids else HANAKO_BD
    fake = install(monkeypatch, disc=disc)
    scan = await _scan(monkeypatch, "/dev/sr0")
    fake.invocations.clear()
    return fake, scan


def _results(reports: dict[str, list[RipResult]]) -> dict[str, RipResult]:
    assert all(len(r) == 1 for r in reports.values() if r), reports
    return {track_id: r[0] for track_id, r in reports.items() if r}


@pytest.mark.parametrize("same_ids", [False, True], ids=["backup-ids-permuted", "backup-ids-identical"])
async def test_a_blu_ray_with_the_play_all_excluded_rips_only_the_episodes(monkeypatch, tmp_path, same_ids):
    fake, scan = await _bd_job(monkeypatch, tmp_path, same_ids=same_ids)
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"

    reports, started = await _rip_tracks(tracks, out, scan_titles=scan.titles)

    episodes = [t for t in tracks if t.index != 7]
    _assert_own_titles(reports, episodes, scan, out)
    assert sorted(p.name for p in out.iterdir()) == [f"ToiletBound HanakoKun D1_t{i:02d}.mkv" for i in range(7)]
    assert reports["trk_7"] == [] and "trk_7" not in started
    assert [inv.command for inv in fake.drive_reads] == ["backup"]
    assert all(not inv.probes_drive for inv in fake.invocations if inv.command != "backup")
    assert sum(inv.command == "mkv" for inv in fake.invocations) == 7


async def test_excluding_title_0_still_gives_title_1_its_own_file(monkeypatch, tmp_path):
    """The D2 trap: with `_t00` gone, nothing may shift onto title 1."""
    _, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _exclude(_rip_start_tracks(scan), 0)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, scan_titles=scan.titles)

    title_1 = next(t for t in scan.titles if t.index == 1)
    (result,) = reports["trk_1"]
    assert result.output_path is not None and result.output_path.name.endswith("_t01.mkv")
    assert result.size_bytes == file_size(title_1.size_bytes or 0)
    assert reports["trk_0"] == []
    _assert_own_titles(reports, tracks[1:], scan, out)


@pytest.mark.parametrize("source", ["dvd-iso", "bd-folder"])
async def test_a_file_source_remuxes_its_kept_titles_with_no_backup(monkeypatch, tmp_path, source):
    fake, device = _install_sources(monkeypatch, tmp_path, source)
    scan = await _scan(monkeypatch, device)
    fake.invocations.clear()
    tracks = _rip_start_tracks(scan)
    excluded = tracks[1].index
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(
        _exclude(tracks, excluded), out, disc_type=scan.disc_type, device=device, scan_titles=scan.titles
    )

    kept = [t for t in tracks if t.index != excluded]
    _assert_own_titles(reports, kept, scan, out)
    assert reports[f"trk_{excluded}"] == []
    assert sorted(p.name for p in out.iterdir()) == sorted(reports[t.id][0].output_path.name for t in kept)
    assert fake.drive_reads == []
    assert {inv.command for inv in fake.invocations} == {"info", "mkv"}


async def test_a_dvd_at_minimum_0_with_a_clip_excluded_backs_up_to_one_image(monkeypatch, tmp_path):
    """ACCA_CS_D1 at minimum 0: four titles, one a 17 s clip that is excluded.
    The DVD backup is one image (`.backup/disc.iso`, read back as `iso:`,
    as recorded on 2.0.0; S17 17.6 checks 1.18.4), and the kept titles are
    remuxed from it."""
    fake = install(monkeypatch, disc=ACCA_DVD)
    scan = await _scan(monkeypatch, "/dev/sr0")
    fake.invocations.clear()
    tracks = _exclude(_rip_start_tracks(scan, baseline=0), 1)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, disc_type=DiscType.DVD, scan_titles=scan.titles, min_length_seconds=0)

    _assert_own_titles(reports, [t for t in tracks if t.index != 1], scan, out)
    assert reports["trk_1"] == []
    assert [inv.command for inv in fake.drive_reads] == ["backup"]
    image = f"iso:{out / '.backup' / 'disc.iso'}"
    assert {inv.source for inv in _work(fake) if inv.command in ("info", "mkv")} == {image}
    assert not (out / ".backup").exists()


async def test_kept_tracks_get_their_sha256_and_start_only_when_their_remux_does(monkeypatch, tmp_path):
    _, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"
    events: list[str] = []

    async def on_start(track: TrackView) -> None:
        events.append(f"start {track.index}")

    async def on_done(track: TrackView, result: RipResult) -> None:
        assert result.ok and result.output_path is not None
        assert result.sha256 == hashlib.sha256(result.output_path.read_bytes()).hexdigest()
        events.append(f"done {track.index}")

    async def on_progress(track: TrackView, fraction: float, stats: Any = None) -> None:
        if not events or events[-1] != "progress":
            events.append("progress")

    await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        scan_titles=scan.titles,
        output_dir=out,
        on_track_start=on_start,
        on_track_done=on_done,
        on_track_progress=on_progress,
    )

    # The backup's progress comes before any track starts.
    assert events[0] == "progress"
    assert [e for e in events if e.startswith("start")] == [f"start {i}" for i in range(7)]
    assert sorted(e for e in events if e.startswith("done")) == sorted(f"done {i}" for i in range(7))


async def test_one_progress_bar_backup_then_remuxes(monkeypatch, tmp_path):
    """S7.4: one bar per job, credited to the first kept track: the backup
    fills it to 0.9, the remuxes from 0.9 to 1.0, never going back; the
    hash pass reports itself as "verifying"."""
    _, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _exclude(_rip_start_tracks(scan), 0, 7)
    bar: list[tuple[str, float]] = []
    verifying: list[float] = []

    async def on_progress(track: TrackView, fraction: float, stats: Any = None) -> None:
        if stats is not None:
            assert stats["phase"] == "verifying"
            verifying.append(fraction)
            return
        bar.append((track.id, fraction))

    await _rip_tracks(tracks, tmp_path / "job", scan_titles=scan.titles, on_track_progress=on_progress)

    assert {track_id for track_id, _ in bar} == {"trk_1"}
    fractions = [f for _, f in bar]
    assert fractions == sorted(fractions)
    assert any(0.0 < f < 0.9 for f in fractions)
    assert any(0.9 < f < 1.0 for f in fractions)
    assert fractions[-1] == pytest.approx(1.0)
    assert all(0.0 <= f <= 1.0 for f in verifying)


async def test_a_kept_track_below_the_minimum_fails_and_the_rest_are_remuxed(monkeypatch, tmp_path):
    _, iso = _install_sources(monkeypatch, tmp_path, "dvd-iso")
    scan = await _scan(monkeypatch, iso)
    tracks = _exclude(_rip_start_tracks(scan), 0)

    reports, _ = await _rip_tracks(
        tracks, tmp_path / "job", disc_type=DiscType.DVD, device=iso, scan_titles=scan.titles, min_length_seconds=600
    )

    final = _results(reports)
    assert sorted(final) == ["trk_1", "trk_3", "trk_4", "trk_6"]
    for idx in (3, 4):
        assert final[f"trk_{idx}"].ok is False
        assert "below minlength=600s" in (final[f"trk_{idx}"].error or "")
    assert final["trk_1"].ok and final["trk_6"].ok


async def test_a_failed_backup_fails_every_kept_track_and_is_deleted(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    fake.fail("backup", message="L-EC uncorrectable error")
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"

    reports, started = await _rip_tracks(tracks, out, scan_titles=scan.titles)

    final = _results(reports)
    assert sorted(final) == [f"trk_{i}" for i in range(7)]
    for result in final.values():
        assert result.ok is False
        assert (result.error or "").startswith("disc backup failed:")
        assert "L-EC uncorrectable error" in (result.error or "")
    assert sorted(started) == sorted(final)
    assert not (out / ".backup").exists()
    assert [inv.command for inv in _work(fake)] == ["backup"]


async def test_a_drive_makemkv_does_not_list_fails_every_kept_track(monkeypatch, tmp_path):
    """No `DRV:` line names the device, so there is no `disc:<N>` to back up:
    every kept track is FAILED saying so, and nothing reads the drive."""
    _, scan = await _bd_job(monkeypatch, tmp_path)
    # MakeMKV lists the drive under a node other than the one the ripper has.
    fake = install(monkeypatch, disc=HANAKO_BD, device="/dev/sr3")
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"

    reports, started = await _rip_tracks(tracks, out, scan_titles=scan.titles)

    final = _results(reports)
    assert sorted(final) == sorted(started) == [f"trk_{i}" for i in range(7)]
    assert {r.error for r in final.values()} == {
        "disc backup failed: makemkvcon lists no drive at /dev/sr0, so it can't back the disc up"
    }
    assert [inv.command for inv in fake.invocations] == ["info"]
    assert not (out / ".backup").exists()


async def test_one_failed_remux_fails_only_its_track_and_keeps_the_backup(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    fake.fail("mkv", title="00010.mpls")  # disc title 1
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, scan_titles=scan.titles)

    final = _results(reports)
    assert final["trk_1"].ok is False
    assert "produced no .mkv" in (final["trk_1"].error or "")
    _assert_own_titles(reports, [t for t in tracks if t.index not in (1, 7)], scan, out)
    assert (out / ".backup" / ".complete").read_text().strip() == f"file:{out / '.backup' / 'disc' / 'BDMV'}"
    assert not (out / ".remux").exists()


async def test_a_title_missing_from_the_backup_fails_only_that_track(monkeypatch, tmp_path):
    """A kept title the backup doesn't list (no title plays its playlist) is
    FAILED rather than remuxed from the wrong title; the backup is kept."""
    _, scan = await _bd_job(monkeypatch, tmp_path)
    titles = [t.model_copy(update={"source_playlist": "99999.mpls"}) if t.index == 2 else t for t in scan.titles]
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, scan_titles=titles)

    final = _results(reports)
    assert final["trk_2"].ok is False
    assert final["trk_2"].error == "could not map title 2 to the backup"
    _assert_own_titles(reports, [t for t in tracks if t.index not in (2, 7)], scan, out)
    assert (out / ".backup" / ".complete").is_file()


async def test_a_full_selection_from_a_drive_keeps_mkv_all(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _rip_start_tracks(scan)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, scan_titles=scan.titles)

    _assert_own_titles(reports, tracks, scan, out)
    assert [(inv.command, inv.argv[-2]) for inv in fake.drive_reads] == [("mkv", "all")]


async def test_backup_full_selections_sends_a_full_drive_selection_down_the_backup_path(monkeypatch, tmp_path):
    """`ARM_BACKUP_FULL_SELECTIONS` (Q6): every title kept, still one backup
    read and a remux per title; an ISO source keeps `mkv all`."""
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _rip_start_tracks(scan)
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, scan_titles=scan.titles, backup_full_selections=True)

    _assert_own_titles(reports, tracks, scan, out)
    assert [inv.command for inv in fake.drive_reads] == ["backup"]
    assert not (out / ".backup").exists()

    fake, iso = _install_sources(monkeypatch, tmp_path, "dvd-iso")
    iso_scan = await _scan(monkeypatch, iso)
    fake.invocations.clear()
    iso_tracks = _rip_start_tracks(iso_scan)
    await _rip_tracks(
        iso_tracks,
        tmp_path / "iso-job",
        disc_type=DiscType.DVD,
        device=iso,
        scan_titles=iso_scan.titles,
        backup_full_selections=True,
    )
    assert [(inv.command, inv.argv[-2]) for inv in fake.invocations] == [("mkv", "all")]


async def test_without_a_stored_scan_exclusions_fall_back_to_mkv_all_and_stay_unreported(monkeypatch, tmp_path):
    """A job scanned before S5 has nothing to find titles by in a backup:
    `mkv all` rips everything, the excluded title's file stays unclaimed,
    and the excluded track gets no callbacks."""
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = [
        t.model_copy(update={"duration_seconds": t.expected_duration_seconds})
        for t in _exclude(_rip_start_tracks(scan), 7)
    ]
    out = tmp_path / "job"

    reports, started = await _rip_tracks(tracks, out)

    assert reports["trk_7"] == [] and "trk_7" not in started
    assert all(reports[f"trk_{i}"][0].ok for i in range(7))
    assert len(list(out.glob("*.mkv"))) == 8
    assert [inv.command for inv in fake.drive_reads] == ["mkv"]


async def test_a_disc_level_error_on_mkv_all_never_reports_an_excluded_track(monkeypatch, tmp_path):
    """Audit A23: the overall-error branch fails every unfinished track, but
    never an excluded one (here excluded and below the minimum, so `mkv all`
    stays the strategy)."""
    _stub_rip_disc(monkeypatch, RipDiscResult(overall_error="boom", titles={}), fire_per_title_start=False)
    tracks = [_track(0), _track(1), _track(2, duration_seconds=30).model_copy(update={"excluded": True})]

    reports, started = await _rip_tracks(
        tracks,
        tmp_path,
        scan_titles=[
            ScanTitle(index=0, duration_seconds=1800),
            ScanTitle(index=1, duration_seconds=1800),
            ScanTitle(index=2, duration_seconds=30),
        ],
    )

    assert reports["trk_2"] == [] and "trk_2" not in started
    assert [r.error for r in reports["trk_0"] + reports["trk_1"]] == ["boom", "boom"]


async def test_an_excluded_track_with_a_bad_source_ref_is_not_reported(monkeypatch, tmp_path):
    _stub_rip_disc(monkeypatch, RipDiscResult(overall_error=None, titles={}), fire_per_title_start=False)
    bad = _track(5, source_ref="bogus").model_copy(update={"excluded": True})

    reports, _ = await _rip_tracks([bad], tmp_path)

    assert reports[bad.id] == []


# --- D-133 on the selection path ----------------------------------------------


async def test_a_drop_during_the_backup_reruns_it_on_the_resume(monkeypatch, tmp_path):
    """The drive leaves mid-backup: the backup is stopped and deleted, every
    kept track is reported with the stop error (the controller holds them for
    the resume), and the resume pass backs the disc up again."""
    from arm_ripper.rip.makemkv_rip import STOPPED_ERROR

    fake, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _exclude(_rip_start_tracks(scan), 7)
    job = tmp_path / "job"
    stop = asyncio.Event()

    async def drop_halfway(_track: TrackView, fraction: float, _stats: Any = None) -> None:
        if fraction > 0.4:
            stop.set()

    first, _ = await _rip_tracks(
        tracks, job, scan_titles=scan.titles, stop=stop, on_track_progress=drop_halfway, backup_dir=job / ".backup"
    )

    assert all(r.error == f"disc backup failed: {STOPPED_ERROR}" for r in _results(first).values())
    assert len(_results(first)) == 7
    assert not (job / ".backup").exists()

    second, _ = await _rip_tracks(
        tracks,
        job / "resume-1",
        scan_titles=scan.titles,
        stop=asyncio.Event(),
        skip_track_ids={"trk_7"},
        backup_dir=job / ".backup",
    )

    _assert_own_titles(second, [t for t in tracks if t.index != 7], scan, job / "resume-1")
    assert [inv.command for inv in fake.drive_reads] == ["backup", "backup"]
    assert not (job / ".backup").exists()


async def test_a_backup_that_finished_after_the_drive_dropped_does_not_count(monkeypatch, tmp_path):
    """makemkvcon can exit 0 after the drive left under it, before anything
    stopped it: the copy may be cut short, so it is deleted and the kept
    tracks are reported stopped (unfinished, for the resume)."""
    from arm_ripper.rip.makemkv_rip import STOPPED_ERROR

    fake, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"
    stop = asyncio.Event()

    reports, _ = await _rip_tracks(tracks, out, scan_titles=scan.titles, stop=stop, drive_lost=lambda: True)

    assert stop.is_set()
    assert {r.error for r in _results(reports).values()} == {f"disc backup failed: {STOPPED_ERROR}"}
    assert not (out / ".backup").exists()
    assert [inv.command for inv in _work(fake)] == ["backup"]


async def test_a_drop_after_the_backup_finished_costs_nothing(monkeypatch, tmp_path):
    """Once the backup is complete the remuxes read only from disk: a drive
    that leaves now stops nothing, and every kept title is ripped."""
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _exclude(_rip_start_tracks(scan), 7)
    out = tmp_path / "job"
    stop = asyncio.Event()

    async def drop_while_remuxing(_track: TrackView, fraction: float, stats: Any = None) -> None:
        if stats is None and fraction > 0.9:
            stop.set()

    reports, _ = await _rip_tracks(
        tracks, out, scan_titles=scan.titles, stop=stop, on_track_progress=drop_while_remuxing
    )

    assert stop.is_set()
    _assert_own_titles(reports, [t for t in tracks if t.index != 7], scan, out)
    assert [inv.command for inv in fake.drive_reads] == ["backup"]


async def test_a_resume_remuxes_from_the_finished_backup_with_no_drive_read(monkeypatch, tmp_path):
    """A pass that left a finished backup (here: one remux failed) and a
    resume over it: the resume reads no drive, remuxes only the tracks not
    yet settled, and removes the backup once they are all done."""
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    fake.fail("mkv", title="00010.mpls")  # disc title 1
    tracks = _exclude(_rip_start_tracks(scan), 7)
    job = tmp_path / "job"

    first, _ = await _rip_tracks(tracks, job, scan_titles=scan.titles, backup_dir=job / ".backup")
    assert _results(first)["trk_1"].ok is False
    fake.clear_failures()
    fake.invocations.clear()

    settled = {t.id for t in tracks if t.index != 1}
    second, started = await _rip_tracks(
        tracks, job / "resume-1", scan_titles=scan.titles, skip_track_ids=settled, backup_dir=job / ".backup"
    )

    assert started == ["trk_1"]
    assert {track_id for track_id, r in second.items() if r} == {"trk_1"}
    _assert_own_titles(second, [tracks[1]], scan, job / "resume-1")
    assert fake.drive_reads == []
    assert [inv.command for inv in fake.invocations] == ["info", "mkv"]
    assert not (job / ".backup").exists()


class _FakeAbcde:
    """abcde as `rip_cd` runs it: expands its trailing track arguments (`N` or
    `N-M`) the way abcde does, writes `trackNN.wav` for each of those tracks
    on a disc of `disc_tracks`, and prints the read-done lines."""

    def __init__(self, monkeypatch, disc_tracks: int) -> None:
        self.argvs: list[list[str]] = []
        self.read: list[int] = []
        self._disc_tracks = disc_tracks
        monkeypatch.setattr(abcde_rip_module.asyncio, "create_subprocess_exec", self._exec)

    async def _exec(self, *argv: str, cwd: str, **_kw: Any) -> Any:
        self.argvs.append(list(argv))
        queue: list[int] = []
        for arg in argv[argv.index("-j") + 2 :]:
            first, _, last = arg.partition("-")
            queue.extend(range(int(first), int(last or first) + 1))
        lines = []
        for idx in sorted(set(queue)):
            if idx > self._disc_tracks:
                continue
            (Path(cwd) / f"track{idx:02d}.wav").write_bytes(b"RIFF" + bytes([idx]) * 16)
            self.read.append(idx)
            lines.append(f"[wavencode] Track {idx:02d}: Reading audio from sectors 1-2...done\n".encode())
        return _AbcdeProc(lines)


class _AbcdeProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"".join(lines))
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:  # pragma: no cover - the fake always exits on its own
        self.returncode = -9


def _cd_tracks(count: int) -> list[TrackView]:
    return [_track(i).model_copy(update={"kind": TrackKind.AUDIO_TRACK}) for i in range(1, count + 1)]


async def test_a_cd_with_a_track_excluded_reads_only_the_kept_tracks_in_one_abcde(monkeypatch, tmp_path):
    abcde = _FakeAbcde(monkeypatch, disc_tracks=5)
    tracks = _exclude(_cd_tracks(5), 4)

    reports, started = await _rip_tracks(tracks, tmp_path, disc_type=DiscType.CD)

    assert len(abcde.argvs) == 1
    assert abcde.argvs[0][-2:] == ["1-3", "5"]
    assert abcde.read == [1, 2, 3, 5]
    assert sorted(p.name for p in tmp_path.glob("*.wav")) == [
        "track01.wav",
        "track02.wav",
        "track03.wav",
        "track05.wav",
    ]
    assert started == ["trk_1", "trk_2", "trk_3", "trk_5"]
    assert reports["trk_4"] == []
    assert all(len(reports[t]) == 1 and reports[t][0].ok for t in ("trk_1", "trk_2", "trk_3", "trk_5"))


async def test_a_cd_with_nothing_excluded_lists_every_track(monkeypatch, tmp_path):
    abcde = _FakeAbcde(monkeypatch, disc_tracks=3)

    reports, _ = await _rip_tracks(_cd_tracks(3), tmp_path, disc_type=DiscType.CD)

    assert [argv[-1] for argv in abcde.argvs] == ["1-3"]
    assert all(r[0].ok for r in reports.values())


async def test_a_cd_with_every_track_excluded_runs_no_abcde(monkeypatch, tmp_path):
    abcde = _FakeAbcde(monkeypatch, disc_tracks=2)

    reports, started = await _rip_tracks(_exclude(_cd_tracks(2), 1, 2), tmp_path, disc_type=DiscType.CD)

    assert abcde.argvs == []
    assert started == []
    assert reports == {"trk_1": [], "trk_2": []}


# --- disk-space preflight before a backup (S8) --------------------------------


async def test_not_enough_space_fails_the_kept_tracks_before_the_drive_is_read(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    _set_free_space(monkeypatch, 20 * _GB)
    tracks = _exclude(_rip_start_tracks(scan), 7)

    reports, _ = await _rip_tracks(tracks, tmp_path / "job", scan_titles=scan.titles)

    results = _results(reports)
    assert set(results) == {t.id for t in tracks if t.index != 7}
    for result in results.values():
        assert result.ok is False
        assert (result.error or "").startswith(f"not enough space on {tmp_path}: need ~")
        assert "have 20.0 GB free" in (result.error or "")
    assert fake.invocations == []
    assert not (tmp_path / "job" / ".backup").exists()


async def test_a_play_all_is_not_counted_twice(monkeypatch, tmp_path):
    """The Hanako-kun titles add up to 92.5 GB, but the play-all holds the
    episodes' clips: the backup needs about the disc (46.5 GB), so 60 GB
    free is enough for it plus one 6.6 GB episode."""
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    _set_free_space(monkeypatch, 60 * _GB)
    assert sum(t.size_bytes or 0 for t in scan.titles) > 90 * _GB
    tracks = _exclude(_rip_start_tracks(scan), 1, 2, 3, 4, 5, 6, 7)

    reports, _ = await _rip_tracks(tracks, tmp_path / "job", scan_titles=scan.titles)

    assert _results(reports)["trk_0"].ok is True, _results(reports)["trk_0"].error
    assert [inv.command for inv in fake.drive_reads] == ["backup"]


async def test_the_space_message_names_backups_other_jobs_keep(monkeypatch, tmp_path):
    _, scan = await _bd_job(monkeypatch, tmp_path)
    _set_free_space(monkeypatch, 30 * _GB)
    other = tmp_path / "job_older" / ".backup" / "disc" / "BDMV" / "STREAM"
    other.mkdir(parents=True)
    with (other / "00014.m2ts").open("wb") as f:
        f.truncate(25 * _GB)  # sparse: costs nothing here
    (tmp_path / "job_folder" / "backup").mkdir(parents=True)  # a FOLDER preset's output is not scratch
    tracks = _exclude(_rip_start_tracks(scan), 7)

    reports, _ = await _rip_tracks(tracks, tmp_path / "job", scan_titles=scan.titles)

    error = _results(reports)["trk_0"].error or ""
    assert "1 other job(s) keep a disc backup in <job>/.backup/" in error
    assert "job_older (25.0 GB)" in error
    assert "job_folder" not in error


async def test_a_resume_checks_the_space_of_the_job_not_its_resume_folder(monkeypatch, tmp_path):
    """A resume rips into `<job>/resume-N` with the backup in `<job>/.backup`:
    the message names `/raw`, and the job's own unfinished backup is cleared
    first so it isn't listed as another job's."""
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    _set_free_space(monkeypatch, 20 * _GB)
    job = tmp_path / "job"
    (job / ".backup" / "disc").mkdir(parents=True)  # cut short by the drive drop: no `.complete`
    tracks = _exclude(_rip_start_tracks(scan), 7)

    reports, _ = await _rip_tracks(tracks, job / "resume-1", scan_titles=scan.titles, backup_dir=job / ".backup")

    error = _results(reports)["trk_0"].error or ""
    assert error.startswith(f"not enough space on {tmp_path}: need ~")
    assert "other job" not in error
    assert fake.invocations == []


async def test_a_folder_preset_backup_is_refused_when_the_disc_wont_fit(monkeypatch, tmp_path):
    """W6: upstream's FOLDER backup gets the same check (the backup alone)."""
    _, scan = await _bd_job(monkeypatch, tmp_path)
    fake = install(monkeypatch, disc=HANAKO_BD)
    _set_free_space(monkeypatch, 40 * _GB)
    backup = _track(0, source_ref="full").model_copy(update={"kind": TrackKind.DISC_BACKUP})

    reports, started = await _rip_tracks([backup], tmp_path / "job", scan_titles=scan.titles)

    (result,) = reports[backup.id]
    assert result.ok is False
    assert "(disc backup ~46.5 GB + kept titles ~0.0 GB), have 40.0 GB free" in (result.error or "")
    assert started == [backup.id]
    assert fake.invocations == []


async def test_a_folder_preset_backup_with_room_proceeds(monkeypatch, tmp_path):
    _, scan = await _bd_job(monkeypatch, tmp_path)
    fake = install(monkeypatch, disc=HANAKO_BD)
    _set_free_space(monkeypatch, 60 * _GB)
    backup = _track(0, source_ref="full").model_copy(update={"kind": TrackKind.DISC_BACKUP})

    reports, _ = await _rip_tracks([backup], tmp_path / "job", scan_titles=scan.titles)

    assert reports[backup.id][0].ok is True, reports[backup.id][0].error
    assert [inv.command for inv in fake.drive_reads] == ["backup"]


# --- give the disc back once its backup is done (S9) ---------------------------


async def test_the_disc_is_released_after_the_backup_and_before_any_remux(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    tracks = _exclude(_rip_start_tracks(scan), 7)
    seen: list[list[str]] = []

    async def on_backup_done() -> None:
        seen.append([inv.command for inv in _work(fake)])

    reports, _ = await _rip_tracks(tracks, tmp_path / "job", scan_titles=scan.titles, on_backup_done=on_backup_done)

    assert seen == [["backup"]]
    after = _work(fake)[1:]
    assert after and not any(inv.probes_drive for inv in after)
    assert all(r.ok for r in _results(reports).values())


async def test_no_release_without_a_fresh_drive_backup(monkeypatch, tmp_path):
    """`mkv all` (the post-rip eject follows the rip), a backup the drive
    already gave (a resume reads no drive), a failed backup and a file source
    never call it."""
    released: list[str] = []

    async def on_backup_done() -> None:
        released.append("released")

    fake, scan = await _bd_job(monkeypatch, tmp_path)
    await _rip_tracks(_rip_start_tracks(scan), tmp_path / "all", scan_titles=scan.titles, on_backup_done=on_backup_done)

    tracks = _exclude(_rip_start_tracks(scan), 7)
    fake.fail("mkv", title="00010.mpls")
    job = tmp_path / "job"
    await _rip_tracks(tracks, job, scan_titles=scan.titles, backup_dir=job / ".backup", on_backup_done=on_backup_done)
    assert released == ["released"]  # the first pass's fresh backup
    fake.clear_failures()
    settled = {t.id for t in tracks if t.index != 1}
    await _rip_tracks(
        tracks,
        job / "resume-1",
        scan_titles=scan.titles,
        skip_track_ids=settled,
        backup_dir=job / ".backup",
        on_backup_done=on_backup_done,
    )
    assert released == ["released"]

    fake.fail("backup")
    await _rip_tracks(tracks, tmp_path / "failed", scan_titles=scan.titles, on_backup_done=on_backup_done)
    assert released == ["released"]

    iso_fake, device = _install_sources(monkeypatch, tmp_path, "dvd-iso")
    iso_scan = await _scan(monkeypatch, device)
    iso_tracks = _exclude(_rip_start_tracks(iso_scan), _rip_start_tracks(iso_scan)[1].index)
    await _rip_tracks(
        iso_tracks,
        tmp_path / "iso",
        disc_type=iso_scan.disc_type,
        device=device,
        scan_titles=iso_scan.titles,
        on_backup_done=on_backup_done,
    )
    assert released == ["released"]


# --- retry the failed titles from the kept backup (S15) ------------------------


class _RetryClient:
    """The Backend as a retry sees it: rip-start hands back the job's tracks,
    DONE and queued, marked as a retry."""

    def __init__(self, job_id: str, tracks: list[TrackView], scan: ScanResult) -> None:
        from arm_common import JobStatus
        from arm_common.schemas import JobView, RipStartResponse

        self.job = JobView(
            id=job_id,
            drive_id="drv_x",
            disc_type=DiscType.BLURAY,
            status=JobStatus.RIPPED_PARTIAL,
            title=None,
            year=None,
            metadata_json={},
            resumed_from_crash=False,
        )
        self.start = RipStartResponse(
            job_id=job_id, rip_preset_id="rpr", tracks=tracks, scan_titles=scan.titles, retry=True
        )
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self.completed: list[str] = []

    async def get_ripper_config(self) -> Any:
        return SimpleNamespace(makemkv_key=None)

    async def get_job(self, job_id: str) -> Any:
        return self.job

    async def rip_start(self, job_id: str) -> Any:
        return self.start

    async def update_track(self, track_id: str, **fields: Any) -> TrackView:
        self.patches.append((track_id, fields))
        return self.start.tracks[0]

    async def rip_complete(self, job_id: str) -> Any:
        self.completed.append(job_id)
        return self.job


async def test_a_retry_remuxes_only_the_failed_title_from_the_kept_backup(monkeypatch, tmp_path):
    import arm_ripper.job_controller as jc_module
    from arm_ripper.drive_handle import DriveHandle
    from arm_ripper.job_controller import JobController

    fake, scan = await _bd_job(monkeypatch, tmp_path)
    job_id = "job_retry"
    job = tmp_path / job_id
    fake.fail("mkv", title="00010.mpls")  # disc title 1
    tracks = _exclude(_rip_start_tracks(scan), 7)
    first, _ = await _rip_tracks(tracks, job, scan_titles=scan.titles, backup_dir=job / ".backup")
    assert _results(first)["trk_1"].ok is False
    assert (job / ".backup" / ".complete").is_file()
    fake.clear_failures()
    fake.invocations.clear()

    done = {track_id: r for track_id, r in _results(first).items() if r.ok}
    before = {track_id: r.output_path.read_bytes() for track_id, r in done.items() if r.output_path}
    retry_tracks = [
        t.model_copy(update={"status": TrackStatus.DONE, "output_path": str(done[t.id].output_path)})
        if t.id in done
        else t
        for t in tracks
    ]
    client = _RetryClient(job_id, retry_tracks, scan)
    monkeypatch.setattr(jc_module, "RAW_ROOT", tmp_path)

    async def no_key(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(jc_module, "refresh_makemkv_key", no_key)
    ejected: list[str] = []

    async def eject(self: Any, device_path: str) -> None:
        ejected.append(device_path)

    monkeypatch.setattr(JobController, "_eject_with_retry", eject)
    controller = JobController(client, "drv_x", device_path=DriveHandle("/dev/sr0"))  # type: ignore[arg-type]

    await controller.handle_retry(job_id)

    assert fake.drive_reads == []
    assert ejected == []
    assert {track_id for track_id, _ in client.patches} == {"trk_1"}
    final = [f for track_id, f in client.patches if track_id == "trk_1"][-1]
    assert final["status"] == TrackStatus.DONE
    assert Path(final["output_path"]).parent == job / "retry-1"
    assert {track_id: done[track_id].output_path.read_bytes() for track_id in before} == before
    assert client.completed == [job_id]
    assert not (job / ".backup").exists()  # every kept title is ripped now
    assert not controller.drive_released


async def test_a_retry_with_no_finished_backup_or_a_busy_drive_does_nothing(monkeypatch, tmp_path):
    import arm_ripper.job_controller as jc_module
    from arm_ripper.drive_handle import DriveHandle
    from arm_ripper.job_controller import JobController

    monkeypatch.setattr(jc_module, "RAW_ROOT", tmp_path)
    client = AsyncMock()
    controller = JobController(client, "drv_x", device_path=DriveHandle("/dev/sr0"))

    await controller.handle_retry("job_none")
    (tmp_path / "job_busy" / ".backup").mkdir(parents=True)
    (tmp_path / "job_busy" / ".backup" / ".complete").write_text("file:/x\n")
    async with controller._active_lock:
        await controller.handle_retry("job_busy")

    client.rip_start.assert_not_called()
    client.get_job.assert_not_called()


async def test_the_rip_retry_command_starts_the_retry(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from arm_common.schemas import WSEnvelope

    from arm_ripper.drive_handle import DriveHandle
    from arm_ripper.job_controller import JobController

    controller = JobController(AsyncMock(), "drv_x", device_path=DriveHandle("/dev/sr0"))
    started: list[str] = []

    async def handle_retry(job_id: str) -> None:
        started.append(job_id)

    monkeypatch.setattr(controller, "handle_retry", handle_retry)

    def envelope(payload: dict[str, Any]) -> WSEnvelope:
        return WSEnvelope(
            event_id="evt_retry",
            event_type="rip.retry",
            emitted_at=datetime.now(timezone.utc),
            topic="ripper.commands.drv_x",
            job_id="job_retry",
            payload=payload,
        )

    await controller.on_ws_command(envelope({"job_id": "job_retry", "drive_id": "drv_x"}))
    await controller.on_ws_command(envelope({"drive_id": "drv_x"}))
    await asyncio.sleep(0)

    assert started == ["job_retry"]
