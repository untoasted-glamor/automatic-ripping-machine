"""Text subtitles (S5): the Settings switch and default, the per-disc choice
(`jobs.subtitle_ocr`), the OCR task an encode's /complete queues, and the job
page's Make text subtitles (`POST /api/jobs/{id}/subtitle-ocr`).

A subtitle OCR task is created only after its encode completes, only when the
encode wrote image subtitles, and only while text subtitles are on; the job
page's action leaves an encode whose `.srt` exist unless told to overwrite.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from arm_backend.subtitle_ocr_tasks import (  # noqa: E402
    bundled_model_view,
    job_subtitle_ocr_view,
    queue_after_encode,
    subtitle_ocr_choice,
    text_subtitle_paths,
)
from arm_common import (  # noqa: E402
    Config,
    SessionApplication,
    SessionApplicationStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
    User,
)
from arm_common.enums import RetentionPolicy  # noqa: E402
from arm_common.subtitle_ocr import BUNDLED_MODEL  # noqa: E402
from tests._fakes import FakeSession  # noqa: E402
from tests.test_apply_session import _CapturingHub, _auth  # noqa: E402
from tests.test_apply_session import _make_app as _jobs_app  # noqa: E402
from tests.test_apply_session import _seed as _jobs_seed  # noqa: E402
from tests.test_config_transcode_enabled import _make_app as _config_app  # noqa: E402
from tests.test_transcoder_router import _HOSTNAME, _SERVICE_AUTH, _seed  # noqa: E402
from tests.test_transcoder_router import _make_app as _transcoder_app  # noqa: E402

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
_VIDEO = "Iron Man (2008)/Iron Man.mkv"
_SUPS = ["Iron Man (2008)/Iron Man.eng.sup", "Iron Man (2008)/Iron Man.eng.2.sup"]
_SRTS = ["Iron Man (2008)/Iron Man.eng.srt", "Iron Man (2008)/Iron Man.eng.2.srt"]


def _config(*, enabled: bool = True, default_on: bool | None = True) -> Config:
    return Config(id=1, subtitle_ocr_enabled=enabled, subtitle_ocr_default_on=default_on)


@pytest.fixture
def signing_key() -> bytes:
    return secrets.token_bytes(32)


# ---- shared pieces -------------------------------------------------------------


def test_text_subtitle_paths_follow_the_image_sidecars() -> None:
    assert text_subtitle_paths([*_SUPS, "Iron Man (2008)/Iron Man.fra.mks"]) == _SRTS
    assert text_subtitle_paths(None) == []


def test_bundled_model_view_is_the_image_model() -> None:
    view = bundled_model_view()
    assert (view.name, view.version, view.sha256, view.size) == (
        BUNDLED_MODEL.name,
        BUNDLED_MODEL.version,
        BUNDLED_MODEL.sha256,
        BUNDLED_MODEL.size,
    )
    assert view.licence == BUNDLED_MODEL.licence and view.source == BUNDLED_MODEL.source


async def test_the_choice_is_none_while_off_and_the_default_when_unset() -> None:
    db = FakeSession()
    _jobs_seed(db)
    job = db.rows["jobs"][0]
    assert await subtitle_ocr_choice(db, job) is None  # type: ignore[arg-type]
    db.rows["config"] = [_config(enabled=False)]
    assert await subtitle_ocr_choice(db, job) is None  # type: ignore[arg-type]
    db.rows["config"] = [_config(default_on=None)]  # a bare row reads as the column default
    choice = await subtitle_ocr_choice(db, job)  # type: ignore[arg-type]
    assert choice is not None and (choice.override, choice.default, choice.effective) == (None, True, True)
    job.subtitle_ocr = False
    choice = await subtitle_ocr_choice(db, job)  # type: ignore[arg-type]
    assert choice is not None and (choice.override, choice.effective) == (False, False)


# ---- Settings ------------------------------------------------------------------


def test_config_view_and_patch(signing_key: bytes) -> None:
    db = FakeSession()
    db.rows["config"] = [
        Config(
            id=1,
            auto_transcode_on_idle=False,
            auto_rip_on_insert=True,
            block_on_miss=True,
            default_retention_policy=RetentionPolicy.PRUNE_AFTER_SESSION,
            notification_apprise_urls=[],
            notifications_enabled=False,
        )
    ]
    db.rows["users"] = [User(id="usr_admin", username="admin", password_hash="x", password_must_change=False)]
    app, auth = _config_app(signing_key, db)
    with TestClient(app) as client:
        before = client.get("/api/config", headers=auth).json()
        r = client.patch(
            "/api/config", json={"subtitle_ocr_enabled": True, "subtitle_ocr_default_on": False}, headers=auth
        )
        refused = [
            client.patch("/api/config", json={key: None}, headers=auth)
            for key in ("subtitle_ocr_enabled", "subtitle_ocr_default_on")
        ]
    assert (before["subtitle_ocr_enabled"], before["subtitle_ocr_default_on"]) == (False, True)
    assert before["subtitle_ocr_model"]["sha256"] == BUNDLED_MODEL.sha256
    assert r.status_code == 200, r.text
    assert (r.json()["subtitle_ocr_enabled"], r.json()["subtitle_ocr_default_on"]) == (True, False)
    cfg = db.rows["config"][0]
    assert (cfg.subtitle_ocr_enabled, cfg.subtitle_ocr_default_on) == (True, False)
    assert [x.status_code for x in refused] == [400, 400]
    assert refused[0].json()["detail"] == "subtitle_ocr_enabled must be true or false"


# ---- created when an encode completes --------------------------------------------


def _running_encode_db(*, config: Config | None, job_choice: bool | None = None) -> FakeSession:
    db = FakeSession()
    _seed(db, task_status=TranscodeTaskStatus.IN_PROGRESS)
    db.rows["jobs"][0].subtitle_ocr = job_choice
    if config is not None:
        db.rows["config"] = [config]
    return db


def _complete(db: FakeSession, sidecars: list[str]) -> Any:
    with TestClient(_transcoder_app(db)) as client:
        r = client.patch(
            "/api/transcoder/tasks/txt_1/complete",
            json={"output_path": _VIDEO, "sidecar_paths": sidecars},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    return [t for t in db.rows["transcode_tasks"] if t.kind == TranscodeTaskKind.SUBTITLE_OCR]


def test_complete_queues_the_text_subtitles_task() -> None:
    db = _running_encode_db(config=_config())
    (ocr,) = _complete(db, [*_SUPS, "Iron Man (2008)/Iron Man.fra.mks"])
    assert ocr.status == TranscodeTaskStatus.QUEUED
    assert (ocr.parent_task_id, ocr.session_application_id, ocr.source_track_id, ocr.output_path) == (
        "txt_1",
        "sap_x",
        "trk_1",
        _VIDEO,
    )
    assert ocr.replaced_sidecar_paths is None and ocr.attempts == 0
    # The encode's application finishes as before: the OCR task isn't part of it.
    assert db.rows["session_applications"][0].status == SessionApplicationStatus.DONE


@pytest.mark.parametrize(
    ("config", "job_choice", "sidecars", "queued"),
    [
        (None, None, _SUPS, 0),  # no config row: off
        (_config(enabled=False), True, _SUPS, 0),  # off in Settings, whatever the job says
        (_config(), False, _SUPS, 0),  # the job said no
        (_config(default_on=False), None, _SUPS, 0),  # default off, no choice of its own
        (_config(default_on=False), True, _SUPS, 1),  # default off, the job said yes
        (_config(), None, ["Iron Man (2008)/Iron Man.fra.mks"], 0),  # no image subtitles it can read
        (_config(), None, [], 0),
    ],
)
def test_complete_queues_only_when_due(
    config: Config | None, job_choice: bool | None, sidecars: list[str], queued: int
) -> None:
    db = _running_encode_db(config=config, job_choice=job_choice)
    assert len(_complete(db, sidecars)) == queued


def test_an_ocr_tasks_own_completion_queues_nothing() -> None:
    db = _running_encode_db(config=_config())
    encode = db.rows["transcode_tasks"][0]
    encode.status, encode.sidecar_paths, encode.output_path = TranscodeTaskStatus.DONE, list(_SUPS), _VIDEO
    db.rows["transcode_tasks"].append(
        TranscodeTask(
            id="txt_ocr",
            session_application_id="sap_x",
            source_track_id="trk_1",
            kind=TranscodeTaskKind.SUBTITLE_OCR,
            parent_task_id="txt_1",
            status=TranscodeTaskStatus.IN_PROGRESS,
            claimed_by=_HOSTNAME,
            claim_heartbeat_at=datetime.now(UTC),
            attempts=1,
            progress_pct=0,
            output_path=_VIDEO,
        )
    )
    with TestClient(_transcoder_app(db)) as client:
        r = client.patch(
            "/api/transcoder/tasks/txt_ocr/complete",
            json={"output_path": _VIDEO, "sidecar_paths": _SRTS},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    assert len(db.rows["transcode_tasks"]) == 2


async def test_queue_after_encode_guards_and_event() -> None:
    db = _running_encode_db(config=_config())
    encode = db.rows["transcode_tasks"][0]
    hub = _CapturingHub()
    # Not done yet: nothing.
    assert await queue_after_encode(db, encode, JOB, hub) is None  # type: ignore[arg-type]
    encode.status, encode.sidecar_paths, encode.output_path = TranscodeTaskStatus.DONE, list(_SUPS), None
    assert await queue_after_encode(db, encode, JOB, hub) is None  # type: ignore[arg-type]
    encode.output_path = _VIDEO
    # A job that is gone: nothing.
    assert await queue_after_encode(db, encode, "job_01JZXR7K3M5Q8N4VWA00000009", hub) is None  # type: ignore[arg-type]
    task = await queue_after_encode(db, encode, JOB, hub)  # type: ignore[arg-type]
    assert task is not None and task.parent_task_id == "txt_1"
    assert [(e["topic"], e["event_type"]) for e in hub.events] == [("transcode.events", "task.queued")]
    assert hub.events[0]["payload"]["kind"] == "subtitle_ocr"
    # Without a hub it still queues.
    assert await queue_after_encode(db, encode, JOB, None) is not None  # type: ignore[arg-type]


# ---- the job page: Make text subtitles --------------------------------------------


def _done_encodes_db(tmp_path: Path, *, config: Config | None = None) -> FakeSession:
    """Two DONE encodes with PGS sidecars (txt_1, txt_2), one DONE encode with
    none (txt_3), and a failed one (txt_4), all of one job."""
    db = FakeSession()
    _jobs_seed(db)
    db.rows["config"] = [config or _config()]
    db.rows["session_applications"] = [
        SessionApplication(id="sap_1", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.DONE)
    ]

    def encode(task_id: str, stem: str, sidecars: list[str], status: TranscodeTaskStatus) -> TranscodeTask:
        return TranscodeTask(
            id=task_id,
            session_application_id="sap_1",
            source_track_id="trk_1",
            status=status,
            attempts=1,
            progress_pct=100,
            output_path=f"Show/{stem}.mkv",
            sidecar_paths=sidecars,
        )

    db.rows["transcode_tasks"] = [
        encode("txt_2", "E02", ["Show/E02.eng.sup"], TranscodeTaskStatus.DONE),
        encode("txt_1", "E01", ["Show/E01.eng.sup", "Show/E01.eng.2.sup"], TranscodeTaskStatus.DONE),
        encode("txt_3", "E03", [], TranscodeTaskStatus.DONE),
        encode("txt_4", "E04", ["Show/E04.eng.sup"], TranscodeTaskStatus.FAILED),
    ]
    (tmp_path / "Show").mkdir()
    return db


def _ocr_rows(db: FakeSession) -> list[TranscodeTask]:
    return [t for t in db.rows["transcode_tasks"] if t.kind == TranscodeTaskKind.SUBTITLE_OCR]


def test_make_text_subtitles_queues_one_per_finished_encode(signing_key: bytes, tmp_path: Path) -> None:
    db = _done_encodes_db(tmp_path)
    hub = _CapturingHub()
    app, token = _jobs_app(signing_key, db, tmp_path, hub=hub)
    with TestClient(app) as client:
        r = client.post(f"/api/jobs/{JOB}/subtitle-ocr", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["parent_task_id"] for t in body["tasks"]] == ["txt_1", "txt_2"]
    assert all(t["kind"] == "subtitle_ocr" and t["status"] == "queued" for t in body["tasks"])
    assert [t["output_path"] for t in body["tasks"]] == ["Show/E01.mkv", "Show/E02.mkv"]
    assert body["skipped"] == []
    assert [e["event_type"] for e in hub.events] == ["task.queued", "task.queued"]
    assert db.committed >= 1


def test_make_text_subtitles_skips_running_and_existing(signing_key: bytes, tmp_path: Path) -> None:
    db = _done_encodes_db(tmp_path)
    (tmp_path / "Show" / "E02.eng.srt").write_text("1")
    db.rows["transcode_tasks"].append(
        TranscodeTask(
            id="txt_9",
            session_application_id="sap_1",
            source_track_id="trk_1",
            kind=TranscodeTaskKind.SUBTITLE_OCR,
            parent_task_id="txt_1",
            status=TranscodeTaskStatus.IN_PROGRESS,
            attempts=1,
            progress_pct=10,
            output_path="Show/E01.mkv",
        )
    )
    app, token = _jobs_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.post(f"/api/jobs/{JOB}/subtitle-ocr", json={"overwrite": False}, headers=_auth(token))
        # Overwrite doesn't touch a running one either.
        r2 = client.post(f"/api/jobs/{JOB}/subtitle-ocr", json={"overwrite": True}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["tasks"] == []
    assert [(s["encode_task_id"], s["reason"]) for s in r.json()["skipped"]] == [
        ("txt_1", "running"),
        ("txt_2", "exists"),
    ]
    assert [t["parent_task_id"] for t in r2.json()["tasks"]] == ["txt_2"]
    assert [(s["encode_task_id"], s["reason"]) for s in r2.json()["skipped"]] == [("txt_1", "running")]


def test_overwrite_hands_the_old_srt_to_the_new_task(signing_key: bytes, tmp_path: Path) -> None:
    db = _done_encodes_db(tmp_path)
    (tmp_path / "Show" / "E01.eng.srt").write_text("1")
    old_done = TranscodeTask(
        id="txt_7",
        session_application_id="sap_1",
        source_track_id="trk_1",
        kind=TranscodeTaskKind.SUBTITLE_OCR,
        parent_task_id="txt_1",
        status=TranscodeTaskStatus.DONE,
        attempts=1,
        progress_pct=100,
        output_path="Show/E01.mkv",
        sidecar_paths=["Show/E01.eng.srt"],
    )
    old_failed = TranscodeTask(
        id="txt_8",
        session_application_id="sap_1",
        source_track_id="trk_1",
        kind=TranscodeTaskKind.SUBTITLE_OCR,
        parent_task_id="txt_1",
        status=TranscodeTaskStatus.FAILED,
        attempts=1,
        progress_pct=50,
        output_path="Show/E01.mkv",
        sidecar_paths=["Show/E01.eng.srt"],
        replaced_sidecar_paths=["Show/E01.old.srt"],
    )
    db.rows["transcode_tasks"] += [old_done, old_failed]
    app, token = _jobs_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.post(f"/api/jobs/{JOB}/subtitle-ocr", json={"overwrite": True}, headers=_auth(token))
    assert r.status_code == 200, r.text
    assert old_done in db.deleted and old_failed in db.deleted
    new = {t.parent_task_id: t for t in _ocr_rows(db)}
    assert set(new) == {"txt_1", "txt_2"}
    assert new["txt_1"].replaced_sidecar_paths == ["Show/E01.eng.srt", "Show/E01.old.srt"]
    assert new["txt_2"].replaced_sidecar_paths is None
    # The file is left for the new task to rewrite.
    assert (tmp_path / "Show" / "E01.eng.srt").exists()


def test_make_text_subtitles_refused_while_off_and_for_an_unknown_job(signing_key: bytes, tmp_path: Path) -> None:
    db = _done_encodes_db(tmp_path, config=_config(enabled=False))
    app, token = _jobs_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        off = client.post(f"/api/jobs/{JOB}/subtitle-ocr", headers=_auth(token))
        missing = client.post("/api/jobs/job_01JZXR7K3M5Q8N4VWA00000009/subtitle-ocr", headers=_auth(token))
    assert off.status_code == 409
    assert off.json()["detail"] == "text subtitles are off in Settings > Transcoding"
    assert missing.status_code == 404
    assert _ocr_rows(db) == []


def test_make_text_subtitles_with_nothing_to_read(signing_key: bytes, tmp_path: Path) -> None:
    db = FakeSession()
    _jobs_seed(db)
    db.rows["config"] = [_config()]
    db.rows["session_applications"] = []
    app, token = _jobs_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.post(f"/api/jobs/{JOB}/subtitle-ocr", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"tasks": [], "skipped": []}


# ---- the job page and the picker ----------------------------------------------------


async def test_job_view_counts_what_make_text_subtitles_would_read(tmp_path: Path) -> None:
    db = _done_encodes_db(tmp_path)
    (tmp_path / "Show" / "E01.eng.2.srt").write_text("1")
    job = db.rows["jobs"][0]
    view = await job_subtitle_ocr_view(db, job, tmp_path)  # type: ignore[arg-type]
    assert view is not None
    assert (view.eligible_encodes, view.with_text_subtitles, view.effective) == (2, 1, True)
    db.rows["config"] = [_config(enabled=False)]
    assert await job_subtitle_ocr_view(db, job, tmp_path) is None  # type: ignore[arg-type]


def test_job_detail_carries_the_view_and_each_tracks_ocr_status(signing_key: bytes, tmp_path: Path) -> None:
    db = _done_encodes_db(tmp_path)
    db.rows["jobs"][0].resumed_from_crash = False
    db.rows["tracks"][0].attempts = 0
    db.rows["transcode_tasks"].append(
        TranscodeTask(
            id="txt_9",
            session_application_id="sap_1",
            source_track_id="trk_1",
            kind=TranscodeTaskKind.SUBTITLE_OCR,
            parent_task_id="txt_1",
            status=TranscodeTaskStatus.FAILED,
            attempts=1,
            progress_pct=10,
            output_path="Show/E01.mkv",
        )
    )
    app, token = _jobs_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.get(f"/api/jobs/{JOB}", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subtitle_ocr"]["eligible_encodes"] == 2
    # The track's transcode status is its newest encode's; its OCR status, its newest OCR task's.
    assert body["tracks"][0]["transcode_status"] == "failed"
    assert body["tracks"][0]["subtitle_ocr_status"] == "failed"


def test_picker_shows_and_saves_the_choice(signing_key: bytes, tmp_path: Path) -> None:
    db = FakeSession()
    _jobs_seed(db)
    db.rows["config"] = [_config(default_on=False)]
    db.rows["jobs"][0].track_selection = {"subtitle_output": "both"}
    app, token = _jobs_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        before = client.get(f"/api/jobs/{JOB}/track-selection", headers=_auth(token)).json()
        # Only the choice: the track selection stays.
        r = client.put(f"/api/jobs/{JOB}/track-selection", json={"subtitle_ocr": True}, headers=_auth(token))
        assert r.status_code == 200, r.text
        assert r.json()["subtitle_ocr"] == {"override": True, "default": False, "effective": True}
        assert db.rows["jobs"][0].track_selection == {"subtitle_output": "both"}
        # Both: each is set; null takes the default.
        r2 = client.put(
            f"/api/jobs/{JOB}/track-selection",
            json={"track_selection": None, "subtitle_ocr": None},
            headers=_auth(token),
        )
        # An old-style body with neither still clears the track selection, and keeps the choice.
        db.rows["jobs"][0].subtitle_ocr = False
        r3 = client.put(f"/api/jobs/{JOB}/track-selection", json={}, headers=_auth(token))
        db.rows["config"] = [_config(enabled=False)]
        off = client.get(f"/api/jobs/{JOB}/track-selection", headers=_auth(token)).json()
    assert before["subtitle_ocr"] == {"override": None, "default": False, "effective": False}
    assert r2.json()["subtitle_ocr"]["override"] is None and db.rows["jobs"][0].track_selection is None
    assert r3.status_code == 200 and db.rows["jobs"][0].subtitle_ocr is False
    assert off["subtitle_ocr"] is None


def test_apply_session_sets_or_keeps_the_choice(signing_key: bytes, tmp_path: Path) -> None:
    db = FakeSession()
    _jobs_seed(db)
    app, token = _jobs_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.post(
            f"/api/jobs/{JOB}/transcode", json={"session_id": "ses_x", "subtitle_ocr": False}, headers=_auth(token)
        )
        assert r.status_code == 200, r.text
        assert db.rows["jobs"][0].subtitle_ocr is False
        r2 = client.post(
            f"/api/jobs/{JOB}/transcode", json={"session_id": "ses_x", "overwrite": True}, headers=_auth(token)
        )
    assert r2.status_code == 200, r2.text
    assert db.rows["jobs"][0].subtitle_ocr is False
