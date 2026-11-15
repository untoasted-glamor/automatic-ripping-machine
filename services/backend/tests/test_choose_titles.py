"""Stop 2 (fork only): a DVD/BD from a drive is backed up whole, parks at `awaiting_titles` (`rip-parked`), opens once
its menu preview has ended (the default ticks applied), starts like stop 1 (`stop_may_start`) through retry-failed's
path, and its backup goes at rip-complete once no title failed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402

import arm_backend.choose_titles as choose_module  # noqa: E402
import arm_backend.raw_backup as raw_backup_module  # noqa: E402
from arm_backend.choose_titles import (  # noqa: E402
    default_ticks,
    delete_parked_backup,
    parks_after_backup,
    redoes_first_pass,
    sweep_choose_titles,
)
from arm_backend.disc_menus.preview_tasks import preview_settled, sweep_menu_previews  # noqa: E402
from arm_common import (  # noqa: E402
    DiscType,
    Drive,
    DriveStatus,
    Job,
    JobStatus,
    MediaType,
    SessionApplication,
    SessionApplicationStatus,
    TrackKind,
    TrackRole,
    TrackStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)
from arm_common.enums import DriveSourceKind, MenuProposalStatus  # noqa: E402
from arm_common.models import Track  # noqa: E402
from arm_common.models.menu_proposal import MenuProposal  # noqa: E402
from arm_common.schemas.job_metadata import flag_is_set  # noqa: E402

from tests._fakes import FakeSession  # noqa: E402
from tests.test_retry_failed import (  # noqa: E402
    _SCAN,
    _SERVICE_AUTH,
    JOB_ID,
    _backup,
    _config,
    _Hub,
    _jobs,
    _ripper,
)

MENU = f"/raw/{JOB_ID}/menu"


@pytest.fixture
def raw_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(raw_backup_module.settings, "RAW_ROOT", str(tmp_path))
    return tmp_path


def _title(
    index: int,
    *,
    role: TrackRole | None = None,
    excluded: bool = False,
    status: TrackStatus = TrackStatus.QUEUED,
    **kw: Any,
) -> Track:
    return Track(
        id=f"trk_{index}",
        job_id=JOB_ID,
        kind=TrackKind.VIDEO_TITLE,
        index=index,
        source_ref=str(index),
        status=status,
        role=role,
        excluded=excluded,
        attempts=0,
        **kw,
    )


def _menu() -> Track:
    return Track(
        id="trk_menu",
        job_id=JOB_ID,
        kind=TrackKind.DISC_MENU,
        index=-1,
        source_ref="menu",
        status=TrackStatus.DONE,
        output_path=MENU,
        attempts=1,
    )


def _proposal(track_id: str, role: TrackRole, status: MenuProposalStatus = MenuProposalStatus.PROPOSED) -> MenuProposal:
    return MenuProposal(
        id=f"mnp_{track_id}_{role.value}",
        job_id=JOB_ID,
        track_id=track_id,
        page="p",
        button=1,
        role=role,
        status=status,
    )


def _job(status: JobStatus = JobStatus.AWAITING_TITLES, **kw: Any) -> Job:
    fields: dict[str, Any] = dict(
        id=JOB_ID,
        drive_id="drv_x",
        disc_type=DiscType.BLURAY,
        media_type=MediaType.TV,
        status=status,
        title="Toilet-Bound Hanako-kun",
        metadata_json={"scan_result": _SCAN, "flags": {"parked": True}},
        resumed_from_crash=False,
        manual_pause=False,
    )
    fields.update(kw)
    return Job(**fields)


def _db(*, job: Job | None = None, tracks: list[Track] | None = None, **cfg: Any) -> FakeSession:
    db = FakeSession()
    config = _config()
    for key, value in cfg.items():
        setattr(config, key, value)
    db.rows["config"] = [config]
    db.rows["drives"] = [Drive(id="drv_x", hostname="host", device_path="/dev/sr0", status=DriveStatus.ONLINE)]
    db.rows["jobs"] = [job or _job()]
    db.rows["tracks"] = (
        tracks
        if tracks is not None
        else [
            _title(0, role=TrackRole.EPISODE, excluded=True),
            _title(1, role=TrackRole.EPISODE),
            _title(2, role=TrackRole.PLAY_ALL),
            _title(3, excluded=True),
        ]
    )
    db.rows["menu_proposals"] = []
    db.rows["transcode_tasks"] = []
    db.rows["session_applications"] = []
    return db


def _with_preview(db: FakeSession, status: TranscodeTaskStatus | None) -> None:
    """A copied menu and a session application; with `status`, a preview task in it."""
    db.rows["tracks"].append(_menu())
    db.rows["session_applications"] = [
        SessionApplication(
            id="sap_x", session_id="ses_x", job_id=JOB_ID, status=SessionApplicationStatus.WAITING_IDENTIFY
        )
    ]
    if status is not None:
        db.rows["transcode_tasks"] = [
            TranscodeTask(
                id="txt_prev",
                session_application_id="sap_x",
                source_track_id="trk_menu",
                kind=TranscodeTaskKind.MENU_PREVIEW,
                status=status,
                attempts=0,
                progress_pct=0,
                output_path=f"{MENU}/preview",
            )
        ]


def _excluded(db: FakeSession) -> dict[str, bool]:
    return {t.id: t.excluded for t in db.rows["tracks"] if t.kind == TrackKind.VIDEO_TITLE}


# --- parks_after_backup -----------------------------------------------------------------


def _physical() -> Drive:
    return Drive(id="drv_x", hostname="host", device_path="/dev/sr0", status=DriveStatus.ONLINE)


@pytest.mark.parametrize(
    ("job", "drive", "tracks", "retry", "parks"),
    [
        (_job(JobStatus.RIPPING), _physical(), [_title(0)], False, True),
        (_job(JobStatus.RIPPING, disc_type=DiscType.DVD), _physical(), [_title(0)], False, True),
        (_job(JobStatus.RIPPING), _physical(), [_title(0)], True, False),
        (_job(JobStatus.REMUXING), _physical(), [_title(0)], False, False),
        (_job(JobStatus.RIPPING, disc_type=DiscType.CD), _physical(), [_title(0)], False, False),
        (
            _job(JobStatus.RIPPING),
            Drive(id="drv_x", hostname="h", device_path="/x.iso", source_kind=DriveSourceKind.ISO),
            [_title(0)],
            False,
            False,
        ),
        (_job(JobStatus.RIPPING), None, [_title(0)], False, False),
        (
            _job(JobStatus.RIPPING),
            _physical(),
            [Track(id="trk_b", job_id=JOB_ID, kind=TrackKind.DISC_BACKUP, index=0, source_ref="0", attempts=0)],
            False,
            False,
        ),
    ],
    ids=["bluray", "dvd", "retry", "remux", "cd", "iso", "no-drive", "folder-backup"],
)
def test_parks_after_backup(job: Job, drive: Drive | None, tracks: list[Track], retry: bool, parks: bool) -> None:
    assert parks_after_backup(job, drive, tracks, retry=retry) is parks


# --- default ticks ------------------------------------------------------------------------


def test_default_ticks_keep_episodes_specials_op_ed_and_trailers_and_leave_play_alls_out() -> None:
    tracks = [
        _title(0, role=TrackRole.EPISODE, excluded=True),
        _title(1, excluded=True),  # the menu calls it a special
        _title(2, excluded=True),  # OP/ED
        _title(3, excluded=True),  # trailer
        _title(4),  # the menu calls it a play-all
        _title(5, role=TrackRole.EXTRA, excluded=True),  # an extra keeps the preset's default
        _title(6, excluded=True),  # a rejected proposal says nothing
        _title(7, role=TrackRole.MAIN, excluded=True),  # main counts on a movie disc only
    ]
    proposals = [
        _proposal("trk_1", TrackRole.SPECIAL),
        _proposal("trk_2", TrackRole.OTHER, MenuProposalStatus.ACCEPTED),
        _proposal("trk_3", TrackRole.TRAILER, MenuProposalStatus.INFO),
        _proposal("trk_4", TrackRole.PLAY_ALL, MenuProposalStatus.INFO),
        _proposal("trk_6", TrackRole.SPECIAL, MenuProposalStatus.REJECTED),
    ]

    changed = default_ticks(tracks, proposals, movie=False)

    assert {t.id: t.excluded for t in tracks} == {
        "trk_0": False,
        "trk_1": False,
        "trk_2": False,
        "trk_3": False,
        "trk_4": True,
        "trk_5": True,
        "trk_6": True,
        "trk_7": True,
    }
    assert {t.id for t in changed} == {"trk_0", "trk_1", "trk_2", "trk_3", "trk_4"}


def test_default_ticks_keep_a_movie_discs_main_feature_and_never_the_menu() -> None:
    menu = _menu()
    menu.excluded = True
    tracks = [_title(0, role=TrackRole.MAIN, excluded=True), menu]

    default_ticks(tracks, [], movie=True)

    assert tracks[0].excluded is False and menu.excluded is True


# --- opening ------------------------------------------------------------------------------


@pytest.mark.parametrize("running", [TranscodeTaskStatus.QUEUED, TranscodeTaskStatus.IN_PROGRESS])
async def test_the_stop_waits_for_its_menu_preview(running: TranscodeTaskStatus) -> None:
    db = _db(review_requires_confirm=True)
    _with_preview(db, running)

    assert await sweep_choose_titles(db, _Hub()) == 0

    assert db.rows["jobs"][0].wait_start_time is None
    assert _excluded(db)["trk_0"] is True  # no ticks yet


@pytest.mark.parametrize("preview", ["done", "failed", "no-menu", "no-application"], ids=lambda p: f"preview-{p}")
async def test_it_opens_once_there_is_no_preview_to_wait_for(preview: str) -> None:
    db = _db(review_requires_confirm=True)
    if preview in ("done", "failed"):
        _with_preview(db, TranscodeTaskStatus(preview))
    elif preview == "no-application":
        db.rows["tracks"].append(_menu())

    assert await sweep_choose_titles(db, _Hub()) == 1

    job = db.rows["jobs"][0]
    assert job.wait_start_time is not None
    assert job.status == JobStatus.AWAITING_TITLES  # confirm mode: waits for the click
    assert _excluded(db) == {"trk_0": False, "trk_1": False, "trk_2": True, "trk_3": True}


async def test_a_preview_due_at_stop_2_is_queued_first() -> None:
    db = _db(review_requires_confirm=True)
    _with_preview(db, None)

    assert not await preview_settled(db, db.rows["jobs"][0])
    assert await sweep_menu_previews(db) == 1  # queued at awaiting_titles, with no ripped_at
    assert await sweep_choose_titles(db, _Hub()) == 0


async def test_an_open_stop_is_never_ticked_again() -> None:
    """A backend restart (or the next tick) after opening keeps what the operator chose since."""
    db = _db(review_requires_confirm=True)
    await sweep_choose_titles(db, _Hub())
    opened = db.rows["jobs"][0].wait_start_time
    next(t for t in db.rows["tracks"] if t.id == "trk_0").excluded = True  # the operator unticks it

    assert await sweep_choose_titles(db, _Hub()) == 0

    assert db.rows["jobs"][0].wait_start_time == opened
    assert _excluded(db)["trk_0"] is True


# --- starting -----------------------------------------------------------------------------


def _remux_started(db: FakeSession, hub: _Hub) -> bool:
    job = db.rows["jobs"][0]
    return job.status == JobStatus.REMUXING and [e["event_type"] for e in hub.events] == ["rip.retry"]


async def test_hold_off_opens_and_starts_in_one_sweep(raw_root: Path) -> None:
    _backup(raw_root, complete=True)
    db, hub = _db(hold_for_review=False), _Hub()

    assert await sweep_choose_titles(db, hub) == 2

    assert _remux_started(db, hub)
    # Not armed as a retry: rip-start says retry=false, and the files land in the rip folder.
    assert not flag_is_set(db.rows["jobs"][0].metadata_json, "retry_armed")


async def test_the_countdown_starts_an_open_stop(raw_root: Path) -> None:
    _backup(raw_root, complete=True)
    db, hub = _db(), _Hub()
    await sweep_choose_titles(db, hub)
    assert db.rows["jobs"][0].status == JobStatus.AWAITING_TITLES  # opened just now: counting down

    db.rows["jobs"][0].wait_start_time = datetime.now(UTC) - timedelta(seconds=61)
    assert await sweep_choose_titles(db, hub) == 1

    assert _remux_started(db, hub)


@pytest.mark.parametrize("why", ["paused", "job-paused", "confirm", "nothing-ticked", "no-backup"])
async def test_an_open_stop_waits(raw_root: Path, why: str) -> None:
    if why != "no-backup":
        _backup(raw_root, complete=True)
    cfg: dict[str, Any] = {"ripping_paused": True} if why == "paused" else {}
    if why == "confirm":
        cfg["review_requires_confirm"] = True
    db, hub = _db(**cfg), _Hub()
    job = db.rows["jobs"][0]
    job.wait_start_time = datetime.now(UTC) - timedelta(seconds=600)
    job.manual_pause = why == "job-paused"
    if why == "nothing-ticked":
        for track in db.rows["tracks"]:
            track.excluded = True

    assert await sweep_choose_titles(db, hub) == 0

    assert job.status == JobStatus.AWAITING_TITLES and hub.events == []


async def test_no_stop_waiting_is_a_quick_no_op() -> None:
    db = _db(job=_job(JobStatus.RIPPED))
    assert await sweep_choose_titles(db, _Hub()) == 0


# --- retry-failed starts it (the "Rip titles" button) ---------------------------------------


def test_retry_failed_starts_stop_2_from_the_backup(raw_root: Path) -> None:
    _backup(raw_root, complete=True)
    db, hub = _db(), _Hub()
    db.rows["users"] = _users()

    client, auth = _jobs(db, hub)
    r = client.post(f"/api/jobs/{JOB_ID}/retry-failed", headers=auth)

    assert r.status_code == 200, r.text
    # Not opened yet, so no default ticks: the kept titles as they stand (the play-all too).
    assert r.json()["from_backup"] is True and r.json()["retried"] == 2
    assert _remux_started(db, hub)
    assert not flag_is_set(db.rows["jobs"][0].metadata_json, "retry_armed")

    # The ripper's rip-start: no retry, so no retry-N/ folder; remuxing stays.
    rs = _ripper(db, hub).post(f"/api/ripper/jobs/{JOB_ID}/rip-start", headers=_SERVICE_AUTH)
    assert rs.status_code == 200, rs.text
    assert rs.json()["retry"] is False and rs.json()["park_after_backup"] is False
    assert db.rows["jobs"][0].status == JobStatus.REMUXING


def test_retry_failed_at_stop_2_with_nothing_ticked_is_422(raw_root: Path) -> None:
    _backup(raw_root, complete=True)
    db = _db()
    db.rows["users"] = _users()
    for track in db.rows["tracks"]:
        track.excluded = True

    client, auth = _jobs(db, _Hub())
    r = client.post(f"/api/jobs/{JOB_ID}/retry-failed", headers=auth)

    assert r.status_code == 422 and r.json()["detail"] == "no title is ticked to rip"


def test_retry_failed_at_stop_2_without_its_backup_is_409(raw_root: Path) -> None:
    db = _db()
    db.rows["users"] = _users()

    client, auth = _jobs(db, _Hub())
    r = client.post(f"/api/jobs/{JOB_ID}/retry-failed", headers=auth)

    assert r.status_code == 409 and "disc backup is gone" in r.json()["detail"]
    assert db.rows["jobs"][0].status == JobStatus.AWAITING_TITLES


def _users() -> list[Any]:
    from arm_common import User

    return [User(id="usr_admin", username="admin", password_hash="x", password_must_change=False)]


# --- rip-parked -----------------------------------------------------------------------------


def test_rip_parked_moves_the_job_to_stop_2() -> None:
    db, hub = _db(job=_job(JobStatus.RIPPING, metadata_json={"scan_result": _SCAN})), _Hub()
    db.rows["jobs"][0].wait_start_time = datetime.now(UTC)  # stop 1's

    r = _ripper(db, hub).post(f"/api/ripper/jobs/{JOB_ID}/rip-parked", headers=_SERVICE_AUTH)
    again = _ripper(db, hub).post(f"/api/ripper/jobs/{JOB_ID}/rip-parked", headers=_SERVICE_AUTH)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_titles" and again.status_code == 200
    job = db.rows["jobs"][0]
    assert job.wait_start_time is None  # opened by the sweep, once the menu has named the titles
    assert flag_is_set(job.metadata_json, "parked")
    assert [e["event_type"] for e in hub.events] == ["rip.parked"]


def test_rip_parked_refuses_a_job_that_isnt_ripping() -> None:
    db = _db(job=_job(JobStatus.RIPPED))
    r = _ripper(db, _Hub()).post(f"/api/ripper/jobs/{JOB_ID}/rip-parked", headers=_SERVICE_AUTH)
    assert r.status_code == 409


# --- the backup after the remux --------------------------------------------------------------


@pytest.mark.parametrize(
    ("parked", "failed", "deleted"),
    [(True, None, True), (True, "trk_1", False), (True, "trk_menu", False), (False, None, False)],
    ids=["clean", "title-failed", "menu-failed", "not-parked"],
)
async def test_the_backup_goes_once_no_kept_track_failed(
    raw_root: Path, parked: bool, failed: str | None, deleted: bool
) -> None:
    _backup(raw_root, complete=True)
    job = _job(JobStatus.RIPPED, metadata_json={"flags": {"parked": parked}})
    tracks = [_title(1, status=TrackStatus.DONE), _title(2, excluded=True), _menu()]
    for track in tracks:
        if track.id == failed:
            track.status = TrackStatus.FAILED

    await delete_parked_backup(job, tracks)

    assert (raw_root / JOB_ID / ".backup").exists() is not deleted


async def test_a_backup_that_cant_be_deleted_is_logged(
    raw_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(_job_id: str) -> bool:
        raise OSError("read-only")

    monkeypatch.setattr(choose_module, "delete_backup", boom)

    await delete_parked_backup(_job(JobStatus.RIPPED), [_title(1, status=TrackStatus.DONE)])

    assert "could not delete the disc backup" in caplog.text


def test_rip_complete_of_a_parked_job_deletes_its_backup(raw_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import arm_backend.routers.ripper as ripper_router

    _backup(raw_root, complete=True)
    db = _db(job=_job(JobStatus.REMUXING), tracks=[_title(1, status=TrackStatus.DONE), _title(2, excluded=True)])

    async def no_after_rip(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(ripper_router, "after_rip", no_after_rip)
    r = _ripper(db, _Hub()).post(f"/api/ripper/jobs/{JOB_ID}/rip-complete", json={}, headers=_SERVICE_AUTH)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ripped"
    assert not (raw_root / JOB_ID / ".backup").exists()


# --- rip-start tells the ripper to park ------------------------------------------------------


@pytest.mark.parametrize("iso", [False, True])
def test_rip_start_tells_the_ripper_to_park_a_first_pass_from_a_drive(iso: bool) -> None:
    db = _db(job=_job(JobStatus.RIPPING, metadata_json={"scan_result": _SCAN}))
    if iso:
        db.rows["drives"][0].source_kind = DriveSourceKind.ISO

    r = _ripper(db, _Hub()).post(f"/api/ripper/jobs/{JOB_ID}/rip-start", headers=_SERVICE_AUTH)

    assert r.status_code == 200, r.text
    assert r.json()["park_after_backup"] is (not iso)


# --- review-pause at stop 2 ------------------------------------------------------------------


@pytest.mark.parametrize("opened", [True, False])
def test_review_pause_at_stop_2(opened: bool) -> None:
    db = _db()
    db.rows["users"] = _users()
    job = db.rows["jobs"][0]
    old = datetime(2020, 1, 1, tzinfo=UTC)
    job.wait_start_time = old if opened else None
    client, auth = _jobs(db, _Hub())

    paused = client.post(f"/api/jobs/{JOB_ID}/review-pause?paused=true", headers=auth)
    assert paused.status_code == 200, paused.text
    assert job.manual_pause is True

    resumed = client.post(f"/api/jobs/{JOB_ID}/review-pause?paused=false", headers=auth)
    assert resumed.status_code == 200, resumed.text
    assert job.manual_pause is False
    if opened:
        assert job.wait_start_time is not None and job.wait_start_time > old  # a fresh countdown
    else:
        assert job.wait_start_time is None  # left for the sweep to open


# --- the job page learns whether the disc parks ------------------------------------------------


@pytest.mark.parametrize(
    ("status", "iso", "parks"),
    [
        (JobStatus.AWAITING_REVIEW, False, True),
        (JobStatus.IDENTIFIED, False, True),
        (JobStatus.AWAITING_REVIEW, True, False),
        (JobStatus.AWAITING_TITLES, False, False),
        (JobStatus.RIPPED, False, False),
    ],
)
def test_the_job_detail_says_whether_stop_1_backs_the_disc_up_first(status: JobStatus, iso: bool, parks: bool) -> None:
    db = _db(job=_job(status, metadata_json={"scan_result": _SCAN}))
    db.rows["users"] = _users()
    if iso:
        db.rows["drives"][0].source_kind = DriveSourceKind.ISO
    client, auth = _jobs(db, _Hub())

    r = client.get(f"/api/jobs/{JOB_ID}", headers=auth)

    assert r.status_code == 200, r.text
    assert r.json()["parks_after_backup"] is parks


def test_the_backup_cant_be_deleted_while_its_titles_wait_to_be_chosen(raw_root: Path) -> None:
    _backup(raw_root, complete=True)
    db = _db()
    db.rows["users"] = _users()
    client, auth = _jobs(db, _Hub())

    r = client.delete(f"/api/jobs/{JOB_ID}/backup", headers=auth)

    assert r.status_code == 409 and "once they're chosen" in r.json()["detail"]
    assert (raw_root / JOB_ID / ".backup").exists()


# --- a failed first pass that left nothing runs again (Retry backup and menu) ---------------------


def _failed_first_pass(*, done: bool = False) -> FakeSession:
    """The backup failed, then so did the titles ripped straight from the disc after it."""
    failed = TrackStatus.FAILED
    menu = _menu()
    menu.status, menu.output_path = failed, None
    return _db(
        job=_job(JobStatus.FAILED, metadata_json={"scan_result": _SCAN}),
        tracks=[
            _title(0, status=TrackStatus.DONE if done else failed, role=TrackRole.EPISODE),
            _title(1, status=failed),
            menu,
        ],
    )


@pytest.mark.parametrize(
    ("status", "done", "backup", "iso", "redo"),
    [
        (JobStatus.FAILED, False, False, False, True),
        (JobStatus.FAILED, True, False, False, False),  # a title was ripped: retry the rest from the disc
        (JobStatus.FAILED, False, True, False, False),  # a finished backup: remux from it
        (JobStatus.FAILED, False, False, True, False),  # an ISO never parks
        (JobStatus.RIPPED_PARTIAL, False, False, False, False),
    ],
    ids=["nothing-left", "title-done", "backup-kept", "iso", "partial"],
)
def test_redoes_first_pass(raw_root: Path, status: JobStatus, done: bool, backup: bool, iso: bool, redo: bool) -> None:
    if backup:
        _backup(raw_root, complete=True)
    db = _failed_first_pass(done=done)
    job, drive = db.rows["jobs"][0], db.rows["drives"][0]
    job.status = status
    if iso:
        drive.source_kind = DriveSourceKind.ISO
    assert redoes_first_pass(job, drive, db.rows["tracks"]) is redo


def test_the_job_detail_says_a_retry_backs_the_disc_up_again(raw_root: Path) -> None:
    db = _failed_first_pass()
    db.rows["users"] = _users()
    client, auth = _jobs(db, _Hub())

    r = client.get(f"/api/jobs/{JOB_ID}", headers=auth)

    assert r.status_code == 200, r.text
    assert r.json()["retry_backs_up"] is True and r.json()["parks_after_backup"] is False


def test_a_retry_of_a_first_pass_that_left_nothing_backs_up_and_parks_again(raw_root: Path) -> None:
    db, hub = _failed_first_pass(), _Hub()
    db.rows["users"] = _users()
    job = db.rows["jobs"][0]

    client, auth = _jobs(db, hub)
    r = client.post(f"/api/jobs/{JOB_ID}/retry-failed", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["from_backup"] is False  # it waits for the disc

    # The disc goes back in: rip-start runs the first pass again, not a retry, and parks after the backup.
    for _ in range(2):  # a repeated rip-start answers the same
        rs = _ripper(db, hub).post(f"/api/ripper/jobs/{JOB_ID}/rip-start", headers=_SERVICE_AUTH)
        assert rs.status_code == 200, rs.text
        assert rs.json()["retry"] is False and rs.json()["park_after_backup"] is True
        assert rs.json()["extract_menu"] is not None
    assert job.status == JobStatus.RIPPING
    assert not flag_is_set(job.metadata_json, "retry_armed") and not flag_is_set(job.metadata_json, "retrying")
