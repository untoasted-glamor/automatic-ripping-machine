"""Unit tests for arm_transcode.main helpers."""

from __future__ import annotations

import asyncio
import json

import pytest

from arm_common import AudioSelection, ContainerFormat, MediaType, SubtitleOutput, SubtitleSelection, TranscodeTool
from arm_common.schemas import TranscodePresetView
from arm_transcode.heartbeat import ProgressState
from arm_transcode.main import (
    _MAX_ERROR_CHARS,
    CancelRequested,
    ClaimRevoked,
    _clip_error,
    _encode_until_stopped,
)


def test_clip_error_passes_short_messages_through() -> None:
    msg = "HandBrakeCLI exited rc=3\nstderr tail:\nDriver does not support nvenc"
    assert _clip_error(msg) == msg


def test_clip_error_keeps_the_tail_not_the_head() -> None:
    # The decisive encoder error is the LAST line; the historical str(exc)[:500]
    # kept the head and dropped exactly this line. Verify we keep the tail.
    head = "x" * (_MAX_ERROR_CHARS * 2)
    msg = head + "\nDriver does not support the required nvenc API version"
    clipped = _clip_error(msg)
    assert len(clipped) <= _MAX_ERROR_CHARS + len("…(truncated)…\n")
    assert clipped.startswith("…(truncated)…\n")
    assert "Driver does not support the required nvenc API version" in clipped


def test_clip_error_at_exact_boundary_is_unchanged() -> None:
    msg = "y" * _MAX_ERROR_CHARS
    assert _clip_error(msg) == msg


def test_probe_device_mode_prints_json(monkeypatch, capsys) -> None:
    import json

    import arm_transcode.main as m

    monkeypatch.setattr(m.sys, "argv", ["arm_transcode", "--probe-device"])
    monkeypatch.setenv("ARM_GPU_VENDOR", "qsv")
    monkeypatch.setenv("ARM_GPU_DEVICE", "/dev/dri/renderD129")
    monkeypatch.setattr(m, "probe_device", lambda v, d: {"verified": ["h265"], "errors": {}})
    rc = m.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"verified": ["h265"], "errors": {}}
    assert "\n" not in out  # exactly one JSON line


def test_probe_device_mode_requires_env(monkeypatch, capsys) -> None:
    import arm_transcode.main as m

    monkeypatch.setattr(m.sys, "argv", ["arm_transcode", "--probe-device"])
    monkeypatch.delenv("ARM_GPU_VENDOR", raising=False)
    monkeypatch.delenv("ARM_GPU_DEVICE", raising=False)
    rc = m.main()
    assert rc == 2
    assert capsys.readouterr().err.strip()


def test_probe_device_mode_reports_setup_error(monkeypatch, capsys) -> None:
    import arm_transcode.main as m

    monkeypatch.setattr(m.sys, "argv", ["arm_transcode", "--probe-device"])
    monkeypatch.setenv("ARM_GPU_VENDOR", "qsv")
    monkeypatch.setenv("ARM_GPU_DEVICE", "/dev/dri/renderD129")

    def boom(vendor, device):
        raise m.ProbeSetupError("could not generate the test clip: clip failed")

    monkeypatch.setattr(m, "probe_device", boom)
    rc = m.main()
    assert rc == 3
    assert "could not generate the test clip" in capsys.readouterr().err


def test_probe_device_mode_rejects_unknown_vendor(monkeypatch, capsys) -> None:
    import arm_transcode.main as m

    monkeypatch.setattr(m.sys, "argv", ["arm_transcode", "--probe-device"])
    monkeypatch.setenv("ARM_GPU_VENDOR", "not-a-real-vendor")
    monkeypatch.setenv("ARM_GPU_DEVICE", "/dev/dri/renderD129")

    def must_not_call(vendor, device):
        raise AssertionError("probe_device should not run for an unknown vendor")

    monkeypatch.setattr(m, "probe_device", must_not_call)
    rc = m.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "not-a-real-vendor" in err
    assert "qsv" in err  # names a valid vendor


def test_probe_encoders_mode_is_deprecated_and_empty(monkeypatch, capsys) -> None:
    import json

    import arm_transcode.main as m

    monkeypatch.setattr(m.sys, "argv", ["arm_transcode", "--probe-encoders"])
    rc = m.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip()) == {}
    assert "deprecated" in captured.err.lower()
    assert "--probe-device" in captured.err


def _handbrake_preset(*, preset_ref: str | None, extra_args: str | None = None) -> TranscodePresetView:
    return TranscodePresetView(
        id="p1",
        name="AMD HEVC",
        media_type=MediaType.MOVIE,
        is_builtin=True,
        tool=TranscodeTool.HANDBRAKE,
        preset_ref=preset_ref,
        preset_json=None,
        container=ContainerFormat.MKV,
        encoder="vaapi_h265",
        extra_args=extra_args,
        created_by_user_id=None,
        created_at=None,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_run_encoder_ffmpeg_vaapi_routes_without_preset_ref(monkeypatch, tmp_path) -> None:
    """A HandBrake-tool preset with a vaapi encoder and no preset_ref routes
    to transcode_ffmpeg_vaapi instead of raising the "requires a preset_ref"
    guard meant for HandBrake/ABCDE."""
    import arm_transcode.main as m

    monkeypatch.setenv("ARM_TRANSCODE_ENCODER", "vaapi_h265")
    monkeypatch.setenv("ARM_GPU_DEVICE", "/dev/dri/renderD128")

    called: dict = {}

    async def fake_transcode_ffmpeg_vaapi(**kwargs):
        called.update(kwargs)
        kwargs["output_path"].write_bytes(b"z" * 99)
        return 99

    monkeypatch.setattr(m, "transcode_ffmpeg_vaapi", fake_transcode_ffmpeg_vaapi)

    preset = _handbrake_preset(preset_ref=None)
    state = ProgressState()
    result = await m._run_encoder(
        tool=TranscodeTool.HANDBRAKE,
        preset=preset,
        raw_input=tmp_path / "in.mkv",
        final_output=tmp_path / "out.mkv",
        duration_seconds=100,
        state=state,
    )
    assert result.size == 99
    assert result.sidecars == []
    assert called["plan"] is None
    assert state.pct == 100
    assert called["device"] == "/dev/dri/renderD128"
    assert called["container"] == ContainerFormat.MKV
    assert called["spec"].id == "vaapi_h265"


@pytest.mark.asyncio
async def test_run_encoder_ffmpeg_vaapi_requires_gpu_device(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    monkeypatch.setenv("ARM_TRANSCODE_ENCODER", "vaapi_h265")
    monkeypatch.delenv("ARM_GPU_DEVICE", raising=False)

    async def must_not_call(**_kwargs):
        raise AssertionError("transcode_ffmpeg_vaapi should not be invoked without ARM_GPU_DEVICE")

    monkeypatch.setattr(m, "transcode_ffmpeg_vaapi", must_not_call)

    preset = _handbrake_preset(preset_ref=None)
    state = ProgressState()
    with pytest.raises(RuntimeError, match="ffmpeg_vaapi encoder requires ARM_GPU_DEVICE"):
        await m._run_encoder(
            tool=TranscodeTool.HANDBRAKE,
            preset=preset,
            raw_input=tmp_path / "in.mkv",
            final_output=tmp_path / "out.mkv",
            duration_seconds=100,
            state=state,
        )


async def _slow_encode(started: asyncio.Event, outcome: list[str]) -> int:
    started.set()
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        outcome.append("cancelled")
        raise
    return 1


async def test_encode_until_stopped_returns_the_encoders_size() -> None:
    async def encode() -> int:
        return 1234

    size = await _encode_until_stopped(encode(), cancel_event=asyncio.Event(), revoked_event=asyncio.Event())
    assert size == 1234


async def test_encode_until_stopped_propagates_encoder_errors() -> None:
    async def encode() -> int:
        raise RuntimeError("HandBrakeCLI exited rc=3")

    with pytest.raises(RuntimeError, match="rc=3"):
        await _encode_until_stopped(encode(), cancel_event=asyncio.Event(), revoked_event=asyncio.Event())


@pytest.mark.parametrize(("which", "expected"), [("cancel", CancelRequested), ("revoke", ClaimRevoked)])
async def test_encode_until_stopped_cancels_the_encoder(which: str, expected: type[Exception]) -> None:
    cancel_event, revoked_event = asyncio.Event(), asyncio.Event()
    started, outcome = asyncio.Event(), []

    async def stop_after_start() -> None:
        await started.wait()
        (cancel_event if which == "cancel" else revoked_event).set()

    stopper = asyncio.create_task(stop_after_start())
    with pytest.raises(expected):
        await _encode_until_stopped(
            _slow_encode(started, outcome), cancel_event=cancel_event, revoked_event=revoked_event
        )
    await stopper
    assert outcome == ["cancelled"]


# --- track selection + sidecars ------------------------------------------------

_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "truehd", "tags": {"language": "eng"}},
        {"codec_type": "audio", "codec_name": "ac3", "tags": {"language": "jpn"}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
        {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "jpn"}},
    ]
}


def _selecting_preset(encoder: str = "any_h265") -> TranscodePresetView:
    return _handbrake_preset(preset_ref="H.265 MKV 1080p30").model_copy(
        update={
            "encoder": encoder,
            "audio_selection": AudioSelection.LANGUAGES,
            "audio_languages": ["jpn", "eng"],
            "subtitle_selection": SubtitleSelection.LANGUAGES,
            "subtitle_languages": ["eng"],
            "subtitle_output": SubtitleOutput.SEPARATE,
        }
    )


def _stub_tools(monkeypatch, m, *, fail_encode: bool = False, engine: str = "handbrake") -> dict:
    import arm_transcode.tracks as tracks

    calls: dict = {"extract": [], "encode": None}

    async def fake_probe(path):
        return tracks.parse_ffprobe(json.dumps(_PROBE))

    async def fake_preset_audio(ref):
        calls["preset_ref"] = ref
        from arm_transcode.engines.handbrake_engine import AudioEncoding, PresetAudio

        return PresetAudio(encodings=[AudioEncoding("av_aac", "stereo", 160)])

    async def fake_extract(raw, sidecars):
        calls["extract"] = [s.path.name for s in sidecars]
        for sidecar in sidecars:
            sidecar.tmp_path.write_bytes(b"pgs")

    async def fake_encode(**kwargs):
        calls["encode"] = kwargs
        if fail_encode:
            raise RuntimeError("HandBrakeCLI exited rc=3")
        kwargs["output_path"].write_bytes(b"v" * 10)
        return 10

    monkeypatch.setattr(m, "probe_source", fake_probe)
    monkeypatch.setattr(m, "preset_audio", fake_preset_audio)
    monkeypatch.setattr(m, "extract_sidecars", fake_extract)
    monkeypatch.setattr(m, "transcode_handbrake" if engine == "handbrake" else "transcode_ffmpeg_vaapi", fake_encode)
    return calls


async def test_run_encoder_handbrake_writes_sidecars_after_the_video(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    monkeypatch.delenv("ARM_TRANSCODE_ENCODER", raising=False)
    calls = _stub_tools(monkeypatch, m)
    final = tmp_path / "Show" / "Show - S01E01.mkv"
    result = await m._run_encoder(
        tool=TranscodeTool.HANDBRAKE,
        preset=_selecting_preset(),
        raw_input=tmp_path / "in.mkv",
        final_output=final,
        duration_seconds=60,
        state=ProgressState(),
    )
    assert result.size == 10
    assert result.sidecars == [final.with_name("Show - S01E01.eng.sup")]
    assert final.exists() and result.sidecars[0].read_bytes() == b"pgs"
    assert sorted(p.name for p in final.parent.iterdir()) == ["Show - S01E01.eng.sup", "Show - S01E01.mkv"]
    assert calls["preset_ref"] == "H.265 MKV 1080p30"
    # jpn first, then eng; no subtitle in the MKV.
    assert calls["encode"]["track_args"][:2] == ["--audio", "2,1"]
    assert calls["encode"]["track_args"][-2:] == ["--subtitle", "none"]


async def test_run_encoder_failure_removes_partial_sidecars(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    monkeypatch.delenv("ARM_TRANSCODE_ENCODER", raising=False)
    _stub_tools(monkeypatch, m, fail_encode=True)
    final = tmp_path / "Show - S01E01.mkv"
    with pytest.raises(RuntimeError, match="rc=3"):
        await m._run_encoder(
            tool=TranscodeTool.HANDBRAKE,
            preset=_selecting_preset(),
            raw_input=tmp_path / "in.mkv",
            final_output=final,
            duration_seconds=60,
            state=ProgressState(),
        )
    assert list(tmp_path.iterdir()) == []


async def test_run_encoder_handbrake_subtitles_only_skips_the_preset_export(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    monkeypatch.delenv("ARM_TRANSCODE_ENCODER", raising=False)
    calls = _stub_tools(monkeypatch, m)
    preset = _selecting_preset().model_copy(update={"audio_selection": AudioSelection.PRESET})
    await m._run_encoder(
        tool=TranscodeTool.HANDBRAKE,
        preset=preset,
        raw_input=tmp_path / "in.mkv",
        final_output=tmp_path / "out.mkv",
        duration_seconds=60,
        state=ProgressState(),
    )
    assert "preset_ref" not in calls
    assert calls["encode"]["track_args"] == ["--subtitle", "none"]


async def test_run_encoder_handbrake_without_selection_passes_no_track_args(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    monkeypatch.delenv("ARM_TRANSCODE_ENCODER", raising=False)
    calls = _stub_tools(monkeypatch, m)

    async def must_not_probe(path):
        raise AssertionError("a preset without track selection is not probed")

    monkeypatch.setattr(m, "probe_source", must_not_probe)
    result = await m._run_encoder(
        tool=TranscodeTool.HANDBRAKE,
        preset=_handbrake_preset(preset_ref="H.265 MKV 1080p30"),
        raw_input=tmp_path / "in.mkv",
        final_output=tmp_path / "out.mkv",
        duration_seconds=60,
        state=ProgressState(),
    )
    assert result.sidecars == []
    assert calls["encode"]["track_args"] == []
    assert calls["extract"] == []


async def test_run_encoder_ffmpeg_vaapi_gets_the_plan_and_sidecars(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    monkeypatch.setenv("ARM_TRANSCODE_ENCODER", "vaapi_h265")
    monkeypatch.setenv("ARM_GPU_DEVICE", "/dev/dri/renderD128")
    calls = _stub_tools(monkeypatch, m, engine="vaapi")
    final = tmp_path / "Movie.mkv"
    result = await m._run_encoder(
        tool=TranscodeTool.HANDBRAKE,
        preset=_selecting_preset(encoder="vaapi_h265"),
        raw_input=tmp_path / "in.mkv",
        final_output=final,
        duration_seconds=60,
        state=ProgressState(),
    )
    plan = calls["encode"]["plan"]
    assert [s.position for s in plan.audio] == [1, 0]
    assert result.sidecars == [tmp_path / "Movie.eng.sup"]


async def test_run_encoder_passthrough_and_abcde_report_no_sidecars(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    src = tmp_path / "in.mkv"
    src.write_bytes(b"abc")
    result = await m._run_encoder(
        tool=TranscodeTool.NONE,
        preset=None,
        raw_input=src,
        final_output=tmp_path / "out.mkv",
        duration_seconds=None,
        state=ProgressState(),
    )
    assert result.size == 3 and result.sidecars == []


async def test_run_encoder_abcde_reports_no_sidecars(monkeypatch, tmp_path) -> None:
    import arm_transcode.main as m

    async def fake_audio(**kwargs):
        kwargs["output_path"].write_bytes(b"f" * 4)
        return 4

    monkeypatch.setattr(m, "transcode_audio", fake_audio)
    preset = _handbrake_preset(preset_ref="flac").model_copy(
        update={"tool": TranscodeTool.ABCDE, "container": ContainerFormat.FLAC, "encoder": "preset"}
    )
    result = await m._run_encoder(
        tool=TranscodeTool.ABCDE,
        preset=preset,
        raw_input=tmp_path / "track_01.wav",
        final_output=tmp_path / "01.flac",
        duration_seconds=10,
        state=ProgressState(),
    )
    assert result == m.EncodeResult(size=4)
