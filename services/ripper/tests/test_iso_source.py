"""ARM_MANUAL_TRIGGER_ISO mode tests.

Covers the five code paths the iso-source helper flips:

  - source.is_iso_source / source.makemkv_source_url helpers
  - scan_disc: makemkvcon info reads the `.iso`, not the drive
  - rip_disc: makemkvcon mkv rips the `.iso`, not the drive
  - _eject_with_retry: skipped for ISO sources
  - heartbeat_loop: probe_drive_media is bypassed for ISO sources
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# arm_ripper.config builds a pydantic Settings at import time and refuses
# to load without these vars. Set placeholders before any arm_ripper.*
# import so importing main.py (for the heartbeat test) doesn't blow up.
os.environ.setdefault("ARM_DRIVE_DEV", "/dev/sr0")
os.environ.setdefault("ARM_BACKEND_URL", "https://backend.invalid")
os.environ.setdefault("ARM_SERVICE_TOKEN", "test-token")

import arm_ripper.main as ripper_main  # noqa: E402
import arm_ripper.rip.makemkv_rip as makemkv_rip  # noqa: E402
import arm_ripper.scan.disc_probe as disc_probe  # noqa: E402
import arm_ripper.scan.makemkv as scan_makemkv  # noqa: E402
from arm_common import DriveMediaStatus  # noqa: E402
from arm_ripper.source import is_iso_source, makemkv_source_url  # noqa: E402
from tests._makemkvcon_fake import HANAKO_BD, SINTEL_ISO, install  # noqa: E402


# --- source helpers --------------------------------------------------------


def test_is_iso_source_true_for_real_iso_file(tmp_path: Path) -> None:
    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")
    assert is_iso_source(str(iso)) is True


def test_is_iso_source_false_for_dev_node() -> None:
    # `/dev/sr0` always exists on a host with an optical drive, but it's
    # a block device, not a regular file — Path.is_file() returns False.
    assert is_iso_source("/dev/sr0") is False


def test_is_iso_source_false_for_iso_suffix_with_missing_file(tmp_path: Path) -> None:
    # Suffix matches but file is absent — must not classify as ISO.
    assert is_iso_source(str(tmp_path / "ghost.iso")) is False


def test_is_iso_source_false_for_uppercase_suffix(tmp_path: Path) -> None:
    # Helper is intentionally lower-case-only to avoid widening detection
    # by accident. Document it in the test.
    iso = tmp_path / "SINTEL.ISO"
    iso.write_bytes(b"")
    assert is_iso_source(str(iso)) is False


def test_is_iso_source_false_for_directory(tmp_path: Path) -> None:
    d = tmp_path / "sintel.iso"
    d.mkdir()
    assert is_iso_source(str(d)) is False


def test_makemkv_source_url_iso_for_file(tmp_path: Path) -> None:
    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")
    assert makemkv_source_url(str(iso)) == f"iso:{iso}"


def test_makemkv_source_url_dev_for_device() -> None:
    assert makemkv_source_url("/dev/sr0") == "dev:/dev/sr0"


# --- scan_disc / rip_disc read the right source ----------------------------
#
# Against the recorded-output makemkvcon fake, with the Hanako-kun BD seated
# in /dev/sr0 and the Sintel DVD as the ISO: what comes back says which
# source was read, and the fake records whether the drive was.


def _fake_with_both(monkeypatch, tmp_path: Path) -> tuple[Any, str]:
    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")
    fake = install(monkeypatch, disc=HANAKO_BD, isos={str(iso): SINTEL_ISO})
    # Skip the real probe_disc — it would read the disc device.
    monkeypatch.setattr(
        scan_makemkv,
        "probe_disc",
        AsyncMock(return_value=disc_probe.DiscProbe(crc64=None)),
    )
    return fake, str(iso)


@pytest.mark.asyncio
async def test_scan_disc_reads_the_iso_not_the_drive(monkeypatch, tmp_path: Path) -> None:
    fake, iso = _fake_with_both(monkeypatch, tmp_path)

    scan = await scan_makemkv.scan_disc(iso)

    assert scan.volume_label == "Sintel_NTSC"
    assert len(scan.titles) == 5
    assert fake.drive_reads == []


@pytest.mark.asyncio
async def test_scan_disc_reads_the_drive_for_a_block_device(monkeypatch, tmp_path: Path) -> None:
    fake, _ = _fake_with_both(monkeypatch, tmp_path)

    scan = await scan_makemkv.scan_disc("/dev/sr0")

    assert scan.volume_label == "ToiletBound HanakoKun D1"
    assert len(scan.titles) == 8
    assert len(fake.drive_reads) == 1


@pytest.mark.asyncio
async def test_rip_disc_rips_the_iso_not_the_drive(monkeypatch, tmp_path: Path) -> None:
    fake, iso = _fake_with_both(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"

    await makemkv_rip.rip_disc(
        device_path=iso,
        output_dir=output_dir,
        minlength_seconds=600,
        eligible_source_indexes=[],
    )

    assert sorted(p.name for p in output_dir.iterdir()) == ["A1_t01.mkv", "B1_t02.mkv", "C1_t00.mkv"]
    assert fake.drive_reads == []


@pytest.mark.asyncio
async def test_rip_disc_rips_the_drive_for_a_block_device(monkeypatch, tmp_path: Path) -> None:
    fake, _ = _fake_with_both(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"

    await makemkv_rip.rip_disc(
        device_path="/dev/sr0",
        output_dir=output_dir,
        minlength_seconds=600,
        eligible_source_indexes=[],
    )

    assert len(list(output_dir.glob("ToiletBound HanakoKun D1_t*.mkv"))) == 8
    assert len(fake.drive_reads) == 1


# --- eject skip ------------------------------------------------------------


@pytest.mark.asyncio
async def test_eject_with_retry_skips_for_iso_source(tmp_path: Path) -> None:
    from arm_ripper.job_controller import JobController

    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")

    controller = JobController(
        client=MagicMock(),
        drive_id="drv_iso",
        ws=MagicMock(),
        device_path=str(iso),
        default_min_length_seconds=120,
    )
    # If _run_command got called, it'd shell `umount` / `eject`. The test
    # would not crash (the AsyncMock would absorb it), but we want to
    # *assert* no shell-out happened at all.
    controller._run_command = AsyncMock()  # type: ignore[method-assign]

    await controller._eject_with_retry(str(iso))

    controller._run_command.assert_not_called()


@pytest.mark.asyncio
async def test_eject_with_retry_runs_for_block_device(monkeypatch) -> None:
    from arm_ripper.job_controller import JobController

    controller = JobController(
        client=MagicMock(),
        drive_id="drv_sr0",
        ws=MagicMock(),
        device_path="/dev/sr0",
        default_min_length_seconds=120,
    )
    # Short-circuit the retry delays so the test runs fast.
    monkeypatch.setattr("arm_ripper.job_controller.EJECT_RETRY_DELAYS", [0])
    controller._run_command = AsyncMock(return_value=(0, ""))  # type: ignore[method-assign]

    await controller._eject_with_retry("/dev/sr0")

    # umount first, then eject — both fired.
    called_argvs = [call.args for call in controller._run_command.call_args_list]
    assert any("umount" in argv for argv in called_argvs)
    assert any("eject" in argv for argv in called_argvs)


# --- heartbeat ISO short-circuit ------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_short_circuits_probe_for_iso_source(monkeypatch, tmp_path: Path) -> None:
    iso = tmp_path / "sintel.iso"
    iso.write_bytes(b"")
    probe_calls: list[str] = []

    def fake_probe(path: str) -> tuple[DriveMediaStatus, str]:
        probe_calls.append(path)
        return DriveMediaStatus.UNKNOWN, "should-not-be-called"

    monkeypatch.setattr(ripper_main, "probe_drive_media", fake_probe)

    client = MagicMock()
    client.heartbeat = AsyncMock()

    # Idle controller stub — is_idle() returns True so maybe_reacquire_current_job
    # would call get_current_job, but get_current_job is not set on the MagicMock
    # here; we patch it to return None so no pickup happens.
    controller = MagicMock()
    controller.is_idle = MagicMock(return_value=True)
    controller.pickup = AsyncMock()
    client.get_current_job = AsyncMock(return_value=None)

    # Cancel after one tick so the test doesn't hang on the loop.
    async def cancel_after_one_tick() -> None:
        await asyncio.sleep(0)
        task.cancel()

    task = asyncio.create_task(ripper_main.heartbeat_loop(client, "drv_iso", str(iso), controller))
    asyncio.create_task(cancel_after_one_tick())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert probe_calls == [], "probe_drive_media must not be called for ISO sources"
    client.heartbeat.assert_called_once()
    assert client.heartbeat.call_args.kwargs["media_status"] == DriveMediaStatus.LOADED
