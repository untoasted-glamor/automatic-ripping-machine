"""The disc menu as a track (fork only, design v5 A1).

A DVD or Blu-ray job gets a "Disc menu" row (`disc_menu`, `source_ref` "menu",
index -1) from its scan's `menu_kind`: ticked per the "Rip disc menus" setting
when it can be ripped, already `skipped` with the reason when it can't. The
ripper is never sent it as a track (rip-start's `extract_menu` instead), a
skipped menu counts as finished everywhere, and everything that counts titles
leaves it out. Also the task-kind table every "which kind" decision reads.
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

from arm_backend.crash_recovery import reset_job_for_recovery  # noqa: E402
from arm_backend.db import get_session  # noqa: E402
from arm_backend.iso_rips import _fail_job  # noqa: E402
from arm_backend.jwt_utils import issue_access_token  # noqa: E402
from arm_backend.routers import config as config_router  # noqa: E402
from arm_backend.routers import identity as identity_router  # noqa: E402
from arm_backend.routers.jobs import _summarize_rip_progress  # noqa: E402
from arm_backend.task_kinds import TASK_KIND_RULES, rules_for  # noqa: E402
from arm_backend.track_selection import (  # noqa: E402
    MENU_SKIP_REASONS,
    menu_track,
    select_tracks,
    select_tracks_for_review,
)
from arm_common import (  # noqa: E402
    Config,
    DiscType,
    Job,
    JobStatus,
    MenuKind,
    OutputMode,
    RetentionPolicy,
    TrackKind,
    TrackStatus,
    TranscodeTaskKind,
    User,
)
from arm_common.models import Track  # noqa: E402
from arm_common.schemas import ScanResult  # noqa: E402
from arm_common.tracks import MENU_INDEX, MENU_SOURCE_REF, is_title, titles  # noqa: E402

from tests._fakes import FakeSession  # noqa: E402
from tests.test_retry_failed import JOB_ID, _db, _Hub, _jobs, _retry, _ripper, _SERVICE_AUTH, raw_root  # noqa: E402, F401
from tests.test_ripper_router import _movie_preset  # noqa: E402

_START = f"/api/ripper/jobs/{JOB_ID}/rip-start"


def _scan(menu_kind: MenuKind | None, disc_type: DiscType = DiscType.BLURAY, n: int = 2) -> ScanResult:
    return ScanResult.model_validate(
        {
            "disc_type": disc_type,
            "titles": [{"index": i, "duration_seconds": 1447} for i in range(n)],
            "menu_kind": menu_kind,
        }
    )


def _menu(status: TrackStatus = TrackStatus.QUEUED, **kw: Any) -> Track:
    return Track(
        id="trk_menu",
        job_id=JOB_ID,
        kind=TrackKind.DISC_MENU,
        index=MENU_INDEX,
        source_ref=MENU_SOURCE_REF,
        status=status,
        attempts=0,
        **kw,
    )


# --- the helper ----------------------------------------------------------------------------------


def test_is_title_is_every_kind_but_the_menu() -> None:
    assert [k for k in TrackKind if not is_title(Track(job_id="j", kind=k, index=0, source_ref="0"))] == [
        TrackKind.DISC_MENU
    ]
    rows = [_menu(), Track(job_id="j", kind=TrackKind.VIDEO_TITLE, index=0, source_ref="0")]
    assert titles(rows) == rows[1:]


# --- track selection -----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [MenuKind.HDMV, MenuKind.DVD, MenuKind.UNKNOWN])
@pytest.mark.parametrize("rip_menus", [True, False])
def test_a_menu_that_can_be_ripped_is_ticked_per_the_setting(kind: MenuKind, rip_menus: bool) -> None:
    track = menu_track(JOB_ID, _scan(kind), rip_menus=rip_menus)
    assert track is not None
    assert (track.kind, track.index, track.source_ref) == (TrackKind.DISC_MENU, -1, "menu")
    assert track.status == TrackStatus.QUEUED and track.last_error is None
    assert track.excluded is (not rip_menus)


@pytest.mark.parametrize("kind", [MenuKind.BDJ, MenuKind.NONE])
def test_a_menu_that_cant_be_ripped_starts_skipped(kind: MenuKind) -> None:
    track = menu_track(JOB_ID, _scan(kind), rip_menus=True)
    assert track is not None
    assert track.status == TrackStatus.SKIPPED and track.excluded
    assert track.last_error == MENU_SKIP_REASONS[kind]


def test_no_menu_row_without_a_menu_kind_or_on_a_cd() -> None:
    assert menu_track(JOB_ID, _scan(None), rip_menus=True) is None
    assert menu_track(JOB_ID, _scan(MenuKind.DVD, DiscType.CD), rip_menus=True) is None


def test_select_tracks_puts_the_menu_first() -> None:
    rows = select_tracks(JOB_ID, _scan(MenuKind.HDMV), _movie_preset())
    assert [t.source_ref for t in rows] == ["menu", "0", "1"]
    off = select_tracks(JOB_ID, _scan(MenuKind.HDMV), _movie_preset(), rip_menus=False)
    assert off[0].excluded and not any(t.excluded for t in off[1:])


def test_a_disc_with_no_titles_gets_no_menu_row_either() -> None:
    assert select_tracks(JOB_ID, _scan(MenuKind.HDMV, n=0), _movie_preset()) == []
    assert select_tracks_for_review(JOB_ID, _scan(MenuKind.HDMV, n=0), _movie_preset()) == []


def test_a_whole_disc_preset_gets_no_menu_row() -> None:
    preset = _movie_preset()
    preset.output_mode = OutputMode.FOLDER
    rows = select_tracks(JOB_ID, _scan(MenuKind.HDMV), preset)
    assert [t.kind for t in rows] == [TrackKind.DISC_BACKUP]


def test_review_rows_have_the_menu_first() -> None:
    rows = select_tracks_for_review(JOB_ID, _scan(MenuKind.BDJ), _movie_preset())
    assert [t.source_ref for t in rows] == ["menu", "0", "1"]
    assert rows[0].status == TrackStatus.SKIPPED
    assert not any(t.excluded for t in rows[1:])  # the preset keeps both titles; the menu isn't one


# --- rip-start: the menu is extract_menu, never a ripper track --------------------------------


def _fresh(menu_kind: MenuKind | None, *, rip_menus: bool | None = None) -> FakeSession:
    db = _db(JobStatus.IDENTIFIED, meta={"scan_result": _scan(menu_kind).model_dump(mode="json")})
    db.rows["tracks"] = []
    db.rows["rip_presets"] = [_movie_preset()]
    if rip_menus is not None:
        db.rows["config"] = [Config(id=1, rip_disc_menus=rip_menus)]
    return db


def test_rip_start_sends_the_menu_as_extract_menu() -> None:
    db = _fresh(MenuKind.HDMV)

    r = _ripper(db, _Hub()).post(_START, headers=_SERVICE_AUTH)

    assert r.status_code == 200, r.text
    body = r.json()
    menu = next(t for t in db.rows["tracks"] if t.kind == TrackKind.DISC_MENU)
    assert body["extract_menu"] == {"track_id": menu.id}
    assert [t["source_ref"] for t in body["tracks"]] == ["0", "1"]


@pytest.mark.parametrize(("kind", "rip_menus"), [(MenuKind.HDMV, False), (MenuKind.BDJ, True), (None, True)])
def test_rip_start_with_no_menu_to_extract(kind: MenuKind | None, rip_menus: bool) -> None:
    db = _fresh(kind, rip_menus=rip_menus)
    body = _ripper(db, _Hub()).post(_START, headers=_SERVICE_AUTH).json()
    assert body["extract_menu"] is None
    assert all(t["kind"] != "disc_menu" for t in body["tracks"])


def test_rip_start_with_existing_rows_and_resume_leave_the_menu_out() -> None:
    db = _db(JobStatus.RIPPING)
    db.rows["rip_presets"] = [_movie_preset()]
    db.rows["tracks"].append(_menu())
    client = _ripper(db, _Hub())

    start = client.post(_START, headers=_SERVICE_AUTH).json()
    resumed = client.post(f"/api/ripper/jobs/{JOB_ID}/resume", headers=_SERVICE_AUTH).json()

    for body in (start, resumed):
        assert body["extract_menu"] == {"track_id": "trk_menu"}
        assert "trk_menu" not in {t["id"] for t in body["tracks"]}
    # A menu that already finished isn't extracted again.
    db.rows["tracks"][-1].status = TrackStatus.DONE
    assert client.post(_START, headers=_SERVICE_AUTH).json()["extract_menu"] is None


# --- PATCH: skipped is for the menu --------------------------------------------------------------


def _patch(db: FakeSession, hub: _Hub, track_id: str, **body: Any) -> Any:
    return _ripper(db, hub).patch(f"/api/ripper/tracks/{track_id}", json=body, headers=_SERVICE_AUTH)


@pytest.mark.parametrize("start", [TrackStatus.QUEUED, TrackStatus.IN_PROGRESS])
def test_the_menu_can_be_skipped_with_its_reason(start: TrackStatus) -> None:
    db, hub = _db(JobStatus.RIPPING), _Hub()
    db.rows["tracks"].append(_menu(start))

    r = _patch(db, hub, "trk_menu", status="skipped", last_error="This disc has no menu")

    assert r.status_code == 200, r.text
    assert db.rows["tracks"][-1].status == TrackStatus.SKIPPED
    assert db.rows["tracks"][-1].last_error == "This disc has no menu"
    assert [e["event_type"] for e in hub.events] == ["track.skipped"]


def test_a_title_or_a_finished_menu_cant_be_skipped() -> None:
    db = _db(JobStatus.RIPPING)
    db.rows["tracks"].append(_menu(TrackStatus.DONE))
    assert _patch(db, _Hub(), "trk_1", status="skipped").status_code == 409
    r = _patch(db, _Hub(), "trk_menu", status="skipped")
    assert r.status_code == 409 and "disc_menu track done -> skipped" in r.json()["detail"]


# --- rip-complete: titles decide, a failed menu makes it partial ------------------------------


def _complete(menu_status: TrackStatus, title_status: TrackStatus) -> tuple[Job, list[dict[str, Any]]]:
    db, hub = _db(JobStatus.RIPPING), _Hub()
    db.rows["tracks"] = [db.rows["tracks"][0], _menu(menu_status)]
    db.rows["tracks"][0].status = title_status
    r = _ripper(db, hub).post(f"/api/ripper/jobs/{JOB_ID}/rip-complete", json={}, headers=_SERVICE_AUTH)
    assert r.status_code == 200, r.text
    return db.rows["jobs"][0], hub.events


def test_a_skipped_menu_leaves_a_clean_rip_ripped() -> None:
    job, events = _complete(TrackStatus.SKIPPED, TrackStatus.DONE)
    assert job.status == JobStatus.RIPPED
    done = next(e for e in events if e["event_type"] == "rip.completed")["payload"]
    assert (done["tracks_total"], done["tracks_done"], done["tracks_failed"]) == (1, 1, 0)


def test_a_failed_menu_makes_the_rip_partial() -> None:
    job, _ = _complete(TrackStatus.FAILED, TrackStatus.DONE)
    assert job.status == JobStatus.RIPPED_PARTIAL


def test_a_menu_alone_isnt_a_rip() -> None:
    job, _ = _complete(TrackStatus.DONE, TrackStatus.FAILED)
    assert job.status == JobStatus.FAILED


# --- retry-failed, review start, edits, progress --------------------------------------------------


def test_a_failed_menu_is_retried_with_the_titles(raw_root: Path) -> None:  # noqa: F811
    db = _db()
    db.rows["tracks"].append(_menu(TrackStatus.FAILED, last_error="copy failed"))

    body = _retry(db, _Hub()).json()

    assert body["retried"] == 1  # titles
    assert db.rows["tracks"][-1].status == TrackStatus.QUEUED and db.rows["tracks"][-1].last_error is None


def test_a_menu_alone_isnt_retried_and_a_skipped_one_never_is(raw_root: Path) -> None:  # noqa: F811
    db = _db()
    db.rows["tracks"] = [db.rows["tracks"][0], _menu(TrackStatus.FAILED)]
    r = _retry(db, _Hub())
    assert r.status_code == 422 and r.json()["detail"] == "every kept title is ripped"

    db = _db()
    db.rows["tracks"].append(_menu(TrackStatus.SKIPPED, last_error="This disc has no menu", excluded=True))
    assert _retry(db, _Hub()).status_code == 200
    assert db.rows["tracks"][-1].status == TrackStatus.SKIPPED


def test_a_held_disc_with_only_its_menu_kept_cant_start(raw_root: Path) -> None:  # noqa: F811
    db = _db(JobStatus.AWAITING_REVIEW)
    for t in db.rows["tracks"]:
        t.excluded = True
    db.rows["tracks"].append(_menu())
    client, auth = _jobs(db, _Hub())
    r = client.post(f"/api/jobs/{JOB_ID}/rip-start-review", headers=auth)
    assert r.status_code == 422 and "at least one track" in r.json()["detail"]


def _edit(db: FakeSession, **edit: Any) -> Any:
    client, auth = _jobs(db, _Hub())
    return client.patch(f"/api/jobs/{JOB_ID}", json={"tracks": [{"track_id": "trk_menu", **edit}]}, headers=auth)


def test_the_menu_takes_only_keep_or_drop() -> None:
    db = _db(JobStatus.AWAITING_REVIEW)
    db.rows["tracks"].append(_menu(excluded=True))

    assert _edit(db, excluded=False).status_code == 200
    assert db.rows["tracks"][-1].excluded is False
    r = _edit(db, title="Menu", role="extra")
    assert r.status_code == 422 and r.json()["detail"] == "the disc menu takes only excluded, not role, title"


def test_a_skipped_menu_cant_be_ticked() -> None:
    db = _db(JobStatus.AWAITING_REVIEW)
    db.rows["tracks"].append(_menu(TrackStatus.SKIPPED, last_error="Java menus can't be played", excluded=True))
    r = _edit(db, excluded=False)
    assert r.status_code == 409 and r.json()["detail"] == "the disc menu can't be ripped: Java menus can't be played"
    assert _edit(db, excluded=True).status_code == 200


def test_ticking_the_menu_mid_rip_names_it() -> None:
    db = _db(JobStatus.RIPPING)
    db.rows["tracks"].append(_menu(excluded=True))
    r = _edit(db, excluded=False)
    assert r.status_code == 409 and r.json()["detail"].startswith("the disc menu has no ripped file")


def test_summarize_rip_progress_counts_titles_only() -> None:
    progress = _summarize_rip_progress([_menu(TrackStatus.IN_PROGRESS), *_db().rows["tracks"]])
    assert progress.tracks_total == 2 and progress.current_track_id is None


# --- crash recovery, ISO watchdog, identity ------------------------------------------------------


async def test_crash_recovery_leaves_a_skipped_menu_skipped() -> None:
    db = _db(JobStatus.RIPPING)
    db.rows["tracks"].append(_menu(TrackStatus.SKIPPED))
    await reset_job_for_recovery(db, db.rows["jobs"][0])
    assert db.rows["tracks"][-1].status == TrackStatus.SKIPPED
    assert {t.status for t in db.rows["tracks"][:-1]} == {TrackStatus.QUEUED}


async def test_an_iso_rip_that_stops_counts_titles_only() -> None:
    db, hub = _db(JobStatus.RIPPING), _Hub()
    db.rows["tracks"].append(_menu(TrackStatus.SKIPPED, excluded=True))
    await _fail_job(db, hub, db.rows["jobs"][0])
    payload = hub.events[0]["payload"]
    assert (payload["tracks_total"], payload["tracks_skipped"]) == (2, 1)


async def test_identity_lists_titles_only() -> None:
    db = _db()
    db.rows["tracks"].append(_menu())
    assert [t.id for t in await identity_router._get_tracks(db, JOB_ID)] == ["trk_0", "trk_1", "trk_2"]


# --- the setting -----------------------------------------------------------------------------------


def _config_client(db: FakeSession) -> tuple[TestClient, dict[str, str]]:
    key = secrets.token_bytes(32)
    app = FastAPI()
    app.state.signing_key = key
    app.include_router(config_router.router)

    async def _session() -> FakeSession:
        return db

    app.dependency_overrides[get_session] = _session
    token, _ = issue_access_token("usr_admin", "admin", key)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_rip_disc_menus_round_trips() -> None:
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
    client, auth = _config_client(db)

    assert client.get("/api/config", headers=auth).json()["rip_disc_menus"] is True
    assert client.patch("/api/config", json={"rip_disc_menus": False}, headers=auth).status_code == 200
    assert db.rows["config"][0].rip_disc_menus is False
    r = client.patch("/api/config", json={"rip_disc_menus": None}, headers=auth)
    assert r.status_code == 400 and r.json()["detail"] == "rip_disc_menus must be true or false"


# --- task kinds ----------------------------------------------------------------------------------


def test_every_task_kind_has_its_rules() -> None:
    assert set(TASK_KIND_RULES) == set(TranscodeTaskKind)


def test_the_rules_of_the_kinds_there_are() -> None:
    encode, ocr = rules_for(TranscodeTaskKind.ENCODE), rules_for(TranscodeTaskKind.SUBTITLE_OCR)
    assert encode.encodes and encode.mounts_raw and encode.sidecar_suffix is None and not encode.failed_owns_files
    assert not ocr.encodes and not ocr.mounts_raw and ocr.sidecar_suffix == ".srt" and ocr.failed_owns_files
