"""A job's raw disc backup on the job detail, and DELETE /api/jobs/{id}/backup.

The ripper keeps `/raw/<job_id>/.backup/` only while a kept title still
needs it (one failed to remux); the operator can see it and delete it.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import arm_backend.raw_backup as raw_backup_module  # noqa: E402
from arm_backend.db import get_session  # noqa: E402
from arm_backend.jwt_utils import issue_access_token  # noqa: E402
from arm_backend.routers import jobs as jobs_router  # noqa: E402
from arm_common import DiscType, Job, JobStatus, User  # noqa: E402

from tests._fakes import FakeSession  # noqa: E402

JOB_ID = "job_01JZXR7K3M5Q8N4VWA00000001"
_KEY = secrets.token_bytes(32)


def _client(db: FakeSession, status: JobStatus = JobStatus.RIPPED_PARTIAL) -> tuple[TestClient, dict[str, str]]:
    db.rows["users"] = [User(id="usr_admin", username="admin", password_hash="x", password_must_change=False)]
    db.rows["jobs"] = [
        Job(
            id=JOB_ID,
            drive_id="drv_x",
            disc_type=DiscType.BLURAY,
            status=status,
            title="X",
            metadata_json={},
            resumed_from_crash=False,
        )
    ]
    app = FastAPI()
    app.state.signing_key = _KEY
    app.include_router(jobs_router.router)

    async def _session() -> FakeSession:
        return db

    app.dependency_overrides[get_session] = _session
    token, _ = issue_access_token("usr_admin", "admin", _KEY)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


@pytest.fixture
def raw_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(raw_backup_module.settings, "RAW_ROOT", str(tmp_path))
    (tmp_path / JOB_ID).mkdir()
    (tmp_path / JOB_ID / "Disc_t00.mkv").write_bytes(b"ripped title")
    return tmp_path


def _blu_ray_backup(raw_root: Path, *, complete: bool = True) -> Path:
    backup = raw_root / JOB_ID / ".backup"
    stream = backup / "disc" / "BDMV" / "STREAM"
    stream.mkdir(parents=True)
    (stream / "00014.m2ts").write_bytes(b"x" * 1000)
    (backup / "disc" / "BDMV" / "index.bdmv").write_bytes(b"y" * 24)
    if complete:
        (backup / ".complete").write_text("file:/raw/x/.backup/disc\n")
    return backup


def test_detail_reports_a_finished_backup_and_its_size(raw_root: Path) -> None:
    _blu_ray_backup(raw_root)
    client, auth = _client(FakeSession())

    body = client.get(f"/api/jobs/{JOB_ID}", headers=auth).json()

    assert body["raw_backup"] == {"size_bytes": 1000 + 24 + len("file:/raw/x/.backup/disc\n"), "complete": True}


def test_detail_reports_a_dvd_image_still_being_written(raw_root: Path) -> None:
    (raw_root / JOB_ID / ".backup").mkdir()
    (raw_root / JOB_ID / ".backup" / "disc.iso").write_bytes(b"z" * 2048)
    client, auth = _client(FakeSession(), JobStatus.RIPPING)

    body = client.get(f"/api/jobs/{JOB_ID}", headers=auth).json()

    assert body["raw_backup"] == {"size_bytes": 2048, "complete": False}


def test_detail_without_a_backup(raw_root: Path) -> None:
    client, auth = _client(FakeSession(), JobStatus.RIPPED)

    assert client.get(f"/api/jobs/{JOB_ID}", headers=auth).json()["raw_backup"] is None


def test_delete_removes_only_the_backup(raw_root: Path) -> None:
    backup = _blu_ray_backup(raw_root)
    db = FakeSession()
    client, auth = _client(db)

    r = client.delete(f"/api/jobs/{JOB_ID}/backup", headers=auth)

    assert r.status_code == 204
    assert not backup.exists()
    assert (raw_root / JOB_ID / "Disc_t00.mkv").read_bytes() == b"ripped title"
    assert [j.id for j in db.rows["jobs"]] == [JOB_ID]
    assert client.get(f"/api/jobs/{JOB_ID}", headers=auth).json()["raw_backup"] is None


def test_delete_without_a_backup_is_a_no_op(raw_root: Path) -> None:
    client, auth = _client(FakeSession())

    assert client.delete(f"/api/jobs/{JOB_ID}/backup", headers=auth).status_code == 204


def test_delete_is_refused_while_ripping(raw_root: Path) -> None:
    backup = _blu_ray_backup(raw_root, complete=False)
    client, auth = _client(FakeSession(), JobStatus.RIPPING)

    r = client.delete(f"/api/jobs/{JOB_ID}/backup", headers=auth)

    assert r.status_code == 409
    assert "ripping" in r.json()["detail"]
    assert backup.is_dir()


def test_delete_of_an_unknown_job_is_404(raw_root: Path) -> None:
    client, auth = _client(FakeSession())

    assert client.delete("/api/jobs/job_01JZXR7K3M5Q8N4VWA0000000M/backup", headers=auth).status_code == 404


def test_a_backup_that_cannot_be_removed_is_a_500(raw_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _blu_ray_backup(raw_root)

    def _refuse(path: Path) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(raw_backup_module.shutil, "rmtree", _refuse)
    client, auth = _client(FakeSession())

    r = client.delete(f"/api/jobs/{JOB_ID}/backup", headers=auth)

    assert r.status_code == 500
    assert "could not delete the raw backup" in r.json()["detail"]


def test_a_file_that_vanishes_mid_walk_is_skipped(raw_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _blu_ray_backup(raw_root)
    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object) -> os.stat_result:
        if self.name == "00014.m2ts":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky_stat)

    view = raw_backup_module.describe_backup(JOB_ID)

    assert view is not None and view.size_bytes == 24 + len("file:/raw/x/.backup/disc\n")


def test_a_crafted_job_id_stays_under_the_raw_root(raw_root: Path) -> None:
    assert raw_backup_module.backup_dir("../../etc") == raw_root / "etc" / ".backup"
