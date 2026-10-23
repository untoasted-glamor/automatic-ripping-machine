"""Retry a ripped job's failed titles (track-select plan S15).

POST /api/jobs/{id}/retry-failed queues the failed kept titles again and arms
the job. With a finished disc backup the drive's ripper is told to remux them
from it (`rip.retry`); without one, identify reuses the armed job when its disc
goes back in. rip-start hands the ripper the tracks marked as a retry, and
rip-complete gives the sessions already applied tasks for the new files.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import arm_backend.raw_backup as raw_backup_module  # noqa: E402
from arm_backend import config as backend_config  # noqa: E402
from arm_backend.auto_session import fan_out_retried_tracks  # noqa: E402
from arm_backend.db import get_session  # noqa: E402
from arm_backend.jwt_utils import issue_access_token  # noqa: E402
from arm_backend.routers import jobs as jobs_router  # noqa: E402
from arm_backend.routers import ripper as ripper_router  # noqa: E402
from arm_backend.seeders import CONFIG_SINGLETON_ID  # noqa: E402
from arm_common import (  # noqa: E402
    Config,
    ContainerFormat,
    DiscFingerprint,
    DiscType,
    Drive,
    DriveStatus,
    IdentificationMode,
    Job,
    JobStatus,
    MediaType,
    OutputMode,
    RetentionPolicy,
    RipPreset,
    Session,
    SessionApplication,
    SessionApplicationStatus,
    TrackKind,
    TrackSelection,
    TrackStatus,
    TranscodePreset,
    TranscodeTask,
    TranscodeTaskStatus,
    TranscodeTool,
    User,
)
from arm_common.enums import DriveSourceKind  # noqa: E402
from arm_common.models import Track  # noqa: E402
from arm_common.schemas.job_metadata import flag_is_set  # noqa: E402

from tests._fakes import FakeSession  # noqa: E402

JOB_ID = "job_01JZXR7K3M5Q8N4VWA00000001"
_SERVICE_AUTH = {"Authorization": "Bearer tok-service", "X-ARM-Hostname": "host"}
_FP = {"algo": "matrix256", "value": "25dc9f67"}
_SCAN = {"disc_type": "bluray", "titles": [{"index": i, "duration_seconds": 1447} for i in range(3)]}


class _Hub:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, topic: str, event_type: str, payload: dict[str, Any], **_kw: Any) -> None:
        self.events.append({"topic": topic, "event_type": event_type, "payload": payload})


def _track(index: int, status: TrackStatus, **kw: Any) -> Track:
    return Track(
        id=f"trk_{index}",
        job_id=JOB_ID,
        kind=TrackKind.VIDEO_TITLE,
        index=index,
        source_ref=str(index),
        status=status,
        attempts=1,
        **kw,
    )


def _db(
    status: JobStatus = JobStatus.RIPPED_PARTIAL,
    *,
    meta: dict[str, Any] | None = None,
    drive: Drive | None = None,
) -> FakeSession:
    db = FakeSession()
    db.rows["users"] = [User(id="usr_admin", username="admin", password_hash="x", password_must_change=False)]
    db.rows["drives"] = [drive or Drive(id="drv_x", hostname="host", device_path="/dev/sr0", status=DriveStatus.ONLINE)]
    db.rows["jobs"] = [
        Job(
            id=JOB_ID,
            drive_id="drv_x",
            disc_type=DiscType.BLURAY,
            status=status,
            title="Toilet-Bound Hanako-kun",
            year=2020,
            metadata_json={"scan_result": _SCAN} if meta is None else meta,
            resumed_from_crash=False,
        )
    ]
    db.rows["tracks"] = [
        _track(0, TrackStatus.DONE, output_path=f"/raw/{JOB_ID}/D1_t00.mkv"),
        _track(1, TrackStatus.FAILED, last_error="remux failed"),
        _track(2, TrackStatus.FAILED, last_error="remux failed", excluded=True),
    ]
    return db


def _jobs(db: FakeSession, hub: _Hub) -> tuple[TestClient, dict[str, str]]:
    key = secrets.token_bytes(32)
    app = FastAPI()
    app.state.signing_key = key
    app.state.ws_hub = hub
    app.include_router(jobs_router.router)

    async def _session() -> FakeSession:
        return db

    app.dependency_overrides[get_session] = _session
    token, _ = issue_access_token("usr_admin", "admin", key)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


class _Dispatcher:
    async def identify(self, _scan: Any, _cfg: Any, **_kw: Any) -> None:
        return None


def _ripper(db: FakeSession, hub: _Hub) -> TestClient:
    app = FastAPI()
    app.state.signing_key = secrets.token_bytes(32)
    app.state.ws_hub = hub
    app.state.dispatcher = _Dispatcher()
    app.include_router(ripper_router.router)

    async def _session() -> FakeSession:
        return db

    app.dependency_overrides[get_session] = _session
    return TestClient(app)


@pytest.fixture
def raw_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(raw_backup_module.settings, "RAW_ROOT", str(tmp_path))
    return tmp_path


def _backup(raw_root: Path, *, complete: bool) -> None:
    backup = raw_root / JOB_ID / ".backup" / "disc" / "BDMV"
    backup.mkdir(parents=True)
    (backup / "index.bdmv").write_bytes(b"x")
    if complete:
        (raw_root / JOB_ID / ".backup" / ".complete").write_text("file:/raw/x/.backup/disc/BDMV\n")


def _retry(db: FakeSession, hub: _Hub, job_id: str = JOB_ID) -> Any:
    client, auth = _jobs(db, hub)
    return client.post(f"/api/jobs/{job_id}/retry-failed", headers=auth)


# --- POST /api/jobs/{id}/retry-failed ------------------------------------------


def test_with_a_finished_backup_the_ripper_is_told_to_remux(raw_root: Path) -> None:
    _backup(raw_root, complete=True)
    db, hub = _db(), _Hub()

    r = _retry(db, hub)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["retried"] == 1 and body["from_backup"] is True
    by_id = {t.id: t for t in db.rows["tracks"]}
    assert by_id["trk_1"].status == TrackStatus.QUEUED and by_id["trk_1"].last_error is None
    assert by_id["trk_1"].attempts == 1  # counts on when the retry starts it
    assert by_id["trk_0"].status == TrackStatus.DONE
    assert by_id["trk_2"].status == TrackStatus.FAILED  # excluded: not kept, not retried
    assert flag_is_set(db.rows["jobs"][0].metadata_json, "retry_armed")
    assert db.rows["jobs"][0].status == JobStatus.RIPPED_PARTIAL
    assert hub.events == [
        {
            "topic": "ripper.commands.drv_x",
            "event_type": "rip.retry",
            "payload": {"job_id": JOB_ID, "drive_id": "drv_x"},
        }
    ]


@pytest.mark.parametrize("backup", ["none", "unfinished"])
def test_without_a_finished_backup_it_waits_for_the_disc(raw_root: Path, backup: str) -> None:
    if backup == "unfinished":
        _backup(raw_root, complete=False)
    db, hub = _db(), _Hub()

    body = _retry(db, hub).json()

    assert body["from_backup"] is False and body["retried"] == 1
    assert hub.events == []
    assert flag_is_set(db.rows["jobs"][0].metadata_json, "retry_armed")


def test_a_failed_job_can_retry_too(raw_root: Path) -> None:
    db = _db(JobStatus.FAILED)
    assert _retry(db, _Hub()).status_code == 200


def test_unknown_job_is_404(raw_root: Path) -> None:
    assert _retry(_db(), _Hub(), job_id="job_01JZXR7K3M5Q8N4VWA0000000Z").status_code == 404


@pytest.mark.parametrize("status", [JobStatus.RIPPED, JobStatus.RIPPING, JobStatus.AWAITING_REVIEW])
def test_only_a_partial_or_failed_rip_retries(raw_root: Path, status: JobStatus) -> None:
    r = _retry(_db(status), _Hub())
    assert r.status_code == 409
    assert "ripped_partial or failed" in r.json()["detail"]


def test_a_job_with_no_stored_scan_is_refused(raw_root: Path) -> None:
    r = _retry(_db(meta={}), _Hub())
    assert r.status_code == 409 and "scan" in r.json()["detail"]


def test_an_iso_rip_is_refused(raw_root: Path) -> None:
    virtual = Drive(
        id="drv_x",
        hostname="host",
        device_path="/iso/x.iso",
        status=DriveStatus.ONLINE,
        source_kind=DriveSourceKind.ISO,
    )
    r = _retry(_db(drive=virtual), _Hub())
    assert r.status_code == 409 and "rip its source again" in r.json()["detail"]


def test_a_retired_drive_is_refused(raw_root: Path) -> None:
    db = _db()
    db.rows["drives"] = []
    assert _retry(db, _Hub()).status_code == 409


def test_a_busy_drive_is_refused(raw_root: Path) -> None:
    _backup(raw_root, complete=True)
    db, hub = _db(), _Hub()
    db.rows["jobs"].append(
        Job(
            id="job_01JZXR7K3M5Q8N4VWA00000002",
            drive_id="drv_x",
            disc_type=DiscType.DVD,
            status=JobStatus.AWAITING_REVIEW,
        )
    )

    r = _retry(db, hub)

    assert r.status_code == 409
    assert "busy with another disc (job job_01JZXR7K3M5Q8N4VWA00000002)" in r.json()["detail"]
    assert hub.events == []
    assert {t.id: t.status for t in db.rows["tracks"]}["trk_1"] == TrackStatus.FAILED


def test_nothing_failed_and_kept_is_422(raw_root: Path) -> None:
    db = _db()
    db.rows["tracks"] = [t for t in db.rows["tracks"] if t.id != "trk_1"]
    assert _retry(db, _Hub()).status_code == 422


# --- rip-start ------------------------------------------------------------------


def _armed_db(status: JobStatus = JobStatus.RIPPED_PARTIAL) -> FakeSession:
    db = _db(status, meta={"scan_result": _SCAN, "flags": {"retry_armed": True}})
    db.rows["tracks"][1].status = TrackStatus.QUEUED
    db.rows["rip_presets"] = [
        RipPreset(
            id="rpr_builtin_movie_archive",
            name="Movie archive",
            media_type=MediaType.MOVIE,
            is_builtin=True,
            track_selection=TrackSelection.ALL_TRACKS,
            identification_mode=IdentificationMode.SKIP,
            output_mode=OutputMode.TRACKS,
        )
    ]
    return db


def test_rip_start_of_an_armed_job_is_a_retry_and_stays_one(raw_root: Path) -> None:
    db = _armed_db()
    client = _ripper(db, _Hub())

    first = client.post(f"/api/ripper/jobs/{JOB_ID}/rip-start", headers=_SERVICE_AUTH)
    again = client.post(f"/api/ripper/jobs/{JOB_ID}/rip-start", headers=_SERVICE_AUTH)

    assert first.status_code == 200, first.text
    assert first.json()["retry"] is True and again.json()["retry"] is True
    assert {t["id"]: t["status"] for t in first.json()["tracks"]} == {
        "trk_0": "done",
        "trk_1": "queued",
        "trk_2": "failed",
    }
    job = db.rows["jobs"][0]
    assert job.status == JobStatus.RIPPING
    assert not flag_is_set(job.metadata_json, "retry_armed")
    assert flag_is_set(job.metadata_json, "retrying")


@pytest.mark.parametrize("status", [JobStatus.RIPPED, JobStatus.RIPPED_PARTIAL, JobStatus.FAILED])
def test_rip_start_refuses_a_ripped_job_nobody_armed(raw_root: Path, status: JobStatus) -> None:
    db = _db(status)
    db.rows["rip_presets"] = _armed_db().rows["rip_presets"]
    r = _ripper(db, _Hub()).post(f"/api/ripper/jobs/{JOB_ID}/rip-start", headers=_SERVICE_AUTH)
    assert r.status_code == 409
    assert db.rows["jobs"][0].status == status


def test_a_fresh_rip_start_is_no_retry(raw_root: Path) -> None:
    db = _armed_db(JobStatus.IDENTIFIED)
    db.rows["jobs"][0].metadata_json = {"scan_result": _SCAN}
    r = _ripper(db, _Hub()).post(f"/api/ripper/jobs/{JOB_ID}/rip-start", headers=_SERVICE_AUTH)
    assert r.status_code == 200 and r.json()["retry"] is False


# --- identify: the disc goes back in --------------------------------------------


def _config() -> Config:
    return Config(
        id=CONFIG_SINGLETON_ID,
        auto_transcode_on_idle=False,
        auto_rip_on_insert=True,
        block_on_miss=True,
        hold_for_review=True,
        ripping_paused=False,
        thediscdb_enabled=False,
        manual_wait_seconds=60,
        default_retention_policy=RetentionPolicy.PRUNE_AFTER_SESSION,
        notification_apprise_urls=[],
        notifications_enabled=False,
    )


def _identify(db: FakeSession) -> Any:
    db.rows["config"] = [_config()]
    db.rows["disc_fingerprints"] = [DiscFingerprint(job_id=JOB_ID, algo=_FP["algo"], value=_FP["value"])]
    scan = {**_SCAN, "volume_label": "HANAKO_D1", "fingerprints": [_FP], "raw": {}}
    return _ripper(db, _Hub()).post(
        "/api/ripper/identify", json={"drive_id": "drv_x", "scan_result": scan}, headers=_SERVICE_AUTH
    )


@pytest.mark.parametrize("status", [JobStatus.RIPPED_PARTIAL, JobStatus.FAILED])
def test_the_disc_of_an_armed_job_goes_straight_to_its_retry(raw_root: Path, status: JobStatus) -> None:
    db = _armed_db(status)

    r = _identify(db)

    assert r.status_code == 200, r.text
    assert r.json()["id"] == JOB_ID and r.json()["status"] == "identified"
    assert [j.id for j in db.rows["jobs"]] == [JOB_ID]
    assert flag_is_set(db.rows["jobs"][0].metadata_json, "retry_armed")  # rip-start consumes it


def test_the_disc_of_a_job_nobody_armed_is_a_new_job(raw_root: Path) -> None:
    db = _db()
    r = _identify(db)
    assert r.status_code == 200, r.text
    assert r.json()["id"] != JOB_ID
    assert db.rows["jobs"][0].status == JobStatus.RIPPED_PARTIAL


# --- rip-complete and the fan-out -----------------------------------------------


def _with_session(db: FakeSession, application_status: SessionApplicationStatus) -> None:
    db.rows["transcode_presets"] = [
        TranscodePreset(
            id="tpr_x",
            name="TV",
            media_type=MediaType.TV,
            is_builtin=True,
            tool=TranscodeTool.HANDBRAKE,
            container=ContainerFormat.MKV,
            encoder="preset",
        )
    ]
    db.rows["sessions"] = [
        Session(
            id="ses_x",
            name="TV",
            media_type=MediaType.TV,
            is_builtin=False,
            rip_preset_id="rpr_builtin_movie_archive",
            transcode_preset_id="tpr_x",
            output_path_template="{title}/{title} - {track}.{ext}",
        )
    ]
    db.rows["session_applications"] = [
        SessionApplication(id="sap_x", session_id="ses_x", job_id=JOB_ID, status=application_status, overwrite=False),
        SessionApplication(
            id="sap_parked",
            session_id="ses_x",
            job_id=JOB_ID,
            status=SessionApplicationStatus.WAITING_IDENTIFY,
            overwrite=False,
        ),
    ]
    db.rows["transcode_tasks"] = [
        TranscodeTask(
            id="txt_0",
            session_application_id="sap_x",
            source_track_id="trk_0",
            status=TranscodeTaskStatus.DONE,
            output_path="Toilet-Bound Hanako-kun/Toilet-Bound Hanako-kun - 00.mkv",
        )
    ]


async def test_a_retry_queues_the_new_files_in_the_applied_session(tmp_path: Path) -> None:
    backend_config.settings.MEDIA_ROOT = str(tmp_path)
    db, hub = _armed_db(), _Hub()
    _with_session(db, SessionApplicationStatus.DONE)
    db.rows["tracks"][1].status = TrackStatus.DONE
    db.rows["tracks"][1].output_path = f"/raw/{JOB_ID}/retry-1/D1_t01.mkv"

    added = await fan_out_retried_tracks(db, db.rows["jobs"][0], hub)  # type: ignore[arg-type]

    assert added == 1
    new = [t for t in db.rows["transcode_tasks"] if t.id != "txt_0"]
    assert [(t.session_application_id, t.source_track_id, t.status) for t in new] == [
        ("sap_x", "trk_1", TranscodeTaskStatus.QUEUED)
    ]
    assert {a.id: a.status for a in db.rows["session_applications"]}["sap_x"] == SessionApplicationStatus.RUNNING
    assert [e["event_type"] for e in hub.events] == ["session.queued"]


async def test_a_new_file_whose_output_exists_is_left_for_the_operator(tmp_path: Path) -> None:
    backend_config.settings.MEDIA_ROOT = str(tmp_path)
    (tmp_path / "Toilet-Bound Hanako-kun").mkdir()
    (tmp_path / "Toilet-Bound Hanako-kun" / "Toilet-Bound Hanako-kun - 01.mkv").write_bytes(b"old")
    db = _armed_db()
    _with_session(db, SessionApplicationStatus.QUEUED)
    db.rows["tracks"][1].status = TrackStatus.DONE
    db.rows["tracks"][1].output_path = f"/raw/{JOB_ID}/retry-1/D1_t01.mkv"

    assert await fan_out_retried_tracks(db, db.rows["jobs"][0], None) == 0  # type: ignore[arg-type]
    assert [t.id for t in db.rows["transcode_tasks"]] == ["txt_0"]


async def test_no_session_applied_means_nothing_to_fan_out(tmp_path: Path) -> None:
    db = _armed_db()
    assert await fan_out_retried_tracks(db, db.rows["jobs"][0], None) == 0  # type: ignore[arg-type]


def test_rip_complete_of_a_retry_clears_it_and_fans_out(raw_root: Path, tmp_path: Path, monkeypatch) -> None:
    backend_config.settings.MEDIA_ROOT = str(tmp_path)
    db = _armed_db()
    db.rows["jobs"][0].status = JobStatus.RIPPING
    db.rows["jobs"][0].metadata_json = {"scan_result": _SCAN, "flags": {"retrying": True}}
    db.rows["tracks"][1].status = TrackStatus.DONE
    db.rows["tracks"][1].output_path = f"/raw/{JOB_ID}/retry-1/D1_t01.mkv"
    calls: list[str] = []

    async def fake_after_rip(_db: Any, job: Job, _hub: Any) -> None:
        calls.append("after_rip")

    async def fake_fan_out(_db: Any, job: Job, _hub: Any) -> int:
        calls.append("fan_out")
        return 1

    monkeypatch.setattr(ripper_router, "after_rip", fake_after_rip)
    monkeypatch.setattr(ripper_router, "fan_out_retried_tracks", fake_fan_out)

    r = _ripper(db, _Hub()).post(f"/api/ripper/jobs/{JOB_ID}/rip-complete", json={}, headers=_SERVICE_AUTH)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ripped_partial"  # trk_2 is still failed (excluded)
    assert not flag_is_set(db.rows["jobs"][0].metadata_json, "retrying")
    assert calls == ["after_rip", "fan_out"]


async def test_each_applied_session_gets_only_what_it_lacks(tmp_path: Path) -> None:
    """A passthrough session (no transcode preset) still running gets the new
    file; one whose session row is gone, or that already has every file, is
    left alone."""
    backend_config.settings.MEDIA_ROOT = str(tmp_path)
    db = _armed_db()
    _with_session(db, SessionApplicationStatus.RUNNING)
    db.rows["sessions"].append(
        Session(
            id="ses_copy",
            name="Copy",
            media_type=MediaType.TV,
            is_builtin=False,
            rip_preset_id="rpr_builtin_movie_archive",
            transcode_preset_id=None,
            output_path_template="copy/{title} - {track}.mkv",
        )
    )
    db.rows["session_applications"] += [
        SessionApplication(
            id="sap_gone",
            session_id="ses_deleted",
            job_id=JOB_ID,
            status=SessionApplicationStatus.DONE,
            overwrite=False,
        ),
        SessionApplication(
            id="sap_full", session_id="ses_copy", job_id=JOB_ID, status=SessionApplicationStatus.DONE, overwrite=False
        ),
    ]
    db.rows["transcode_tasks"].append(
        TranscodeTask(
            id="txt_full",
            session_application_id="sap_full",
            source_track_id="trk_0",
            status=TranscodeTaskStatus.DONE,
            output_path="copy/x - 00.mkv",
        )
    )
    db.rows["tracks"] = [t for t in db.rows["tracks"] if t.id != "trk_1"]  # nothing new for sap_full

    assert await fan_out_retried_tracks(db, db.rows["jobs"][0], None) == 0  # type: ignore[arg-type]

    db.rows["tracks"].append(_track(1, TrackStatus.DONE, output_path=f"/raw/{JOB_ID}/retry-1/D1_t01.mkv"))
    db.rows["session_applications"][-1].session_id = "ses_copy"

    added = await fan_out_retried_tracks(db, db.rows["jobs"][0], None)  # type: ignore[arg-type]

    assert added == 2  # sap_x (running, stays running) and sap_full (done -> running)
    statuses = {a.id: a.status for a in db.rows["session_applications"]}
    assert statuses["sap_x"] == SessionApplicationStatus.RUNNING
    assert statuses["sap_full"] == SessionApplicationStatus.RUNNING
    assert statuses["sap_gone"] == SessionApplicationStatus.DONE


async def test_a_session_whose_template_cannot_resolve_is_skipped(tmp_path: Path) -> None:
    """rip-complete must never fail over a fan-out: the title is left out."""
    backend_config.settings.MEDIA_ROOT = str(tmp_path)
    db = _armed_db()
    _with_session(db, SessionApplicationStatus.DONE)
    db.rows["sessions"][0].output_path_template = "{title}/{episode_name}.{ext}"
    db.rows["tracks"][1].status = TrackStatus.DONE
    db.rows["tracks"][1].output_path = f"/raw/{JOB_ID}/retry-1/D1_t01.mkv"

    assert await fan_out_retried_tracks(db, db.rows["jobs"][0], None) == 0  # type: ignore[arg-type]
