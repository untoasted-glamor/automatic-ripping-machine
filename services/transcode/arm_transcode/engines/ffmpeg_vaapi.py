"""AMD (Mesa VAAPI) encode path. No preset settings model: container from the
preset, fixed defaults below, and preset.extra_args for tuning (spec section 2).

Defaults: only the first video stream is mapped
(`-map 0:v:0`, HandBrake parity) so the single `-vf format=nv12,hwupload
-c:v <encoder>` chain never has to apply to more than one stream; a bare
`-map 0` would also pull in extra angles and attached-picture "video"
streams. Audio is copied (`-c:a copy`), so TrueHD, DTS and AC-3 tracks
stay as they are, unless the plan says otherwise (`TrackPlan.audio_out`,
any `audio_format` but `preset`): then each converted track is AAC with its
planned channel count (96/160/384 kbit/s for mono/stereo/5.1) and gets its
new title; a copied track keeps its source title. ffmpeg carries every
mapped stream's language tag over.

Which audio and subtitle tracks are mapped follows the preset's track
selection (`arm_transcode.tracks.TrackPlan`). Without one (the `preset`
modes) every audio track is copied (`-map 0:a?`), and subtitles are copied
only for MKV (`-map 0:s? -c:s copy`) because MP4 cannot mux bitmap subtitle
codecs (Blu-ray PGS `hdmv_pgs_subtitle`, DVD `dvd_subtitle`) and would abort
the encode; MP4 output instead drops subtitles entirely (`-sn`). With a plan,
each kept track is mapped by position in the plan's order (`-map 0:a:2
-map 0:a:0`), the first audio track is the default one, embedded subtitles
keep the source's forced flag and none is a default track, and MP4 embeds
only text subtitles (as `mov_text`). There is no burn-in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from arm_common import ContainerFormat
from arm_common.encoders import EncoderSpec
from arm_transcode.ffmpeg_progress import ProgressCallback, consume_progress, drain_stderr
from arm_transcode.tracks import TrackPlan

logger = logging.getLogger("arm_transcode.engines.ffmpeg_vaapi")

FFMPEG_MUXER: dict[ContainerFormat, str] = {ContainerFormat.MKV: "matroska", ContainerFormat.MP4: "mp4"}
DEFAULT_QP = "22"
_AAC_BITRATES = {1: "96k", 2: "160k", 6: "384k"}


def build_command(
    *,
    input_path: Path,
    output_path: Path,
    spec: EncoderSpec,
    device: str,
    container: ContainerFormat,
    extra_args: str | None,
    plan: TrackPlan | None = None,
) -> list[str]:
    muxer = FFMPEG_MUXER.get(container)
    if muxer is None:
        raise ValueError(f"ffmpeg_vaapi does not support container {container.value}")
    audio = plan.audio if plan is not None else None
    subtitles = plan.embedded_subtitles if plan is not None else None
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-y",
        "-vaapi_device",
        device,
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
    ]
    if audio is None:
        cmd += ["-map", "0:a?"]
    else:
        for stream in audio:
            cmd += ["-map", f"0:a:{stream.position}"]
    if subtitles is None:
        if container == ContainerFormat.MKV:
            cmd += ["-map", "0:s?"]
    else:
        for stream in subtitles:
            cmd += ["-map", f"0:s:{stream.position}"]
    cmd += [
        "-vf",
        "format=nv12,hwupload",
        "-c:v",
        str(spec.engine_encoder),
        "-rc_mode",
        "CQP",
        "-qp",
        DEFAULT_QP,
        "-c:a",
        "copy",
    ]
    outs = plan.audio_out if plan is not None else None
    for index, out in enumerate(outs or []):
        if out.copy:
            continue
        channels = out.channels or 2
        cmd += [f"-c:a:{index}", "aac", f"-ac:a:{index}", str(channels)]
        cmd += [f"-b:a:{index}", _AAC_BITRATES.get(channels, "384k")]
        cmd += [f"-metadata:s:a:{index}", f"title={out.title or ''}"]
    if audio is not None:
        for index, _ in enumerate(audio):
            cmd += [f"-disposition:a:{index}", "default" if index == 0 else "0"]
    if subtitles is None:
        cmd += ["-c:s", "copy"] if container == ContainerFormat.MKV else ["-sn"]
    elif subtitles:
        cmd += ["-c:s", "copy" if container == ContainerFormat.MKV else "mov_text"]
        for index, stream in enumerate(subtitles):
            cmd += [f"-disposition:s:{index}", "forced" if stream.forced else "0"]
    else:
        cmd += ["-sn"]
    cmd += ["-f", muxer]
    if extra_args:
        cmd.extend(extra_args.split())
    cmd.append(str(output_path))
    return cmd


async def transcode_ffmpeg_vaapi(
    *,
    input_path: Path,
    output_path: Path,
    spec: EncoderSpec,
    device: str,
    container: ContainerFormat,
    extra_args: str | None,
    duration_seconds: int | None,
    progress_callback: ProgressCallback,
    plan: TrackPlan | None = None,
) -> int:
    cmd = build_command(
        input_path=input_path,
        output_path=output_path,
        spec=spec,
        device=device,
        container=container,
        extra_args=extra_args,
        plan=plan,
    )
    logger.info("ffmpeg_vaapi start encoder=%s device=%s", spec.engine_encoder, device)
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stderr_buf: list[str] = []
    assert proc.stdout is not None and proc.stderr is not None
    try:
        await asyncio.gather(
            consume_progress(proc.stdout, duration_seconds, progress_callback),
            drain_stderr(proc.stderr, stderr_buf),
        )
        rc = await proc.wait()
    except BaseException:
        # Cancelled (user cancel or a revoked claim): don't leave ffmpeg
        # encoding on its own.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    if rc != 0:
        raise RuntimeError(f"ffmpeg_vaapi exited {rc}: {' | '.join(stderr_buf[-30:])}")
    return output_path.stat().st_size
