"""When a review stop may start on its own (timed review gate; fork: both stops).

A disc can stop twice: at `awaiting_review` before its backup (stop 1, the
ripper waits) and at `awaiting_titles` before its titles are remuxed from that
backup (stop 2, the backend's `sweep_choose_titles` waits). Both read the same
settings and job fields through this one rule, so they can't drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class StopSettings(Protocol):
    """The settings the rule reads: `Config`, or the ripper's `RipperConfigView`."""

    @property
    def hold_for_review(self) -> bool: ...
    @property
    def ripping_paused(self) -> bool: ...
    @property
    def review_requires_confirm(self) -> bool: ...
    @property
    def manual_wait_seconds(self) -> int: ...


class StoppedJob(Protocol):
    """The job fields the rule reads: `Job`, or a `JobView`."""

    @property
    def manual_pause(self) -> bool: ...
    @property
    def wait_start_time(self) -> datetime | None: ...


def stop_may_start(config: StopSettings, job: StoppedJob, now: datetime) -> bool:
    """True when a disc held at a review stop may go on without a click.

    - Paused (global `ripping_paused`, or the job's `manual_pause`): wait.
    - `hold_for_review` off: go now.
    - `review_requires_confirm`: wait for the operator's click.
    - Otherwise go once `manual_wait_seconds` have passed since `wait_start_time`
      (no `wait_start_time`: the stop hasn't opened, so wait).
    """
    if config.ripping_paused or job.manual_pause:
        return False
    if not config.hold_for_review:
        return True
    if config.review_requires_confirm or job.wait_start_time is None:
        return False
    started = job.wait_start_time
    if started.tzinfo is None:
        # SQLite (tests) hands back naive datetimes; they're UTC.
        started = started.replace(tzinfo=timezone.utc)
    return (now - started).total_seconds() >= (config.manual_wait_seconds or 0)
