"""Heartbeat loop — REST every 30 s, WS publish every ~1 s.

The REST heartbeat is the durable surface the Backend's stale-claim
sweep reads (`claim_heartbeat_at < now() - 90s` triggers reset). The WS
publish is fire-and-forget telemetry the UI consumes for live progress
bars; the hub already throttles `transcode.progress.*` at 1 Hz so a
publisher tick rate slightly above 1 Hz is harmless.

A heartbeat the Backend refuses with 403/404/409 means our claim is gone
(the stale sweep re-queued the task, or it was cancelled or deleted). The
pump then sets `revoked` and stops; `main` stops the encode on it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from arm_transcode.api_client import BackendClient
from arm_transcode.ws_client import WSClient

logger = logging.getLogger("arm_transcode.heartbeat")

REST_HEARTBEAT_INTERVAL_SECONDS = 30.0
WS_PUBLISH_INTERVAL_SECONDS = 1.0

# Heartbeat refusals that mean this transcoder no longer owns the task.
_CLAIM_LOST_STATUSES = frozenset({403, 404, 409})


@dataclass
class ProgressState:
    pct: int = 0
    eta_seconds: int | None = None
    current_pass: str | None = None


class HeartbeatPump:
    """Background task that periodically reports `state.pct` to Backend."""

    def __init__(
        self,
        *,
        api: BackendClient,
        ws: WSClient,
        task_id: str,
        state: ProgressState,
    ) -> None:
        self._api = api
        self._ws = ws
        self._task_id = task_id
        self._state = state
        self._stop = asyncio.Event()
        self.revoked = asyncio.Event()
        self._rest_task: asyncio.Task[None] | None = None
        self._ws_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "HeartbeatPump":
        self._rest_task = asyncio.create_task(self._rest_loop(), name="hb-rest")
        self._ws_task = asyncio.create_task(self._ws_loop(), name="hb-ws")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop.set()
        for t in (self._rest_task, self._ws_task):
            if t is None:
                continue
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except asyncio.TimeoutError:
                t.cancel()

    async def _rest_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=REST_HEARTBEAT_INTERVAL_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._api.heartbeat(
                    self._task_id,
                    progress_pct=self._state.pct,
                    current_pass=self._state.current_pass,
                    eta_seconds=self._state.eta_seconds,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in _CLAIM_LOST_STATUSES:
                    logger.warning(
                        "claim revoked by Backend (HTTP %d); stopping the encode",
                        exc.response.status_code,
                    )
                    self.revoked.set()
                    return
                logger.warning("heartbeat REST failed (will retry): %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("heartbeat REST failed (will retry): %s", exc)

    async def _ws_loop(self) -> None:
        last_pct = -1
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=WS_PUBLISH_INTERVAL_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            if self._state.pct == last_pct:
                continue
            last_pct = self._state.pct
            await self._ws.publish(
                topic=f"transcode.progress.{self._task_id}",
                event_type="transcode.progress",
                payload={
                    "task_id": self._task_id,
                    "progress_pct": self._state.pct,
                    "eta_seconds": self._state.eta_seconds,
                    "current_pass": self._state.current_pass,
                },
            )
