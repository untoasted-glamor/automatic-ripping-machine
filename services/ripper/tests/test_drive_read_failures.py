"""Edge-triggered logging for drive-status read failures.

The dev rig hit this for real: the USB drive was detached and re-attached with
usbipd, came back as host /dev/sr1, and the ripper container kept the /dev/sr0
node Docker copied in at create time. The poll loop then logged "ioctl failed:
[Errno 6] No such device or address" every 2s forever — the wrong call (it is
the open() that fails), no remediation, and at a volume that buried everything
else in the log.
"""

import os

os.environ.setdefault("ARM_DRIVE_DEV", "/dev/sr0")
os.environ.setdefault("ARM_BACKEND_URL", "https://backend")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok")

import asyncio
import errno
import logging

import pytest
from arm_ripper import main as ripper_main
from arm_ripper.main import DriveReadFailureTracker


def _oserror(err: int) -> OSError:
    return OSError(err, os.strerror(err))


class TestClassification:
    @pytest.mark.parametrize("err", [errno.ENXIO, errno.ENODEV, errno.ENOENT])
    def test_missing_device_errnos(self, err):
        assert DriveReadFailureTracker.is_device_missing(_oserror(err)) is True

    @pytest.mark.parametrize("err", [errno.EBUSY, errno.EACCES, errno.EIO, errno.ENOTTY])
    def test_other_errnos_are_not_missing_device(self, err):
        """Busy / permission / unsupported-ioctl failures must not be reported as
        a vanished device — they need a different fix from 'recreate the container'."""
        assert DriveReadFailureTracker.is_device_missing(_oserror(err)) is False


class TestEdgeLogging:
    def test_missing_device_logs_one_error_with_remediation(self, caplog):
        tracker = DriveReadFailureTracker("/dev/sr0")
        with caplog.at_level(logging.INFO, logger="arm_ripper"):
            for _ in range(20):
                tracker.record_failure(_oserror(errno.ENXIO))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, "a persistent outage must log once, not once per poll"
        msg = errors[0].getMessage()
        assert "/dev/sr0" in msg
        # Names the *device* basename so multi-drive rigs get the right container.
        assert "--force-recreate arm-ripper-sr0" in msg
        assert "usbipd attach" in msg
        # read_drive_status does both the open() and the ioctl, and either can
        # raise these errnos, so the message must not blame one specific call.
        assert "cannot read drive status" in msg
        assert "ioctl failed" not in msg
        assert "open failed" not in msg

    def test_other_failures_log_a_warning_not_an_error(self, caplog):
        """A drive held busy mid-rip is normal, so it must not raise an ERROR."""
        tracker = DriveReadFailureTracker("/dev/sr0")
        with caplog.at_level(logging.INFO, logger="arm_ripper"):
            for _ in range(5):
                tracker.record_failure(_oserror(errno.EBUSY))

        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert "status read failed" in caplog.records[0].getMessage()

    def test_transient_failure_does_not_suppress_a_later_missing_device(self, caplog):
        """The rig's real sequence: makemkv holds the drive (EBUSY), then the
        operator re-attaches it and the kernel hands it back as a different srN
        (ENXIO forever). A single latch swallowed the ERROR here - and because
        the node never returns on its own, record_success never cleared it, so
        the remediation was lost for the life of the container.
        """
        caplog.set_level(logging.INFO, logger="arm_ripper")
        tracker = DriveReadFailureTracker("/dev/sr0")
        tracker.record_failure(_oserror(errno.EBUSY))
        caplog.clear()

        for _ in range(10):
            tracker.record_failure(_oserror(errno.ENXIO))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1, "a vanished device must be reported even after a transient failure"
        assert "--force-recreate arm-ripper-sr0" in errors[0].getMessage()

    def test_flapping_errno_classes_stay_capped_at_one_line_each(self, caplog):
        """Alternating classes must not reopen the spam the edge logging exists
        to stop: at most one ERROR and one WARNING per outage."""
        caplog.set_level(logging.INFO, logger="arm_ripper")
        tracker = DriveReadFailureTracker("/dev/sr0")
        for _ in range(10):
            tracker.record_failure(_oserror(errno.ENXIO))
            tracker.record_failure(_oserror(errno.EBUSY))

        assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    def test_recovery_logs_once_with_the_failed_poll_count(self, caplog):
        caplog.set_level(logging.INFO, logger="arm_ripper")
        tracker = DriveReadFailureTracker("/dev/sr0")
        for _ in range(3):
            tracker.record_failure(_oserror(errno.ENXIO))
        caplog.clear()  # drop the outage ERROR; this case is about the recovery line

        tracker.record_success()
        tracker.record_success()  # steady state: must stay silent

        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "recovered after 3 failed poll(s)" in msg
        assert "/dev/sr0" in msg

    def test_success_before_any_failure_is_silent(self, caplog):
        tracker = DriveReadFailureTracker("/dev/sr0")
        with caplog.at_level(logging.INFO, logger="arm_ripper"):
            tracker.record_success()
        assert caplog.records == []

    def test_second_outage_after_recovery_logs_again(self, caplog):
        """The edge must re-arm, or a drive that flaps goes silent after the first
        outage and the operator never learns about the later ones."""
        caplog.set_level(logging.INFO, logger="arm_ripper")
        tracker = DriveReadFailureTracker("/dev/sr0")
        tracker.record_failure(_oserror(errno.ENXIO))
        tracker.record_success()
        caplog.clear()  # only the *second* outage's output is under test here

        tracker.record_failure(_oserror(errno.ENXIO))

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1


class TestPollLoopWiring:
    @pytest.mark.asyncio
    async def test_poll_loop_reports_a_missing_device_once_then_recovers(self, caplog, monkeypatch):
        """End-to-end through poll_loop: many failing polls produce one ERROR, and
        the first good read produces the recovery INFO."""
        from arm_ripper.drive_poll import DriveState

        polls = 0

        def fake_read(device_path):
            nonlocal polls
            polls += 1
            if polls <= 5:
                raise _oserror(errno.ENXIO)
            return DriveState.NO_DISC

        monkeypatch.setattr(ripper_main, "read_drive_status", fake_read)
        monkeypatch.setattr(ripper_main.settings, "POLL_INTERVAL_SECONDS", 0)

        with caplog.at_level(logging.INFO, logger="arm_ripper"):
            # NO_DISC never fires the insert detector, so the controller is unused.
            task = asyncio.create_task(ripper_main.poll_loop(controller=object()))
            while polls < 8:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "--force-recreate arm-ripper-sr0" in errors[0].getMessage()

        recoveries = [r for r in caplog.records if "recovered after" in r.getMessage()]
        assert len(recoveries) == 1
        assert "recovered after 5 failed poll(s)" in recoveries[0].getMessage()
