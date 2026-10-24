"""HandBrake engine: turns a selected `EncoderSpec` into the `--encoder`
args `transcode_handbrake` appends after `--preset`, and a `TrackPlan` into
the audio and subtitle track args.

Track args. HandBrakeCLI numbers a title's audio and subtitle tracks from 1
in source order, so a kept stream at ffmpeg position N is HandBrake track
N+1.

When the plan says what each audio track becomes (`TrackPlan.audio_out`, any
`audio_format` but `preset`), each track is a passthru (`copy`, with every
codec HandBrake can pass through in `--audio-copy-mask`) or `av_aac` with a
mono, stereo or 5point1 mixdown at HandBrake's default bitrate (160 kbit/s
stereo, 384 kbit/s 5.1). Every track is named explicitly with `--aname`
(HandBrakeCLI drops some source names when an entry is empty and has no
escape for a comma, so a comma in a source name becomes `;`): a copy keeps
its source title, a converted track gets its new one.

With `audio_format` `preset`, any audio track option on the command line
replaces the preset's audio encoder list with HandBrakeCLI's own defaults
(MP3 for MKV), so the preset's `AudioList` is read back with
`--preset-export` and repeated for every kept track, the same way HandBrake
applies a preset to a title:

- every `AudioList` entry applies to the first kept track; with
  `AudioSecondaryEncoderMode` on (all built-in presets) the other tracks get
  only the first entry;
- a `copy:<codec>` entry passes the track through only when the source is that
  codec, otherwise it uses the preset's `AudioEncoderFallback` (HandBrakeCLI
  itself would drop the track); a fallback of `none` skips that entry;
- `--audio-copy-mask` and `--audio-fallback` carry the preset's values for
  plain `copy` entries.

For the built-in `H.265 MKV *` presets that is AAC stereo 160 kbit/s for
every kept track, whatever the source codec (TrueHD and AC-3 alike).

Subtitles are passed through (`--subtitle 1,2`, never burned in, no default
track) or dropped (`--subtitle none`) per the plan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from arm_common.encoders import EncoderSpec
from arm_transcode.tracks import AudioOut, SourceStream, TrackPlan, run_tool

logger = logging.getLogger("arm_transcode.engines.handbrake")


def encoder_args(spec: EncoderSpec | None) -> list[str]:
    if spec is None or spec.kind == "preset" or spec.engine != "handbrake" or not spec.engine_encoder:
        return []
    return ["--encoder", spec.engine_encoder]


@dataclass(frozen=True)
class AudioEncoding:
    """One `AudioList` entry of a HandBrake preset."""

    encoder: str
    mixdown: str = "none"
    bitrate: int | None = None
    samplerate: str = "auto"


@dataclass(frozen=True)
class PresetAudio:
    """The audio part of a HandBrake preset."""

    encodings: list[AudioEncoding]
    copy_mask: list[str] = field(default_factory=list)
    fallback: str | None = None
    secondary_first_only: bool = True


# Default when a preset has no AudioList at all: HandBrake's own AAC stereo.
_DEFAULT_AUDIO = PresetAudio(encodings=[AudioEncoding(encoder="av_aac", mixdown="stereo", bitrate=160)])


def parse_preset_export(payload: str) -> PresetAudio:
    """Read the audio settings out of `HandBrakeCLI --preset-export` JSON."""
    data = json.loads(payload)
    preset = data["PresetList"][0]
    encodings: list[AudioEncoding] = []
    for entry in preset.get("AudioList") or []:
        bitrate = entry.get("AudioBitrate")
        encodings.append(
            AudioEncoding(
                encoder=str(entry["AudioEncoder"]),
                mixdown=str(entry.get("AudioMixdown") or "none"),
                bitrate=int(bitrate) if isinstance(bitrate, (int, float)) and bitrate > 0 else None,
                samplerate=str(entry.get("AudioSamplerate") or "auto"),
            )
        )
    if not encodings:
        return _DEFAULT_AUDIO
    mask = [str(m).removeprefix("copy:") for m in preset.get("AudioCopyMask") or []]
    fallback = preset.get("AudioEncoderFallback")
    return PresetAudio(
        encodings=encodings,
        copy_mask=mask,
        fallback=str(fallback) if fallback else None,
        secondary_first_only=bool(preset.get("AudioSecondaryEncoderMode", True)),
    )


async def preset_audio(preset_ref: str) -> PresetAudio:
    """The named preset's audio settings, read from HandBrakeCLI itself.

    HandBrakeCLI answers an unknown preset name with its whole preset list on
    stderr, so the error names the preset instead of quoting that list (the
    full output stays chained for the log)."""
    try:
        payload = await run_tool(["HandBrakeCLI", "--preset", preset_ref, "--preset-export", "arm"], "HandBrakeCLI")
    except RuntimeError as exc:
        raise RuntimeError(f"could not read HandBrake preset {preset_ref!r}; check the preset name") from exc
    return parse_preset_export(payload)


def _passthru_names(stream: SourceStream) -> set[str]:
    """HandBrake `copy:<name>` values that can pass `stream` through."""
    codec = stream.codec
    if codec.startswith("pcm_"):
        return {"pcm"}
    if codec == "dts":
        return {"dts", "dtshd"} if "HD" in (stream.profile or "") else {"dts"}
    return {codec}


def _resolve_encoder(entry: AudioEncoding, stream: SourceStream, audio: PresetAudio) -> str | None:
    if not entry.encoder.startswith("copy:"):
        return entry.encoder
    if entry.encoder.removeprefix("copy:") in _passthru_names(stream):
        return entry.encoder
    if audio.fallback and audio.fallback != "none":
        return audio.fallback
    return None


def audio_track_args(tracks: list[SourceStream], audio: PresetAudio) -> list[str]:
    if not tracks:
        return ["--audio", "none"]
    numbers: list[str] = []
    encoders: list[str] = []
    mixdowns: list[str] = []
    bitrates: list[int | None] = []
    samplerates: list[str] = []
    for index, stream in enumerate(tracks):
        entries = audio.encodings if index == 0 or not audio.secondary_first_only else audio.encodings[:1]
        for entry in entries:
            encoder = _resolve_encoder(entry, stream, audio)
            if encoder is None:
                logger.info(
                    "audio track %d: no passthru or fallback for %s; entry skipped", stream.position, entry.encoder
                )
                continue
            numbers.append(str(stream.position + 1))
            encoders.append(encoder)
            mixdowns.append(entry.mixdown)
            bitrates.append(entry.bitrate)
            samplerates.append(entry.samplerate)
    if not numbers:
        # Every entry was an impossible passthru: keep the first track with
        # HandBrake's default encoder rather than write a silent file.
        numbers, encoders, mixdowns = [str(tracks[0].position + 1)], ["av_aac"], ["stereo"]
        bitrates, samplerates = [160], ["auto"]
    args = ["--audio", ",".join(numbers), "--aencoder", ",".join(encoders), "--mixdown", ",".join(mixdowns)]
    if all(b is not None for b in bitrates):
        args += ["--ab", ",".join(str(b) for b in bitrates)]
    if any(r != "auto" for r in samplerates):
        args += ["--arate", ",".join(samplerates)]
    if audio.copy_mask:
        args += ["--audio-copy-mask", ",".join(audio.copy_mask)]
    if audio.fallback:
        args += ["--audio-fallback", audio.fallback]
    return args


def subtitle_track_args(tracks: list[SourceStream]) -> list[str]:
    if not tracks:
        return ["--subtitle", "none"]
    numbers = ",".join(str(s.position + 1) for s in tracks)
    return ["--subtitle", numbers, "--subtitle-burned=none", "--subtitle-default=none"]


_MIXDOWNS = {1: "mono", 2: "stereo", 6: "5point1"}
# Every codec HandBrakeCLI 1.11 can pass through (its --audio-copy-mask list).
_COPY_MASK = "aac,ac3,eac3,truehd,dts,dtshd,mp2,mp3,opus,vorbis,flac,alac,pcm"


def audio_out_args(outs: list[AudioOut]) -> list[str]:
    """HandBrakeCLI audio args for an explicit per-track plan."""
    if not outs:
        return ["--audio", "none"]
    numbers = ",".join(str(o.stream.position + 1) for o in outs)
    encoders = ",".join("copy" if o.copy else "av_aac" for o in outs)
    mixdowns = ",".join("none" if o.channels is None else _MIXDOWNS.get(o.channels, "5point1") for o in outs)
    names = ",".join(((o.stream.title or "") if o.copy else (o.title or "")).replace(",", ";") for o in outs)
    args = ["--audio", numbers, "--aencoder", encoders, "--mixdown", mixdowns, "--aname", names]
    if any(o.copy for o in outs):
        args += ["--audio-copy-mask", _COPY_MASK]
    return [*args, "--audio-fallback", "av_aac"]


def track_args(plan: TrackPlan, audio: PresetAudio | None) -> list[str]:
    """HandBrakeCLI args for `plan`; `audio` is the preset's audio settings,
    needed only when the plan picks the audio tracks with `audio_format`
    `preset`."""
    args: list[str] = []
    if plan.audio_out is not None:
        args += audio_out_args(plan.audio_out)
    elif plan.audio is not None:
        args += audio_track_args(plan.audio, audio or _DEFAULT_AUDIO)
    if plan.embedded_subtitles is not None:
        args += subtitle_track_args(plan.embedded_subtitles)
    return args
