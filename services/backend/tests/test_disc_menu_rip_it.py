"""Ripping a title the disc menu plays but the job has no track for (fork only; `arm_backend.disc_menus.rip`).

A 92 s textless opening is under the rip's 120 s minimum, so the job got no track for it; its menu button says it
belongs to the disc. "Rip it" makes the track (named, `any_length`), arms the job's retry from the kept backup, and
the retry's fan-out encodes it into `other/`, where the pending rename finds it already in place.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from arm_backend import config as backend_config  # noqa: E402
from arm_backend.auto_session import fan_out_retried_tracks  # noqa: E402
from arm_backend.db import get_session  # noqa: E402
from arm_backend.disc_menus.rename import apply_pending  # noqa: E402
from arm_backend.disc_menus.rip import RipRefused, rip_from_menu, rip_refusal  # noqa: E402
from arm_backend.disc_menus.views import job_disc_menu_view  # noqa: E402
from arm_backend.jwt_utils import issue_access_token  # noqa: E402
from arm_backend.routers import disc_menus as router_module  # noqa: E402
from arm_common import (  # noqa: E402
    Job,
    JobStatus,
    MediaType,
    MenuProposal,
    MenuProposalStatus,
    SessionApplicationStatus,
    TrackKind,
    TrackRole,
    TrackStatus,
    TranscodeTaskStatus,
)
from arm_common.models import Track  # noqa: E402
from arm_common.schemas.job_metadata import flag_is_set  # noqa: E402

from tests._fakes import FakeSession  # noqa: E402
import arm_backend.raw_backup as raw_backup_module  # noqa: E402
from tests.test_retry_failed import JOB_ID, _backup, _db, _Hub, _with_session  # noqa: E402

SHOW = "Toilet-Bound Hanako-kun"
_SCAN = {
    "disc_type": "bluray",
    "min_length_seconds": 120,
    "titles": [
        *({"index": i, "duration_seconds": 1447} for i in range(3)),
        {"index": 3, "duration_seconds": 92, "size_bytes": 336_000_000},
    ],
}


@pytest.fixture
def raw_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(raw_backup_module.settings, "RAW_ROOT", str(tmp_path))
    return tmp_path


def _menu_db(**kw: Any) -> FakeSession:
    db = _db(JobStatus.RIPPED, meta={"scan_result": _SCAN}, **kw)
    job: Job = db.rows["jobs"][0]
    job.media_type = MediaType.TV
    db.rows["tracks"].append(
        Track(id="trk_menu", job_id=JOB_ID, kind=TrackKind.DISC_MENU, index=-1, source_ref="menu",
              status=TrackStatus.DONE, output_path=f"/raw/{JOB_ID}/menu", attempts=0)
    )  # fmt: skip
    db.rows["menu_proposals"] = [_op()]
    return db


def _op(**kw: Any) -> MenuProposal:
    fields: dict[str, Any] = dict(
        id="mnp_op", job_id=JOB_ID, track_id=None, scan_title=3, page="4", button=13,
        label="Textless Opening Song ver. 1", plays="00024.mpls", role=TrackRole.OTHER,
        name="Textless Opening Song ver. 1", note="not ripped: 92 s, under the rip's 120 s minimum",
        status=MenuProposalStatus.INFO,
    )  # fmt: skip
    fields.update(kw)
    return MenuProposal(**fields)


async def test_rip_it_makes_the_track_arms_the_retry_and_lands_in_other(raw_root: Path, tmp_path: Path) -> None:
    media = tmp_path / "media"
    backend_config.settings.MEDIA_ROOT = str(media)
    db, hub = _menu_db(), _Hub()
    _backup(raw_root, complete=True)
    _with_session(db, SessionApplicationStatus.DONE)
    job, proposal = db.rows["jobs"][0], db.rows["menu_proposals"][0]

    await rip_from_menu(db, job, proposal, hub, media)  # type: ignore[arg-type]

    track = next(t for t in db.rows["tracks"] if t.index == 3)
    assert (track.kind, track.source_ref, track.status, track.any_length) == (
        TrackKind.VIDEO_TITLE, "3", TrackStatus.QUEUED, True
    )  # fmt: skip
    assert (track.role, track.custom_filename) == (TrackRole.OTHER, "Textless Opening Song ver. 1")
    assert (track.expected_duration_seconds, track.expected_size_bytes) == (92, 336_000_000)
    assert track.identity_provenance == {"role": "disc_menu"}
    assert (proposal.track_id, proposal.status, proposal.note) == (track.id, MenuProposalStatus.ACCEPTED, None)
    assert flag_is_set(job.metadata_json, "retry_armed")
    assert [e["event_type"] for e in hub.events] == ["rip.retry"]

    # The retry rips it; the fan-out encodes it straight into the show's other/ folder.
    track.status, track.output_path = TrackStatus.DONE, f"/raw/{JOB_ID}/retry-1/D2_t03.mkv"
    await fan_out_retried_tracks(db, job, None)  # type: ignore[arg-type]
    encode = next(t for t in db.rows["transcode_tasks"] if t.source_track_id == track.id)
    assert encode.output_path == f"{SHOW}/other/Textless Opening Song ver. 1.mkv"

    # At its /complete the accepted name is found in place, and marked applied.
    encode.status = TranscodeTaskStatus.DONE
    (media / f"{SHOW}/other").mkdir(parents=True)
    (media / encode.output_path).write_bytes(b"mkv")
    assert await apply_pending(db, encode, media) == 1  # type: ignore[arg-type]
    assert (proposal.status, proposal.applied_path) == (MenuProposalStatus.APPLIED, encode.output_path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"track_id": "trk_0"}, "already has a track for this title"),
        ({"scan_title": None}, "didn't find this title in the disc scan"),
        ({"role": None}, "give it a bonus role and a name first"),
        ({"name": ""}, "give it a bonus role and a name first"),
        ({"role": TrackRole.SPECIAL}, "season 0 number"),
        ({"scan_title": 9}, "title 9 isn't in the job's disc scan"),
        ({"scan_title": 1}, "title 1 already has a track"),
    ],
)
async def test_rip_it_refuses_a_proposal_it_cant_rip(
    raw_root: Path, tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    db, hub = _menu_db(), _Hub()
    _backup(raw_root, complete=True)
    proposal = _op(**change)
    with pytest.raises(RipRefused, match=message):
        await rip_from_menu(db, db.rows["jobs"][0], proposal, hub, tmp_path)  # type: ignore[arg-type]
    assert not any(t.index == 3 for t in db.rows["tracks"]) and hub.events == []


async def test_rip_it_needs_a_kept_backup_a_free_drive_and_a_free_name(raw_root: Path, tmp_path: Path) -> None:
    db, hub = _menu_db(), _Hub()
    job, proposal = db.rows["jobs"][0], db.rows["menu_proposals"][0]
    assert await rip_refusal(db, job) == "the job's disc backup isn't kept, so ARM can't rip the title without the disc"  # type: ignore[arg-type]
    _backup(raw_root, complete=False)
    with pytest.raises(RipRefused, match="backup isn't kept"):
        await rip_from_menu(db, job, proposal, hub, tmp_path)  # type: ignore[arg-type]
    (raw_root / JOB_ID / ".backup" / ".complete").write_text("x\n")
    assert await rip_refusal(db, job) is None  # type: ignore[arg-type]
    job.status = JobStatus.RIPPING
    assert "only a ripped, ripped_partial or failed job" in (await rip_refusal(db, job) or "")  # type: ignore[arg-type]
    job.status = JobStatus.RIPPED

    # A file already at the name (an earlier by-hand encode) is never overwritten.
    _with_session(db, SessionApplicationStatus.DONE)
    (tmp_path / SHOW / "other").mkdir(parents=True)
    (tmp_path / SHOW / "other" / "Textless Opening Song ver. 1.mkv").write_bytes(b"by hand")
    with pytest.raises(RipRefused, match=f"{SHOW}/other/Textless Opening Song ver. 1.mkv already exists"):
        await rip_from_menu(db, job, proposal, hub, tmp_path)  # type: ignore[arg-type]
    assert proposal.status == MenuProposalStatus.INFO and hub.events == []


async def test_the_panel_says_why_it_cant_rip(raw_root: Path) -> None:
    db = _menu_db()
    view = await job_disc_menu_view(db, db.rows["jobs"][0])  # type: ignore[arg-type]
    assert view is not None and view.rip_refusal is not None and "backup isn't kept" in view.rip_refusal
    assert view.proposals[0].scan_title == 3
    _backup(raw_root, complete=True)
    view = await job_disc_menu_view(db, db.rows["jobs"][0])  # type: ignore[arg-type]
    assert view is not None and view.rip_refusal is None
    # Nothing to rip: no need to say.
    db.rows["menu_proposals"] = [_op(scan_title=None)]
    (raw_root / JOB_ID / ".backup" / ".complete").unlink()
    view = await job_disc_menu_view(db, db.rows["jobs"][0])  # type: ignore[arg-type]
    assert view is not None and view.rip_refusal is None


def _client(db: FakeSession) -> tuple[TestClient, dict[str, str]]:
    import secrets

    key = secrets.token_bytes(32)
    app = FastAPI()
    app.state.signing_key = key
    app.state.ws_hub = _Hub()
    app.include_router(router_module.router)

    async def _session() -> FakeSession:
        return db

    app.dependency_overrides[get_session] = _session
    token, _ = issue_access_token("usr_admin", "admin", key)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_the_rip_endpoint(raw_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module.settings, "MEDIA_ROOT", str(tmp_path))
    db = _menu_db()
    client, auth = _client(db)
    base = f"/api/jobs/{JOB_ID}/disc-menu/proposals"
    r = client.post(f"{base}/mnp_op/rip", headers=auth)
    assert r.status_code == 409 and "backup isn't kept" in r.json()["detail"]
    _backup(raw_root, complete=True)
    r = client.post(f"{base}/mnp_op/rip", headers=auth)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["scan_title"]) == ("accepted", 3)
    assert r.json()["track_id"] == next(t.id for t in db.rows["tracks"] if t.index == 3)
    assert client.post(f"{base}/mnp_nope/rip", headers=auth).status_code == 404


def test_retry_failed_still_refuses_like_before() -> None:
    """The checks moved to `arm_backend.rip_retry`; the endpoint's answers didn't change."""
    from tests.test_retry_failed import _retry

    db = _db(JobStatus.RIPPING)
    r = _retry(db, _Hub())
    assert r.status_code == 409 and "only a ripped, ripped_partial or failed job" in r.json()["detail"]
