"""Verify the SCSI-generic (`/dev/sgN`) passthrough for the bound optical drive.

MakeMKV drives the disc through SCSI-generic ioctls, not the `/dev/srN` block
device, so the container also needs the sg node the kernel pairs with srN.
That pairing is not stable: it follows SCSI attach order, so attaching the
drive through a different USB bridge (or after WSL adds a virtual disk) can
move sr0 from sg4 to sg6. The compose file still maps the old node, MakeMKV
sees no drive at all, every scan returns 0 titles, and the only symptom used
to be the generic DISC_UNREADABLE_AFTER_RETRIES give-up.

Sysfs is the source of truth here: `/sys/block/srN/device/scsi_generic/`
names the paired sg node(s), and `/sys/class/scsi_generic/sgM/device/type`
says what each sg node actually is (5 = CD/DVD). Docker mounts /sys
read-only into the container, which is all this needs.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("arm_ripper.sg_passthrough")

# SCSI peripheral device type for CD/DVD/BD (MMC) devices.
_SCSI_TYPE_CDROM = "5"


@dataclass
class SgCheck:
    # sg node names (e.g. ["sg6"]) sysfs pairs with the drive; empty when
    # sysfs couldn't tell us (then `verified` is False).
    paired: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    verified: bool = False


def _read_attr(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _describe_sg(sys_root: Path, sg: str) -> tuple[str | None, str]:
    dev = sys_root / "class" / "scsi_generic" / sg / "device"
    scsi_type = _read_attr(dev / "type")
    name = " ".join(filter(None, (_read_attr(dev / "vendor"), _read_attr(dev / "model")))) or "unknown device"
    return scsi_type, name


def check_sg_passthrough(
    device_path: str,
    *,
    sys_root: Path = Path("/sys"),
    dev_root: Path = Path("/dev"),
) -> SgCheck:
    """Check that the sg node paired with `device_path` is present and usable
    in this container, and that no other sg node mapped in is a non-optical
    device (a stale mapping left over from an older pairing).

    Never raises; problems come back as operator-facing strings.
    """
    result = SgCheck()
    block = Path(os.path.realpath(device_path)).name
    paired_dir = sys_root / "block" / block / "device" / "scsi_generic"
    try:
        result.paired = sorted(p.name for p in paired_dir.iterdir())
    except OSError:
        return result
    result.verified = True

    remedy = (
        f"re-run devtools/setup-dev.sh to regenerate the compose device lines, then "
        f"`docker compose up -d --force-recreate arm-ripper-{block}` (a plain restart keeps the old mapping)"
    )

    for sg in result.paired:
        node = dev_root / sg
        if not node.exists():
            result.problems.append(
                f"/dev/{sg} is the SCSI-generic node the kernel pairs with {device_path}, but it is not "
                f"mapped into this container — MakeMKV cannot reach the drive and every scan will return "
                f"0 titles; {remedy}"
            )
        elif not os.access(node, os.R_OK | os.W_OK):
            result.problems.append(
                f"/dev/{sg} (paired with {device_path}) is mapped in but not read/writable by uid "
                f"{os.getuid()} — MakeMKV cannot send SCSI commands to the drive; check the container's "
                f"cdrom group membership"
            )

    try:
        mapped = sorted(p.name for p in dev_root.glob("sg*") if p.name[2:].isdigit())
    except OSError:
        mapped = []
    for sg in mapped:
        if sg in result.paired:
            continue
        scsi_type, name = _describe_sg(sys_root, sg)
        if scsi_type == _SCSI_TYPE_CDROM:
            what = f"a different optical drive ({name})"
        else:
            what = f"not an optical drive (SCSI type {scsi_type or '?'}, {name})"
        result.problems.append(
            f"/dev/{sg} is mapped into this container but is {what}, not the drive paired with "
            f"{device_path} — a stale device mapping, and it gives the ripper raw access to that device; {remedy}"
        )

    return result


def log_sg_passthrough(device_path: str) -> SgCheck:
    """Run the check and log the outcome: one ERROR per problem, else INFO."""
    check = check_sg_passthrough(device_path)
    if not check.verified:
        logger.info("sg passthrough: cannot verify for %s (no sysfs scsi_generic entry)", device_path)
    elif check.problems:
        for problem in check.problems:
            logger.error("SG_PASSTHROUGH_MISMATCH %s", problem)
    else:
        logger.info("sg passthrough ok: %s <-> %s", device_path, ", ".join(f"/dev/{s}" for s in check.paired))
    return check
