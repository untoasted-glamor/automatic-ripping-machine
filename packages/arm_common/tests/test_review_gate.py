from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from arm_common.review_gate import stop_may_start

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _cfg(**kw: object) -> SimpleNamespace:
    base = {
        "hold_for_review": True,
        "ripping_paused": False,
        "review_requires_confirm": False,
        "manual_wait_seconds": 60,
    }
    return SimpleNamespace(**{**base, **kw})


def _job(*, pause: bool = False, opened: datetime | None = NOW - timedelta(seconds=61)) -> SimpleNamespace:
    return SimpleNamespace(manual_pause=pause, wait_start_time=opened)


def test_the_countdown_starts_it_once_elapsed() -> None:
    assert stop_may_start(_cfg(), _job(), NOW)
    assert not stop_may_start(_cfg(), _job(opened=NOW - timedelta(seconds=59)), NOW)
    assert not stop_may_start(_cfg(), _job(opened=None), NOW)  # not open yet


def test_a_naive_open_time_reads_as_utc() -> None:
    assert stop_may_start(_cfg(), _job(opened=(NOW - timedelta(seconds=61)).replace(tzinfo=None)), NOW)


def test_confirm_mode_waits_for_the_click() -> None:
    assert not stop_may_start(_cfg(review_requires_confirm=True), _job(), NOW)


def test_hold_off_goes_at_once() -> None:
    assert stop_may_start(_cfg(hold_for_review=False, review_requires_confirm=True), _job(opened=None), NOW)


@pytest.mark.parametrize(("cfg", "job"), [(_cfg(ripping_paused=True), _job()), (_cfg(), _job(pause=True))])
def test_a_pause_holds_either_stop_even_with_hold_off(cfg: SimpleNamespace, job: SimpleNamespace) -> None:
    assert not stop_may_start(cfg, job, NOW)
    cfg.hold_for_review = False
    assert not stop_may_start(cfg, job, NOW)
