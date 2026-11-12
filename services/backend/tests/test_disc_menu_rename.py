"""Applying a name from the disc menu (fork only, disc menus M4): the bonus layout by role, the rename of a
finished encode with its sidecars and OCR rows, a rename that waits for a running encode, and every refusal leaving
the files and rows as they were.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402

from arm_backend.disc_menus import rename  # noqa: E402
from arm_backend.disc_menus.rename import (  # noqa: E402
    RenameRefused,
    apply_pending,
    apply_proposal,
    bonus_output_path,
    bonus_path,
    show_folder,
)
from arm_backend.transcode_apply import compute_outputs  # noqa: E402
from arm_common import (  # noqa: E402
    DiscType,
    Job,
    MediaType,
    MenuProposal,
    MenuProposalStatus,
    Session,
    Track,
    TrackKind,
    TrackRole,
    TrackStatus,
    TranscodeTask,
    TranscodeTaskKind,
    TranscodeTaskStatus,
)
from tests._fakes import FakeSession  # noqa: E402

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
SHOW = "Show (2020) [imdbid-tt1]"
OLD = f"{SHOW}/Season 01/Show - S01D02T08 - slug.mkv"
SUPS = [f"{SHOW}/Season 01/Show - S01D02T08 - slug.eng.sup"]
SRTS = [f"{SHOW}/Season 01/Show - S01D02T08 - slug.eng.srt"]


def test_show_folder() -> None:
    assert str(show_folder(OLD)) == SHOW
    assert str(show_folder("Movie (2008)/Movie.mkv")) == "Movie (2008)"
    assert str(show_folder("Show/Specials/x.mkv")) == "Show"
    assert str(show_folder("Show/trailers/x.mkv")) == "Show"


@pytest.mark.parametrize(
    ("role", "name", "episode", "want"),
    [
        (TrackRole.TRAILER, "Promo Videos", None, f"{SHOW}/trailers/Promo Videos.mkv"),
        (TrackRole.OTHER, "Textless Opening Song", None, f"{SHOW}/other/Textless Opening Song.mkv"),
        (TrackRole.EXTRA, "FunimationCon 2020: Panel", None, f"{SHOW}/extras/FunimationCon 2020 - Panel.mkv"),
        (TrackRole.SPECIAL, "Special 1", 1, f"{SHOW}/Season 00/Show - S00E01 - Special 1.mkv"),
        (TrackRole.SPECIAL, "Special 1", None, None),
        (TrackRole.EPISODE, "x", None, None),
        (TrackRole.EXTRA, "///", None, None),
    ],
)
def test_bonus_path(role: TrackRole, name: str, episode: int | None, want: str | None) -> None:
    assert bonus_path(OLD, role, name, "Show", episode) == want


def test_a_special_with_no_show_name() -> None:
    assert bonus_path(OLD, TrackRole.SPECIAL, "Sp", "", 2) == f"{SHOW}/Season 00/S00E02 - Sp.mkv"


def _job(media_type: MediaType = MediaType.TV) -> Job:
    return Job(id=JOB, disc_type=DiscType.BLURAY, title="Show", year=2020, media_type=media_type, season=1,
               disc_number=2, metadata_json={})  # fmt: skip


def _track(**kw: Any) -> Track:
    fields: dict[str, Any] = dict(
        id="trk_8", job_id=JOB, kind=TrackKind.VIDEO_TITLE, index=8, source_ref="8", status=TrackStatus.DONE,
        role=TrackRole.EXTRA, attempts=0,
    )  # fmt: skip
    fields.update(kw)
    return Track(**fields)


def test_bonus_output_path_only_for_a_named_tv_bonus() -> None:
    named = _track(role=TrackRole.TRAILER, custom_filename="Promo Videos")
    assert bonus_output_path(_job(), named, OLD) == f"{SHOW}/trailers/Promo Videos.mkv"
    assert bonus_output_path(_job(MediaType.MOVIE), named, OLD) == OLD
    assert bonus_output_path(_job(), _track(), OLD) == OLD
    assert bonus_output_path(_job(), _track(role=TrackRole.SPECIAL, custom_filename="Sp"), OLD) == OLD


def test_compute_outputs_puts_a_named_bonus_in_the_bonus_layout() -> None:
    session = Session(
        id="ses_x", name="TV", media_type=MediaType.TV, is_builtin=False, rip_preset_id="rpr_x",
        output_path_template="{show}/Season {season}/{show} - {episode_code}.mkv",
    )  # fmt: skip
    tracks = [
        _track(role=TrackRole.TRAILER, custom_filename="Promo Videos"),
        _track(id="trk_1", index=1, source_ref="1", role=TrackRole.EPISODE, episode_number=1),
    ]
    out = {r.track_id: r.output_path for r in compute_outputs(_job(), tracks, session, None)}
    assert out == {"trk_8": "Show/trailers/Promo Videos.mkv", "trk_1": "Show/Season 01/Show - S01E01.mkv"}


# ---- the rename ------------------------------------------------------------------------


def _encode(status: TranscodeTaskStatus = TranscodeTaskStatus.DONE, **kw: Any) -> TranscodeTask:
    fields: dict[str, Any] = dict(
        id="txt_enc", session_application_id="sap_x", source_track_id="trk_8", kind=TranscodeTaskKind.ENCODE,
        status=status, output_path=OLD, sidecar_paths=list(SUPS), attempts=0, progress_pct=100,
    )  # fmt: skip
    fields.update(kw)
    return TranscodeTask(**fields)


def _ocr() -> TranscodeTask:
    return TranscodeTask(
        id="txt_ocr", session_application_id="sap_x", source_track_id="trk_8", kind=TranscodeTaskKind.SUBTITLE_OCR,
        parent_task_id="txt_enc", status=TranscodeTaskStatus.DONE, output_path=OLD, sidecar_paths=list(SRTS),
    )  # fmt: skip


def _proposal(**kw: Any) -> MenuProposal:
    fields: dict[str, Any] = dict(
        id="mnp_1", job_id=JOB, track_id="trk_8", page="4", button=12, label="Promo Videos", plays="00023.mpls",
        role=TrackRole.TRAILER, name="Promo Videos", status=MenuProposalStatus.PROPOSED,
    )  # fmt: skip
    fields.update(kw)
    return MenuProposal(**fields)


def _setup(media: Path, *, tasks: list[TranscodeTask] | None = None, files: bool = True) -> FakeSession:
    db = FakeSession()
    db.rows["jobs"] = [_job()]
    db.rows["tracks"] = [_track()]
    db.rows["transcode_tasks"] = tasks if tasks is not None else [_encode(), _ocr()]
    if files:
        for rel in [OLD, *SUPS, *SRTS]:
            (media / rel).parent.mkdir(parents=True, exist_ok=True)
            (media / rel).write_text(rel)
        # An episode keeps the season folder.
        (media / SHOW / "Season 01" / "Show - S01E08.mkv").write_text("ep")
    return db


async def test_accepting_renames_the_encode_its_sidecars_and_its_ocr_rows(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    p = await apply_proposal(db, _proposal(), tmp_path)  # type: ignore[arg-type]
    new = f"{SHOW}/trailers/Promo Videos.mkv"
    assert (p.status, p.applied_path, p.last_error) == (MenuProposalStatus.APPLIED, new, None)
    assert sorted(str(f.relative_to(tmp_path)) for f in tmp_path.rglob("*") if f.is_file()) == sorted(
        [new, f"{SHOW}/trailers/Promo Videos.eng.sup", f"{SHOW}/trailers/Promo Videos.eng.srt",
         f"{SHOW}/Season 01/Show - S01E08.mkv"]
    )  # fmt: skip
    encode, ocr = db.rows["transcode_tasks"]
    assert (encode.output_path, encode.sidecar_paths) == (new, [f"{SHOW}/trailers/Promo Videos.eng.sup"])
    assert (ocr.output_path, ocr.sidecar_paths) == (new, [f"{SHOW}/trailers/Promo Videos.eng.srt"])
    track = db.rows["tracks"][0]
    assert (track.role, track.custom_filename, track.identity_provenance) == (
        TrackRole.TRAILER, "Promo Videos", {"role": "disc_menu"}
    )  # fmt: skip


async def test_a_special_takes_season_0_and_the_old_folder_goes_when_empty(tmp_path: Path) -> None:
    db = _setup(tmp_path, tasks=[_encode(sidecar_paths=None, replaced_sidecar_paths=list(SUPS))])
    (tmp_path / SHOW / "Season 01" / "Show - S01E08.mkv").unlink()
    for rel in [*SUPS, *SRTS]:
        (tmp_path / rel).unlink()
    p = await apply_proposal(db, _proposal(role=TrackRole.SPECIAL, name="Sp", episode_number=4), tmp_path)  # type: ignore[arg-type]
    assert p.applied_path == f"{SHOW}/Season 00/Show - S00E04 - Sp.mkv"
    assert not (tmp_path / SHOW / "Season 01").exists()
    track = db.rows["tracks"][0]
    assert (track.season, track.episode_number, track.episode_number_end) == (0, 4, None)
    assert db.rows["transcode_tasks"][0].replaced_sidecar_paths == [f"{SHOW}/Season 00/Show - S00E04 - Sp.eng.sup"]


async def test_a_name_already_in_place_is_applied_without_a_move(tmp_path: Path) -> None:
    new = f"{SHOW}/trailers/Promo Videos.mkv"
    db = _setup(tmp_path, tasks=[_encode(output_path=new, sidecar_paths=None)], files=False)
    p = await apply_proposal(db, _proposal(), tmp_path)  # type: ignore[arg-type]
    assert (p.status, p.applied_path) == (MenuProposalStatus.APPLIED, new)


async def test_a_running_encode_leaves_the_rename_for_its_completion(tmp_path: Path) -> None:
    db = _setup(tmp_path, tasks=[_encode(TranscodeTaskStatus.IN_PROGRESS)])
    p = await apply_proposal(db, _proposal(), tmp_path)  # type: ignore[arg-type]
    assert p.status == MenuProposalStatus.ACCEPTED
    assert (tmp_path / OLD).exists()
    assert db.rows["tracks"][0].custom_filename == "Promo Videos"
    db.rows["menu_proposals"] = [p]
    encode = db.rows["transcode_tasks"][0]
    encode.status = TranscodeTaskStatus.DONE
    assert await apply_pending(db, encode, tmp_path) == 1  # type: ignore[arg-type]
    assert p.status == MenuProposalStatus.APPLIED and (tmp_path / p.applied_path).exists()  # type: ignore[operator]


async def test_a_pending_rename_that_fails_keeps_why(tmp_path: Path) -> None:
    db = _setup(tmp_path, tasks=[_encode()], files=False)
    p = _proposal(status=MenuProposalStatus.ACCEPTED)
    db.rows["menu_proposals"] = [p]
    assert await apply_pending(db, db.rows["transcode_tasks"][0], tmp_path) == 0  # type: ignore[arg-type]
    assert p.status == MenuProposalStatus.ACCEPTED
    assert p.last_error is not None and "isn't at" in p.last_error


@pytest.mark.parametrize(
    ("proposal", "why"),
    [
        (_proposal(track_id=None), "no track"),
        (_proposal(role=TrackRole.EPISODE), "only a bonus"),
        (_proposal(name=None), "only a bonus"),
        (_proposal(role=TrackRole.SPECIAL), "season 0 number"),
    ],
)
async def test_refused_before_anything_changes(tmp_path: Path, proposal: MenuProposal, why: str) -> None:
    db = _setup(tmp_path)
    with pytest.raises(RenameRefused, match=why):
        await apply_proposal(db, proposal, tmp_path)  # type: ignore[arg-type]
    assert db.rows["tracks"][0].custom_filename is None


async def test_refused_with_no_finished_encode(tmp_path: Path) -> None:
    db = _setup(tmp_path, tasks=[_encode(TranscodeTaskStatus.FAILED)])
    with pytest.raises(RenameRefused, match="no finished encode"):
        await apply_proposal(db, _proposal(), tmp_path)  # type: ignore[arg-type]
    assert db.rows["tracks"][0].custom_filename is None


async def test_refused_when_the_target_exists(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    (tmp_path / SHOW / "trailers").mkdir(parents=True)
    (tmp_path / SHOW / "trailers" / "Promo Videos.eng.sup").write_text("x")
    with pytest.raises(RenameRefused, match="already exists"):
        await apply_proposal(db, _proposal(), tmp_path)  # type: ignore[arg-type]
    assert (tmp_path / OLD).exists() and db.rows["transcode_tasks"][0].output_path == OLD


async def test_a_missing_sidecar_is_left_out(tmp_path: Path) -> None:
    db = _setup(tmp_path)
    (tmp_path / SRTS[0]).unlink()
    p = await apply_proposal(db, _proposal(), tmp_path)  # type: ignore[arg-type]
    assert p.status == MenuProposalStatus.APPLIED


async def test_a_failed_move_puts_the_moved_files_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _setup(tmp_path)
    real = os.rename
    calls = {"n": 0}

    def flaky(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr(rename.os, "rename", flaky)
    with pytest.raises(RenameRefused, match="could not move"):
        await apply_proposal(db, _proposal(), tmp_path)  # type: ignore[arg-type]
    assert all((tmp_path / rel).exists() for rel in [OLD, *SUPS, *SRTS])
    assert db.rows["transcode_tasks"][0].output_path == OLD
    assert db.rows["tracks"][0].custom_filename is None
