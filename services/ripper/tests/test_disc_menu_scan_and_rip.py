"""The disc menu in the ripper (fork only, design v5 A1): the scan reads its kind, and the rip marks the menu row
skipped until menu extraction (A2) is built, never stopping the rip over it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import arm_ripper.scan.disc_probe as disc_probe
from arm_common import JobStatus, MenuKind, TrackStatus
from arm_common.schemas import MenuExtract, RipStartResponse, TrackView
from arm_ripper.job_controller import MENU_EXTRACT_UNAVAILABLE, JobController

from tests import _disc_menu_builders as b
from tests.test_job_controller import FakeClient, _job, fast_polls, stub_eject, stub_scan  # noqa: F401


class _MenuClient(FakeClient):
    def __init__(self, refuse: bool = False) -> None:
        super().__init__()
        self.refuse = refuse
        self.patches: list[tuple[str, dict[str, object]]] = []

    async def rip_start(self, job_id: str) -> RipStartResponse:
        self.rip_start_calls.append(job_id)
        return RipStartResponse(
            job_id=job_id,
            rip_preset_id="rpr_builtin_movie_archive",
            tracks=[],
            extract_menu=MenuExtract(track_id="trk_menu"),
        )

    async def update_track(self, track_id: str, **fields: object) -> TrackView:
        self.patches.append((track_id, fields))
        if self.refuse:
            request = httpx.Request("PATCH", "https://x/api")
            raise httpx.HTTPStatusError("409", request=request, response=httpx.Response(409, request=request))
        return None  # type: ignore[return-value]


@pytest.mark.parametrize("refuse", [False, True])
async def test_the_menu_is_skipped_and_the_rip_goes_on(refuse: bool, stub_scan, stub_eject) -> None:  # noqa: F811
    client = _MenuClient(refuse)
    client.identify_responses.append(_job(JobStatus.IDENTIFIED, title="Test Movie"))

    await asyncio.wait_for(JobController(client, "drv_test").handle_disc_inserted("/dev/sr0"), timeout=2.0)

    assert client.patches == [("trk_menu", {"status": TrackStatus.SKIPPED, "last_error": MENU_EXTRACT_UNAVAILABLE})]
    assert client.rip_complete_calls == ["job_test"]


async def test_the_probe_reads_the_menu_kind_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def _kind(source: str) -> MenuKind:
        seen.append(source)
        return MenuKind.DVD

    async def _ready(_dev: str) -> bool:
        return True

    monkeypatch.setattr(disc_probe, "probe_menu_kind", _kind)
    monkeypatch.setattr(disc_probe, "await_device_ready", _ready)
    for name in ("_compute_crc", "probe_thediscdb_hash", "probe_matrix256"):
        monkeypatch.setattr(disc_probe, name, lambda _dev: None)

    assert (await disc_probe.probe_disc("/dev/sr0")).menu_kind is None
    assert (await disc_probe.probe_disc("/dev/sr0", menus=True)).menu_kind == MenuKind.DVD
    assert seen == ["/dev/sr0"]


async def test_an_unready_device_has_an_unknown_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _not_ready(_dev: str) -> bool:
        return False

    monkeypatch.setattr(disc_probe, "await_device_ready", _not_ready)
    assert (await disc_probe.probe_disc("/dev/sr0", menus=True)).menu_kind == MenuKind.UNKNOWN
    assert (await disc_probe.probe_disc("/dev/sr0")).menu_kind is None


async def test_a_folder_is_read_for_its_menu(tmp_path: Path) -> None:
    menus = {"en": [(3, b.pgc(cells=[(0, 0, 1.0)]))]}
    folder = b.write_files(tmp_path / "disc", {"VIDEO_TS/VIDEO_TS.IFO": b.vmg_ifo(nvts=0, menus=menus)})
    probe = await disc_probe.probe_disc(str(folder), menus=True)
    assert (probe.crc64, probe.menu_kind) == (None, MenuKind.DVD)
    assert (await disc_probe.probe_disc(str(folder))).menu_kind is None
