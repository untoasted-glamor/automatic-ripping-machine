"""The job page's Disc menu panel API (fork only, disc menus M3-M4): the panel, its images, another preview, and
editing, accepting, rejecting and adding names."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from arm_backend.db import get_session  # noqa: E402
from arm_backend.jwt_utils import issue_access_token  # noqa: E402
from arm_backend.routers import disc_menus as router_module  # noqa: E402
from arm_common import (  # noqa: E402
    DiscType,
    Job,
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

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
BASE = f"/api/jobs/{JOB}/disc-menu"
OLD = "Show/Season 01/Show - S01D02T08.mkv"

_PREVIEW = {
    "schema": "menu-preview-1",
    "disc": "bd",
    "notes": ["n"],
    "unsupported": ["button sounds"],
    "truncated": False,
    "pages": [
        {"id": "1", "at": "m p2", "language": "eng", "image": "pages/1.jpg", "size": [1920, 1080], "buttons": [
            {"id": 10, "box": [0, 0, 5, 5], "visible": True, "label": "Play All", "score": 1.0,
             "outcome": {"kind": "plays", "playlist": "00008", "start": 0}},
            {"id": 13, "box": [0, 0, 5, 5], "visible": True, "label": "Special Features", "score": 1.0,
             "outcome": {"kind": "menu", "at": "m p8"}},
            {"id": 14, "box": [0, 0, 0, 0], "visible": False, "outcome": {"kind": "menu", "at": "m p2"}},
            {"id": 15, "box": [0, 0, 0, 0], "visible": False, "outcome": {"kind": "menu", "at": "m p9"}},
            {"id": 16, "box": [0, 0, 0, 0], "visible": False, "outcome": {"kind": "stopped", "why": "x"}},
        ]},
        {"id": "2", "at": "m p8", "language": "eng", "image": "pages/2.jpg", "size": [1920, 1080], "buttons": [
            {"id": 12, "box": [1, 2, 3, 4], "visible": True, "label": "Promo Videos", "score": 0.99, "crop": "crops/2.b12.png",
             "outcome": {"kind": "plays", "playlist": "00023", "start": 0}},
            {"id": 13, "box": [1, 2, 3, 4], "visible": True, "label": "Textless OP", "score": 0.99,
             "outcome": {"kind": "plays", "playlist": "00024", "start": 0}},
            {"id": 14, "box": [1, 2, 3, 4], "visible": True, "label": "PV", "score": 0.9,
             "outcome": {"kind": "plays", "playlist": "00023", "start": 0}},
        ]},
    ],
    "playlists": {
        "00008": {"items": [{"clip": "00014", "in": 0, "out": 10}, {"clip": "00015", "in": 0, "out": 10}], "marks": []},
        "00023": {"items": [{"clip": "00028", "in": 0, "out": 10}], "marks": []},
        "00024": {"items": [{"clip": "00029", "in": 0, "out": 10}], "marks": []},
    },
}  # fmt: skip


def _db(tmp_path: Path, *, preview: TranscodeTaskStatus | None = TranscodeTaskStatus.DONE) -> FakeSession:
    menu = tmp_path / "raw" / "menu"
    (menu / "preview" / "pages").mkdir(parents=True)
    (menu / "preview" / "crops").mkdir()
    (menu / "preview" / "preview.json").write_text(json.dumps(_PREVIEW))
    (menu / "preview" / "pages" / "1.jpg").write_bytes(b"\xff\xd8jpg")
    (menu / "preview" / "crops" / "2.b12.png").write_bytes(b"png")
    db = FakeSession()
    db.rows["users"] = [User(id="usr_admin", username="admin", password_hash="x", password_must_change=False)]
    db.rows["jobs"] = [
        Job(id=JOB, disc_type=DiscType.BLURAY, title="Show", media_type=MediaType.TV, season=1, ripped_at=datetime.now(UTC),
            metadata_json={"scan_result": {"titles": [{"index": 5, "segment_map": "14,15"}, {"index": 8, "segment_map": "28"}, {"index": 9}]}})
    ]  # fmt: skip
    db.rows["tracks"] = [
        Track(id="trk_menu", job_id=JOB, kind=TrackKind.DISC_MENU, index=-1, source_ref="menu",
              status=TrackStatus.DONE, output_path=str(menu), size_bytes=100, attempts=0),
        Track(id="trk_5", job_id=JOB, kind=TrackKind.VIDEO_TITLE, index=5, source_ref="5", role=TrackRole.PLAY_ALL,
              status=TrackStatus.QUEUED, excluded=True, attempts=0),
        Track(id="trk_8", job_id=JOB, kind=TrackKind.VIDEO_TITLE, index=8, source_ref="8", role=TrackRole.EXTRA,
              status=TrackStatus.DONE, attempts=0),
    ]  # fmt: skip
    db.rows["session_applications"] = [
        SessionApplication(id="sap_x", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.DONE,
                           overwrite=False)
    ]  # fmt: skip
    db.rows["transcode_tasks"] = [
        TranscodeTask(id="txt_enc", session_application_id="sap_x", source_track_id="trk_8", status="done",
                      output_path=OLD, attempts=0, progress_pct=100)
    ]  # fmt: skip
    if preview is not None:
        db.rows["transcode_tasks"].append(
            TranscodeTask(id="txt_01JZXR7K3M5Q8N4VWA00000001", session_application_id="sap_x", source_track_id="trk_menu",
                          kind=TranscodeTaskKind.MENU_PREVIEW, status=preview, output_path=str(menu / "preview"),
                          attempts=0, progress_pct=100 if preview == "done" else 10)
        )  # fmt: skip
    db.rows["menu_proposals"] = [
        MenuProposal(id="mnp_promo", job_id=JOB, track_id="trk_8", page="2", button=12, label="Promo Videos",
                     plays="00023.mpls", role=TrackRole.TRAILER, name="Promo Videos", status=MenuProposalStatus.PROPOSED),
        MenuProposal(id="mnp_op", job_id=JOB, track_id=None, page="2", button=13, label="Textless OP",
                     plays="00024.mpls", role=TrackRole.OTHER, name="Textless OP", note="not ripped",
                     status=MenuProposalStatus.INFO),
    ]  # fmt: skip
    return db


def _client(db: FakeSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    (tmp_path / "media" / "Show" / "Season 01").mkdir(parents=True, exist_ok=True)
    (tmp_path / "media" / OLD).write_text("video")
    monkeypatch.setattr(router_module.settings, "MEDIA_ROOT", str(tmp_path / "media"))
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


def test_the_panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, auth = _client(_db(tmp_path), tmp_path, monkeypatch)
    r = client.get(BASE, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["track_status"], body["size_bytes"], body["can_preview"]) == ("done", 100, True)
    assert [p["id"] for p in body["proposals"]] == ["mnp_promo", "mnp_op"]
    preview = body["preview"]
    assert (preview["status"], preview["disc"], preview["notes"], preview["unsupported"]) == (
        "done", "bd", ["n"], ["button sounds"]
    )  # fmt: skip
    p1, p2 = preview["pages"]
    outcomes = [(b["id"], b["outcome"], b["goes_to"], b["track_id"], b["proposal_id"]) for b in p1["buttons"]]
    assert outcomes == [
        (10, "plays", None, "trk_5", None),
        (13, "page", "2", None, None),
        (14, "stays", None, None, None),
        (15, "page", None, None, None),
        (16, "stopped", None, None, None),
    ]
    assert [(b["id"], b["plays"], b["track_id"], b["proposal_id"]) for b in p2["buttons"]] == [
        (12, "00023.mpls", "trk_8", "mnp_promo"),
        (13, "00024.mpls", None, "mnp_op"),
        (14, "00023.mpls", "trk_8", "mnp_promo"),
    ]


def test_the_panel_before_and_while_a_preview_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, auth = _client(_db(tmp_path, preview=None), tmp_path, monkeypatch)
    body = client.get(BASE, headers=auth).json()
    assert body["preview"] is None and body["can_preview"] is True
    client, auth = _client(_db(tmp_path / "b", preview=TranscodeTaskStatus.IN_PROGRESS), tmp_path / "b", monkeypatch)
    body = client.get(BASE, headers=auth).json()
    assert (body["preview"]["status"], body["preview"]["pages"], body["can_preview"]) == ("in_progress", [], False)


def test_the_panel_with_an_unreadable_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    (tmp_path / "raw" / "menu" / "preview" / "preview.json").unlink()
    client, auth = _client(db, tmp_path, monkeypatch)
    assert client.get(BASE, headers=auth).json()["preview"]["pages"] == []


def test_404s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    client, auth = _client(db, tmp_path, monkeypatch)
    assert client.get("/api/jobs/job_01JZXR7K3M5Q8N4VWA00000009/disc-menu", headers=auth).status_code == 404
    db.rows["tracks"] = db.rows["tracks"][1:]
    assert client.get(BASE, headers=auth).status_code == 404
    assert client.get(f"{BASE}/files/pages/1.jpg", headers=auth).status_code == 404


def test_preview_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    client, auth = _client(db, tmp_path, monkeypatch)
    r = client.get(f"{BASE}/files/pages/1.jpg", headers=auth)
    assert r.status_code == 200 and r.content == b"\xff\xd8jpg"
    assert client.get(f"{BASE}/files/crops/2.b12.png", headers=auth).content == b"png"
    for bad in ("pages/9.jpg", "pack/menu.json", "pages/1.txt", "crops/..%2F..%2Fpreview.json"):
        assert client.get(f"{BASE}/files/{bad}", headers=auth).status_code == 404, bad
    db.rows["transcode_tasks"][-1].status = TranscodeTaskStatus.FAILED
    assert client.get(f"{BASE}/files/pages/1.jpg", headers=auth).status_code == 404


def test_another_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path, preview=TranscodeTaskStatus.FAILED)
    client, auth = _client(db, tmp_path, monkeypatch)
    r = client.post(f"{BASE}/preview", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["preview"]["status"] == "queued"
    r = client.post(f"{BASE}/preview", headers=auth)
    assert r.status_code == 409 and "already queued" in r.json()["detail"]


def test_accept_renames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    client, auth = _client(db, tmp_path, monkeypatch)
    r = client.patch(f"{BASE}/proposals/mnp_promo", json={"name": "PV Collection", "action": "accept"}, headers=auth)
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["applied_path"]) == ("applied", "Show/trailers/PV Collection.mkv")
    assert (tmp_path / "media" / "Show" / "trailers" / "PV Collection.mkv").read_text() == "video"


def test_edit_reject_reopen_and_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    client, auth = _client(db, tmp_path, monkeypatch)
    r = client.patch(f"{BASE}/proposals/mnp_promo", json={"role": "extra"}, headers=auth)
    assert (r.json()["role"], r.json()["status"]) == ("extra", "proposed")
    assert (
        client.patch(f"{BASE}/proposals/mnp_promo", json={"action": "reject"}, headers=auth).json()["status"]
        == "rejected"
    )
    assert (
        client.patch(f"{BASE}/proposals/mnp_promo", json={"action": "reopen"}, headers=auth).json()["status"]
        == "proposed"
    )
    r = client.patch(f"{BASE}/proposals/mnp_op", json={"action": "accept"}, headers=auth)
    assert r.status_code == 409 and "no track" in r.json()["detail"]
    assert client.patch(f"{BASE}/proposals/mnp_nope", json={}, headers=auth).status_code == 404


def test_name_by_hand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    client, auth = _client(db, tmp_path, monkeypatch)
    r = client.post(f"{BASE}/proposals", json={"track_id": "trk_8", "role": "other", "name": "Ending"}, headers=auth)
    assert r.status_code == 201, r.text
    assert (r.json()["status"], r.json()["plays"]) == ("applied", "set by hand")
    r = client.post(f"{BASE}/proposals", json={"track_id": "trk_nope", "role": "other", "name": "x"}, headers=auth)
    assert r.status_code == 404


def test_accept_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    db.rows["menu_proposals"].append(
        MenuProposal(id="mnp_sp", job_id=JOB, track_id="trk_8", page="2", button=14, label="Episode 1", plays="x",
                     role=TrackRole.SPECIAL, name="Episode 1", status=MenuProposalStatus.PROPOSED)
    )  # fmt: skip
    client, auth = _client(db, tmp_path, monkeypatch)
    r = client.post(f"{BASE}/proposals/accept-all", headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {p["id"]: p["status"] for p in body["proposals"]} == {"mnp_promo": "applied", "mnp_sp": "proposed"}
    assert "season 0" in body["refused"]["mnp_sp"]


def test_writes_need_a_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    db.rows["users"][0].role = "guest"
    client, auth = _client(db, tmp_path, monkeypatch)
    assert client.post(f"{BASE}/preview", headers=auth).status_code == 403
    assert client.get(BASE, headers=auth).status_code == 200


def test_ws_hub_dependency(tmp_path: Path) -> None:
    class _Req:
        class app:  # noqa: N801
            class state:  # noqa: N801
                ws_hub = "hub"

    assert router_module._get_hub(_Req()) == "hub"  # type: ignore[arg-type]
