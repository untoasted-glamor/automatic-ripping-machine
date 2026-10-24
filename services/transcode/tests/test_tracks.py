"""Track selection and subtitle sidecars (arm_transcode.tracks)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from arm_common import AudioSelection, ContainerFormat, MediaType, SubtitleOutput, SubtitleSelection, TranscodeTool
from arm_common.schemas import TranscodePresetView
from arm_transcode import tracks
from arm_transcode.tracks import (
    Sidecar,
    SourceStream,
    SourceTracks,
    embeddable,
    extract_command,
    extract_sidecars,
    needs_plan,
    parse_ffprobe,
    plan_sidecars,
    plan_tracks,
    probe_source,
    run_tool,
    select_audio,
    select_subtitles,
    sidecar_name,
    sidecar_outputs,
    written_sidecars,
)

# The Toilet-Bound Hanako-kun D1 Blu-ray layout (ffprobe of the raw rip).
HANAKO_PROBE = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "tags": {"language": "eng"}},
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "truehd",
            "disposition": {"default": 1, "forced": 0},
            "tags": {"language": "eng", "title": "Surround 5.1"},
        },
        {"index": 2, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "audio", "codec_name": "truehd", "tags": {"language": "jpn"}},
        {"index": 4, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "jpn"}},
        {"index": 5, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
        {
            "index": 6,
            "codec_type": "subtitle",
            "codec_name": "hdmv_pgs_subtitle",
            "disposition": {"forced": 1},
            "tags": {"LANGUAGE": "eng"},
        },
        {"index": 7, "codec_type": "attachment", "codec_name": "ttf"},
    ]
}


def _source() -> SourceTracks:
    return parse_ffprobe(json.dumps(HANAKO_PROBE))


def _preset(**overrides: Any) -> TranscodePresetView:
    fields: dict[str, Any] = {
        "id": "tpr_x",
        "name": "x",
        "media_type": MediaType.TV,
        "is_builtin": False,
        "tool": TranscodeTool.HANDBRAKE,
        "preset_ref": "H.265 MKV 1080p30",
        "preset_json": None,
        "container": ContainerFormat.MKV,
        "encoder": "any_h265",
        "extra_args": None,
        "created_by_user_id": None,
        "created_at": None,
        "updated_at": None,
    }
    fields.update(overrides)
    return TranscodePresetView(**fields)


def _sub(position: int, codec: str = "hdmv_pgs_subtitle", language: str = "eng", forced: bool = False) -> SourceStream:
    return SourceStream(position=position, codec=codec, language=language, forced=forced)


# --- probe parsing -----------------------------------------------------------


def test_parse_ffprobe_counts_positions_per_kind_and_reads_tags() -> None:
    source = _source()
    assert [(s.position, s.codec, s.language) for s in source.audio] == [
        (0, "truehd", "eng"),
        (1, "ac3", "eng"),
        (2, "truehd", "jpn"),
        (3, "ac3", "jpn"),
    ]
    assert source.audio[0].title == "Surround 5.1"
    assert [(s.position, s.forced) for s in source.subtitles] == [(0, False), (1, True)]
    # Upper-case tag keys are read too.
    assert source.subtitles[1].language == "eng"


def test_parse_ffprobe_missing_language_is_und_and_terminology_codes_canonicalise() -> None:
    payload = {
        "streams": [
            {"codec_type": "audio", "codec_name": "ac3"},
            {"codec_type": "audio", "tags": {"language": "deu"}},
            {"codec_type": "subtitle", "codec_name": "dvd_subtitle", "tags": {"language": "x"}},
        ]
    }
    source = parse_ffprobe(json.dumps(payload))
    assert [s.language for s in source.audio] == ["und", "ger"]
    assert source.audio[1].codec == "unknown"
    assert source.subtitles[0].language == "und"


def test_parse_ffprobe_empty_output() -> None:
    assert parse_ffprobe("") == SourceTracks()


# --- selection ---------------------------------------------------------------


def test_select_audio_all_keeps_source_order() -> None:
    audio = _source().audio
    assert select_audio(audio, AudioSelection.ALL, []) == audio


def test_select_audio_languages_follow_list_order() -> None:
    audio = _source().audio
    kept = select_audio(audio, AudioSelection.LANGUAGES, ["jpn", "eng"])
    assert [(s.position, s.language) for s in kept] == [(2, "jpn"), (3, "jpn"), (0, "eng"), (1, "eng")]


def test_select_audio_matches_terminology_codes_and_ignores_duplicates() -> None:
    audio = [SourceStream(position=0, codec="ac3", language="ger")]
    assert select_audio(audio, AudioSelection.LANGUAGES, ["deu", "ger"]) == audio


def test_select_audio_with_no_match_keeps_the_first_track(caplog: pytest.LogCaptureFixture) -> None:
    audio = _source().audio
    with caplog.at_level(logging.WARNING, logger="arm_transcode.tracks"):
        kept = select_audio(audio, AudioSelection.LANGUAGES, ["fre"])
    assert kept == [audio[0]]
    assert "keeping the first audio track" in caplog.text


def test_select_audio_with_no_source_audio_keeps_none() -> None:
    assert select_audio([], AudioSelection.LANGUAGES, ["eng"]) == []


def test_select_subtitles_modes(caplog: pytest.LogCaptureFixture) -> None:
    subs = [_sub(0, language="eng"), _sub(1, language="jpn")]
    assert select_subtitles(subs, SubtitleSelection.NONE, ["eng"]) == []
    assert select_subtitles(subs, SubtitleSelection.ALL, []) == subs
    assert select_subtitles(subs, SubtitleSelection.LANGUAGES, ["jpn"]) == [subs[1]]
    with caplog.at_level(logging.INFO, logger="arm_transcode.tracks"):
        assert select_subtitles(subs, SubtitleSelection.LANGUAGES, ["fre"]) == []
    assert "keeping none" in caplog.text
    assert select_subtitles([], SubtitleSelection.LANGUAGES, ["fre"]) == []


def test_embeddable_per_container(caplog: pytest.LogCaptureFixture) -> None:
    subs = [_sub(0), _sub(1, codec="subrip"), _sub(2, codec="dvd_subtitle")]
    assert embeddable(subs, ContainerFormat.MKV) == subs
    with caplog.at_level(logging.WARNING, logger="arm_transcode.tracks"):
        assert embeddable(subs, ContainerFormat.MP4) == [subs[1]]
    assert "mp4 cannot embed subtitle tracks 0:hdmv_pgs_subtitle,2:dvd_subtitle" in caplog.text
    assert embeddable(subs, ContainerFormat.WEBM) == []
    assert embeddable([], ContainerFormat.MP4) == []


def test_needs_plan() -> None:
    assert not needs_plan(None)
    assert not needs_plan(_preset())
    assert needs_plan(_preset(audio_selection=AudioSelection.ALL))
    assert needs_plan(_preset(subtitle_selection=SubtitleSelection.NONE))


# --- sidecar naming ----------------------------------------------------------


def test_sidecar_name_language_first_then_counter_then_forced() -> None:
    taken: set[str] = set()
    names = [
        sidecar_name("Show - S01E01", _sub(0), "sup", taken),
        sidecar_name("Show - S01E01", _sub(1), "sup", taken),
        sidecar_name("Show - S01E01", _sub(2, forced=True), "sup", taken),
        sidecar_name("Show - S01E01", _sub(3, forced=True), "sup", taken),
        sidecar_name("Show - S01E01", _sub(4, language="und"), "sup", taken),
        sidecar_name("Show - S01E01", _sub(5), "sup", taken),
    ]
    assert names == [
        "Show - S01E01.eng.sup",
        "Show - S01E01.eng.2.sup",
        "Show - S01E01.eng.forced.sup",
        "Show - S01E01.eng.2.forced.sup",
        "Show - S01E01.und.sup",
        "Show - S01E01.eng.3.sup",
    ]


def test_plan_sidecars_formats_and_skips_unknown_codecs(caplog: pytest.LogCaptureFixture) -> None:
    final = Path("/media/Movie (2020)/Movie (2020).mkv")
    subs = [
        _sub(0),
        _sub(1, codec="dvd_subtitle", language="ger"),
        _sub(2, codec="mov_text"),
        _sub(3, codec="eia_608"),
        _sub(4, codec="ass", language="jpn"),
    ]
    with caplog.at_level(logging.WARNING, logger="arm_transcode.tracks"):
        sidecars = plan_sidecars(final, subs)
    assert [(s.path.name, s.muxer, s.codec) for s in sidecars] == [
        ("Movie (2020).eng.sup", "sup", "copy"),
        ("Movie (2020).ger.mks", "matroska", "copy"),
        ("Movie (2020).eng.srt", "srt", "srt"),
        ("Movie (2020).jpn.ass", "ass", "copy"),
    ]
    assert all(s.path.parent == final.parent for s in sidecars)
    assert sidecars[0].tmp_path.name == "Movie (2020).eng.sup.arm-inprogress"
    assert "eia_608" in caplog.text


# --- plans -------------------------------------------------------------------

FINAL = Path("/media/Toilet-Bound Hanako-kun (2020)/Season 01/Hanako - S01E01.mkv")


def test_plan_tracks_preset_modes_leave_everything_to_the_engine() -> None:
    plan = plan_tracks(_preset(), _source(), ContainerFormat.MKV, FINAL)
    assert plan.audio is None
    assert plan.embedded_subtitles is None
    assert plan.sidecars == []


def test_plan_tracks_owner_example_eng_jpn_audio_eng_subs_separate() -> None:
    preset = _preset(
        audio_selection=AudioSelection.LANGUAGES,
        audio_languages=["eng", "jpn"],
        subtitle_selection=SubtitleSelection.LANGUAGES,
        subtitle_languages=["eng"],
        subtitle_output=SubtitleOutput.SEPARATE,
    )
    plan = plan_tracks(preset, _source(), ContainerFormat.MKV, FINAL)
    assert plan.audio is not None
    assert [s.position for s in plan.audio] == [0, 1, 2, 3]
    assert plan.embedded_subtitles == []
    assert [s.path.name for s in plan.sidecars] == ["Hanako - S01E01.eng.sup", "Hanako - S01E01.eng.forced.sup"]


def test_plan_tracks_both_embeds_and_writes_files() -> None:
    preset = _preset(
        audio_selection=AudioSelection.PRESET,
        subtitle_selection=SubtitleSelection.ALL,
        subtitle_output=SubtitleOutput.BOTH,
    )
    plan = plan_tracks(preset, _source(), ContainerFormat.MKV, FINAL)
    assert plan.audio is None
    assert plan.embedded_subtitles is not None and len(plan.embedded_subtitles) == 2
    assert len(plan.sidecars) == 2


def test_plan_tracks_embedded_only_writes_no_files() -> None:
    preset = _preset(subtitle_selection=SubtitleSelection.ALL, subtitle_output=SubtitleOutput.EMBEDDED)
    plan = plan_tracks(preset, _source(), ContainerFormat.MKV, FINAL)
    assert plan.embedded_subtitles is not None and len(plan.embedded_subtitles) == 2
    assert plan.sidecars == []


# --- subprocesses ------------------------------------------------------------


class _Proc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, hang: bool = False) -> None:
        self._stdout, self._stderr, self._rc, self._hang = stdout, stderr, returncode, hang
        self.returncode: int | None = None
        self.killed = False
        self._stop = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await self._stop.wait()
        self.returncode = self._rc
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self._stop.set()

    async def wait(self) -> int:
        return self._rc


def _stub(monkeypatch: pytest.MonkeyPatch, proc: _Proc, seen: list[list[str]] | None = None) -> None:
    async def fake_create(*cmd: str, **_kw: Any) -> _Proc:
        if seen is not None:
            seen.append(list(cmd))
        return proc

    monkeypatch.setattr(tracks.asyncio, "create_subprocess_exec", fake_create)


async def test_run_tool_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, _Proc(stdout=b"hello"))
    assert await run_tool(["x"], "x") == "hello"


async def test_run_tool_raises_with_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, _Proc(stderr=b"line one\nboom\n", returncode=3))
    with pytest.raises(RuntimeError, match=r"ffprobe exited rc=3: line one \| boom"):
        await run_tool(["ffprobe"], "ffprobe")


async def test_run_tool_cancel_kills_the_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _Proc(hang=True)
    _stub(monkeypatch, proc)
    task = asyncio.create_task(run_tool(["ffmpeg"], "ffmpeg"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


async def test_probe_source_parses_ffprobe_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    _stub(monkeypatch, _Proc(stdout=json.dumps(HANAKO_PROBE).encode()), seen)
    source = await probe_source(Path("/raw/in.mkv"))
    assert len(source.audio) == 4
    assert seen == [["ffprobe", "-v", "error", "-show_streams", "-of", "json", "/raw/in.mkv"]]


def test_extract_command_one_output_per_sidecar() -> None:
    sidecars = plan_sidecars(Path("/media/M/M.mkv"), [_sub(0), _sub(1, codec="dvd_subtitle", forced=True)])
    assert extract_command(Path("/raw/in.mkv"), sidecars) == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        "/raw/in.mkv",
        "-map",
        "0:s:0",
        "-c:s",
        "copy",
        "-f",
        "sup",
        "/media/M/M.eng.sup.arm-inprogress",
        "-map",
        "0:s:1",
        "-c:s",
        "copy",
        "-f",
        "matroska",
        "/media/M/M.eng.forced.mks.arm-inprogress",
    ]


async def test_extract_sidecars_runs_ffmpeg_once(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    _stub(monkeypatch, _Proc(), seen)
    await extract_sidecars(Path("/raw/in.mkv"), plan_sidecars(Path("/m/M.mkv"), [_sub(0), _sub(1)]))
    assert len(seen) == 1 and seen[0][0] == "ffmpeg"


async def test_extract_sidecars_drops_files_without_subtitle_packets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sidecars = plan_sidecars(tmp_path / "M.mkv", [_sub(0), _sub(1, codec="dvd_subtitle"), _sub(2, language="jpn")])
    probes: list[list[str]] = []

    async def fake_run(cmd: list[str], what: str) -> str:
        if cmd[0] == "ffmpeg":
            sidecars[0].tmp_path.write_bytes(b"pgs")  # has events
            sidecars[1].tmp_path.write_bytes(b"header-only")  # unreadable, as ffmpeg writes it
            sidecars[2].tmp_path.write_bytes(b"")  # empty: never probed
            return ""
        probes.append(cmd)
        if cmd[cmd.index("-f") + 1] == "matroska":
            raise RuntimeError("ffprobe exited rc=1: End of file")
        return "13\n"

    monkeypatch.setattr(tracks, "run_tool", fake_run)
    await extract_sidecars(Path("/raw/in.mkv"), sidecars)
    assert sidecars[0].tmp_path.exists()
    assert not sidecars[1].tmp_path.exists()
    assert not sidecars[2].tmp_path.exists()
    assert len(probes) == 2
    assert probes[0][-1] == str(sidecars[0].tmp_path)
    assert probes[0][:5] == ["ffprobe", "-v", "error", "-f", "sup"]


@pytest.mark.parametrize(("output", "expected"), [("13\n", 13), ("N/A\n", 0), ("", 0)])
async def test_subtitle_packets_parses_the_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, output: str, expected: int
) -> None:
    from arm_transcode.tracks import subtitle_packets

    sidecar = plan_sidecars(tmp_path / "M.mkv", [_sub(0)])[0]
    sidecar.tmp_path.write_bytes(b"pgs")

    async def fake_run(cmd: list[str], what: str) -> str:
        return output

    monkeypatch.setattr(tracks, "run_tool", fake_run)
    assert await subtitle_packets(sidecar) == expected


async def test_extract_sidecars_without_sidecars_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    _stub(monkeypatch, _Proc(), seen)
    await extract_sidecars(Path("/raw/in.mkv"), [])
    assert seen == []


# --- atomic sidecar output ---------------------------------------------------


def _sidecars(tmp_path: Path) -> list[Sidecar]:
    return plan_sidecars(tmp_path / "Movie.mkv", [_sub(0), _sub(1, forced=True), _sub(2, language="jpn")])


def test_sidecar_outputs_renames_on_success_and_skips_empty_tracks(tmp_path: Path) -> None:
    sidecars = _sidecars(tmp_path)
    with sidecar_outputs(sidecars):
        sidecars[0].tmp_path.write_bytes(b"pgs")
        sidecars[1].tmp_path.write_bytes(b"")  # a track with no events
        # sidecars[2] wrote nothing at all
    assert sidecars[0].path.read_bytes() == b"pgs"
    assert not sidecars[0].tmp_path.exists()
    assert not sidecars[1].path.exists() and not sidecars[1].tmp_path.exists()
    assert not sidecars[2].path.exists()
    assert written_sidecars(sidecars) == [sidecars[0].path]


def test_sidecar_outputs_removes_partials_on_failure(tmp_path: Path) -> None:
    sidecars = _sidecars(tmp_path)
    with pytest.raises(RuntimeError, match="encode failed"):
        with sidecar_outputs(sidecars):
            for sidecar in sidecars[:2]:
                sidecar.tmp_path.write_bytes(b"partial")
            raise RuntimeError("encode failed")
    assert list(tmp_path.iterdir()) == []


async def test_sidecar_outputs_removes_partials_on_cancel(tmp_path: Path) -> None:
    sidecars = _sidecars(tmp_path)
    started = asyncio.Event()

    async def body() -> None:
        with sidecar_outputs(sidecars):
            sidecars[0].tmp_path.write_bytes(b"partial")
            started.set()
            await asyncio.sleep(60)

    task = asyncio.create_task(body())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []
