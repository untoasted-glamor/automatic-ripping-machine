"""HeartbeatPump: a refused heartbeat means the claim is gone; other failures retry."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import arm_transcode.heartbeat as heartbeat
from arm_transcode.heartbeat import HeartbeatPump, ProgressState


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("PATCH", "https://arm-backend:8443/api/transcoder/tasks/txt_1/heartbeat")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=httpx.Response(code, request=request))


class _FakeApi:
    def __init__(self, errors: list[Exception]) -> None:
        self._errors = list(errors)
        self.calls = 0

    async def heartbeat(self, task_id: str, **_kw: Any) -> None:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)


class _FakeWs:
    async def publish(self, **_kw: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat, "REST_HEARTBEAT_INTERVAL_SECONDS", 0.01)


@pytest.mark.parametrize("code", [403, 404, 409])
async def test_refused_heartbeat_sets_revoked_and_stops(code: int) -> None:
    api = _FakeApi([_status_error(code)])
    async with HeartbeatPump(api=api, ws=_FakeWs(), task_id="txt_1", state=ProgressState()) as pump:  # type: ignore[arg-type]
        await asyncio.wait_for(pump.revoked.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
    assert api.calls == 1


@pytest.mark.parametrize("error", [_status_error(500), httpx.ConnectError("Temporary failure in name resolution")])
async def test_other_heartbeat_failures_retry_without_revoking(error: Exception) -> None:
    api = _FakeApi([error])
    async with HeartbeatPump(api=api, ws=_FakeWs(), task_id="txt_1", state=ProgressState()) as pump:  # type: ignore[arg-type]
        while api.calls < 2:
            await asyncio.sleep(0.01)
        assert not pump.revoked.is_set()
