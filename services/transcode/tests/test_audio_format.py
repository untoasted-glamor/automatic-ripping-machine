"""Audio format: per-language track choice (`tracks.plan_audio`) and the
HandBrake / ffmpeg VAAPI args it produces."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arm_common import AudioFormat, ContainerFormat
from arm_common.encoders import get_encoder
from arm_transcode.engines.ffmpeg_vaapi import build_command
from arm_transcode.engines.handbrake_engine import audio_out_args, track_args
from arm_transcode.tracks import AudioOut, SourceStream, TrackPlan, parse_ffprobe, plan_audio


def _a(position: int, codec: str, language: str, channels: int, **extra: Any) -> SourceStream:
    return SourceStream(position=position, codec=codec, language=language, channels=channels, **extra)


HANAKO = [
    _a(0, "truehd", "eng", 6, title="Surround 5.1"),
    _a(1, "ac3", "eng", 6, title="Surround 5.1"),
    _a(2, "truehd", "jpn", 2, title="Stereo"),
    _a(3, "ac3", "jpn", 2, title="Stereo"),
]


def _summary(outs: list[AudioOut] | None) -> list[tuple[int, str]] | None:
    if outs is None:
        return None
    return [(o.stream.position, "copy" if o.copy else f"aac{o.channels}") for o in outs]


def test_ac3_present_keeps_one_ac3_copy_per_language() -> None:
    outs = plan_audio(HANAKO, AudioFormat.AC3_ELSE_AAC51)
    assert _summary(outs) == [(1, "copy"), (3, "copy")]
    assert outs is not None and outs[0].title is None


def test_ac3_with_most_channels_wins() -> None:
    streams = [_a(0, "ac3", "eng", 2), _a(1, "ac3", "eng", 6)]
    assert _summary(plan_audio(streams, AudioFormat.AC3_ELSE_AAC51)) == [(1, "copy")]


def test_no_ac3_converts_the_best_track_to_aac_up_to_51() -> None:
    streams = [
        _a(0, "dts", "eng", 6, profile="DTS"),
        _a(1, "truehd", "eng", 8),  # 7.1 -> 5.1
        _a(2, "dts", "eng", 8, profile="DTS-HD MA"),
        _a(3, "pcm_s24le", "jpn", 2),
        _a(4, "eac3", "jpn", 2),
        _a(5, "mp2", "fre", 1),
    ]
    outs = plan_audio(streams, AudioFormat.AC3_ELSE_AAC51)
    assert _summary(outs) == [(1, "aac6"), (3, "aac2"), (5, "aac1")]
    assert outs is not None
    assert [o.title for o in outs] == ["English AAC 5.1", "Japanese AAC Stereo", "French AAC Mono"]


def test_truehd_only_hanako_falls_back_to_aac() -> None:
    truehd_only = [HANAKO[0], HANAKO[2]]
    assert _summary(plan_audio(truehd_only, AudioFormat.AC3_ELSE_AAC51)) == [(0, "aac6"), (2, "aac2")]


def test_stereo_only_source_stays_stereo() -> None:
    assert _summary(plan_audio([_a(0, "aac", "eng", 2)], AudioFormat.AC3_ELSE_AAC51)) == [(0, "aac2")]
    assert _summary(plan_audio([_a(0, "flac", "eng", 4)], AudioFormat.AAC_51)) == [(0, "aac6")]


def test_commentary_follows_the_main_tracks_and_unmarked_ties_are_kept() -> None:
    streams = [
        _a(0, "ac3", "eng", 2, title="Director's Commentary", extra=True),
        _a(1, "ac3", "eng", 6),
        _a(2, "ac3", "jpn", 6),
        _a(3, "ac3", "jpn", 6),  # nothing tells it apart from track 2
        _a(4, "ac3", "eng", 2, extra=True),
    ]
    assert _summary(plan_audio(streams, AudioFormat.AC3_ELSE_AAC51)) == [
        (1, "copy"),
        (2, "copy"),
        (3, "copy"),
        (0, "copy"),
        (4, "copy"),
    ]


def test_a_language_with_only_commentary_keeps_it_as_its_track() -> None:
    streams = [_a(0, "ac3", "eng", 6), _a(1, "aac", "ger", 2, extra=True)]
    assert _summary(plan_audio(streams, AudioFormat.AC3_ELSE_AAC51)) == [(0, "copy"), (1, "aac2")]


def test_other_formats() -> None:
    assert plan_audio(HANAKO, AudioFormat.PRESET) is None
    assert _summary(plan_audio(HANAKO, AudioFormat.COPY)) == [(0, "copy"), (1, "copy"), (2, "copy"), (3, "copy")]
    assert _summary(plan_audio(HANAKO, AudioFormat.AAC_STEREO)) == [(0, "aac2"), (1, "aac2"), (2, "aac2"), (3, "aac2")]
    assert _summary(plan_audio([_a(0, "mp2", "und", 1)], AudioFormat.AAC_STEREO)) == [(0, "aac2")]
    outs = plan_audio(HANAKO, AudioFormat.AAC_51)
    assert _summary(outs) == [(0, "aac6"), (1, "aac6"), (2, "aac2"), (3, "aac2")]
    unknown = plan_audio([_a(0, "opus", "xyz", 3)], AudioFormat.AAC_51)
    assert unknown is not None and unknown[0].title == "xyz AAC 5.1"
    no_channels = plan_audio([SourceStream(position=0, codec="aac", language="und")], AudioFormat.AAC_51)
    assert no_channels is not None and no_channels[0].title == "Unknown language AAC Stereo"


def test_ffprobe_reads_channels_and_commentary() -> None:
    payload = {
        "streams": [
            {"codec_type": "audio", "codec_name": "ac3", "channels": 6, "tags": {"language": "eng"}},
            {"codec_type": "audio", "codec_name": "ac3", "channels": 2, "disposition": {"comment": 1}},
            {"codec_type": "audio", "codec_name": "aac", "disposition": {"visual_impaired": 1}},
            {"codec_type": "audio", "codec_name": "ac3", "tags": {"title": "Audio Description"}},
            {"codec_type": "audio", "codec_name": "ac3", "channels": "x", "tags": {"title": "Surround 5.1"}},
        ]
    }
    audio = parse_ffprobe(json.dumps(payload)).audio
    assert [(s.channels, s.extra) for s in audio] == [(6, False), (2, True), (None, True), (None, True), (None, False)]


# --- HandBrake ---------------------------------------------------------------------


def test_handbrake_args_for_the_hanako_default() -> None:
    outs = plan_audio(HANAKO, AudioFormat.AC3_ELSE_AAC51)
    assert outs is not None
    assert audio_out_args(outs) == [
        "--audio",
        "2,4",
        "--aencoder",
        "copy,copy",
        "--mixdown",
        "none,none",
        "--aname",
        "Surround 5.1,Stereo",
        "--audio-copy-mask",
        "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,opus,vorbis,flac,alac,pcm",
        "--audio-fallback",
        "av_aac",
    ]


def test_handbrake_args_for_converted_tracks_and_commas_in_names() -> None:
    streams = [_a(0, "truehd", "eng", 6), _a(1, "ac3", "eng", 2, title="Stereo, remastered")]
    outs = [
        *(plan_audio([streams[0]], AudioFormat.AC3_ELSE_AAC51) or []),
        AudioOut(stream=streams[1]),
        AudioOut(stream=_a(2, "ac3", "jpn", 1)),
        AudioOut(stream=_a(3, "dts", "jpn", 6), channels=1, title="Japanese AAC Mono"),
    ]
    args = audio_out_args(outs)
    assert args[args.index("--aencoder") + 1] == "av_aac,copy,copy,av_aac"
    assert args[args.index("--mixdown") + 1] == "5point1,none,none,mono"
    assert args[args.index("--aname") + 1] == "English AAC 5.1,Stereo; remastered,,Japanese AAC Mono"
    only_aac = audio_out_args(outs[:1])
    assert "--audio-copy-mask" not in only_aac
    assert only_aac[-2:] == ["--audio-fallback", "av_aac"]
    assert audio_out_args([]) == ["--audio", "none"]


def test_track_args_prefers_the_explicit_audio_plan() -> None:
    outs = plan_audio(HANAKO, AudioFormat.AC3_ELSE_AAC51)
    plan = TrackPlan(audio=[o.stream for o in outs or []], embedded_subtitles=None, audio_out=outs)
    assert track_args(plan, None)[:2] == ["--audio", "2,4"]


# --- ffmpeg VAAPI ------------------------------------------------------------------


def _vaapi(plan: TrackPlan) -> list[str]:
    return build_command(
        input_path=Path("/raw/in.mkv"),
        output_path=Path("/media/out.mkv.arm-inprogress"),
        spec=get_encoder("vaapi_h265"),
        device="/dev/dri/renderD128",
        container=ContainerFormat.MKV,
        extra_args=None,
        plan=plan,
    )


def test_vaapi_converts_and_titles_planned_tracks_and_copies_the_rest() -> None:
    outs = [
        AudioOut(stream=HANAKO[1]),
        AudioOut(stream=HANAKO[0], channels=6, title="English AAC 5.1"),
        AudioOut(stream=HANAKO[2], channels=2, title="Japanese AAC Stereo"),
        AudioOut(stream=HANAKO[3], channels=1, title=None),
        AudioOut(stream=_a(4, "dts", "fre", 4), channels=4, title="French AAC 4 ch"),
    ]
    cmd = _vaapi(TrackPlan(audio=[o.stream for o in outs], embedded_subtitles=[], audio_out=outs))
    joined = " ".join(cmd)
    assert "-map 0:a:1 -map 0:a:0 -map 0:a:2 -map 0:a:3 -map 0:a:4" in joined
    assert "-c:a copy -c:a:1 aac -ac:a:1 6 -b:a:1 384k -metadata:s:a:1 title=English AAC 5.1" in joined
    assert "-c:a:2 aac -ac:a:2 2 -b:a:2 160k -metadata:s:a:2 title=Japanese AAC Stereo" in joined
    assert "-c:a:3 aac -ac:a:3 1 -b:a:3 96k -metadata:s:a:3 title=" in joined
    assert "-b:a:4 384k" in joined
    assert "-c:a:0" not in joined
    assert cmd[cmd.index("-disposition:a:0") + 1] == "default"


def test_plan_tracks_applies_the_audio_format_to_the_selected_tracks() -> None:
    from arm_common import MediaType, TranscodeTool
    from arm_common.schemas import TranscodePresetView
    from arm_transcode.tracks import SourceTracks, plan_tracks

    preset = TranscodePresetView(
        id="tpr_x",
        name="x",
        media_type=MediaType.TV,
        is_builtin=True,
        tool=TranscodeTool.HANDBRAKE,
        preset_ref="H.265 MKV 1080p30",
        preset_json=None,
        container=ContainerFormat.MKV,
        encoder="any_h265",
        extra_args=None,
        created_by_user_id=None,
        created_at=None,
        updated_at=None,
        audio_selection="languages",
        audio_languages=["jpn", "eng"],
        audio_format="ac3_else_aac51",
    )
    plan = plan_tracks(preset, SourceTracks(audio=HANAKO), ContainerFormat.MKV, Path("/m/S01E01.mkv"))
    assert _summary(plan.audio_out) == [(3, "copy"), (1, "copy")]
    assert [s.position for s in plan.audio or []] == [3, 1]
