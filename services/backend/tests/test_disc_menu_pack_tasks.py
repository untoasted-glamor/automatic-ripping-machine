"""The `menu_pack` task (fork only, disc menus A3): what it builds (the plan `/register` hands it), the sweep that
queues it once a job's encodes finish, Build and Delete pack on the job page, the rebuild after a rename, and job
delete removing the job's own pack.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from arm_backend.db import get_session  # noqa: E402
from arm_backend.disc_menus import hooks, packs  # noqa: E402
from arm_backend.jwt_utils import issue_access_token  # noqa: E402
from arm_backend.routers import disc_menus as router_module  # noqa: E402
from arm_backend.routers import jobs as jobs_router  # noqa: E402
from arm_backend.task_kinds import rules_for  # noqa: E402
from arm_backend.transcode_dispatcher import TranscodeDispatcher  # noqa: E402
from arm_backend.ws import WSHub  # noqa: E402
from arm_common import (  # noqa: E402
    DiscType,
    Job,
    JobStatus,
    MediaType,
    MenuProposal,
    MenuProposalStatus,
    SessionApplication,
    SessionApplicationStatus,
    Track,
    TrackKind,
    TrackRole,
    TrackStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
    User,
)
from tests._fakes import FakeSession  # noqa: E402
from tests.test_retry_failed import _Hub  # noqa: E402
from tests.test_transcode_dispatcher import _app_with_one_task, _db_factory, _settings  # noqa: E402
from tests.test_transcoder_router import _HOSTNAME, _SERVICE_AUTH  # noqa: E402
from tests.test_transcoder_router import _make_app as _transcoder_app  # noqa: E402

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
MENU = f"/raw/{JOB}/menu"
SHOW = "Show (2020)"
PACK = f"{SHOW}/.discmenus/bd-d2"
NOW = datetime.now(UTC)


def _job(**kw: Any) -> Job:
    fields: dict[str, Any] = dict(
        id=JOB,
        disc_type=DiscType.BLURAY,
        title="Show",
        media_type=MediaType.TV,
        season=1,
        disc_number=2,
        status=JobStatus.RIPPED,
        ripped_at=NOW,
        metadata_json={
            "scan_result": {
                "titles": [
                    {"index": 0, "segment_map": "14", "source_playlist": "00009.mpls"},
                    {"index": 1, "segment_map": "15", "source_playlist": "00010.mpls"},
                    {"index": 5, "segment_map": "14,15", "source_playlist": "00008.mpls"},
                    {"index": 7, "segment_map": "27", "source_playlist": "00022.mpls"},
                    {"index": 8, "segment_map": "28,58", "source_playlist": "00023.mpls"},
                    {"index": 9, "segment_map": "29"},
                    {"index": 10, "segment_map": "30,31"},
                    {"index": 11, "segment_map": "32"},
                    {"index": 12, "segment_map": "33"},
                ]
            }
        },
    )
    fields.update(kw)
    return Job(**fields)


def _track(index: int, role: TrackRole | None, **kw: Any) -> Track:
    fields: dict[str, Any] = dict(
        id=f"trk_{index}",
        job_id=JOB,
        kind=TrackKind.VIDEO_TITLE,
        index=index,
        source_ref=str(index),
        role=role,
        status=TrackStatus.DONE,
        attempts=0,
    )
    fields.update(kw)
    return Track(**fields)


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


def _encode(track: str, path: str, status: TranscodeTaskStatus = TranscodeTaskStatus.DONE, **kw: Any) -> TranscodeTask:
    fields: dict[str, Any] = dict(
        id=f"txt_{track}",
        session_application_id="sap_x",
        source_track_id=f"trk_{track}",
        kind=TranscodeTaskKind.ENCODE,
        status=status,
        output_path=path,
        attempts=0,
        progress_pct=100,
        updated_at=NOW - timedelta(minutes=5),
    )
    fields.update(kw)
    return TranscodeTask(**fields)


def _pack_task(status: TranscodeTaskStatus = TranscodeTaskStatus.QUEUED, **kw: Any) -> TranscodeTask:
    fields: dict[str, Any] = dict(
        id="txt_pack",
        session_application_id="sap_x",
        source_track_id="trk_menu",
        kind=TranscodeTaskKind.MENU_PACK,
        status=status,
        attempts=0,
        progress_pct=0,
        output_path=PACK,
        created_at=NOW,
    )
    fields.update(kw)
    return TranscodeTask(**fields)


def _db(**job: Any) -> FakeSession:
    db = _app_with_one_task(TranscodeTaskStatus.DONE)
    db.rows["jobs"] = [_job(**job)]
    db.rows["tracks"] = [
        _menu_track(),
        _track(0, TrackRole.EPISODE, episode_number=10, season=1),
        _track(1, TrackRole.EPISODE, episode_number=11),
        _track(5, TrackRole.PLAY_ALL, excluded=True, status=TrackStatus.QUEUED),
        _track(7, TrackRole.EXTRA),
        _track(8, TrackRole.TRAILER),
        _track(9, TrackRole.SPECIAL, episode_number=4, season=0),
        _track(10, TrackRole.OTHER),
        _track(11, TrackRole.EPISODE),
        _track(12, TrackRole.SPECIAL),
    ]
    db.rows["transcode_tasks"] = [
        _encode("0", f"{SHOW}/Season 01/Show - S01E10.mkv"),
        _encode("1", f"{SHOW}/Season 01/Show - S01E11.mkv"),
        _encode("7", f"{SHOW}/extras/Panel.mkv"),
        _encode("8", f"{SHOW}/Season 01/Show - S01D02T08.mkv"),  # not named from the menu yet
        _encode("9", f"{SHOW}/Season 00/Show - S00E04 - Featurette.mkv"),
        _encode("10", f"{SHOW}/other/Opening.mkv"),
        _encode("11", f"{SHOW}/Season 01/Show - S01D02T11.mkv"),
        _encode("12", f"{SHOW}/Season 01/Show - S01D02T12.mkv"),
    ]
    return db


@pytest.fixture(autouse=True)
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A media folder holding every encode `_db` records (the backend checks they haven't moved)."""
    root = tmp_path / "media"
    for task in _db().rows["transcode_tasks"]:
        if task.kind == TranscodeTaskKind.ENCODE and task.output_path:
            (root / task.output_path).parent.mkdir(parents=True, exist_ok=True)
            (root / task.output_path).write_text("v")
    monkeypatch.setattr(packs, "_media_root", lambda: root)
    return root


def _tasks(db: FakeSession) -> list[TranscodeTask]:
    return [t for t in db.rows["transcode_tasks"] if t.kind == TranscodeTaskKind.MENU_PACK]


# ---- rules, names, folders ---------------------------------------------------------------


def test_a_pack_runs_in_the_transcode_image_with_raw_read_only() -> None:
    rules = rules_for(TranscodeTaskKind.MENU_PACK)
    assert (rules.encodes, rules.mounts_raw, rules.raw_writable, rules.image) == (False, True, False, "transcode")
    assert (rules.sidecar_suffix, rules.failed_owns_files) == (None, False)


def test_disc_id_and_name() -> None:
    assert (packs.disc_id(_job()), packs.disc_name(_job())) == ("bd-d2", "Show, Disc 2")
    dvd = _job(disc_type=DiscType.DVD, disc_number=None, title=None)
    assert (packs.disc_id(dvd), packs.disc_name(dvd)) == ("dvd-000001", "Disc menu")


def test_the_show_folder_is_above_season_and_bonus_folders() -> None:
    assert str(packs.show_folder([f"{SHOW}/Season 01/a.mkv", f"{SHOW}/Season 01/b.mkv"])) == SHOW
    assert str(packs.show_folder([f"{SHOW}/Season 01/a.mkv", f"{SHOW}/trailers/b.mkv"])) == SHOW
    assert str(packs.show_folder([f"{SHOW}/a.mkv"])) == SHOW
    assert packs.show_folder(["a.mkv", "b.mkv"]) is None
    assert packs.show_folder([]) is None


# ---- the plan ----------------------------------------------------------------------------


async def test_the_plan_names_each_title_and_says_what_it_left_out() -> None:
    db = _db()
    plan, reason = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert reason is None and plan is not None
    assert (plan.pack_dir, plan.name, plan.season, plan.job_id) == (PACK, "Show, Disc 2", 1, JOB)
    assert [i.model_dump(exclude_none=True) for i in plan.items] == [
        {"file": f"{SHOW}/Season 01/Show - S01E10.mkv", "episode": 10, "season": 1, "bd_clip": "00014"},
        {"file": f"{SHOW}/Season 01/Show - S01E11.mkv", "episode": 11, "bd_clip": "00015"},
        {"file": f"{SHOW}/extras/Panel.mkv", "extra": "extras/Panel.mkv", "bd_clip": "00027"},
        {"file": f"{SHOW}/Season 00/Show - S00E04 - Featurette.mkv", "episode": 4, "season": 0, "bd_clip": "00029"},
    ]
    assert plan.notes == [
        "title 8 (Show - S01D02T08.mkv): left out, a trailer that isn't in trailers/ yet (name it from the menu)",
        "title 10 (Opening.mkv): the disc scan doesn't say which disc title it is",
        "title 11 (Show - S01D02T11.mkv): left out, an episode with no episode number",
        "title 12 (Show - S01D02T12.mkv): left out, a special with no season 0 number",
    ]


async def test_a_title_of_several_clips_maps_by_its_playlist() -> None:
    db = _db()
    db.rows["transcode_tasks"][3].output_path = f"{SHOW}/trailers/Promo Videos.mkv"
    plan, _ = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is not None
    promo = next(i for i in plan.items if i.extra == "trailers/Promo Videos.mkv")
    assert (promo.bd_playlist, promo.bd_clip) == ("00023", None)


async def test_a_dvd_title_maps_by_its_title_number() -> None:
    db = _db(disc_type=DiscType.DVD, metadata_json={"scan_result": {"titles": [
        {"index": 0, "original_title_id": 3}, {"index": 1}]}})  # fmt: skip
    plan, _ = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is not None
    assert [(i.episode, i.dvd_title) for i in plan.items] == [(10, 3)]
    assert plan.notes[0] == "title 1 (Show - S01E11.mkv): the disc scan doesn't say which disc title it is"


async def test_a_movie_disc_maps_its_main_title_by_file() -> None:
    db = _db(media_type=MediaType.MOVIE)
    db.rows["tracks"][1].role = TrackRole.MAIN
    db.rows["tracks"][2].role = None
    plan, _ = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is not None and plan.season is None
    assert [(i.file, i.episode, i.extra) for i in plan.items][:2] == [
        (f"{SHOW}/Season 01/Show - S01E10.mkv", None, None),
        (f"{SHOW}/Season 01/Show - S01E11.mkv", None, None),
    ]
    assert "title 7 (Panel.mkv): left out, a extra on a movie disc isn't in Jellyfin's extras layout" in plan.notes


async def test_the_plan_skips_rows_that_are_not_titles_and_odd_refs() -> None:
    db = _db()
    db.rows["tracks"][1].source_ref = "x"  # not a scan index
    db.rows["transcode_tasks"].append(_encode("menu", f"{SHOW}/odd.mkv"))  # not a title track
    db.rows["transcode_tasks"].append(_encode("gone", f"{SHOW}/Season 01/gone.mkv"))  # its track was deleted
    plan, _ = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is not None
    assert plan.notes[0] == "title x (Show - S01E10.mkv): the disc scan doesn't say which disc title it is"


async def test_an_encode_outside_the_show_folder_is_left_out() -> None:
    db = _db()
    db.rows["transcode_tasks"] = [
        _encode("0", f"{SHOW}/Season 01/Show - S01E10.mkv"),
        _encode("1", "Other/Season 01/Show - S01E11.mkv"),
    ]
    plan, reason = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is None and reason == "the job's encodes share no show folder"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda db: db.rows.__setitem__("tracks", []), "its menu track is gone"),
        (lambda db: setattr(db.rows["tracks"][0], "status", TrackStatus.FAILED), "hasn't been copied"),
        (lambda db: db.rows.__setitem__("jobs", []), "job or session application is gone"),
        (lambda db: db.rows.__setitem__("transcode_tasks", []), "no finished encodes"),
        (lambda db: [setattr(t, "role", TrackRole.TRAILER) for t in db.rows["tracks"][1:]],
         "is a disc title the pack can play"),
    ],
    ids=["no-track", "not-copied", "no-job", "no-encodes", "nothing-playable"],
)  # fmt: skip
async def test_why_a_pack_cannot_be_built(change: Any, why: str) -> None:
    db = _db()
    change(db)
    plan, reason = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is None and reason is not None and why in reason
    assert (await packs.menu_pack_inputs(db, _pack_task()))[2] == reason  # type: ignore[arg-type]


async def test_inputs_hand_over_the_menu_folder_and_the_plan() -> None:
    menu_dir, plan, reason = await packs.menu_pack_inputs(_db(), _pack_task())  # type: ignore[arg-type]
    assert (menu_dir, reason) == (Path(MENU), None) and plan is not None


# ---- the sweep and Build -------------------------------------------------------------------


async def test_the_sweep_queues_one_pack_once_the_encodes_finish() -> None:
    db = _db()
    hub = _Hub()
    assert await packs.sweep_menu_packs(db, hub) == 1  # type: ignore[arg-type]
    [task] = _tasks(db)
    assert (task.session_application_id, task.source_track_id, task.output_path) == ("sap_x", "trk_menu", PACK)
    assert hub.events[0]["event_type"] == "task.queued"
    assert await packs.sweep_menu_packs(db) == 0  # type: ignore[arg-type]  # live
    task.status = TranscodeTaskStatus.FAILED
    task.created_at = NOW
    assert await packs.sweep_menu_packs(db) == 0  # type: ignore[arg-type]  # failed: waits for Build
    # An encode finished after it (a re-apply, Rip it): another build.
    db.rows["transcode_tasks"][0].updated_at = NOW + timedelta(minutes=1)
    assert await packs.sweep_menu_packs(db) == 1  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "change",
    [
        lambda db: setattr(db.rows["jobs"][0], "ripped_at", None),
        lambda db: setattr(db.rows["transcode_tasks"][0], "status", TranscodeTaskStatus.IN_PROGRESS),
        lambda db: setattr(db.rows["tracks"][0], "output_path", None),
        lambda db: db.rows.__setitem__("transcode_tasks", []),
        lambda db: db.rows.__setitem__("jobs", []),
    ],
    ids=["rip-not-complete", "encode-running", "menu-not-copied", "no-encodes", "job-gone"],
)
async def test_the_sweep_waits(change: Any) -> None:
    db = _db()
    change(db)
    assert await packs.sweep_menu_packs(db) == 0  # type: ignore[arg-type]
    assert not _tasks(db)


async def test_the_newest_application_with_a_finished_encode_owns_the_task(media: Path) -> None:
    (media / SHOW / "Season 01" / "E10.mkv").write_text("v")
    db = _db()
    db.rows["session_applications"].append(
        SessionApplication(id="sap_y", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.FAILED,
                           overwrite=False)
    )  # fmt: skip
    db.rows["session_applications"].append(
        SessionApplication(id="sap_z", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.DONE,
                           overwrite=False)
    )  # fmt: skip
    db.rows["transcode_tasks"].append(
        _encode("0", f"{SHOW}/Season 01/E10.mkv", id="txt_z", session_application_id="sap_z")
    )
    db.rows["transcode_tasks"].append(
        _encode(
            "1", f"{SHOW}/Season 01/E11.mkv", TranscodeTaskStatus.FAILED, id="txt_y", session_application_id="sap_y"
        )
    )
    await packs.sweep_menu_packs(db)  # type: ignore[arg-type]
    assert _tasks(db)[0].session_application_id == "sap_z"


async def test_a_pack_by_hand_and_why_it_is_refused() -> None:
    db = _db()
    job = db.rows["jobs"][0]
    task = await packs.queue_menu_pack(db, job, None)  # type: ignore[arg-type]
    assert task.status == TranscodeTaskStatus.QUEUED
    with pytest.raises(packs.PackRefused, match="already queued or running"):
        await packs.queue_menu_pack(db, job, None)  # type: ignore[arg-type]
    db.rows["transcode_tasks"].remove(task)
    db.rows["transcode_tasks"][0].status = TranscodeTaskStatus.QUEUED
    with pytest.raises(packs.PackRefused, match="still running"):
        await packs.queue_menu_pack(db, job, None)  # type: ignore[arg-type]
    db.rows["tracks"] = []
    with pytest.raises(packs.PackRefused, match="hasn't been copied"):
        await packs.queue_menu_pack(db, job, None)  # type: ignore[arg-type]


async def test_a_rename_rebuilds_a_pack_already_built() -> None:
    db = _db()
    job = db.rows["jobs"][0]
    assert await packs.rebuild_after_rename(db, job, None) is None  # type: ignore[arg-type]  # none built yet
    db.rows["transcode_tasks"].append(_pack_task(TranscodeTaskStatus.DONE))
    assert await packs.rebuild_after_rename(db, job, None) is not None  # type: ignore[arg-type]
    assert await packs.rebuild_after_rename(db, job, None) is None  # type: ignore[arg-type]  # one is live


# ---- the dispatcher, register and complete ---------------------------------------------------


def _dispatcher(db: FakeSession, docker: Any) -> TranscodeDispatcher:
    return TranscodeDispatcher(_settings(), _db_factory(db), docker, WSHub())


async def test_a_pack_spawns_the_transcode_image_with_raw_read_only_and_no_gpu() -> None:
    db = _db()
    db.rows["transcode_tasks"].append(_pack_task())
    docker = MagicMock()
    assert await _dispatcher(db, docker).spawn_pending(db) == 1  # type: ignore[arg-type]
    kwargs = docker.containers.run.call_args.kwargs
    assert kwargs["image"] == _settings().ARM_TRANSCODE_IMAGE
    assert kwargs["volumes"]["/host/raw"] == {"bind": "/raw", "mode": "ro"}
    assert not {"ARM_TRANSCODE_ENCODER", "ARM_GPU_VENDOR"} & set(kwargs["environment"])


async def test_a_pack_that_cannot_be_built_fails_before_any_spawn() -> None:
    db = _db()
    db.rows["tracks"][0].status = TrackStatus.FAILED
    db.rows["transcode_tasks"].append(_pack_task())
    docker = MagicMock()
    assert await _dispatcher(db, docker).spawn_pending(db) == 0  # type: ignore[arg-type]
    docker.containers.run.assert_not_called()
    assert _tasks(db)[0].last_error == "menu pack cannot be built: the job's disc menu hasn't been copied"


async def test_a_failure_to_fail_an_unbuildable_pack_does_not_abort_the_tick() -> None:
    db = _db()
    db.rows["tracks"][0].status = TrackStatus.FAILED
    db.rows["transcode_tasks"].append(_pack_task())
    disp = _dispatcher(db, MagicMock())

    async def boom(*args: Any) -> None:
        raise RuntimeError("db down")

    disp._fail_queued_task = boom  # type: ignore[method-assign]
    assert await disp.spawn_pending(db) == 0  # type: ignore[arg-type]
    assert _tasks(db)[0].status == TranscodeTaskStatus.QUEUED


async def test_the_tick_runs_the_pack_sweep() -> None:
    db = _db()
    await _dispatcher(db, None)._tick()
    assert len(_tasks(db)) == 1


def _register(db: FakeSession) -> Any:
    with TestClient(_transcoder_app(db)) as client:
        return client.post(
            "/api/transcoder/register",
            json={"task_id": "txt_pack", "hostname": _HOSTNAME, "hw_caps": {"cpu_count": 4}},
            headers=_SERVICE_AUTH,
        )


def test_register_hands_a_pack_its_menu_folder_and_plan() -> None:
    db = _db()
    db.rows["transcode_tasks"].append(_pack_task())
    r = _register(db)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"]["kind"] == "menu_pack"
    assert body["raw_input_path"] == MENU and body["transcode_preset"] is None
    assert body["menu_pack"]["pack_dir"] == PACK and len(body["menu_pack"]["items"]) == 4


def test_register_refuses_a_pack_that_cannot_be_built() -> None:
    db = _db()
    db.rows["transcode_tasks"] = [_pack_task()]
    r = _register(db)
    assert r.status_code == 409
    assert r.json()["detail"] == "menu pack cannot be built: the job has no finished encodes"


def test_complete_records_the_folder_it_wrote() -> None:
    db = _db()
    db.rows["transcode_tasks"].append(
        _pack_task(TranscodeTaskStatus.IN_PROGRESS, claimed_by=_HOSTNAME, claim_heartbeat_at=NOW)
    )
    with TestClient(_transcoder_app(db)) as client:
        r = client.patch(
            "/api/transcoder/tasks/txt_pack/complete",
            json={"output_path": f"{PACK}-2", "size_bytes": 10},
            headers=_SERVICE_AUTH,
        )
    assert r.status_code == 200, r.text
    assert _tasks(db)[0].output_path == f"{PACK}-2"


async def test_the_complete_hook_announces_a_pack() -> None:
    hub = _Hub()
    task = _pack_task(TranscodeTaskStatus.DONE)
    await hooks.after_task_complete(_db(), task, JOB, hub)  # type: ignore[arg-type]
    assert hub.events == [{"topic": "transcode.events", "event_type": "menu.pack",
                           "payload": {"job_id": JOB, "task_id": "txt_pack", "path": PACK}}]  # fmt: skip
    await hooks.after_task_complete(_db(), task, JOB, None)  # type: ignore[arg-type]


# ---- owning, deleting and showing the pack ---------------------------------------------------


def _write_pack(media: Path, folder: str = PACK, job: str | None = JOB) -> Path:
    path = media / folder
    path.mkdir(parents=True)
    (path / "menu.json").write_text(json.dumps({"disc": {"job": job}} if job else {"disc": "x"}))
    (path / "bg.jpg").write_bytes(b"12345")
    return path


def test_a_pack_folder_is_the_jobs_only_while_it_names_the_job(tmp_path: Path) -> None:
    media = tmp_path / "media"
    own = _write_pack(media)
    other = _write_pack(media, f"{PACK}-2", "job_other")
    _write_pack(media, f"{SHOW}/notapack/bd-d2")
    (media / "elsewhere").mkdir()
    assert packs.owned_pack_folder(media, PACK, JOB) == own.resolve()
    assert packs.owned_pack_folder(media, f"{PACK}-2", JOB) is None
    assert packs.owned_pack_folder(media, f"{SHOW}/notapack/bd-d2", JOB) is None
    assert packs.owned_pack_folder(media, "../outside/.discmenus/bd-d2", JOB) is None
    assert packs.owned_pack_folder(media, None, JOB) is None
    assert packs.pack_owner(other) == "job_other"
    assert packs.pack_owner(media / "elsewhere") is None
    (own / "menu.json").write_text("[1]")
    assert packs.pack_owner(own) is None
    assert packs.folder_size(other) == len((other / "menu.json").read_bytes()) + 5


async def test_job_delete_removes_only_the_jobs_own_packs(tmp_path: Path) -> None:
    media = tmp_path / "media"
    _write_pack(media)
    _write_pack(media, f"{PACK}-2", "job_other")
    db = _db()
    db.rows["transcode_tasks"] += [
        _pack_task(TranscodeTaskStatus.DONE, id="txt_p1"),
        _pack_task(TranscodeTaskStatus.DONE, id="txt_p2", output_path=f"{PACK}-2"),
        _pack_task(TranscodeTaskStatus.FAILED, id="txt_p3", output_path=f"{PACK}-3"),
    ]
    assert await packs.remove_job_packs(db, JOB, media) == 1  # type: ignore[arg-type]
    assert not (media / PACK).exists() and (media / f"{PACK}-2").exists()
    assert await packs.remove_job_packs(db, JOB, media) == 0  # type: ignore[arg-type]
    db.rows["tracks"] = []
    assert await packs.job_pack_folders(db, JOB, media) == []  # type: ignore[arg-type]


async def test_job_delete_survives_a_pack_it_cannot_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = tmp_path / "media"
    _write_pack(media)
    db = _db()
    db.rows["transcode_tasks"].append(_pack_task(TranscodeTaskStatus.DONE))

    def refuse(_folder: Path) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(packs, "remove_pack_folder", refuse)
    assert await packs.remove_job_packs(db, JOB, media) == 0  # type: ignore[arg-type]

    async def boom(*a: Any) -> list[Path]:
        raise RuntimeError("db down")

    monkeypatch.setattr(packs, "job_pack_folders", boom)
    assert await packs.remove_job_packs(db, JOB, media) == 0  # type: ignore[arg-type]


def test_removing_the_last_pack_removes_its_discmenus_folder(tmp_path: Path) -> None:
    media = tmp_path / "media"
    packs.remove_pack_folder(_write_pack(media))
    assert not (media / SHOW / ".discmenus").exists()
    _write_pack(media)
    packs.remove_pack_folder(_write_pack(media, f"{PACK}-2"))
    assert (media / SHOW / ".discmenus").exists()


def test_the_jobs_delete_route_removes_the_pack_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media, raw = tmp_path / "media", tmp_path / "raw"
    raw.mkdir()
    _write_pack(media)
    db = _db()
    db.rows["users"] = [User(id="usr_admin", username="admin", password_hash="x", password_must_change=False)]
    db.rows["transcode_tasks"].append(_pack_task(TranscodeTaskStatus.DONE))
    monkeypatch.setattr(jobs_router.settings, "MEDIA_ROOT", str(media))
    monkeypatch.setattr(jobs_router.settings, "RAW_ROOT", str(raw))
    client, auth = _client(db, jobs_router.router)
    assert client.delete(f"/api/jobs/{JOB}?delete_raw=true", headers=auth).status_code == 204
    assert not (media / PACK).exists()


def _client(db: FakeSession, router: Any) -> tuple[TestClient, dict[str, str]]:
    key = secrets.token_bytes(32)
    app = FastAPI()
    app.state.signing_key = key
    app.state.ws_hub = _Hub()
    app.include_router(router)

    async def _session() -> FakeSession:
        return db

    app.dependency_overrides[get_session] = _session
    token, _ = issue_access_token("usr_admin", "admin", key)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _panel_db() -> FakeSession:
    db = _db()
    db.rows["users"] = [User(id="usr_admin", username="admin", password_hash="x", password_must_change=False)]
    return db


def test_the_panel_shows_the_pack_and_build_and_delete_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = tmp_path / "media"
    monkeypatch.setattr(router_module.settings, "MEDIA_ROOT", str(media))
    db = _panel_db()
    client, auth = _client(db, router_module.router)
    base = f"/api/jobs/{JOB}/disc-menu"
    body = client.get(base, headers=auth).json()
    assert body["pack"] is None and body["pack_refusal"] is None

    r = client.post(f"{base}/pack", headers=auth)
    assert r.status_code == 200, r.text
    assert (
        r.json()["pack"]["status"] == "queued"
        and r.json()["pack_refusal"] == "a pack build is already queued or running"
    )
    assert client.post(f"{base}/pack", headers=auth).status_code == 409

    task = _tasks(db)[0]
    task.status = TranscodeTaskStatus.DONE
    _write_pack(media)
    body = client.get(base, headers=auth).json()
    assert body["pack"]["path"] == PACK and body["pack"]["size_bytes"] > 0

    r = client.delete(f"{base}/pack", headers=auth)
    assert r.status_code == 200 and r.json()["pack"]["path"] is None
    assert not (media / PACK).exists()
    assert client.delete(f"{base}/pack", headers=auth).status_code == 404


def test_delete_pack_reports_a_folder_it_cannot_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = tmp_path / "media"
    monkeypatch.setattr(router_module.settings, "MEDIA_ROOT", str(media))
    _write_pack(media)
    db = _panel_db()
    db.rows["transcode_tasks"].append(_pack_task(TranscodeTaskStatus.DONE))

    def refuse(_folder: Path) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(router_module, "remove_pack_folder", refuse)
    client, auth = _client(db, router_module.router)
    r = client.delete(f"/api/jobs/{JOB}/disc-menu/pack", headers=auth)
    assert r.status_code == 409 and "read-only" in r.json()["detail"]


def test_accepting_a_name_rebuilds_the_pack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = tmp_path / "media"
    monkeypatch.setattr(router_module.settings, "MEDIA_ROOT", str(media))
    db = _panel_db()
    db.rows["transcode_tasks"].append(_pack_task(TranscodeTaskStatus.DONE, created_at=NOW - timedelta(hours=1)))
    db.rows["menu_proposals"] = [
        MenuProposal(id="mnp_promo", job_id=JOB, track_id="trk_8", page="2", button=12, label="Promo Videos",
                     plays="00023.mpls", role=TrackRole.TRAILER, name="Promo Videos", status=MenuProposalStatus.PROPOSED),
        MenuProposal(id="mnp_x", job_id=JOB, track_id="trk_7", page="2", button=13, label="Panel",
                     plays="00022.mpls", role=TrackRole.EXTRA, name="Panel", status=MenuProposalStatus.PROPOSED),
    ]  # fmt: skip
    client, auth = _client(db, router_module.router)
    r = client.patch(f"/api/jobs/{JOB}/disc-menu/proposals/mnp_promo", json={"action": "accept"}, headers=auth)
    assert r.status_code == 200, r.text
    assert (media / SHOW / "trailers" / "Promo Videos.mkv").exists()
    assert [t.status for t in _tasks(db)] == ["done", "queued"]
    # Accept all: the second rename finds the rebuild already queued.
    _tasks(db)[1].status = TranscodeTaskStatus.DONE
    r = client.post(f"/api/jobs/{JOB}/disc-menu/proposals/accept-all", headers=auth)
    assert r.status_code == 200, r.text
    assert [t.status for t in _tasks(db)] == ["done", "done", "queued"]


async def test_odd_roles_and_naive_times() -> None:
    db = _db()
    db.rows["tracks"][2].role = None
    db.rows["transcode_tasks"][0].updated_at = datetime.now()  # a database that hands back naive times
    db.rows["transcode_tasks"].append(_pack_task(TranscodeTaskStatus.FAILED, created_at=None))
    plan, _ = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is not None
    assert "title 1 (Show - S01E11.mkv): left out, a title with role none" in plan.notes
    assert await packs.sweep_menu_packs(db) == 1  # type: ignore[arg-type]


def test_accepting_a_name_whose_encode_is_running_or_refused_queues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module.settings, "MEDIA_ROOT", str(tmp_path / "media"))
    db = _panel_db()
    db.rows["transcode_tasks"].append(_pack_task(TranscodeTaskStatus.DONE, created_at=NOW - timedelta(hours=1)))
    db.rows["transcode_tasks"][3].status = TranscodeTaskStatus.IN_PROGRESS
    db.rows["menu_proposals"] = [
        MenuProposal(id="mnp_promo", job_id=JOB, track_id="trk_8", page="2", button=12, label="Promo Videos",
                     plays="00023.mpls", role=TrackRole.TRAILER, name="Promo Videos", status=MenuProposalStatus.PROPOSED),
        MenuProposal(id="mnp_x", job_id=JOB, track_id="trk_7", page="2", button=13, label="Panel",
                     plays="00022.mpls", role=TrackRole.EXTRA, name="Panel 2", status=MenuProposalStatus.PROPOSED),
    ]  # fmt: skip
    (tmp_path / "media" / SHOW / "extras" / "Panel.mkv").unlink()
    client, auth = _client(db, router_module.router)
    r = client.patch(f"/api/jobs/{JOB}/disc-menu/proposals/mnp_promo", json={"action": "accept"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    # The panel's file isn't there: refused, so nothing was renamed.
    r = client.post(f"/api/jobs/{JOB}/disc-menu/proposals/accept-all", headers=auth)
    assert r.status_code == 200 and r.json()["refused"]
    assert [t.status for t in _tasks(db)] == ["done"]


async def test_a_job_whose_encodes_have_left_gets_no_pack(media: Path) -> None:
    import shutil

    shutil.rmtree(media)
    db = _db()
    assert await packs.sweep_menu_packs(db) == 0  # type: ignore[arg-type]
    assert await packs.pack_refusal(db, db.rows["jobs"][0], db.rows["tracks"][0]) == packs.MOVED  # type: ignore[arg-type]
    plan, reason = await packs.plan_for(db, _pack_task())  # type: ignore[arg-type]
    assert plan is None and reason == packs.MOVED


def test_the_media_root_is_the_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    assert packs._media_root() == Path(router_module.settings.MEDIA_ROOT)
