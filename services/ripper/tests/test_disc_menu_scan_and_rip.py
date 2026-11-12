"""The disc menu in the ripper (fork only, design v5 A1 and A2): the scan reads its kind, and the rip reports the
menu row's state to the backend, never stopping the rip over it."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

import arm_ripper.job_controller as jc_module
import arm_ripper.scan.disc_probe as disc_probe
from arm_common import JobStatus, MenuKind, TrackStatus
from arm_common.schemas import MenuExtract, RipStartResponse, TrackView
from arm_ripper.job_controller import MENU_NOT_COPIED, JobController
from arm_ripper.menus.extract import MenuOutcome

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


class _WS:
    def __init__(self) -> None:
        self.published: list[dict[str, object]] = []

    async def publish(self, *, topic: str, event_type: str, payload: dict[str, object]) -> None:
        self.published.append(payload)


def _fake_rip(monkeypatch: pytest.MonkeyPatch, outcome: MenuOutcome | None) -> list[object]:
    """rip_all as far as the menu goes: start it, report progress, and finish it with `outcome` (None: never)."""
    seen: list[object] = []

    async def fake_rip_all(**kw: object) -> None:
        menu = kw["menu"]
        seen.append(menu)
        assert menu is not None
        await menu.start()  # type: ignore[attr-defined]
        await menu.progress(0.5)  # type: ignore[attr-defined]
        if outcome is not None:
            await menu.finish(outcome)  # type: ignore[attr-defined]

    monkeypatch.setattr(jc_module, "rip_all", fake_rip_all)
    return seen


async def _rip(client: _MenuClient, ws: _WS | None = None) -> None:
    client.identify_responses.append(_job(JobStatus.IDENTIFIED, title="Test Movie"))
    await asyncio.wait_for(JobController(client, "drv_test", ws=ws).handle_disc_inserted("/dev/sr0"), timeout=2.0)  # type: ignore[arg-type]


async def test_the_menu_row_follows_the_copy(monkeypatch, stub_scan, stub_eject) -> None:  # noqa: F811
    _fake_rip(monkeypatch, MenuOutcome(TrackStatus.DONE, output_path=Path("/raw/job_test/menu"), size_bytes=264))
    client, ws = _MenuClient(), _WS()

    await _rip(client, ws)

    assert client.patches == [
        ("trk_menu", {"status": TrackStatus.IN_PROGRESS}),
        ("trk_menu", {"status": TrackStatus.DONE, "output_path": "/raw/job_test/menu", "size_bytes": 264}),
    ]
    assert ws.published == [{"track_id": "trk_menu", "progress_pct": 50.0}]
    assert client.rip_complete_calls == ["job_test"]


async def test_a_menu_the_rip_never_settled_fails_before_rip_complete(monkeypatch, stub_scan, stub_eject) -> None:  # noqa: F811
    _fake_rip(monkeypatch, None)
    client = _MenuClient()

    await _rip(client)

    assert client.patches[-1] == ("trk_menu", {"status": TrackStatus.FAILED, "last_error": MENU_NOT_COPIED})
    assert client.rip_complete_calls == ["job_test"]


async def test_a_refused_menu_patch_never_stops_the_rip(monkeypatch, stub_scan, stub_eject) -> None:  # noqa: F811
    _fake_rip(monkeypatch, MenuOutcome(TrackStatus.SKIPPED, error="Java menus can't be played"))
    client = _MenuClient(refuse=True)

    await _rip(client)

    assert [f["status"] for _, f in client.patches] == [TrackStatus.IN_PROGRESS, TrackStatus.SKIPPED]
    assert client.rip_complete_calls == ["job_test"]


async def test_no_menu_ticked_sends_none(monkeypatch, stub_scan, stub_eject) -> None:  # noqa: F811
    seen: list[object] = []

    async def fake_rip_all(**kw: object) -> None:
        seen.append(kw["menu"])

    monkeypatch.setattr(jc_module, "rip_all", fake_rip_all)
    client = FakeClient()
    client.identify_responses.append(_job(JobStatus.IDENTIFIED, title="Test Movie"))
    await asyncio.wait_for(JobController(client, "drv_test").handle_disc_inserted("/dev/sr0"), timeout=2.0)
    assert seen == [None]


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
