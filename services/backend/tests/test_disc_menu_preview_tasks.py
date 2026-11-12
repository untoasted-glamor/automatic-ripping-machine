"""The `menu_preview` task (fork only, disc menus M1): queued by the dispatcher sweep once a job's menu is copied and
its rip is complete, spawned in the menu preview image with `/raw` read-write and no GPU, registered with the menu
folder, and turned into name proposals at /complete.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from arm_backend.disc_menus import hooks  # noqa: E402
from arm_backend.disc_menus.preview_tasks import (  # noqa: E402
    PreviewRefused,
    menu_dir_of,
    menu_preview_inputs,
    queue_menu_preview,
    sweep_menu_previews,
)
from arm_backend.task_kinds import rules_for  # noqa: E402
from arm_backend.transcode_dispatcher import TranscodeDispatcher  # noqa: E402
from arm_backend.ws import WSHub  # noqa: E402
from arm_common import (  # noqa: E402
    DiscType,
    Job,
    JobStatus,
    SessionApplication,
    SessionApplicationStatus,
    Track,
    TrackKind,
    TrackRole,
    TrackStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)
from tests._fakes import FakeSession  # noqa: E402
from tests.test_retry_failed import _Hub  # noqa: E402
from tests.test_transcode_dispatcher import _app_with_one_task, _db_factory, _settings  # noqa: E402
from tests.test_transcoder_router import _HOSTNAME, _SERVICE_AUTH  # noqa: E402
from tests.test_transcoder_router import _make_app as _transcoder_app  # noqa: E402

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
MENU = f"/raw/{JOB}/menu"


def _menu_track(**kw: Any) -> Track:
    fields: dict[str, Any] = dict(
        id="trk_menu",
        job_id=JOB,
        kind=TrackKind.DISC_MENU,
        index=-1,
        source_ref="menu",
        status=TrackStatus.DONE,
        output_path=MENU,
        attempts=0,
    )
    fields.update(kw)
    return Track(**fields)


def _preview_task(status: TranscodeTaskStatus = TranscodeTaskStatus.QUEUED, **kw: Any) -> TranscodeTask:
    fields: dict[str, Any] = dict(
        id="txt_prev",
        session_application_id="sap_x",
        source_track_id="trk_menu",
        kind=TranscodeTaskKind.MENU_PREVIEW,
        status=status,
        attempts=0,
        progress_pct=0,
        output_path=f"{MENU}/preview",
        created_at=datetime.now(UTC),
    )
    fields.update(kw)
    return TranscodeTask(**fields)


def _db(*, ripped: bool = True, application: bool = True, track: Track | None = None) -> FakeSession:
    db = _app_with_one_task(TranscodeTaskStatus.DONE)
    if not application:
        db.rows["session_applications"] = []
    db.rows["jobs"] = [
        Job(
            id=JOB,
            disc_type=DiscType.BLURAY,
            title="Show",
            status=JobStatus.RIPPED,
            metadata_json={},
            ripped_at=datetime.now(UTC) if ripped else None,
        )
    ]
    db.rows["tracks"] = [track or _menu_track()]
    return db


def _tasks(db: FakeSession, kind: TranscodeTaskKind = TranscodeTaskKind.MENU_PREVIEW) -> list[TranscodeTask]:
    return [t for t in db.rows["transcode_tasks"] if t.kind == kind]


# ---- rules and inputs -------------------------------------------------------------


def test_a_preview_runs_in_its_own_image_with_raw_read_write() -> None:
    rules = rules_for(TranscodeTaskKind.MENU_PREVIEW)
    assert (rules.encodes, rules.mounts_raw, rules.raw_writable, rules.image) == (False, True, True, "menu_preview")
    assert rules_for(TranscodeTaskKind.ENCODE).image == "transcode"


def test_menu_dir_of_needs_a_done_menu_track_with_its_folder() -> None:
    assert menu_dir_of(_menu_track()) == Path(MENU)
    assert menu_dir_of(_menu_track(status=TrackStatus.SKIPPED)) is None
    assert menu_dir_of(_menu_track(output_path=None)) is None
    assert menu_dir_of(_menu_track(kind=TrackKind.VIDEO_TITLE)) is None


async def test_inputs_say_why_a_preview_cannot_run() -> None:
    db = _db()
    task = _preview_task()
    assert await menu_preview_inputs(db, task) == (Path(MENU), None)  # type: ignore[arg-type]
    db.rows["tracks"][0].status = TrackStatus.FAILED
    assert await menu_preview_inputs(db, task) == (None, "the job's disc menu hasn't been copied")  # type: ignore[arg-type]
    db.rows["tracks"] = []
    assert await menu_preview_inputs(db, task) == (None, "its menu track is gone")  # type: ignore[arg-type]


# ---- the sweep ----------------------------------------------------------------------


async def test_the_sweep_queues_one_preview_once_the_rip_is_complete() -> None:
    db = _db()
    hub = _Hub()
    assert await sweep_menu_previews(db, hub) == 1  # type: ignore[arg-type]
    [task] = _tasks(db)
    assert (task.session_application_id, task.source_track_id, task.status) == ("sap_x", "trk_menu", "queued")
    assert task.output_path == f"{MENU}/preview"
    assert hub.events[0]["event_type"] == "task.queued"
    # Never twice, even once that one failed.
    task.status = TranscodeTaskStatus.FAILED
    assert await sweep_menu_previews(db) == 0  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "db",
    [
        _db(ripped=False),
        _db(application=False),
        _db(track=_menu_track(output_path=None)),
        _db(track=_menu_track(status=TrackStatus.IN_PROGRESS)),
    ],
    ids=["rip-not-complete", "no-application", "no-folder", "not-copied"],
)
async def test_the_sweep_waits(db: FakeSession) -> None:
    assert await sweep_menu_previews(db) == 0  # type: ignore[arg-type]
    assert not _tasks(db)


async def test_the_sweep_skips_a_menu_whose_job_is_gone() -> None:
    db = _db()
    db.rows["jobs"] = []
    assert await sweep_menu_previews(db) == 0  # type: ignore[arg-type]


async def test_the_newest_application_owns_the_task() -> None:
    db = _db()
    db.rows["session_applications"].append(
        SessionApplication(
            id="sap_y", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.DONE, overwrite=False
        )
    )
    await sweep_menu_previews(db)  # type: ignore[arg-type]
    assert _tasks(db)[0].session_application_id == "sap_y"


async def test_a_preview_by_hand() -> None:
    db = _db()
    job = db.rows["jobs"][0]
    db.rows["transcode_tasks"].append(_preview_task(TranscodeTaskStatus.FAILED))
    task = await queue_menu_preview(db, job, None)  # type: ignore[arg-type]
    assert task.status == TranscodeTaskStatus.QUEUED
    with pytest.raises(PreviewRefused, match="already queued or running"):
        await queue_menu_preview(db, job, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("db", "why"),
    [
        (_db(track=_menu_track(status=TrackStatus.SKIPPED)), "hasn't been copied"),
        (_db(application=False), "no session applied"),
    ],
)
async def test_a_preview_by_hand_is_refused(db: FakeSession, why: str) -> None:
    with pytest.raises(PreviewRefused, match=why):
        await queue_menu_preview(db, db.rows["jobs"][0], None)  # type: ignore[arg-type]


async def test_a_preview_by_hand_needs_a_menu_track() -> None:
    db = _db()
    db.rows["tracks"] = []
    with pytest.raises(PreviewRefused):
        await queue_menu_preview(db, db.rows["jobs"][0], None)  # type: ignore[arg-type]


# ---- the dispatcher -----------------------------------------------------------------


def _dispatcher(db: FakeSession, docker: Any, **settings: Any) -> TranscodeDispatcher:
    return TranscodeDispatcher(_settings(**settings), _db_factory(db), docker, WSHub())


async def test_a_preview_spawns_its_image_with_raw_read_write_and_no_gpu() -> None:
    db = _db()
    db.rows["transcode_tasks"].append(_preview_task())
    docker = MagicMock()
    disp = _dispatcher(db, docker, ARM_MENU_PREVIEW_IMAGE="arm-menu-preview:latest")
    assert await disp.spawn_pending(db) == 1  # type: ignore[arg-type]
    kwargs = docker.containers.run.call_args.kwargs
    assert kwargs["image"] == "arm-menu-preview:latest"
    assert kwargs["volumes"]["/host/raw"] == {"bind": "/raw", "mode": "rw"}
    assert not {"ARM_TRANSCODE_ENCODER", "ARM_GPU_VENDOR"} & set(kwargs["environment"])


async def test_a_preview_is_held_while_its_image_is_missing() -> None:
    db = _db()
    db.rows["transcode_tasks"].append(_preview_task())
    docker = MagicMock()
    disp = _dispatcher(db, docker, ARM_MENU_PREVIEW_IMAGE="arm-menu-preview:latest")
    disp.menu_preview_image_available = lambda: False  # type: ignore[method-assign]
    assert await disp.spawn_pending(db) == 0  # type: ignore[arg-type]
    docker.containers.run.assert_not_called()
    assert _tasks(db)[0].status == TranscodeTaskStatus.QUEUED


def test_the_preview_image_is_pulled_when_absent() -> None:
    disp = _dispatcher(FakeSession(), MagicMock(), ARM_MENU_PREVIEW_IMAGE="arm-menu-preview:latest")
    disp.image_exists = lambda image: False  # type: ignore[method-assign]
    pulled: list[str] = []
    disp._pull_image = lambda image, **kw: pulled.append(image) or True  # type: ignore[method-assign]
    assert disp.menu_preview_image_available() is True
    assert pulled == ["arm-menu-preview:latest"]
    disp.image_exists = lambda image: True  # type: ignore[method-assign]
    assert disp.menu_preview_image_available() is True
    assert _dispatcher(FakeSession(), None).menu_preview_image_available() is False


async def test_a_preview_that_cannot_run_fails_before_any_spawn() -> None:
    db = _db(track=_menu_track(status=TrackStatus.FAILED))
    db.rows["transcode_tasks"].append(_preview_task())
    docker = MagicMock()
    assert await _dispatcher(db, docker).spawn_pending(db) == 0  # type: ignore[arg-type]
    docker.containers.run.assert_not_called()
    task = _tasks(db)[0]
    assert task.status == TranscodeTaskStatus.FAILED
    assert task.last_error == "menu preview cannot run: the job's disc menu hasn't been copied"


async def test_a_failure_to_fail_an_unrunnable_preview_does_not_abort_the_tick() -> None:
    db = _db(track=_menu_track(status=TrackStatus.FAILED))
    db.rows["transcode_tasks"].append(_preview_task())
    disp = _dispatcher(db, MagicMock())

    async def boom(*args: Any) -> None:
        raise RuntimeError("db down")

    disp._fail_queued_task = boom  # type: ignore[method-assign]
    assert await disp.spawn_pending(db) == 0  # type: ignore[arg-type]
    assert _tasks(db)[0].status == TranscodeTaskStatus.QUEUED


async def test_the_tick_runs_the_sweep() -> None:
    db = _db()
    disp = _dispatcher(db, None)
    await disp._tick()
    assert len(_tasks(db)) == 1


# ---- register and complete ----------------------------------------------------------


def _router_db(preview_dir: Path | None = None) -> FakeSession:
    db = _db()
    db.rows["sessions"][0].output_path_template = "{title}/{title}.mkv"
    db.rows["tracks"].append(
        Track(
            id="trk_7",
            job_id=JOB,
            kind=TrackKind.VIDEO_TITLE,
            index=7,
            source_ref="7",
            status=TrackStatus.DONE,
            role=TrackRole.EXTRA,
            attempts=0,
        )
    )
    db.rows["jobs"][0].metadata_json = {"scan_result": {"titles": [{"index": 7, "segment_map": "27"}]}}
    output = str(preview_dir) if preview_dir is not None else f"{MENU}/preview"
    db.rows["transcode_tasks"].append(
        _preview_task(
            TranscodeTaskStatus.IN_PROGRESS,
            claimed_by=_HOSTNAME,
            claim_heartbeat_at=datetime.now(UTC),
            output_path=output,
        )
    )
    return db


def test_register_hands_a_preview_its_menu_folder() -> None:
    db = _router_db()
    with TestClient(_transcoder_app(db)) as client:
        r = client.post(
            "/api/transcoder/register",
            json={"task_id": "txt_prev", "hostname": _HOSTNAME, "hw_caps": {"cpu_count": 4}},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["kind"] == "menu_preview"
    assert body["raw_input_path"] == MENU and body["transcode_preset"] is None


def test_register_refuses_a_preview_with_no_menu() -> None:
    db = _router_db()
    db.rows["tracks"][0].status = TrackStatus.FAILED
    with TestClient(_transcoder_app(db)) as client:
        r = client.post(
            "/api/transcoder/register",
            json={"task_id": "txt_prev", "hostname": _HOSTNAME, "hw_caps": {"cpu_count": 4}},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 409
    assert r.json()["detail"] == "menu preview cannot run: the job's disc menu hasn't been copied"


def _preview_json(button: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "menu-preview-1",
        "disc": "bd",
        "pages": [{"id": "1", "at": "m p0", "language": "eng", "image": "pages/1.jpg", "size": [1920, 1080],
                   "buttons": [button]}],
        "playlists": {"00022": {"items": [{"clip": "00027", "in": 0, "out": 100}], "marks": []}},
    }  # fmt: skip


def test_complete_makes_the_proposals(tmp_path: Path) -> None:
    preview = _preview_json(
        {"id": 11, "box": [0, 0, 1, 1], "visible": True, "label": "Promo Videos", "score": 0.99,
         "outcome": {"kind": "plays", "playlist": "00022", "start": 0, "mark": 0}}
    )  # fmt: skip
    (tmp_path / "preview.json").write_text(json.dumps(preview))
    db = _router_db(tmp_path)
    with TestClient(_transcoder_app(db)) as client:
        r = client.patch(
            "/api/transcoder/tasks/txt_prev/complete",
            json={"output_path": str(tmp_path), "size_bytes": 10},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    [row] = db.rows["menu_proposals"]
    assert (row.track_id, row.role, row.name, row.status) == ("trk_7", TrackRole.TRAILER, "Promo Videos", "proposed")
    assert row.task_id == "txt_prev"


async def test_the_hook_never_raises_and_skips_an_unreadable_preview(tmp_path: Path) -> None:
    db = _router_db(tmp_path)  # no preview.json there
    task = _tasks(db)[0]
    await hooks.after_task_complete(db, task, JOB, None)  # type: ignore[arg-type]
    assert "menu_proposals" not in db.rows
    (tmp_path / "preview.json").write_text("[]")  # not an object
    await hooks.after_task_complete(db, task, JOB, None)  # type: ignore[arg-type]
    (tmp_path / "preview.json").write_text(json.dumps(_preview_json({"id": 1, "outcome": {}})))
    db.rows["jobs"] = []
    await hooks.after_task_complete(db, task, JOB, None)  # type: ignore[arg-type]
    assert "menu_proposals" not in db.rows

    async def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("bad")

    db.rows["jobs"] = [Job(id=JOB, disc_type=DiscType.BLURAY, metadata_json={})]
    original = hooks.make_proposals
    hooks.make_proposals = boom  # type: ignore[assignment]
    try:
        await hooks.after_task_complete(db, task, JOB, None)  # type: ignore[arg-type]
    finally:
        hooks.make_proposals = original


async def test_the_hook_applies_a_pending_rename_when_an_encode_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def fake(db: Any, encode: TranscodeTask, media_root: Path) -> int:
        seen.append(encode.id)
        return 0

    monkeypatch.setattr(hooks, "apply_pending", fake)
    encode = TranscodeTask(id="txt_1", session_application_id="sap_x", source_track_id="trk_7")
    await hooks.after_task_complete(FakeSession(), encode, JOB, None)  # type: ignore[arg-type]
    assert seen == ["txt_1"]
    # Other kinds: nothing.
    ocr = TranscodeTask(id="txt_o", session_application_id="sap_x", source_track_id="trk_7",
                        kind=TranscodeTaskKind.SUBTITLE_OCR)  # fmt: skip
    await hooks.after_task_complete(FakeSession(), ocr, JOB, None)  # type: ignore[arg-type]
    assert seen == ["txt_1"]


async def test_the_hook_emits_once_proposals_exist(tmp_path: Path) -> None:
    (tmp_path / "preview.json").write_text(json.dumps(_preview_json({"id": 1, "outcome": {}})))
    db = _router_db(tmp_path)
    hub = _Hub()
    await hooks.after_task_complete(db, _tasks(db)[0], JOB, hub)  # type: ignore[arg-type]
    assert [e["event_type"] for e in hub.events] == ["menu.proposals"]
