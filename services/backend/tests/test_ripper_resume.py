"""Phase 9 — `POST /api/ripper/jobs/{id}/resume` and
`GET /api/ripper/drives/{id}/in-flight-job` end-to-end."""

from __future__ import annotations

import os
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from arm_backend.db import get_session  # noqa: E402
from arm_backend.routers import ripper as ripper_router  # noqa: E402
from arm_common import (  # noqa: E402
    DiscType,
    Drive,
    DriveStatus,
    Job,
    JobStatus,
    Track,
    TrackKind,
    TrackStatus,
)

from tests._fakes import FakeSession  # noqa: E402


class _CapturingHub:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(
        self,
        topic: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        persist: bool = True,
        job_id: str | None = None,
        track_id: str | None = None,
        session: Any = None,
    ) -> None:
        self.events.append({"topic": topic, "event_type": event_type, "payload": payload})


def _seed(db: FakeSession, *, job_status: JobStatus = JobStatus.RIPPING) -> None:
    db.rows["drives"] = [
        Drive(
            id="drv_x",
            hostname="ripper-host",
            device_path="/dev/sr0",
            status=DriveStatus.ONLINE,
        )
    ]
    db.rows["jobs"] = [
        Job(
            id="job_01JZXR7K3M5Q8N4VWA00000001",
            drive_id="drv_x",
            disc_type=DiscType.DVD,
            title="Iron Man",
            year=2008,
            status=job_status,
            metadata_json={},
            resumed_from_crash=False,
        )
    ]
    db.rows["tracks"] = [
        Track(
            id="trk_1",
            job_id="job_01JZXR7K3M5Q8N4VWA00000001",
            kind=TrackKind.VIDEO_TITLE,
            index=1,
            source_ref="1",
            status=TrackStatus.IN_PROGRESS,
            attempts=1,
        )
    ]


def _make_app(db: FakeSession, *, hub: _CapturingHub | None = None) -> FastAPI:
    app = FastAPI()
    app.state.ws_hub = hub
    app.include_router(ripper_router.router)

    async def _override_session() -> FakeSession:
        return db

    app.dependency_overrides[get_session] = _override_session
    return app


def _service_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer tok-service",
        "X-ARM-Hostname": "ripper-host",
    }


# ---------- /resume ----------


def test_resume_happy_path_resets_and_emits() -> None:
    db = FakeSession()
    _seed(db)
    hub = _CapturingHub()
    app = _make_app(db, hub=hub)
    with TestClient(app) as client:
        r = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume", headers=_service_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] == "job_01JZXR7K3M5Q8N4VWA00000001"
    assert len(body["tracks"]) == 1
    assert body["tracks"][0]["status"] == "queued"
    assert body["tracks"][0]["attempts"] == 2  # 1 → 2
    job = db.rows["jobs"][0]
    assert job.resumed_from_crash is True
    resumed = [e for e in hub.events if e["event_type"] == "rip.resumed"]
    assert len(resumed) == 1
    assert resumed[0]["payload"]["job_id"] == "job_01JZXR7K3M5Q8N4VWA00000001"
    assert resumed[0]["payload"]["resumed_from_crash"] is True


def test_resume_idempotent_does_not_inflate_attempts() -> None:
    db = FakeSession()
    _seed(db)
    # First resume.
    app = _make_app(db, hub=_CapturingHub())
    with TestClient(app) as client:
        first = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume", headers=_service_headers())
    assert first.status_code == 200
    track_after_first = db.rows["tracks"][0]
    assert track_after_first.attempts == 2

    # Second resume — track is now QUEUED already, attempts must not increment.
    app2 = _make_app(db, hub=_CapturingHub())
    with TestClient(app2) as client:
        second = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume", headers=_service_headers())
    assert second.status_code == 200
    assert db.rows["tracks"][0].attempts == 2


def test_resume_resolves_routed_session_exactly_once() -> None:
    """Fix 75-8 regression: resume must resolve the routed session ONE time
    per request, mirroring rip-start. Before the fix, resolve_rip_preset_id_for_job
    and _resolve_min_length_override each independently called
    resolve_routed_session_id."""
    db = FakeSession()
    _seed(db)

    calls: list[str] = []
    orig = ripper_router.resolve_routed_session_id

    async def _counting(db_arg: Any, job_arg: Any) -> Any:
        calls.append(job_arg.id)
        return await orig(db_arg, job_arg)

    ripper_router.resolve_routed_session_id = _counting  # type: ignore[assignment]
    try:
        app = _make_app(db, hub=_CapturingHub())
        with TestClient(app) as client:
            r = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume", headers=_service_headers())
    finally:
        ripper_router.resolve_routed_session_id = orig  # type: ignore[assignment]

    assert r.status_code == 200, r.text
    assert len(calls) == 1


def test_resume_returns_scan_titles_and_excluded_tracks() -> None:
    """A resumed rip gets the same scan detail as rip-start, and the excluded
    track is still in `tracks`, flagged, so the ripper re-runs the same
    strategy and skips it again."""
    dvd_keys: dict[str, Any] = {"source_playlist": None, "segment_count": 1, "segment_map": "1-7", "streams": []}
    titles: list[dict[str, Any]] = [
        {
            "index": 1,
            "duration_seconds": 4200,
            "chapter_count": 24,
            "size_bytes": 30_000_000_000,
            "source_file": "t00",
            "original_title_id": 1,
            **dvd_keys,
        },
        {
            "index": 2,
            "duration_seconds": 300,
            "chapter_count": 1,
            "size_bytes": 400_000_000,
            "source_file": "t01",
            "original_title_id": 4,
            **dvd_keys,
        },
    ]
    db = FakeSession()
    _seed(db)
    db.rows["jobs"][0].metadata_json = {
        "scan_result": {"disc_type": "dvd", "volume_label": "IRON_MAN", "titles": titles, "fingerprints": [], "raw": {}}
    }
    db.rows["tracks"].append(
        Track(
            id="trk_2",
            job_id="job_01JZXR7K3M5Q8N4VWA00000001",
            kind=TrackKind.VIDEO_TITLE,
            index=2,
            source_ref="2",
            status=TrackStatus.QUEUED,
            attempts=0,
            excluded=True,
        )
    )
    with TestClient(_make_app(db, hub=_CapturingHub())) as client:
        r = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume", headers=_service_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scan_titles"] == titles
    assert {t["id"]: t["excluded"] for t in body["tracks"]} == {"trk_1": False, "trk_2": True}


def test_resume_without_stored_scan_has_null_scan_titles() -> None:
    db = FakeSession()
    _seed(db)  # metadata_json={}
    with TestClient(_make_app(db, hub=_CapturingHub())) as client:
        r = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume", headers=_service_headers())
    assert r.status_code == 200, r.text
    assert r.json()["scan_titles"] is None


def test_resume_on_non_ripping_job_returns_409() -> None:
    db = FakeSession()
    _seed(db, job_status=JobStatus.RIPPED)
    app = _make_app(db, hub=_CapturingHub())
    with TestClient(app) as client:
        r = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume", headers=_service_headers())
    assert r.status_code == 409


def test_resume_unknown_job_returns_404() -> None:
    db = FakeSession()
    _seed(db)
    app = _make_app(db, hub=_CapturingHub())
    with TestClient(app) as client:
        r = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA0000000M/resume", headers=_service_headers())
    assert r.status_code == 404


def test_resume_unauthenticated_returns_401() -> None:
    db = FakeSession()
    _seed(db)
    app = _make_app(db, hub=_CapturingHub())
    with TestClient(app) as client:
        r = client.post("/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume")
    assert r.status_code == 401


def test_resume_wrong_hostname_returns_403() -> None:
    db = FakeSession()
    _seed(db)
    app = _make_app(db, hub=_CapturingHub())
    with TestClient(app) as client:
        r = client.post(
            "/api/ripper/jobs/job_01JZXR7K3M5Q8N4VWA00000001/resume",
            headers={"Authorization": "Bearer tok-service", "X-ARM-Hostname": "wrong-host"},
        )
    assert r.status_code == 403


# ---------- /drives/{id}/in-flight-job ----------


def test_in_flight_returns_ripping_job() -> None:
    db = FakeSession()
    _seed(db)
    app = _make_app(db)
    with TestClient(app) as client:
        r = client.get(
            "/api/ripper/drives/drv_x/in-flight-job",
            headers={"Authorization": "Bearer tok-service"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "job_01JZXR7K3M5Q8N4VWA00000001"
    assert body["status"] == "ripping"


def test_in_flight_no_ripping_job_returns_404() -> None:
    db = FakeSession()
    _seed(db, job_status=JobStatus.RIPPED)
    app = _make_app(db)
    with TestClient(app) as client:
        r = client.get(
            "/api/ripper/drives/drv_x/in-flight-job",
            headers={"Authorization": "Bearer tok-service"},
        )
    assert r.status_code == 404


def test_in_flight_unknown_drive_returns_404() -> None:
    db = FakeSession()
    _seed(db)
    app = _make_app(db)
    with TestClient(app) as client:
        r = client.get(
            "/api/ripper/drives/drv_missing/in-flight-job",
            headers={"Authorization": "Bearer tok-service"},
        )
    assert r.status_code == 404


def test_in_flight_unauthenticated_returns_401() -> None:
    db = FakeSession()
    _seed(db)
    app = _make_app(db)
    with TestClient(app) as client:
        r = client.get("/api/ripper/drives/drv_x/in-flight-job")
    assert r.status_code == 401
