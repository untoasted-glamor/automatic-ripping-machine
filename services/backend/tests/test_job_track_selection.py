"""A job's own track selection: precedence, the picker's default and disc
languages, the edit lock, and the GET/PUT and Apply session endpoints."""

from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from arm_backend import job_track_selection as jts  # noqa: E402
from arm_common import (  # noqa: E402
    AudioSelection,
    ContainerFormat,
    Drive,
    MediaType,
    Session,
    SessionApplication,
    SessionApplicationStatus,
    SubtitleOutput,
    SubtitleSelection,
    Track,
    TrackKind,
    TrackStatus,
    TranscodePreset,
    TranscodeTask,
    TranscodeTaskStatus,
    TranscodeTool,
)
from arm_common.schemas import TrackSelectionSettings, TranscodePresetView  # noqa: E402

from tests._fakes import FakeSession  # noqa: E402
from tests.test_apply_session import _auth, _make_app, _seed  # noqa: E402

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
OVERRIDE = {
    "audio_selection": "languages",
    "audio_languages": ["jpn", "eng"],
    "subtitle_selection": "languages",
    "subtitle_languages": ["eng"],
    "subtitle_output": "both",
    "audio_format": "copy",
}


def _stream(index: int, kind: str, language: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"index": index, "kind": kind, "language": language, "description": description, **extra}


def _scan(min_length: int | None = 600) -> dict[str, Any]:
    episode = [
        _stream(1, "audio", "eng", "TrueHD Surround 5.1 English", language_name="English"),
        _stream(2, "audio", "eng", "DD Surround 5.1 English", derived=True),
        _stream(3, "audio", "jpn", "TrueHD Stereo Japanese", language_name="Japanese"),
        _stream(5, "subtitle", "eng", "PGS English"),
        _stream(6, "subtitle", "eng", "PGS English (forced only)", forced=True, derived=True),
    ]
    return {
        "disc_type": "bluray",
        "min_length_seconds": min_length,
        "titles": [
            {"index": 0, "duration_seconds": 1447, "streams": episode},
            {"index": 1, "duration_seconds": 1446, "streams": episode[:3]},
            # A short extra with a French track: under the minimum length.
            {"index": 2, "duration_seconds": 90, "streams": [_stream(1, "audio", "fre", "DD Stereo French")]},
            {
                "index": 3,
                "duration_seconds": None,
                "streams": [_stream(4, "subtitle", "ger", "PGS German (forced only)", forced=True)],
            },
        ],
    }


def _preset(**fields: Any) -> TranscodePreset:
    return TranscodePreset(
        id=fields.pop("id", "tpr_y"),
        name=fields.pop("name", "Anime"),
        media_type=MediaType.MOVIE,
        is_builtin=False,
        tool=TranscodeTool.HANDBRAKE,
        container=ContainerFormat.MKV,
        encoder="preset",
        **fields,
    )


def _db() -> FakeSession:
    db = FakeSession()
    _seed(db)
    return db


def _job(db: FakeSession) -> Any:
    return db.rows["jobs"][0]


# --- precedence ------------------------------------------------------------------


def _view(**fields: Any) -> TranscodePresetView:
    return TranscodePresetView.model_validate(_preset(**fields))


def test_override_wins_over_the_preset_and_none_leaves_it() -> None:
    db = _db()
    preset = _view(audio_selection=AudioSelection.ALL, subtitle_output=SubtitleOutput.EMBEDDED)
    assert jts.effective_preset_view(preset, _job(db)) == preset
    _job(db).track_selection = OVERRIDE
    effective = jts.effective_preset_view(preset, _job(db))
    assert effective is not None
    assert effective.audio_languages == ["jpn", "eng"]
    assert effective.subtitle_output == SubtitleOutput.BOTH
    assert effective.audio_format == "copy"
    assert effective.name == "Anime"
    assert jts.effective_preset_view(None, _job(db)) is None


def test_an_override_that_no_longer_validates_lets_the_preset_decide() -> None:
    db = _db()
    _job(db).track_selection = {"audio_selection": "languages", "audio_languages": []}
    assert jts.job_override(_job(db)) is None


# --- default ---------------------------------------------------------------------


async def test_default_without_any_session_is_the_builtin_default() -> None:
    db = _db()
    default = await jts.default_for_job(db, _job(db))
    assert default == jts.DefaultSelection(TrackSelectionSettings(), jts.BUILTIN_DEFAULT_SOURCE, None)


async def test_default_from_the_routed_session_then_the_latest_application_then_an_explicit_one() -> None:
    db = _db()
    db.rows["transcode_presets"][0].subtitle_selection = SubtitleSelection.NONE
    db.rows["transcode_presets"].append(_preset(audio_selection=AudioSelection.ALL))
    db.rows["sessions"].append(
        Session(
            id="ses_y",
            name="Anime",
            media_type=MediaType.MOVIE,
            is_builtin=False,
            rip_preset_id="rpr_x",
            transcode_preset_id="tpr_y",
            output_path_template="{title}.{ext}",
        )
    )
    db.rows["sessions"].append(
        Session(
            id="ses_iso",
            name="ISO",
            media_type=MediaType.MOVIE,
            is_builtin=False,
            rip_preset_id="rpr_x",
            transcode_preset_id=None,
            output_path_template="{title}.{ext}",
        )
    )
    # Routed: the drive default.
    db.rows["drives"] = [Drive(id="drv_x", device_path="/dev/sr0", default_session_id="ses_x")]
    default = await jts.default_for_job(db, _job(db))
    assert (default.source, default.preset_id) == ("Plex 1080p H.265", "tpr_x")
    assert default.settings.subtitle_selection == SubtitleSelection.NONE

    # The latest applied session wins over routing.
    now = datetime.now(UTC)
    db.rows["session_applications"] = [
        SessionApplication(id="sap_1", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.DONE),
        SessionApplication(
            id="sap_2",
            session_id="ses_y",
            job_id=JOB,
            status=SessionApplicationStatus.QUEUED,
            created_at=now + timedelta(minutes=1),
        ),
    ]
    db.rows["session_applications"][0].created_at = now
    assert (await jts.default_for_job(db, _job(db))).preset_id == "tpr_y"

    # An explicit session (Apply session) wins over both; one with no
    # transcode preset, or an unknown one, gives the built-in default.
    assert (await jts.default_for_job(db, _job(db), "ses_x")).preset_id == "tpr_x"
    assert (await jts.default_for_job(db, _job(db), "ses_iso")).source == jts.BUILTIN_DEFAULT_SOURCE
    assert (await jts.default_for_job(db, _job(db), "ses_gone")).source == jts.BUILTIN_DEFAULT_SOURCE


async def test_default_when_the_sessions_preset_row_is_missing() -> None:
    db = _db()
    db.rows["transcode_presets"] = []
    assert (await jts.default_for_job(db, _job(db), "ses_x")).source == jts.BUILTIN_DEFAULT_SOURCE


# --- disc languages --------------------------------------------------------------


async def test_disc_languages_without_a_scan_or_without_streams_are_unknown() -> None:
    db = _db()
    assert (await jts.disc_languages(db, _job(db))).known is False
    _job(db).metadata_json = {"scan_result": {"disc_type": "dvd", "titles": [{"index": 0, "duration_seconds": 9}]}}
    assert (await jts.disc_languages(db, _job(db))).known is False


async def test_disc_languages_from_the_kept_track_rows() -> None:
    db = _db()
    _job(db).metadata_json = {"scan_result": _scan()}
    db.rows["tracks"] = [
        Track(id="t0", job_id=JOB, kind=TrackKind.VIDEO_TITLE, index=0, source_ref="0", status=TrackStatus.QUEUED),
        Track(
            id="t1",
            job_id=JOB,
            kind=TrackKind.VIDEO_TITLE,
            index=1,
            source_ref="1",
            status=TrackStatus.QUEUED,
            excluded=True,
        ),
    ]
    disc = await jts.disc_languages(db, _job(db))
    assert disc.known is True
    assert [(a.language, a.title_count, a.language_name) for a in disc.audio] == [
        ("eng", 1, "English"),
        ("jpn", 1, "Japanese"),
    ]
    assert disc.audio[0].tracks == ["TrueHD Surround 5.1 English", "DD Surround 5.1 English"]
    assert [(s.language, s.forced_only) for s in disc.subtitles] == [("eng", False)]


async def test_disc_languages_before_any_track_row_use_the_minimum_length() -> None:
    db = _db()
    db.rows["tracks"] = []
    _job(db).metadata_json = {"scan_result": _scan()}
    disc = await jts.disc_languages(db, _job(db))
    # Titles 0 and 1 (long enough) and 3 (unknown length): not the short French extra.
    assert [(a.language, a.title_count) for a in disc.audio] == [("eng", 2), ("jpn", 2)]
    assert [(s.language, s.forced_only) for s in disc.subtitles] == [("eng", False), ("ger", True)]

    _job(db).metadata_json = {"scan_result": _scan(min_length=None)}
    assert "fre" in [a.language for a in (await jts.disc_languages(db, _job(db))).audio]


async def test_a_stream_without_a_description_is_labelled_by_codec() -> None:
    db = _db()
    db.rows["tracks"] = []
    _job(db).metadata_json = {
        "scan_result": {
            "disc_type": "dvd",
            "titles": [
                {
                    "index": 0,
                    "duration_seconds": 900,
                    "streams": [
                        {"index": 1, "kind": "subtitle", "language": "eng", "codec_id": "S_VOBSUB"},
                        {"index": 2, "kind": "subtitle", "language": "eng", "codec": "PGS"},
                        {"index": 3, "kind": "subtitle", "language": "eng"},
                    ],
                }
            ],
        }
    }
    (eng,) = (await jts.disc_languages(db, _job(db))).subtitles
    assert eng.tracks == ["S_VOBSUB", "PGS", "eng"]


# --- edit lock -------------------------------------------------------------------


def _task(status: TranscodeTaskStatus) -> TranscodeTask:
    return TranscodeTask(
        id=f"txt_{status.value}",
        session_application_id="sap_1",
        source_track_id="trk_1",
        status=status,
        output_path=f"out-{status.value}.mkv",
        attempts=0,
        progress_pct=0,
    )


async def test_edit_lock_once_a_task_is_claimed() -> None:
    db = _db()
    assert await jts.edit_lock_reason(db, _job(db)) is None
    db.rows["session_applications"] = [
        SessionApplication(id="sap_1", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.QUEUED)
    ]
    db.rows["transcode_tasks"] = [_task(TranscodeTaskStatus.QUEUED)]
    assert await jts.edit_lock_reason(db, _job(db)) is None
    for status in (TranscodeTaskStatus.IN_PROGRESS, TranscodeTaskStatus.DONE, TranscodeTaskStatus.FAILED):
        db.rows["transcode_tasks"] = [_task(TranscodeTaskStatus.QUEUED), _task(status)]
        reason = await jts.edit_lock_reason(db, _job(db))
        assert reason is not None and "Apply session" in reason


# --- endpoints -------------------------------------------------------------------


def test_get_shows_default_disc_and_editable(signing_key: bytes, tmp_path: Path) -> None:
    db = _db()
    db.rows["tracks"] = []
    _job(db).metadata_json = {"scan_result": _scan()}
    app, token = _make_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.get(f"/api/jobs/{JOB}/track-selection", headers=_auth(token))
        r_session = client.get(f"/api/jobs/{JOB}/track-selection?session_id=ses_x", headers=_auth(token))
        missing = client.get("/api/jobs/job_01JZXR7K3M5Q8N4VWA00000009/track-selection", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["override"] is None
    assert body["default_source"] == "built-in default"
    assert body["effective"] == body["default"]
    assert body["editable"] is True and body["locked_reason"] is None
    assert [a["language"] for a in body["disc"]["audio"]] == ["eng", "jpn"]
    assert r_session.json()["default_source"] == "Plex 1080p H.265"
    assert missing.status_code == 404


def test_put_sets_and_clears_the_override(signing_key: bytes, tmp_path: Path) -> None:
    db = _db()
    # The job detail view needs these; the shared seed leaves them unset.
    _job(db).resumed_from_crash = False
    db.rows["tracks"][0].attempts = 0
    app, token = _make_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.put(
            f"/api/jobs/{JOB}/track-selection",
            json={"track_selection": {**OVERRIDE, "audio_languages": ["JPN", "deu"]}},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert r.json()["effective"]["audio_languages"] == ["jpn", "ger"]
        assert _job(db).track_selection["audio_languages"] == ["jpn", "ger"]
        job_view = client.get(f"/api/jobs/{JOB}", headers=_auth(token))
        cleared = client.put(f"/api/jobs/{JOB}/track-selection", json={"track_selection": None}, headers=_auth(token))
    assert job_view.json()["job"]["track_selection"]["audio_languages"] == ["jpn", "ger"]
    assert cleared.status_code == 200
    assert cleared.json()["override"] is None
    assert _job(db).track_selection is None


@pytest.mark.parametrize(
    "body",
    [
        {"track_selection": {"audio_selection": "languages", "audio_languages": []}},
        {"track_selection": {"subtitle_languages": ["english"]}},
        {"track_selection": {"subtitle_output": "burned"}},
    ],
)
def test_put_validates_the_selection(signing_key: bytes, tmp_path: Path, body: dict[str, Any]) -> None:
    db = _db()
    app, token = _make_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.put(f"/api/jobs/{JOB}/track-selection", json=body, headers=_auth(token))
    assert r.status_code == 422
    assert _job(db).track_selection is None


def test_put_refused_once_a_task_is_claimed_and_for_an_unknown_job(signing_key: bytes, tmp_path: Path) -> None:
    db = _db()
    db.rows["session_applications"] = [
        SessionApplication(id="sap_1", session_id="ses_x", job_id=JOB, status=SessionApplicationStatus.RUNNING)
    ]
    db.rows["transcode_tasks"] = [_task(TranscodeTaskStatus.IN_PROGRESS)]
    app, token = _make_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.put(f"/api/jobs/{JOB}/track-selection", json={"track_selection": OVERRIDE}, headers=_auth(token))
        view = client.get(f"/api/jobs/{JOB}/track-selection", headers=_auth(token))
        missing = client.put(
            "/api/jobs/job_01JZXR7K3M5Q8N4VWA00000009/track-selection",
            json={"track_selection": None},
            headers=_auth(token),
        )
    assert r.status_code == 409
    assert "Apply session" in r.json()["detail"]
    assert view.json()["editable"] is False
    assert _job(db).track_selection is None
    assert missing.status_code == 404


def test_apply_session_sets_keeps_or_clears_the_override(signing_key: bytes, tmp_path: Path) -> None:
    db = _db()
    app, token = _make_app(signing_key, db, tmp_path)
    with TestClient(app) as client:
        r = client.post(
            f"/api/jobs/{JOB}/transcode",
            json={"session_id": "ses_x", "track_selection": OVERRIDE},
            headers=_auth(token),
        )
        assert r.status_code == 200, r.text
        assert _job(db).track_selection["subtitle_output"] == "both"
        # Left out: kept (overwrite re-apply).
        r2 = client.post(
            f"/api/jobs/{JOB}/transcode", json={"session_id": "ses_x", "overwrite": True}, headers=_auth(token)
        )
        assert r2.status_code == 200, r2.text
        assert _job(db).track_selection is not None
        r3 = client.post(
            f"/api/jobs/{JOB}/transcode",
            json={"session_id": "ses_x", "overwrite": True, "track_selection": None},
            headers=_auth(token),
        )
    assert r3.status_code == 200, r3.text
    assert _job(db).track_selection is None


@pytest.fixture
def signing_key() -> bytes:
    return secrets.token_bytes(32)
