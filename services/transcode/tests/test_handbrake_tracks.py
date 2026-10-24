"""HandBrakeCLI track args (engines.handbrake_engine) and ffmpeg VAAPI maps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arm_common import ContainerFormat
from arm_common.encoders import get_encoder
from arm_transcode.engines import handbrake_engine
from arm_transcode.engines.ffmpeg_vaapi import build_command
from arm_transcode.engines.handbrake_engine import (
    AudioEncoding,
    PresetAudio,
    audio_track_args,
    parse_preset_export,
    preset_audio,
    subtitle_track_args,
    track_args,
)
from arm_transcode.handbrake import transcode_handbrake
from arm_transcode.tracks import SourceStream, TrackPlan

# The audio part of `HandBrakeCLI --preset "H.265 MKV 1080p30" --preset-export`
# (HandBrake 1.11.1, the transcode image).
H265_MKV_EXPORT = {
    "PresetList": [
        {
            "PresetName": "arm",
            "AudioCopyMask": ["copy:aac"],
            "AudioEncoderFallback": "av_aac",
            "AudioSecondaryEncoderMode": True,
            "AudioList": [
                {
                    "AudioBitrate": 160,
                    "AudioEncoder": "av_aac",
                    "AudioMixdown": "stereo",
                    "AudioSamplerate": "auto",
                    "AudioTrackQualityEnable": False,
                }
            ],
        }
    ]
}
# "HQ 1080p30 Surround": AAC stereo plus AC-3 passthru.
HQ_SURROUND_EXPORT = {
    "PresetList": [
        {
            "AudioCopyMask": ["copy:aac", "copy:ac3"],
            "AudioEncoderFallback": "av_aac",
            "AudioSecondaryEncoderMode": True,
            "AudioList": [
                {"AudioBitrate": 160, "AudioEncoder": "av_aac", "AudioMixdown": "stereo", "AudioSamplerate": "auto"},
                {"AudioBitrate": 640, "AudioEncoder": "copy:ac3", "AudioMixdown": "none", "AudioSamplerate": "auto"},
            ],
        }
    ]
}

HANAKO_AUDIO = [
    SourceStream(position=0, codec="truehd", language="eng"),
    SourceStream(position=1, codec="ac3", language="eng"),
    SourceStream(position=2, codec="truehd", language="jpn"),
    SourceStream(position=3, codec="ac3", language="jpn"),
]


def test_parse_preset_export_reads_the_audio_list() -> None:
    audio = parse_preset_export(json.dumps(H265_MKV_EXPORT))
    assert audio == PresetAudio(
        encodings=[AudioEncoding(encoder="av_aac", mixdown="stereo", bitrate=160, samplerate="auto")],
        copy_mask=["aac"],
        fallback="av_aac",
        secondary_first_only=True,
    )


def test_parse_preset_export_without_audio_list_uses_aac_stereo() -> None:
    audio = parse_preset_export(json.dumps({"PresetList": [{"AudioList": []}]}))
    assert audio.encodings == [AudioEncoding(encoder="av_aac", mixdown="stereo", bitrate=160)]


def test_parse_preset_export_defaults_for_sparse_entries() -> None:
    payload = {"PresetList": [{"AudioList": [{"AudioEncoder": "opus", "AudioBitrate": -1}]}]}
    audio = parse_preset_export(json.dumps(payload))
    assert audio == PresetAudio(encodings=[AudioEncoding(encoder="opus")], copy_mask=[], fallback=None)


def test_hanako_audio_with_the_builtin_preset_is_aac_stereo_per_track() -> None:
    args = audio_track_args(HANAKO_AUDIO, parse_preset_export(json.dumps(H265_MKV_EXPORT)))
    assert args == [
        "--audio",
        "1,2,3,4",
        "--aencoder",
        "av_aac,av_aac,av_aac,av_aac",
        "--mixdown",
        "stereo,stereo,stereo,stereo",
        "--ab",
        "160,160,160,160",
        "--audio-copy-mask",
        "aac",
        "--audio-fallback",
        "av_aac",
    ]


def test_secondary_encoder_mode_applies_every_entry_to_the_first_track_only() -> None:
    audio = parse_preset_export(json.dumps(HQ_SURROUND_EXPORT))
    # jpn AC-3 first (passes through), then eng TrueHD (no AC-3 to copy).
    args = audio_track_args([HANAKO_AUDIO[3], HANAKO_AUDIO[0]], audio)
    assert args[:6] == ["--audio", "4,4,1", "--aencoder", "av_aac,copy:ac3,av_aac", "--mixdown", "stereo,none,stereo"]
    assert "--ab" in args and args[args.index("--ab") + 1] == "160,640,160"


def test_passthru_entry_falls_back_when_the_source_codec_differs() -> None:
    audio = parse_preset_export(json.dumps(HQ_SURROUND_EXPORT))
    args = audio_track_args([HANAKO_AUDIO[0]], audio)
    assert args[1] == "1,1"
    assert args[3] == "av_aac,av_aac"


def test_every_entry_applies_to_every_track_without_secondary_mode() -> None:
    audio = PresetAudio(
        encodings=[AudioEncoding("av_aac", "stereo", 160), AudioEncoding("copy:truehd", "none", None)],
        secondary_first_only=False,
    )
    args = audio_track_args([HANAKO_AUDIO[0], HANAKO_AUDIO[1]], audio)
    # TrueHD passes through; AC-3 has no TrueHD and no fallback, so that entry is skipped.
    assert args[1] == "1,1,2"
    assert args[3] == "av_aac,copy:truehd,av_aac"
    # A missing bitrate on any entry leaves --ab out; no mask or fallback flags.
    assert "--ab" not in args and "--audio-copy-mask" not in args and "--audio-fallback" not in args


def test_an_all_impossible_passthru_keeps_the_first_track_as_aac() -> None:
    audio = PresetAudio(encodings=[AudioEncoding("copy:flac", "none", 0)], fallback="none")
    assert audio_track_args([HANAKO_AUDIO[2]], audio) == [
        "--audio",
        "3",
        "--aencoder",
        "av_aac",
        "--mixdown",
        "stereo",
        "--ab",
        "160",
        "--audio-fallback",
        "none",
    ]


def test_passthru_names_for_pcm_and_dts() -> None:
    pcm = SourceStream(position=0, codec="pcm_s24le", language="eng")
    dts_hd = SourceStream(position=1, codec="dts", language="eng", profile="DTS-HD MA")
    dts = SourceStream(position=2, codec="dts", language="eng", profile="DTS")
    audio = PresetAudio(
        encodings=[AudioEncoding("copy:pcm", "none", 1), AudioEncoding("copy:dtshd", "none", 1)],
        secondary_first_only=False,
    )
    args = audio_track_args([pcm, dts_hd, dts], audio)
    # pcm passes copy:pcm only; DTS-HD passes copy:dtshd; plain DTS passes neither.
    assert args[1] == "1,2"
    assert args[3] == "copy:pcm,copy:dtshd"


def test_non_auto_samplerate_is_passed() -> None:
    audio = PresetAudio(encodings=[AudioEncoding("av_aac", "stereo", 160, samplerate="48")])
    args = audio_track_args([HANAKO_AUDIO[0], HANAKO_AUDIO[1]], audio)
    assert args[args.index("--arate") + 1] == "48,48"


def test_no_kept_audio_is_audio_none() -> None:
    assert audio_track_args([], PresetAudio(encodings=[])) == ["--audio", "none"]


def test_subtitle_track_args() -> None:
    subs = [SourceStream(position=0, codec="hdmv_pgs_subtitle", language="eng")]
    assert subtitle_track_args([]) == ["--subtitle", "none"]
    assert subtitle_track_args(subs + [SourceStream(position=3, codec="dvd_subtitle", language="jpn")]) == [
        "--subtitle",
        "1,4",
        "--subtitle-burned=none",
        "--subtitle-default=none",
    ]


def test_track_args_owner_example() -> None:
    """eng+jpn audio, eng subtitles as separate files: every audio track,
    and no subtitle in the MKV."""
    plan = TrackPlan(audio=HANAKO_AUDIO, embedded_subtitles=[])
    args = track_args(plan, parse_preset_export(json.dumps(H265_MKV_EXPORT)))
    assert args[:2] == ["--audio", "1,2,3,4"]
    assert args[-2:] == ["--subtitle", "none"]


def test_track_args_engine_choice_and_default_audio() -> None:
    assert track_args(TrackPlan(audio=None, embedded_subtitles=None), None) == []
    args = track_args(TrackPlan(audio=[HANAKO_AUDIO[0]], embedded_subtitles=None), None)
    assert args == ["--audio", "1", "--aencoder", "av_aac", "--mixdown", "stereo", "--ab", "160"]


async def test_preset_audio_reads_the_export(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    async def fake_run(cmd: list[str], what: str) -> str:
        seen.append(cmd)
        return json.dumps(H265_MKV_EXPORT)

    monkeypatch.setattr(handbrake_engine, "run_tool", fake_run)
    audio = await preset_audio("H.265 MKV 1080p30")
    assert audio.fallback == "av_aac"
    assert seen == [["HandBrakeCLI", "--preset", "H.265 MKV 1080p30", "--preset-export", "arm"]]


async def test_preset_audio_names_an_unknown_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run(cmd: list[str], what: str) -> str:
        raise RuntimeError("HandBrakeCLI exited rc=2: Valid presets are: ... (the whole list)")

    monkeypatch.setattr(handbrake_engine, "run_tool", fake_run)
    with pytest.raises(RuntimeError, match=r"^could not read HandBrake preset 'No Such'; check the preset name$"):
        await preset_audio("No Such")


async def test_transcode_handbrake_puts_track_args_before_extra_args(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import arm_transcode.handbrake as hb

    seen: list[tuple[str, ...]] = []

    class _Stream:
        async def read(self, _n: int) -> bytes:
            return b""

        async def readline(self) -> bytes:
            return b""

    class _Proc:
        stdout = _Stream()
        stderr = _Stream()

        async def wait(self) -> int:
            return 0

    async def fake_create(*cmd: str, **_kw: Any) -> _Proc:
        seen.append(cmd)
        return _Proc()

    monkeypatch.setattr(hb.asyncio, "create_subprocess_exec", fake_create)
    out = tmp_path / "out.mkv"
    out.write_bytes(b"x" * 5)

    async def cb(_p: int, _e: int | None, _c: str | None) -> None:
        return None

    size = await transcode_handbrake(
        input_path=tmp_path / "in.mkv",
        output_path=out,
        preset_ref="H.265 MKV 1080p30",
        extra_args="--first-audio",
        encoder_args=["--encoder", "x265"],
        progress_callback=cb,
        track_args=["--audio", "1,2"],
    )
    assert size == 5
    cmd = list(seen[0])
    assert cmd[cmd.index("--encoder") :] == ["--encoder", "x265", "--audio", "1,2", "--first-audio"]


# --- ffmpeg VAAPI maps -------------------------------------------------------


def _vaapi(container: ContainerFormat, plan: TrackPlan | None) -> list[str]:
    return build_command(
        input_path=Path("/raw/in.mkv"),
        output_path=Path("/media/out.arm-inprogress"),
        spec=get_encoder("vaapi_h265"),
        device="/dev/dri/renderD128",
        container=container,
        extra_args=None,
        plan=plan,
    )


def _after(cmd: list[str], flag: str) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == flag]


def test_vaapi_plan_maps_by_position_in_plan_order() -> None:
    subs = [
        SourceStream(position=1, codec="hdmv_pgs_subtitle", language="eng", forced=True),
        SourceStream(position=0, codec="hdmv_pgs_subtitle", language="eng"),
    ]
    plan = TrackPlan(audio=[HANAKO_AUDIO[2], HANAKO_AUDIO[0]], embedded_subtitles=subs)
    cmd = _vaapi(ContainerFormat.MKV, plan)
    assert _after(cmd, "-map") == ["0:v:0", "0:a:2", "0:a:0", "0:s:1", "0:s:0"]
    assert cmd[cmd.index("-disposition:a:0") + 1] == "default"
    assert cmd[cmd.index("-disposition:a:1") + 1] == "0"
    assert cmd[cmd.index("-disposition:s:0") + 1] == "forced"
    assert cmd[cmd.index("-disposition:s:1") + 1] == "0"
    assert _after(cmd, "-c:s") == ["copy"]
    assert "-sn" not in cmd


def test_vaapi_plan_mp4_text_subtitles_become_mov_text() -> None:
    plan = TrackPlan(audio=None, embedded_subtitles=[SourceStream(position=2, codec="subrip", language="eng")])
    cmd = _vaapi(ContainerFormat.MP4, plan)
    assert _after(cmd, "-map") == ["0:v:0", "0:a?", "0:s:2"]
    assert _after(cmd, "-c:s") == ["mov_text"]
    assert not any(a.startswith("-disposition:a") for a in cmd)


def test_vaapi_plan_without_embedded_subtitles_drops_them() -> None:
    cmd = _vaapi(ContainerFormat.MKV, TrackPlan(audio=[], embedded_subtitles=[]))
    assert _after(cmd, "-map") == ["0:v:0"]
    assert "-sn" in cmd and "-c:s" not in cmd


def test_vaapi_without_plan_keeps_the_engine_defaults() -> None:
    cmd = _vaapi(ContainerFormat.MKV, TrackPlan(audio=None, embedded_subtitles=None))
    assert _after(cmd, "-map") == ["0:v:0", "0:a?", "0:s?"]
    assert _after(cmd, "-c:s") == ["copy"]
    assert _vaapi(ContainerFormat.MKV, None) == cmd
