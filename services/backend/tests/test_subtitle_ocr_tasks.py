"""Subtitle OCR tasks (`transcode_tasks.kind = 'subtitle_ocr'`): the dispatcher
spawns them with no raw mount, GPU or preset and its own parallel limit; the
transcoder endpoints hand them their encode's image subtitles and record the
`.srt` files; and every encode-only path (aggregate, collisions, orphan sweep,
held-encode count, auto retry, job rollups, Transcoder stats) gives the same
result with an OCR row as without it.
"""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from fastapi.testclient import TestClient  # noqa: E402

from arm_backend.auto_session import _evict_colliding_tasks, _retry_failed_tasks  # noqa: E402
from arm_backend.routers import jobs as jobs_router  # noqa: E402
from arm_backend.sidecars import image_subtitle_sidecars  # noqa: E402
from arm_backend.subtitle_ocr_tasks import subtitle_ocr_inputs  # noqa: E402
from arm_backend.transcode_apply import aggregate_session_application, find_collisions  # noqa: E402
from arm_backend.transcode_dispatcher import TranscodeDispatcher, count_queued_encode_tasks  # noqa: E402
from arm_backend.ws import WSHub  # noqa: E402
from arm_common import (  # noqa: E402
    SessionApplication,
    SessionApplicationStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)
from tests._fakes import FakeSession  # noqa: E402
from tests.test_transcode_dispatcher import _app_with_one_task, _db_factory, _settings  # noqa: E402
from tests.test_transcoder_router import _HOSTNAME, _SERVICE_AUTH, _seed  # noqa: E402
from tests.test_transcoder_router import _make_app as _transcoder_app  # noqa: E402
from tests.test_transcodes_router import _auth, _task  # noqa: E402
from tests.test_transcodes_router import _make_app as _transcodes_app  # noqa: E402

_VIDEO = "Iron Man (2008)/Iron Man.mkv"
_SUPS = ["Iron Man (2008)/Iron Man.eng.sup", "Iron Man (2008)/Iron Man.eng.2.sup"]


def _ocr_task(
    task_id: str = "txt_ocr", *, status: TranscodeTaskStatus = TranscodeTaskStatus.QUEUED, **kw: Any
) -> TranscodeTask:
    fields: dict[str, Any] = dict(
        id=task_id,
        session_application_id="sap_x",
        source_track_id="trk_1",
        kind=TranscodeTaskKind.SUBTITLE_OCR,
        parent_task_id="txt_1",
        status=status,
        attempts=0,
        progress_pct=0,
        output_path=_VIDEO,
        created_at=datetime.now(UTC),
    )
    fields.update(kw)
    return TranscodeTask(**fields)


def _done_encode_with_ocr(*, passthrough: bool = False, **ocr: Any) -> FakeSession:
    """A DONE encode (txt_1) that wrote two PGS sidecars and a VobSub one, its
    application DONE, and a queued OCR task reading it."""
    db = _app_with_one_task(
        TranscodeTaskStatus.DONE,
        passthrough=passthrough,
        output_path=_VIDEO,
        sidecar_paths=[*_SUPS, "Iron Man (2008)/Iron Man.fra.mks"],
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db.rows["session_applications"][0].status = SessionApplicationStatus.DONE
    db.rows["transcode_tasks"].append(_ocr_task(**ocr))
    return db


def _dispatcher(db: FakeSession, docker: Any, **settings: Any) -> TranscodeDispatcher:
    return TranscodeDispatcher(_settings(**settings), _db_factory(db), docker, WSHub())


def _task_row(db: FakeSession, task_id: str) -> TranscodeTask:
    return next(t for t in db.rows["transcode_tasks"] if t.id == task_id)


# ---- inputs ------------------------------------------------------------------


def test_image_subtitle_sidecars_are_the_pgs_ones() -> None:
    assert image_subtitle_sidecars([*_SUPS, "a/b.fra.mks", "a/b.eng.SUP", "a/b.eng.srt"]) == [*_SUPS, "a/b.eng.SUP"]
    assert image_subtitle_sidecars(None) == []


async def test_inputs_come_from_the_done_encode() -> None:
    db = _done_encode_with_ocr()
    assert await subtitle_ocr_inputs(db, _task_row(db, "txt_ocr")) == (_SUPS, None)  # type: ignore[arg-type]


async def test_inputs_say_why_a_task_cannot_run() -> None:
    db = _done_encode_with_ocr()
    ocr = _task_row(db, "txt_ocr")
    encode = _task_row(db, "txt_1")
    cases = [
        (lambda: setattr(ocr, "parent_task_id", None), "its encode task is gone"),
        (lambda: setattr(ocr, "parent_task_id", "txt_missing"), "its encode task is gone"),
        (
            lambda: (setattr(ocr, "parent_task_id", "txt_1"), setattr(encode, "status", TranscodeTaskStatus.FAILED)),
            "its encode task is failed, not done",
        ),
        (
            lambda: (
                setattr(encode, "status", TranscodeTaskStatus.DONE),
                setattr(encode, "sidecar_paths", ["x.fra.mks"]),
            ),
            "its encode task wrote no image subtitles",
        ),
    ]
    for change, reason in cases:
        change()
        assert await subtitle_ocr_inputs(db, ocr) == ([], reason)  # type: ignore[arg-type]


# ---- dispatcher ----------------------------------------------------------------


async def test_ocr_task_spawns_the_base_image_with_media_only_and_no_gpu() -> None:
    db = _done_encode_with_ocr()
    docker = MagicMock()
    assert await _dispatcher(db, docker).spawn_pending(db) == 1  # type: ignore[arg-type]
    kwargs = docker.containers.run.call_args.kwargs
    assert kwargs["image"] == "arm-transcode:latest"
    assert kwargs["labels"] == {"arm.task_id": "txt_ocr"}
    assert "/host/raw" not in kwargs["volumes"]
    assert kwargs["volumes"]["/host/media"] == {"bind": "/media", "mode": "rw"}
    assert not {"ARM_TRANSCODE_ENCODER", "ARM_GPU_VENDOR", "ARM_GPU_DEVICE"} & set(kwargs["environment"])
    assert "devices" not in kwargs and "device_requests" not in kwargs


async def test_ocr_task_of_a_passthrough_session_still_spawns() -> None:
    """A session with no preset is passthrough for its encodes; its OCR task
    is never run in-process as a file move."""
    db = _done_encode_with_ocr(passthrough=True)
    docker = MagicMock()
    assert await _dispatcher(db, docker).spawn_pending(db) == 1  # type: ignore[arg-type]
    assert docker.containers.run.call_args.kwargs["labels"] == {"arm.task_id": "txt_ocr"}
    assert _task_row(db, "txt_ocr").status == TranscodeTaskStatus.QUEUED


async def test_ocr_tasks_and_encodes_have_separate_slots() -> None:
    # One encode running fills MAX_PARALLEL_TRANSCODES=1; the OCR task still spawns.
    db = _done_encode_with_ocr()
    db.rows["transcode_tasks"].append(
        TranscodeTask(
            id="txt_running",
            session_application_id="sap_x",
            source_track_id="trk_2",
            status=TranscodeTaskStatus.IN_PROGRESS,
            attempts=1,
            progress_pct=10,
            output_path="Other/Other.mkv",
            claimed_by="h",
            claim_heartbeat_at=datetime.now(UTC),
        )
    )
    docker = MagicMock()
    assert await _dispatcher(db, docker, MAX_PARALLEL_TRANSCODES=1).spawn_pending(db) == 1  # type: ignore[arg-type]
    assert docker.containers.run.call_args.kwargs["labels"] == {"arm.task_id": "txt_ocr"}

    # One OCR task running fills ARM_SUBTITLE_OCR_MAX_PARALLEL=1; an encode still spawns, a second OCR doesn't.
    db = _done_encode_with_ocr(
        status=TranscodeTaskStatus.IN_PROGRESS, claimed_by="h", claim_heartbeat_at=datetime.now(UTC)
    )
    db.rows["transcode_tasks"] += [
        _ocr_task("txt_ocr2"),
        TranscodeTask(
            id="txt_queued",
            session_application_id="sap_x",
            source_track_id="trk_2",
            status=TranscodeTaskStatus.QUEUED,
            attempts=0,
            progress_pct=0,
            output_path="Other/Other.mkv",
            created_at=datetime.now(UTC),
        ),
    ]
    docker = MagicMock()
    disp = _dispatcher(db, docker, MAX_PARALLEL_TRANSCODES=1, ARM_SUBTITLE_OCR_MAX_PARALLEL=1)
    assert await disp.spawn_pending(db) == 1  # type: ignore[arg-type]
    assert [c.kwargs["labels"] for c in docker.containers.run.call_args_list] == [{"arm.task_id": "txt_queued"}]


async def test_a_starting_ocr_container_holds_its_slot_not_an_encode_slot() -> None:
    db = _done_encode_with_ocr()
    db.rows["transcode_tasks"].append(_ocr_task("txt_ocr2"))
    docker = MagicMock()
    disp = _dispatcher(db, docker, MAX_PARALLEL_TRANSCODES=1, ARM_SUBTITLE_OCR_MAX_PARALLEL=1)
    assert await disp.spawn_pending(db) == 1  # type: ignore[arg-type]
    # Tick 2: txt_ocr hasn't claimed yet; it still holds the only OCR slot,
    # but an encode queued now gets the encode slot.
    db.rows["transcode_tasks"].append(
        TranscodeTask(
            id="txt_queued",
            session_application_id="sap_x",
            source_track_id="trk_2",
            status=TranscodeTaskStatus.QUEUED,
            attempts=0,
            progress_pct=0,
            output_path="Other/Other.mkv",
            created_at=datetime.now(UTC),
        )
    )
    assert await disp.spawn_pending(db) == 1  # type: ignore[arg-type]
    labels = [c.kwargs["labels"]["arm.task_id"] for c in docker.containers.run.call_args_list]
    assert labels == ["txt_ocr", "txt_queued"]


async def test_an_ocr_task_that_cannot_run_fails_before_any_spawn() -> None:
    db = _done_encode_with_ocr(parent_task_id=None)
    docker = MagicMock()
    assert await _dispatcher(db, docker).spawn_pending(db) == 0  # type: ignore[arg-type]
    docker.containers.run.assert_not_called()
    ocr = _task_row(db, "txt_ocr")
    assert ocr.status == TranscodeTaskStatus.FAILED
    assert ocr.last_error == "subtitle OCR cannot run: its encode task is gone"
    # The application is the encode's: it stays DONE.
    assert db.rows["session_applications"][0].status == SessionApplicationStatus.DONE


async def test_a_failure_to_fail_an_unrunnable_ocr_task_does_not_abort_the_tick() -> None:
    db = _done_encode_with_ocr(parent_task_id=None)
    db.rows["transcode_tasks"].append(_ocr_task("txt_ocr2", created_at=datetime.now(UTC) + timedelta(seconds=1)))
    docker = MagicMock()
    disp = _dispatcher(db, docker)

    async def boom(*args: Any) -> None:
        raise RuntimeError("db down")

    disp._fail_queued_task = boom  # type: ignore[method-assign]
    assert await disp.spawn_pending(db) == 1  # type: ignore[arg-type]
    assert docker.containers.run.call_args.kwargs["labels"] == {"arm.task_id": "txt_ocr2"}
    assert _task_row(db, "txt_ocr").status == TranscodeTaskStatus.QUEUED


async def test_ocr_tasks_are_held_like_encodes_without_docker() -> None:
    db = _done_encode_with_ocr()
    assert await _dispatcher(db, None).spawn_pending(db) == 0  # type: ignore[arg-type]
    assert _task_row(db, "txt_ocr").status == TranscodeTaskStatus.QUEUED


async def test_orphan_sweep_counts_encodes_only() -> None:
    old = datetime.now(UTC) - timedelta(hours=1)
    # Encodes all done, an OCR task still queued: the application settles.
    db = _done_encode_with_ocr()
    app = db.rows["session_applications"][0]
    app.status, app.created_at = SessionApplicationStatus.RUNNING, old
    disp = _dispatcher(db, MagicMock())
    assert await disp.sweep_orphaned_applications(db) == 1  # type: ignore[arg-type]
    assert app.status == SessionApplicationStatus.DONE

    # Only OCR rows left (the encode row was deleted): not a husk.
    db.rows["transcode_tasks"] = [t for t in db.rows["transcode_tasks"] if t.id == "txt_ocr"]
    app.status = SessionApplicationStatus.RUNNING
    assert await disp.sweep_orphaned_applications(db) == 0  # type: ignore[arg-type]
    assert app.status == SessionApplicationStatus.RUNNING


async def test_held_encode_count_leaves_ocr_tasks_out() -> None:
    db = _done_encode_with_ocr()
    assert await count_queued_encode_tasks(db) == 0  # type: ignore[arg-type]


# ---- transcoder endpoints ----------------------------------------------------------


def _router_db(**ocr: Any) -> FakeSession:
    db = FakeSession()
    _seed(db, task_status=TranscodeTaskStatus.DONE)
    encode = db.rows["transcode_tasks"][0]
    encode.output_path = _VIDEO
    encode.sidecar_paths = [*_SUPS]
    db.rows["session_applications"][0].status = SessionApplicationStatus.DONE
    db.rows["transcode_tasks"].append(_ocr_task(**ocr))
    return db


def test_register_hands_an_ocr_task_its_image_subtitles() -> None:
    db = _router_db()
    db.rows["tracks"][0].output_path = None  # the raw rip may be gone; OCR doesn't read it
    with TestClient(_transcoder_app(db)) as client:
        r = client.post(
            "/api/transcoder/register",
            json={"task_id": "txt_ocr", "hostname": _HOSTNAME, "hw_caps": {"cpu_count": 4}},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["kind"] == "subtitle_ocr" and body["task"]["parent_task_id"] == "txt_1"
    assert body["input_sidecar_paths"] == _SUPS
    assert body["raw_input_path"] is None and body["transcode_preset"] is None
    assert body["source_track"]["id"] == "trk_1"


def test_register_refuses_an_ocr_task_with_no_input() -> None:
    db = _router_db(parent_task_id=None)
    with TestClient(_transcoder_app(db)) as client:
        r = client.post(
            "/api/transcoder/register",
            json={"task_id": "txt_ocr", "hostname": _HOSTNAME, "hw_caps": {"cpu_count": 4}},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 409
    assert r.json()["detail"] == "subtitle OCR task cannot run: its encode task is gone"


def _running(db: FakeSession) -> TranscodeTask:
    ocr = _task_row(db, "txt_ocr")
    ocr.status, ocr.claimed_by, ocr.claim_heartbeat_at = TranscodeTaskStatus.IN_PROGRESS, _HOSTNAME, datetime.now(UTC)
    return ocr


def test_complete_records_only_the_srt_and_leaves_the_application() -> None:
    db = _router_db()
    ocr = _running(db)
    with TestClient(_transcoder_app(db)) as client:
        r = client.patch(
            "/api/transcoder/tasks/txt_ocr/complete",
            json={
                "output_path": _VIDEO,
                "sidecar_paths": ["Iron Man (2008)/Iron Man.eng.srt", _SUPS[0], "Elsewhere/x.eng.srt"],
            },
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    assert ocr.status == TranscodeTaskStatus.DONE
    assert ocr.sidecar_paths == ["Iron Man (2008)/Iron Man.eng.srt"]
    assert db.rows["session_applications"][0].status == SessionApplicationStatus.DONE
    # The encode's own sidecars are untouched.
    assert _task_row(db, "txt_1").sidecar_paths == _SUPS


def test_fail_keeps_the_srt_it_finished_and_leaves_the_application() -> None:
    db = _router_db()
    ocr = _running(db)
    with TestClient(_transcoder_app(db)) as client:
        r = client.patch(
            "/api/transcoder/tasks/txt_ocr/fail",
            json={"last_error": "Iron Man.eng.2.sup: PgsError", "sidecar_paths": ["Iron Man (2008)/Iron Man.eng.srt"]},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    assert ocr.status == TranscodeTaskStatus.FAILED and ocr.last_error == "Iron Man.eng.2.sup: PgsError"
    assert ocr.sidecar_paths == ["Iron Man (2008)/Iron Man.eng.srt"]
    assert db.rows["session_applications"][0].status == SessionApplicationStatus.DONE


def test_fail_without_files_keeps_what_was_recorded() -> None:
    db = _router_db(sidecar_paths=["Iron Man (2008)/Iron Man.eng.srt"])
    ocr = _running(db)
    with TestClient(_transcoder_app(db)) as client:
        r = client.patch("/api/transcoder/tasks/txt_ocr/fail", json={"last_error": "boom"}, headers=_SERVICE_AUTH)
    assert r.status_code == 200, r.text
    assert ocr.sidecar_paths == ["Iron Man (2008)/Iron Man.eng.srt"]


# ---- encode-only paths ---------------------------------------------------------


async def test_aggregate_ignores_ocr_tasks() -> None:
    db = _done_encode_with_ocr(status=TranscodeTaskStatus.FAILED)
    app: SessionApplication = db.rows["session_applications"][0]
    app.status = SessionApplicationStatus.RUNNING
    outcome = await aggregate_session_application(db, app)  # type: ignore[arg-type]
    assert app.status == SessionApplicationStatus.DONE and outcome.event_type == "session.completed"


async def test_collisions_ignore_an_ocr_tasks_video_path(tmp_path: Path) -> None:
    db = _done_encode_with_ocr()
    db.rows["transcode_tasks"] = [t for t in db.rows["transcode_tasks"] if t.id == "txt_ocr"]
    assert await find_collisions(db, [_VIDEO], tmp_path) == []  # type: ignore[arg-type]


async def test_auto_retry_requeues_failed_encodes_only() -> None:
    db = _done_encode_with_ocr(status=TranscodeTaskStatus.FAILED)
    encode = _task_row(db, "txt_1")
    encode.status = TranscodeTaskStatus.FAILED
    assert await _retry_failed_tasks(db, list(db.rows["transcode_tasks"])) == 1  # type: ignore[arg-type]
    assert encode.status == TranscodeTaskStatus.QUEUED
    assert _task_row(db, "txt_ocr").status == TranscodeTaskStatus.FAILED


async def test_overwrite_evicts_the_ocr_task_with_its_encode_and_hands_on_its_srt() -> None:
    srt_file = "Iron Man (2008)/Iron Man.eng.srt"
    db = _done_encode_with_ocr(status=TranscodeTaskStatus.DONE, sidecar_paths=[srt_file])
    replaced = await _evict_colliding_tasks(db, [_VIDEO], job_id="job_01JZXR7K3M5Q8N4VWA00000001")  # type: ignore[arg-type]
    assert replaced == {_VIDEO: [*_SUPS, "Iron Man (2008)/Iron Man.fra.mks", srt_file]}
    assert db.rows["transcode_tasks"] == []


async def test_job_detail_rollup_and_track_status_are_the_encodes() -> None:
    from arm_common import DiscType, Job, JobStatus, Track, TrackKind, TrackStatus

    db = _done_encode_with_ocr(status=TranscodeTaskStatus.IN_PROGRESS, id="txt_zzz_ocr", progress_pct=10)
    db.rows["jobs"] = [
        Job(
            id="job_01JZXR7K3M5Q8N4VWA00000001",
            drive_id="drv_x",
            disc_type=DiscType.BLURAY,
            title="Iron Man",
            year=2008,
            status=JobStatus.RIPPED,
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
            status=TrackStatus.DONE,
            attempts=0,
        )
    ]
    detail = await jobs_router.get_job_detail("job_01JZXR7K3M5Q8N4VWA00000001", None, db)  # type: ignore[arg-type]
    progress = detail.job.transcode_progress
    assert progress is not None and (progress.state, progress.tasks_total) == ("done", 1)
    assert detail.tracks[0].transcode_status == TranscodeTaskStatus.DONE

    jobs = await jobs_router.list_jobs(None, db, None, None, 50, 0)  # type: ignore[arg-type]
    assert jobs[0].transcode_progress is not None and jobs[0].transcode_progress.tasks_total == 1


def test_transcoder_stats_and_workers_count_ocr_apart() -> None:
    key = secrets.token_bytes(32)
    db = FakeSession()
    db.rows["transcode_tasks"] = [
        _task("txt_1", status=TranscodeTaskStatus.IN_PROGRESS),
        _task("txt_2", status=TranscodeTaskStatus.DONE),
        _ocr_task("txt_ocr", status=TranscodeTaskStatus.IN_PROGRESS),
        _ocr_task("txt_ocr2", status=TranscodeTaskStatus.FAILED),
    ]
    app, token = _transcodes_app(key, db)
    with TestClient(app) as c:
        stats = c.get("/api/transcodes/stats", headers=_auth(token)).json()
        workers = {w["task_id"]: w["kind"] for w in c.get("/api/transcodes/workers", headers=_auth(token)).json()}
        listed = {t["id"]: t["kind"] for t in c.get("/api/transcodes", headers=_auth(token)).json()}
    assert stats["tasks_by_status"] == {"in_progress": 1, "done": 1} and stats["total_tasks"] == 2
    assert stats["subtitle_ocr_tasks_by_status"] == {"in_progress": 1, "failed": 1}
    assert workers == {"txt_1": "encode", "txt_ocr": "subtitle_ocr"}
    assert listed["txt_ocr2"] == "subtitle_ocr" and listed["txt_2"] == "encode"
