"""Tests for the /dev/sgN passthrough check.

Reproduces the 2026-10-16 incident: the Blu-ray drive moved to a new USB
bridge, the kernel re-paired sr0 with sg6, but the compose file still mapped
sg4 (by then a WSL virtual disk). MakeMKV saw no drive, every scan returned
0 titles, and the job fell back to a raw dd data copy.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import arm_ripper.sg_passthrough as sgp
from arm_ripper.sg_passthrough import check_sg_passthrough, log_sg_passthrough


def _fake_sysfs(root: Path, *, paired: list[str], sg_types: dict[str, tuple[str, str, str]]) -> Path:
    """Build /sys/block/sr0/device/scsi_generic/<paired> and
    /sys/class/scsi_generic/<sg>/device/{type,vendor,model}."""
    sys_root = root / "sys"
    pair_dir = sys_root / "block" / "sr0" / "device" / "scsi_generic"
    pair_dir.mkdir(parents=True)
    for sg in paired:
        (pair_dir / sg).mkdir()
    for sg, (scsi_type, vendor, model) in sg_types.items():
        dev = sys_root / "class" / "scsi_generic" / sg / "device"
        dev.mkdir(parents=True)
        (dev / "type").write_text(f"{scsi_type}\n")
        (dev / "vendor").write_text(f"{vendor}\n")
        (dev / "model").write_text(f"{model}\n")
    return sys_root


def _fake_dev(root: Path, nodes: list[str]) -> Path:
    dev_root = root / "dev"
    dev_root.mkdir()
    for n in nodes:
        (dev_root / n).touch()
    return dev_root


_TYPES = {
    "sg4": ("0", "Msft", "Virtual Disk"),
    "sg6": ("5", "HL-DT-ST", "BD-RE BU40N"),
}


def test_stale_mapping_reports_missing_pair_and_wrong_device(tmp_path: Path) -> None:
    sys_root = _fake_sysfs(tmp_path, paired=["sg6"], sg_types=_TYPES)
    dev_root = _fake_dev(tmp_path, ["sr0", "sg4"])

    check = check_sg_passthrough("/dev/sr0", sys_root=sys_root, dev_root=dev_root)

    assert check.verified
    assert check.paired == ["sg6"]
    assert len(check.problems) == 2
    missing, stale = check.problems
    assert "/dev/sg6" in missing and "not mapped into this container" in missing
    assert "force-recreate arm-ripper-sr0" in missing
    assert "/dev/sg4" in stale and "not an optical drive" in stale and "Msft Virtual Disk" in stale


def test_correct_mapping_has_no_problems(tmp_path: Path) -> None:
    sys_root = _fake_sysfs(tmp_path, paired=["sg6"], sg_types=_TYPES)
    dev_root = _fake_dev(tmp_path, ["sr0", "sg6"])

    check = check_sg_passthrough("/dev/sr0", sys_root=sys_root, dev_root=dev_root)

    assert check.verified
    assert check.problems == []


def test_other_optical_drive_mapped_in_is_flagged(tmp_path: Path) -> None:
    types = {**_TYPES, "sg2": ("5", "MediaTek", "Slim Portable DVD")}
    sys_root = _fake_sysfs(tmp_path, paired=["sg6"], sg_types=types)
    dev_root = _fake_dev(tmp_path, ["sr0", "sg6", "sg2"])

    check = check_sg_passthrough("/dev/sr0", sys_root=sys_root, dev_root=dev_root)

    assert len(check.problems) == 1
    assert "/dev/sg2" in check.problems[0] and "a different optical drive" in check.problems[0]


def test_unreadable_paired_node_is_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sys_root = _fake_sysfs(tmp_path, paired=["sg6"], sg_types=_TYPES)
    dev_root = _fake_dev(tmp_path, ["sr0", "sg6"])
    monkeypatch.setattr(sgp.os, "access", lambda *_a, **_k: False)

    check = check_sg_passthrough("/dev/sr0", sys_root=sys_root, dev_root=dev_root)

    assert len(check.problems) == 1
    assert "not read/writable" in check.problems[0]


def test_no_sysfs_entry_is_unverified_not_a_problem(tmp_path: Path) -> None:
    """ISO mode / no sysfs: can't tell, so say nothing rather than guess."""
    check = check_sg_passthrough("/dev/sr0", sys_root=tmp_path / "nosys", dev_root=_fake_dev(tmp_path, []))

    assert not check.verified
    assert check.problems == []


def test_log_sg_passthrough_logs_error_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sys_root = _fake_sysfs(tmp_path, paired=["sg6"], sg_types=_TYPES)
    dev_root = _fake_dev(tmp_path, ["sr0", "sg4"])
    real = sgp.check_sg_passthrough
    monkeypatch.setattr(
        sgp, "check_sg_passthrough", lambda p: real(p, sys_root=sys_root, dev_root=dev_root)
    )

    with caplog.at_level(logging.INFO, logger="arm_ripper.sg_passthrough"):
        log_sg_passthrough("/dev/sr0")

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2
    assert all("SG_PASSTHROUGH_MISMATCH" in r.getMessage() for r in errors)
