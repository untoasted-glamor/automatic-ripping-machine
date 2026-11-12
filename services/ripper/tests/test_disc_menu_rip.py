"""The disc menu in the rip (fork only, design v5 A2): which path a ticked menu sends the disc down, when it is copied,
what it costs the titles, and when it keeps the backup.

The copy itself is faked here (`menus.extract.extract_menu`): the fake makemkvcon writes empty structure files, which
no menu reader can parse. `test_disc_menu_extract.py` runs the real copy on discs built at test time.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import arm_ripper.menus.extract as menu_extract
from arm_common import DiscType
from arm_common.enums import TrackKind, TrackStatus
from arm_common.schemas import TrackView
from arm_ripper.menus.extract import MenuOutcome
from arm_ripper.rip.dispatcher import ENCRYPTED_SOURCE, SELECTION_BACKUP_DIRNAME

from tests._makemkvcon_fake import HANAKO_BD
from tests.test_rip_dispatcher import (  # noqa: F401 - _plenty_of_space is an autouse fixture
    _bd_job,
    _exclude,
    _GB,
    _install_sources,
    _plenty_of_space,
    _results,
    _rip_start_tracks,
    _rip_tracks,
    _scan,
    _set_free_space,
    _track,
    install,
)

_DONE = MenuOutcome(TrackStatus.DONE, output_path=Path("/x"), size_bytes=264)


class _Sink:
    """The menu row as the rip sees it: every start, progress and finish, in order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.progress_seen: list[float] = []
        self.outcomes: list[MenuOutcome] = []

    async def start(self) -> None:
        self.events.append("start")

    async def progress(self, fraction: float) -> None:
        self.progress_seen.append(fraction)

    async def finish(self, outcome: MenuOutcome) -> None:
        self.events.append("finish")
        self.outcomes.append(outcome)

    @property
    def outcome(self) -> MenuOutcome:
        (outcome,) = self.outcomes
        return outcome


def _fake_extract(monkeypatch: pytest.MonkeyPatch, fake: Any, outcome: MenuOutcome = _DONE) -> list[Any]:
    """Record each copy: what it read, where it wrote, the lost files it was told of, and how many remuxes and
    drive reads had run by then."""
    calls: list[Any] = []

    async def extract(source: str | Path, dest: Path, *, lost: Any = ()) -> MenuOutcome:
        calls.append(
            SimpleNamespace(
                source=Path(source),
                dest=dest,
                lost=tuple(lost),
                exists=Path(source).exists(),
                mkv_before=sum(inv.command == "mkv" for inv in fake.invocations),
                drive_reads=len(fake.drive_reads),
            )
        )
        return outcome

    monkeypatch.setattr(menu_extract, "extract_menu", extract)
    return calls


def _mkv_all(fake: Any) -> list[Any]:
    return [inv for inv in fake.invocations if inv.command == "mkv" and "all" in inv.argv]


async def test_a_ticked_menu_sends_a_full_selection_through_one_backup(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    tracks = _rip_start_tracks(scan)
    sink = _Sink()
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, scan_titles=scan.titles, menu=sink)

    assert all(r.ok for r in _results(reports).values())
    assert [inv.command for inv in fake.drive_reads] == ["backup"]
    assert _mkv_all(fake) == []
    (call,) = calls
    # Out of the finished backup, into <job raw>/menu/, before any remux.
    assert call.source == out / SELECTION_BACKUP_DIRNAME / "disc" and call.exists
    assert call.dest == out / "menu"
    assert call.mkv_before == 0 and call.lost == ()
    assert sink.events == ["start", "finish"] and sink.outcome == _DONE
    # Every title remuxed and the menu copied: the backup goes.
    assert not (out / SELECTION_BACKUP_DIRNAME).exists()


async def test_with_full_selection_backups_on_the_menu_changes_nothing(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    sink = _Sink()

    await _rip_tracks(
        _rip_start_tracks(scan), tmp_path / "job", scan_titles=scan.titles, menu=sink, backup_full_selections=True
    )

    assert [inv.command for inv in fake.drive_reads] == ["backup"] and len(calls) == 1


@pytest.mark.parametrize("why", ["no-room", "backup-fails", "no-drive-index"])
async def test_when_only_the_menu_needed_the_backup_its_failure_costs_only_the_menu(monkeypatch, tmp_path, why):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    if why == "no-room":
        _set_free_space(monkeypatch, 1 * _GB)
    elif why == "backup-fails":
        fake.fail("backup")
    else:

        async def no_drive(_device: str) -> None:
            return None

        monkeypatch.setattr("arm_ripper.rip.makemkv_backup.find_drive_index", no_drive)
    tracks = _rip_start_tracks(scan)
    sink = _Sink()
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(tracks, out, scan_titles=scan.titles, menu=sink)

    # The titles are ripped with mkv all, as without the menu.
    assert all(r.ok for r in _results(reports).values()), _results(reports)
    assert len(_mkv_all(fake)) == 1
    assert calls == []
    assert sink.outcome.status == TrackStatus.FAILED
    assert sink.outcome.error is not None and sink.outcome.error.endswith("; the titles were ripped without it")
    assert not (out / SELECTION_BACKUP_DIRNAME).exists()


async def test_when_the_backup_was_needed_anyway_its_failure_fails_the_menu_too(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    fake.fail("backup")
    tracks = _exclude(_rip_start_tracks(scan), 7)
    sink = _Sink()

    reports, _ = await _rip_tracks(tracks, tmp_path / "job", scan_titles=scan.titles, menu=sink)

    kept = [r for t, r in _results(reports).items() if t != "trk_7"]
    assert kept and all(not r.ok and r.error is not None and r.error.startswith("disc backup failed") for r in kept)
    assert _mkv_all(fake) == [] and calls == []
    assert sink.outcome.status == TrackStatus.FAILED and sink.outcome.error == kept[0].error


async def test_a_damaged_backup_tells_the_copy_what_it_lost(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    fake.lose_in_backup("/BDMV/STREAM/00099.m2ts")  # no title plays it
    sink = _Sink()
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(_rip_start_tracks(scan), out, scan_titles=scan.titles, menu=sink)

    assert all(r.ok for r in _results(reports).values())
    (call,) = calls
    assert call.lost == ("/BDMV/STREAM/00099.m2ts",)
    assert sink.outcome == _DONE
    assert not (out / SELECTION_BACKUP_DIRNAME).exists()  # damaged: a retry reads the disc again


async def test_unnamed_damage_on_a_menu_only_backup_falls_back_to_mkv_all(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    fake.lose_in_backup("/BDMV/PLAYLIST/00000.mpls")  # not a clip: no title can be trusted from it
    sink = _Sink()
    out = tmp_path / "job"

    reports, _ = await _rip_tracks(_rip_start_tracks(scan), out, scan_titles=scan.titles, menu=sink)

    assert all(r.ok for r in _results(reports).values())
    assert [inv.command for inv in fake.drive_reads] == ["backup", "mkv"]
    # The copy still ran, told of the lost playlist; it decides whether the menu needed it.
    (call,) = calls
    assert call.lost == ("/BDMV/PLAYLIST/00000.mpls",) and call.mkv_before == 0
    assert sink.outcome == _DONE
    assert not (out / SELECTION_BACKUP_DIRNAME).exists()


@pytest.mark.parametrize(
    ("outcome", "kept"),
    [
        (menu_extract.failed("menu files lost or unreadable: BDMV/STREAM/00046.m2ts"), True),
        (menu_extract.skipped(menu_extract.JAVA_MENU), False),
    ],
    ids=["failed-keeps-it", "skipped-doesnt"],
)
async def test_a_failed_menu_keeps_the_backup_and_a_skipped_one_doesnt(monkeypatch, tmp_path, outcome, kept):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    _fake_extract(monkeypatch, fake, outcome)
    sink = _Sink()
    out = tmp_path / "job"

    await _rip_tracks(_rip_start_tracks(scan), out, scan_titles=scan.titles, menu=sink)

    assert sink.outcome == outcome
    assert (out / SELECTION_BACKUP_DIRNAME / ".complete").is_file() is kept


async def test_a_menu_alone_comes_out_of_the_kept_backup_with_no_drive_read(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    out = tmp_path / "job"
    # The first rip: the menu copy failed, so the backup stayed.
    _fake_extract(monkeypatch, fake, menu_extract.failed("menu copy failed: boom"))
    first, _ = await _rip_tracks(_rip_start_tracks(scan), out, scan_titles=scan.titles, menu=_Sink())
    assert (out / SELECTION_BACKUP_DIRNAME / ".complete").is_file()
    fake.invocations.clear()
    calls = _fake_extract(monkeypatch, fake)
    # The retry: every title is DONE, so each is left out; only the menu is wanted.
    done = [
        t.model_copy(update={"status": TrackStatus.DONE, "excluded": True, "output_path": str(r[0].output_path)})
        for t, r in zip(_rip_start_tracks(scan), first.values(), strict=True)
    ]
    sink = _Sink()

    reports, started = await _rip_tracks(done, out / "retry-1", scan_titles=scan.titles, menu=sink,
                                         backup_dir=out / SELECTION_BACKUP_DIRNAME)  # fmt: skip

    assert all(r == [] for r in reports.values()) and started == []
    assert fake.drive_reads == [] and [inv for inv in fake.invocations if inv.command == "mkv"] == []
    (call,) = calls
    assert call.source == out / SELECTION_BACKUP_DIRNAME / "disc" and call.dest == out / "menu"
    assert sink.outcome == _DONE
    assert not (out / SELECTION_BACKUP_DIRNAME).exists()


async def test_a_menu_alone_with_no_kept_backup_reads_the_disc_for_it(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    done = [t.model_copy(update={"status": TrackStatus.DONE, "excluded": True}) for t in _rip_start_tracks(scan)]
    sink = _Sink()
    out = tmp_path / "job"
    progress: list[str] = []

    async def on_progress(track: TrackView, _fraction: float, _stats: Any = None) -> None:
        progress.append(track.id)

    await _rip_tracks(done, out, scan_titles=scan.titles, menu=sink, on_track_progress=on_progress)

    assert [inv.command for inv in fake.drive_reads] == ["backup"]
    assert len(calls) == 1 and sink.outcome == _DONE
    # With no title to credit, the backup's progress bar is the menu's.
    assert progress == [] and sink.progress_seen and sink.progress_seen[-1] > 0.99
    assert not (out / SELECTION_BACKUP_DIRNAME).exists()


async def test_a_backup_the_drive_dropped_under_leaves_the_menu_for_the_resume(monkeypatch, tmp_path):
    fake, scan = await _bd_job(monkeypatch, tmp_path)
    calls = _fake_extract(monkeypatch, fake)
    sink = _Sink()

    reports, _ = await _rip_tracks(
        _rip_start_tracks(scan),
        tmp_path / "job",
        scan_titles=scan.titles,
        menu=sink,
        stop=asyncio.Event(),
        drive_lost=lambda: True,
    )

    assert all(not r.ok for r in _results(reports).values())
    assert calls == [] and sink.outcomes == [] and sink.events == ["start"]
    assert _mkv_all(fake) == []


@pytest.mark.parametrize("exclude", [False, True], ids=["mkv-all", "remux"])
@pytest.mark.parametrize("source", ["bd-folder", "dvd-iso"])
async def test_a_file_source_is_copied_from_directly_after_its_titles(monkeypatch, tmp_path, source, exclude):
    fake, device = _install_sources(monkeypatch, tmp_path, source)
    scan = await _scan(monkeypatch, device)
    fake.invocations.clear()
    calls = _fake_extract(monkeypatch, fake)
    monkeypatch.setattr(menu_extract, "source_encrypted", lambda _source: False)
    sink = _Sink()
    out = tmp_path / "job"

    tracks = _rip_start_tracks(scan)
    if exclude:
        tracks = _exclude(tracks, tracks[1].index)

    reports, _ = await _rip_tracks(
        tracks, out, disc_type=scan.disc_type, device=device, scan_titles=scan.titles, menu=sink
    )

    assert all(r.ok for r in _results(reports).values())
    assert fake.drive_reads == [] and all(inv.command != "backup" for inv in fake.invocations)
    (call,) = calls
    mkvs = sum(inv.command == "mkv" for inv in fake.invocations)
    assert call.source == Path(device) and call.dest == out / "menu" and call.mkv_before == mkvs >= 1
    assert sink.outcome == _DONE


async def test_an_encrypted_file_source_skips_its_menu(monkeypatch, tmp_path):
    fake, device = _install_sources(monkeypatch, tmp_path, "dvd-iso")
    scan = await _scan(monkeypatch, device)
    calls = _fake_extract(monkeypatch, fake)
    monkeypatch.setattr(menu_extract, "source_encrypted", lambda _source: True)
    sink = _Sink()

    await _rip_tracks(
        _rip_start_tracks(scan), tmp_path / "job", disc_type=DiscType.DVD, device=device, scan_titles=scan.titles,
        menu=sink,
    )  # fmt: skip

    assert calls == []
    assert sink.outcome == menu_extract.skipped(ENCRYPTED_SOURCE)


async def test_a_file_source_menu_alone_needs_no_titles(monkeypatch, tmp_path):
    fake, device = _install_sources(monkeypatch, tmp_path, "bd-folder")
    calls = _fake_extract(monkeypatch, fake)
    monkeypatch.setattr(menu_extract, "source_encrypted", lambda _source: False)
    sink = _Sink()

    await _rip_tracks([], tmp_path / "job", device=device, menu=sink)

    assert len(calls) == 1 and sink.outcome == _DONE
    assert fake.invocations == []


async def test_a_drive_job_with_no_stored_scan_skips_its_menu(monkeypatch, tmp_path):
    fake = install(monkeypatch, disc=HANAKO_BD)
    calls = _fake_extract(monkeypatch, fake)
    sink = _Sink()

    await _rip_tracks([_track(0)], tmp_path / "job", menu=sink)

    assert calls == [] and sink.outcome.status == TrackStatus.SKIPPED
    assert [inv.command for inv in fake.drive_reads] == ["mkv"]


@pytest.mark.parametrize(
    ("disc_type", "kind"), [(DiscType.BLURAY, TrackKind.DISC_BACKUP), (DiscType.DATA, TrackKind.DATA_DUMP)]
)
async def test_a_whole_disc_rip_skips_a_menu(monkeypatch, tmp_path, disc_type, kind):
    fake = install(monkeypatch, disc=HANAKO_BD)
    calls = _fake_extract(monkeypatch, fake)
    sink = _Sink()
    whole = _track(0, source_ref="full").model_copy(update={"kind": kind})
    monkeypatch.setattr("arm_ripper.rip.dispatcher.rip_data", _no_data_rip)

    await _rip_tracks([whole], tmp_path / "job", disc_type=disc_type, menu=sink)

    assert calls == [] and sink.outcome.status == TrackStatus.SKIPPED


async def _no_data_rip(**_kwargs: Any) -> Any:
    from arm_ripper.rip.makemkv_rip import RipResult

    return RipResult(ok=False, error="not in this test")
