"""Names proposed from a disc menu (fork only, disc menus M2): which track a button plays, the role and name its
label gives, one row per title, and a re-run that keeps the owner's decisions. The Blu-ray case is cut down from
Hanako BD D2 (ledger T47): episode buttons play the play-all from its marks, the bonus buttons play their own
playlists, and three bonuses weren't ripped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

import pytest  # noqa: E402

from arm_backend.disc_menus.proposals import (  # noqa: E402
    clean_label,
    lead_number,
    make_proposals,
    match_bd,
    match_dvd,
    plays_text,
    read_preview,
    role_of,
)
from arm_common import (  # noqa: E402
    DiscType,
    Job,
    MenuProposal,
    MenuProposalStatus,
    Track,
    TrackKind,
    TrackRole,
    TrackStatus,
)
from tests._fakes import FakeSession  # noqa: E402

JOB = "job_01JZXR7K3M5Q8N4VWA00000001"
T = 45000


@pytest.mark.parametrize(
    ("label", "role"),
    [
        ("Play All", TrackRole.PLAY_ALL),
        ("Promo Videos", TrackRole.TRAILER),
        ("Trailers", TrackRole.TRAILER),
        ("Japanese PV", TrackRole.TRAILER),
        ("TV CM", TrackRole.TRAILER),
        ("Textless Opening Song ver. 1", TrackRole.OTHER),
        ("Creditless ED", TrackRole.OTHER),
        ("OVA", TrackRole.SPECIAL),
        ("Episode 3", TrackRole.SPECIAL),
        ("Next Episode Preview", TrackRole.EXTRA),
        ("FunimationCon 2020: Toilet-bound Hanako-kun Panel", TrackRole.EXTRA),
    ],
)
def test_role_of(label: str, role: TrackRole) -> None:
    assert role_of(label) == role


def test_label_helpers() -> None:
    assert clean_label("  ■ Dolby TrueHD:  English 5.1 ") == "Dolby TrueHD: English 5.1"
    assert clean_label("Close —") == "Close"
    assert lead_number("Spook 8: Mitsuba") == 8
    assert lead_number("Episode 12") == 12
    assert lead_number("03 - The Title") == 3
    assert lead_number("Spook 1l: The Hell of Mirrors Part 2") is None
    assert lead_number("Promo Videos") is None


_PLAYLISTS = {
    "00008": {
        "items": [{"clip": "00014", "in": 0, "out": 1446 * T}, {"clip": "00015", "in": 0, "out": 1447 * T}],
        "marks": [],
    },
    "00022": {"items": [{"clip": "00027", "in": 0, "out": 2618 * T}], "marks": []},
    "00023": {"items": [{"clip": "00028", "in": 0, "out": 100 * T}, {"clip": "00058", "in": 0, "out": 35 * T}],
              "marks": []},
    "00024": {"items": [{"clip": "00029", "in": 0, "out": 92 * T}], "marks": []},
}  # fmt: skip
_TITLES = [
    {"index": 0, "segment_map": "14", "source_playlist": "00009.mpls"},
    {"index": 1, "segment_map": "15", "source_playlist": "00010.mpls"},
    {"index": 5, "segment_map": "14,15", "source_playlist": "00008.mpls"},
    {"index": 7, "segment_map": "27", "source_playlist": "00022.mpls"},
    {"index": 8, "segment_map": "28,58", "source_playlist": "00023.mpls"},
    {"index": 19, "segment_map": "29", "source_playlist": "00024.mpls"},
]


def _plays(playlist: str, start: int = 0) -> dict[str, Any]:
    return {"kind": "plays", "playlist": playlist, "start": start, "mark": None}


def test_match_bd() -> None:
    assert match_bd(_PLAYLISTS, _TITLES, _plays("00008"), "Play All") == 5
    # An episode button playing the play-all from a mark: the clip it starts in.
    assert match_bd(_PLAYLISTS, _TITLES, _plays("00008"), "Spook 8") == 0
    assert match_bd(_PLAYLISTS, _TITLES, _plays("00008", 1446 * T + 10), "Spook 9") == 1
    # A multi-clip bonus with no single-clip title: the whole playlist.
    assert match_bd(_PLAYLISTS, _TITLES, _plays("00023"), "Promo Videos") == 8
    assert match_bd(_PLAYLISTS, _TITLES, _plays("00022"), "Panel") == 7
    assert match_bd(_PLAYLISTS, _TITLES, _plays("00099"), "x") is None
    assert match_bd(_PLAYLISTS, [], _plays("00022"), "x") is None
    # Past the playlist's end: no clip, the whole playlist.
    assert match_bd(_PLAYLISTS, _TITLES, _plays("00008", 99999 * T), "x") == 5


def test_match_bd_prefers_the_title_named_after_the_playlist() -> None:
    titles = [
        {"index": 3, "segment_map": "27", "source_playlist": "00030.mpls"},
        {"index": 7, "segment_map": "27", "source_playlist": "00022.mpls"},
    ]
    assert match_bd(_PLAYLISTS, titles, _plays("00022"), "x") == 7


def test_match_dvd_and_plays_text() -> None:
    titles = [{"index": 0, "original_title_id": 1}, {"index": 1, "original_title_id": 2}, {"index": 2}]
    o = {"kind": "plays_title", "title": 2, "chapter": 1}
    assert match_dvd(titles, o) == 1
    assert match_dvd(titles, {**o, "title": 9}) is None
    assert plays_text(o) == "title 2, chapter 1"
    assert plays_text(_plays("00008", 1446 * T)) == "00008.mpls from 24:06"
    assert plays_text(_plays("00022")) == "00022.mpls"


def test_read_preview(tmp_path: Path) -> None:
    assert read_preview(tmp_path) is None
    (tmp_path / "preview.json").write_text("{bad")
    assert read_preview(tmp_path) is None
    (tmp_path / "preview.json").write_text("[1]")
    assert read_preview(tmp_path) is None
    (tmp_path / "preview.json").write_text('{"pages": []}')
    assert read_preview(tmp_path) == {"pages": []}


def _track(index: int, **kw: Any) -> Track:
    fields: dict[str, Any] = dict(
        id=f"trk_{index}", job_id=JOB, kind=TrackKind.VIDEO_TITLE, index=index, source_ref=str(index),
        status=TrackStatus.DONE, attempts=0,
    )  # fmt: skip
    fields.update(kw)
    return Track(**fields)


def _button(page: str, bid: int, label: str, outcome: dict[str, Any], score: float = 0.99) -> dict[str, Any]:
    return {"id": bid, "box": [0, 0, 1, 1], "visible": True, "label": label, "score": score, "outcome": outcome}


def _preview(*buttons: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    pages: dict[str, list[dict[str, Any]]] = {}
    for page, b in buttons:
        pages.setdefault(page, []).append(b)
    return {
        "schema": "menu-preview-1",
        "disc": "bd",
        "pages": [{"id": p, "at": f"m p{p}", "buttons": bs} for p, bs in pages.items()],
        "playlists": _PLAYLISTS,
    }


def _db(*tracks: Track) -> tuple[FakeSession, Job]:
    db = FakeSession()
    job = Job(id=JOB, disc_type=DiscType.BLURAY, title="Show", metadata_json={"scan_result": {"titles": _TITLES}})
    db.rows["jobs"] = [job]
    db.rows["tracks"] = list(tracks) or [
        _track(0, role=TrackRole.EPISODE, episode_number=8),
        _track(1, role=TrackRole.EPISODE, episode_number=9),
        _track(5, role=TrackRole.PLAY_ALL, excluded=True),
        _track(7, role=TrackRole.EXTRA),
        _track(8, role=TrackRole.EXTRA),
    ]
    return db, job


_D2 = _preview(
    ("1", _button("1", 10, "Play All", _plays("00008"))),
    ("2", _button("2", 9, "Spook 8: Mitsuba", _plays("00008"))),
    ("2", _button("2", 10, "Spook 7: Wrong", _plays("00008", 1446 * T))),
    ("2", _button("2", 11, "Episodes", {"kind": "menu", "at": "m p2"})),
    ("3", _button("3", 11, "FunimationCon 2020: Panel", _plays("00022"), 0.5)),
    ("3", _button("3", 12, "Promo Videos", _plays("00023"))),
    ("3", _button("3", 13, "Textless Opening Song ver. 1", _plays("00024"))),
    ("4", _button("4", 11, "FunimationCon 2020: Toilet-bound Hanako-kun Panel", _plays("00022"), 0.99)),
    ("4", _button("4", 2, "", _plays("00022"))),
)


async def test_make_proposals_on_a_blu_ray() -> None:
    db, job = _db()
    rows = await make_proposals(db, job, _D2)  # type: ignore[arg-type]
    got = {(r.track_id, r.plays): (r.status.value, r.role, r.name, r.note) for r in rows}
    assert got == {
        ("trk_5", "00008.mpls"): ("info", TrackRole.PLAY_ALL, None, "plays the episodes back to back"),
        ("trk_0", "00008.mpls"): ("info", TrackRole.EPISODE, None, "confirms episode 8"),
        ("trk_1", "00008.mpls from 24:06"): ("info", TrackRole.EPISODE, None, "identity has episode 9, the menu says 7"),
        ("trk_7", "00022.mpls"): (
            "proposed", TrackRole.EXTRA, "FunimationCon 2020: Toilet-bound Hanako-kun Panel", None
        ),
        ("trk_8", "00023.mpls"): ("proposed", TrackRole.TRAILER, "Promo Videos", None),
        (None, "00024.mpls"): ("info", TrackRole.OTHER, "Textless Opening Song ver. 1", "not ripped"),
    }  # fmt: skip
    # The better-read label wins, from its own page.
    panel = next(r for r in rows if r.track_id == "trk_7")
    assert (panel.page, panel.button, panel.score) == ("4", 11, 0.99)


async def test_a_rerun_replaces_undecided_rows_and_keeps_decisions() -> None:
    db, job = _db()
    first = await make_proposals(db, job, _D2)  # type: ignore[arg-type]
    promo = next(r for r in first if r.track_id == "trk_8")
    promo.status = MenuProposalStatus.APPLIED
    rows = await make_proposals(db, job, _D2)  # type: ignore[arg-type]
    assert promo in db.rows["menu_proposals"]
    assert not any(r.track_id == "trk_8" for r in rows)
    assert len([r for r in db.rows["menu_proposals"] if isinstance(r, MenuProposal)]) == len(first)


async def test_excluded_bonus_and_an_episode_with_no_number() -> None:
    db, job = _db(_track(0, role=TrackRole.EPISODE), _track(7, excluded=True))
    preview = _preview(
        ("1", _button("1", 1, "Spook 8", _plays("00008"))), ("1", _button("1", 2, "Panel", _plays("00022")))
    )
    rows = {r.track_id: r for r in await make_proposals(db, job, preview)}  # type: ignore[arg-type]
    assert rows["trk_0"].note == "an episode button"
    assert (rows["trk_7"].status, rows["trk_7"].note) == (MenuProposalStatus.INFO, "left out of the rip")


async def test_a_special_waits_for_its_season_0_number() -> None:
    db, job = _db(_track(7))
    rows = await make_proposals(db, job, _preview(("1", _button("1", 1, "Episode 1", _plays("00022")))))  # type: ignore[arg-type]
    assert (rows[0].role, rows[0].episode_number, rows[0].status) == (TrackRole.SPECIAL, None, "proposed")


async def test_dvd_buttons_for_chapters_of_one_title_are_info() -> None:
    db, job = _db(_track(0), _track(1))
    job.metadata_json = {"scan_result": {"titles": [{"index": 0, "original_title_id": 1},
                                                    {"index": 1, "original_title_id": 2}]}}  # fmt: skip
    preview = {
        "disc": "dvd",
        "pages": [{"id": "1", "buttons": [
            _button("1", 1, "Extra one", {"kind": "plays_title", "title": 1, "chapter": 1}),
            _button("1", 2, "Extra two", {"kind": "plays_title", "title": 1, "chapter": 3}),
            _button("1", 3, "Interview", {"kind": "plays_title", "title": 2, "chapter": 1}),
        ]}],
    }  # fmt: skip
    rows = {r.track_id: r for r in await make_proposals(db, job, preview)}  # type: ignore[arg-type]
    assert rows["trk_0"].note == "a chapter of this title"
    assert (rows["trk_1"].name, rows["trk_1"].status) == ("Interview", MenuProposalStatus.PROPOSED)


async def test_no_scan_means_nothing_is_ripped() -> None:
    db, job = _db(_track(7))
    job.metadata_json = {"scan_result": {"titles": "bad"}}
    rows = await make_proposals(db, job, _preview(("1", _button("1", 1, "Panel", _plays("00022")))))  # type: ignore[arg-type]
    assert rows[0].note == "not ripped"
    job.metadata_json = {"scan_result": "bad"}
    rows = await make_proposals(db, job, _preview(("1", _button("1", 1, "Panel", _plays("00022")))))  # type: ignore[arg-type]
    assert rows[0].note == "not ripped"
