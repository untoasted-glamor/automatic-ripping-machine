"""Stop 2 (fork only): `park_after_backup` backs a DVD/BD up whole, copies its menu and stops before any remux. The
titles stay queued and the backup stays, to be chosen and remuxed later; the ripper calls `rip-parked`, not
rip-complete. A backup that fails or has no room rips the kept titles with `mkv all` instead, as today."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import arm_ripper.job_controller as jc_module
from arm_common import DiscType, JobStatus
from arm_common.enums import TrackStatus
from arm_common.schemas import JobView, RipStartResponse, TrackView
from arm_ripper.rip.dispatcher import rip_all
from arm_ripper.rip.optical_rip import BACKUP_COMPLETE_MARKER, SELECTION_BACKUP_DIRNAME

from tests.test_disc_menu_rip import _DONE, _fake_extract, _mkv_all, _Sink
from tests.test_rip_device_lost import _JOB, _Client, _script, _track, env  # noqa: F401 - env is a fixture
from tests.test_rip_dispatcher import (  # noqa: F401 - _plenty_of_space is an autouse fixture
    _GB,
    _bd_job,
    _exclude,
    _plenty_of_space,
    _results,
    _rip_start_tracks,
    _set_free_space,
)


async def _park(
    tracks: list[TrackView], out: Path, scan: Any, **kwargs: Any
) -> tuple[bool, dict[str, list[Any]], list[str]]:
    reports: dict[str, list[Any]] = {t.id: [] for t in tracks}
    started: list[str] = []

    async def on_start(track: TrackView) -> None:
        started.append(track.id)

    async def on_done(track: TrackView, result: Any) -> None:
        reports[track.id].append(result)

    parked = await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        output_dir=out,
        on_track_start=on_start,
        on_track_done=on_done,
        min_length_seconds=120,
        scan_titles=scan.titles,
        park_after_backup=True,
        **kwargs,
    )
    return parked, reports, started


@pytest.mark.parametrize("left_out", [(7,), ()], ids=["preset-defaults", "every-title-kept"])
async def test_it_backs_up_copies_the_menu_and_stops(monkeypatch, tmp_path, left_out):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    sink = _Sink()
    out = tmp_path / "job"

    parked, reports, started = await _park(_exclude(_rip_start_tracks(scan), *left_out), out, scan, menu=sink)

    assert parked is True
    assert [inv.command for inv in fake.drive_reads] == ["backup"]
    assert sum(inv.command == "mkv" for inv in fake.invocations) == 0  # no remux, no mkv all
    assert started == [] and all(r == [] for r in reports.values())  # the titles wait, queued
    assert (out / SELECTION_BACKUP_DIRNAME / BACKUP_COMPLETE_MARKER).is_file()
    (call,) = calls
    assert call.dest == out / "menu"
    assert sink.outcome == _DONE


async def test_with_disc_menus_off_it_parks_right_after_the_backup(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    out = tmp_path / "job"

    parked, reports, _ = await _park(_exclude(_rip_start_tracks(scan), 7), out, scan)

    assert parked is True
    assert [inv.command for inv in fake.drive_reads] == ["backup"]
    assert all(r == [] for r in reports.values())
    assert (out / SELECTION_BACKUP_DIRNAME / BACKUP_COMPLETE_MARKER).is_file()


@pytest.mark.parametrize("why", ["no-room", "backup-fails"])
async def test_without_a_backup_it_rips_the_kept_titles_with_mkv_all(monkeypatch, tmp_path, why):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    _fake_extract(monkeypatch, fake)
    if why == "no-room":
        _set_free_space(monkeypatch, 1 * _GB)
    else:
        fake.fail("backup")
    tracks = _exclude(_rip_start_tracks(scan), 7)
    sink = _Sink()

    parked, reports, _ = await _park(tracks, tmp_path / "job", scan, menu=sink)

    assert parked is False
    assert len(_mkv_all(fake)) == 1
    results = _results(reports)
    assert set(results) == {t.id for t in tracks if not t.excluded}
    assert all(r.ok for r in results.values()), results
    assert sink.outcome.status == TrackStatus.FAILED
    assert (sink.outcome.error or "").endswith("; the titles were ripped without it")


async def test_a_backup_an_earlier_pass_finished_parks_without_reading_the_drive(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    out = tmp_path / "job"
    tracks = _exclude(_rip_start_tracks(scan), 7)
    assert (await _park(tracks, out, scan))[0] is True
    fake.invocations.clear()

    parked, reports, _ = await _park(tracks, out, scan)

    assert parked is True
    assert fake.invocations == []
    assert all(r == [] for r in reports.values())


async def test_a_disc_rip_without_park_is_as_before(monkeypatch, tmp_path):
    """Every title kept, no park: one `mkv all`, as today."""
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    out = tmp_path / "job"
    tracks = _rip_start_tracks(scan)

    parked = await rip_all(
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        tracks=tracks,
        output_dir=out,
        on_track_start=_noop,
        on_track_done=_noop2,
        min_length_seconds=120,
        scan_titles=scan.titles,
    )

    assert parked is False
    assert len(_mkv_all(fake)) == 1


async def _noop(_track: Any) -> None:
    return None


async def _noop2(_track: Any, _result: Any) -> None:
    return None


# --- the controller: rip-parked instead of rip-complete ---------------------------------------


class _ParkClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.parked: list[str] = []

    async def rip_parked(self, job_id: str) -> JobView:
        self.parked.append(job_id)
        return JobView(
            id=job_id,
            drive_id="drv_x",
            disc_type=DiscType.BLURAY,
            status=JobStatus.AWAITING_TITLES,
            title=None,
            year=None,
            metadata_json={},
            resumed_from_crash=False,
        )


@pytest.mark.parametrize("backup_read_now", [True, False], ids=["fresh-backup", "reused-backup"])
async def test_a_parked_rip_calls_rip_parked_and_gives_the_disc_back_once(monkeypatch, env, backup_read_now):  # noqa: F811 - env is a fixture
    client = _ParkClient()
    controller = env["controller"]
    controller._client = client

    async def backed_up(**kw: Any) -> bool:
        assert kw["park_after_backup"] is True
        if backup_read_now:
            await kw["on_backup_done"]()
        return True

    calls: list[dict[str, Any]] = []

    async def fake_rip_all(**kw: Any) -> bool:
        calls.append(kw)
        return await backed_up(**kw)

    monkeypatch.setattr(jc_module, "rip_all", fake_rip_all)
    tracks = [_track(0), _track(1)]

    await controller._execute_rip(
        job_id=_JOB,
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        rip_start=RipStartResponse(job_id=_JOB, rip_preset_id="rpr", tracks=tracks, park_after_backup=True),
    )

    assert len(calls) == 1
    assert client.parked == [_JOB] and client.completed == []
    assert env["ejected"] == ["/dev/sr0"]
    assert not controller.drive_released


async def test_a_remux_never_parks(monkeypatch, env):  # noqa: F811 - env is a fixture
    controller = env["controller"]

    async def remux(**kw: Any) -> None:
        assert kw["park_after_backup"] is False

    _script(monkeypatch, [remux])

    await controller._execute_rip(
        job_id=_JOB,
        disc_type=DiscType.BLURAY,
        device_path="/dev/sr0",
        rip_start=RipStartResponse(job_id=_JOB, rip_preset_id="rpr", tracks=[_track(0)], park_after_backup=True),
        drive_free=True,
    )

    assert env["client"].completed == [_JOB]
    assert env["ejected"] == []
