"""`job_log_context`: a request's log lines carry its job, and only for that request."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from arm_backend.log_context import job_log_context
from arm_common import JsonFormatter, bind_log_context

logger = logging.getLogger("arm_backend.tests.log_context")


def _app() -> FastAPI:
    router = APIRouter(dependencies=[Depends(job_log_context)])

    @router.get("/jobs/{job_id}")
    async def by_path(job_id: str) -> None:
        logger.info("by-path")

    @router.post("/created")
    async def created() -> None:
        logger.info("before-bind")
        bind_log_context(job_id="job_created")
        logger.info("after-bind")

    @router.get("/sync/{job_id}")
    def sync_handler(job_id: str) -> None:
        logger.info("sync")

    @router.get("/plain")
    async def plain() -> None:
        logger.info("plain")

    app = FastAPI()
    app.include_router(router)
    return app


def _job_ids(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    rows = [json.loads(line) for line in caplog.text.splitlines()]
    return {row["msg"]: row["job_id"] for row in rows if row["extra"]["logger"] == logger.name}


def test_request_lines_carry_the_job(caplog: pytest.LogCaptureFixture) -> None:
    caplog.handler.setFormatter(JsonFormatter("test"))
    caplog.set_level(logging.INFO, logger="arm_backend.tests.log_context")
    with TestClient(_app()) as client:
        client.get("/jobs/job_path")
        client.post("/created")
        client.get("/sync/job_sync")
        client.get("/plain")

    assert _job_ids(caplog) == {
        "by-path": "job_path",
        "before-bind": None,
        "after-bind": "job_created",
        "sync": "job_sync",
        # The bind in /created ended with its request.
        "plain": None,
    }
