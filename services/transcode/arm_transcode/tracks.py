"""Audio and subtitle track selection, and subtitle sidecar files.

A transcode preset says which source tracks to keep (`audio_selection` +
`audio_languages`, `subtitle_selection` + `subtitle_languages`) and where
kept subtitles go (`subtitle_output`). When either selection is not
`preset`, the transcoder probes the source with ffprobe, picks the tracks
here, and each engine turns the resulting `TrackPlan` into its own
arguments (`engines/handbrake_engine.track_args`,
`engines/ffmpeg_vaapi.build_command`).

Selection rules:
- `all` keeps every track in source order.
- `languages` keeps every track whose language is listed, grouped by the
  list's order (`["jpn", "eng"]` puts the Japanese tracks first), each group
  in source order. The first kept audio track is the default track.
- An audio `languages` selection that matches nothing keeps the first audio
  track instead (and logs it): a file with no sound is never the intent.
  A subtitle selection that matches nothing keeps none.

Audio format (`audio_format`, used when ARM picks the audio tracks):
- `ac3_else_aac51` keeps one track per language: an untouched copy of its
  AC-3 track with the most channels, else its best track (most channels,
  then TrueHD, DTS-HD, LPCM, FLAC, DTS, E-AC-3, others) converted to AAC with
  the source's channels up to 5.1 (a stereo or mono source stays so).
  Commentary and audio-description tracks (the source's `comment` /
  `visual_impaired` disposition, or a title naming one) never stand in for
  the main track: they follow every language's main track, chosen by the same
  rule. Candidates that still tie (two AC-3 5.1 tracks of one language that
  nothing marks apart) are all kept, in disc order.
- `aac_stereo` converts every kept track to AAC stereo, `aac_51` to AAC with
  up to 5.1 channels, and `copy` keeps every kept track untouched.
- `preset` leaves it to the engine.
A converted track is titled for what it now is (`English AAC 5.1`); a copy
keeps its source title. Language tags stay on every track.

Sidecars: bitmap subtitles are copied losslessly in their own format, Blu-ray
PGS to `.sup` and DVD VobSub to `.mks` (a one-track Matroska file: ffmpeg
has no VobSub `.idx`/`.sub` muxer, and Jellyfin reads `.mks`). Text
subtitles keep their text format (`.srt`, `.ass`, `.vtt`). Each file is
named for Jellyfin, `<video stem>.<lang>[.<n>][.forced].<ext>`: language
first, a counter from 2 for a second track with the same name, then the
`forced` flag when the source marks the track forced. Every sidecar is
written as `<name>.arm-inprogress` and renamed only once the video itself
has landed; on a failure or cancel the partial files are removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import re

from arm_common import AudioFormat, AudioSelection, ContainerFormat, SubtitleOutput, SubtitleSelection
from arm_common.fileops import INPROGRESS_SUFFIX
from arm_common.languages import canonical_language, language_name
from arm_common.schemas import TranscodePresetView

logger = logging.getLogger("arm_transcode.tracks")

# Subtitle codecs that are text; everything else is a picture (PGS, VobSub,
# DVB) that MP4 cannot carry.
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"})

# Subtitle codec -> (file extension, ffmpeg muxer, ffmpeg codec) for a sidecar.
# `copy` keeps the stream bit for bit; mov_text (MP4 text) has no file format
# of its own and becomes SubRip text.
SIDECAR_FORMATS: dict[str, tuple[str, str, str]] = {
    "hdmv_pgs_subtitle": ("sup", "sup", "copy"),
    "dvd_subtitle": ("mks", "matroska", "copy"),
    "dvb_subtitle": ("mks", "matroska", "copy"),
    "subrip": ("srt", "srt", "copy"),
    "srt": ("srt", "srt", "copy"),
    "ass": ("ass", "ass", "copy"),
    "ssa": ("ass", "ass", "copy"),
    "webvtt": ("vtt", "webvtt", "copy"),
    "mov_text": ("srt", "srt", "srt"),
}

_STDERR_TAIL_LINES = 30


@dataclass(frozen=True)
class SourceStream:
    """One audio or subtitle stream of the source file.

    `position` counts streams of the same kind from 0 (ffmpeg's `0:a:N` /
    `0:s:N`); HandBrake numbers the same tracks from 1.
    """

    position: int
    codec: str
    language: str
    forced: bool = False
    title: str | None = None
    profile: str | None = None
    channels: int | None = None
    # Commentary or audio description (see `_is_extra`).
    extra: bool = False


@dataclass(frozen=True)
class AudioOut:
    """One output audio track: `stream` copied untouched (`channels` None),
    or converted to AAC with `channels` channels and titled `title`."""

    stream: SourceStream
    channels: int | None = None
    title: str | None = None

    @property
    def copy(self) -> bool:
        return self.channels is None


@dataclass(frozen=True)
class SourceTracks:
    audio: list[SourceStream] = field(default_factory=list)
    subtitles: list[SourceStream] = field(default_factory=list)


@dataclass(frozen=True)
class Sidecar:
    stream: SourceStream
    path: Path
    muxer: str
    codec: str

    @property
    def tmp_path(self) -> Path:
        return self.path.with_name(self.path.name + INPROGRESS_SUFFIX)


@dataclass(frozen=True)
class TrackPlan:
    """What an engine keeps. `None` means "leave it to the engine"
    (the preset's own rule); an empty list means "keep none"."""

    audio: list[SourceStream] | None
    embedded_subtitles: list[SourceStream] | None
    sidecars: list[Sidecar] = field(default_factory=list)
    # What each kept audio track becomes; None leaves it to the engine
    # (`audio_format` `preset`). When set, `audio` lists the same streams.
    audio_out: list[AudioOut] | None = None


def needs_plan(preset: TranscodePresetView | None) -> bool:
    """True when the preset asks for any track selection of its own."""
    if preset is None:
        return False
    return preset.audio_selection != AudioSelection.PRESET or preset.subtitle_selection != SubtitleSelection.PRESET


def parse_ffprobe(payload: str) -> SourceTracks:
    """Read `ffprobe -show_streams -of json` output into audio/subtitle lists."""
    data = json.loads(payload or "{}")
    audio: list[SourceStream] = []
    subtitles: list[SourceStream] = []
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        tags = {str(k).lower(): v for k, v in (stream.get("tags") or {}).items()}
        disposition = stream.get("disposition") or {}
        target = audio if kind == "audio" else subtitles
        target.append(
            SourceStream(
                position=len(target),
                codec=str(stream.get("codec_name") or "unknown"),
                language=canonical_language(tags.get("language")),
                forced=bool(disposition.get("forced")),
                title=tags.get("title"),
                profile=stream.get("profile"),
                channels=stream.get("channels") if isinstance(stream.get("channels"), int) else None,
                extra=_is_extra(disposition, tags.get("title")),
            )
        )
    return SourceTracks(audio=audio, subtitles=subtitles)


_EXTRA_TITLE_RE = re.compile(r"comment|descripti|narrat", re.IGNORECASE)


def _is_extra(disposition: dict[str, object], title: str | None) -> bool:
    """A commentary or audio-description track: flagged so by the source
    (Matroska FlagCommentary / FlagVisualImpaired, read by ffprobe as the
    `comment` / `visual_impaired` disposition), or titled so."""
    if disposition.get("comment") or disposition.get("visual_impaired"):
        return True
    return bool(title and _EXTRA_TITLE_RE.search(title))


# Fallback ranking when a language has no AC-3 track: lossless first.
_CODEC_RANK = {"truehd": 0, "dts_hd": 1, "flac": 3, "dts": 4, "eac3": 5, "aac": 6, "mp2": 7, "mp3": 7}


def _codec_rank(stream: SourceStream) -> int:
    if stream.codec == "dts" and "HD" in (stream.profile or ""):
        return _CODEC_RANK["dts_hd"]
    if stream.codec.startswith("pcm_"):
        return 2
    return _CODEC_RANK.get(stream.codec, 8)


def _aac_channels(stream: SourceStream, *, stereo: bool) -> int:
    if stereo:
        return 2
    channels = stream.channels or 2
    return 6 if channels >= 3 else channels


_LAYOUTS = {1: "Mono", 2: "Stereo", 6: "5.1"}


def _converted(stream: SourceStream, *, stereo: bool) -> AudioOut:
    channels = _aac_channels(stream, stereo=stereo)
    title = f"{language_name(stream.language)} AAC {_LAYOUTS.get(channels, f'{channels} ch')}"
    return AudioOut(stream=stream, channels=channels, title=title)


def _best_of(candidates: list[SourceStream]) -> list[AudioOut]:
    """One language's pick (see the module doc); ties are all kept."""
    ac3 = [s for s in candidates if s.codec == "ac3"]
    if ac3:
        most = max(s.channels or 0 for s in ac3)
        return [AudioOut(stream=s) for s in ac3 if (s.channels or 0) == most]
    best = min((-(s.channels or 0), _codec_rank(s)) for s in candidates)
    return [_converted(s, stereo=False) for s in candidates if (-(s.channels or 0), _codec_rank(s)) == best]


def plan_audio(streams: list[SourceStream], audio_format: AudioFormat) -> list[AudioOut] | None:
    """What each of the kept `streams` becomes; None for `preset`."""
    if audio_format == AudioFormat.PRESET:
        return None
    if audio_format == AudioFormat.COPY:
        return [AudioOut(stream=s) for s in streams]
    if audio_format in (AudioFormat.AAC_STEREO, AudioFormat.AAC_51):
        return [_converted(s, stereo=audio_format == AudioFormat.AAC_STEREO) for s in streams]
    by_language: dict[str, list[SourceStream]] = {}
    for stream in streams:
        by_language.setdefault(stream.language, []).append(stream)
    mains: list[AudioOut] = []
    extras: list[AudioOut] = []
    for members in by_language.values():
        main = [s for s in members if not s.extra]
        extra = [s for s in members if s.extra]
        if not main:
            # Only commentary in this language: it is the language's track.
            main, extra = extra, []
        mains += _best_of(main)
        if extra:
            extras += _best_of(extra)
    return mains + extras


def _by_languages(streams: list[SourceStream], languages: list[str]) -> list[SourceStream]:
    wanted = [canonical_language(code) for code in languages]
    return [s for lang in dict.fromkeys(wanted) for s in streams if s.language == lang]


def select_audio(streams: list[SourceStream], selection: AudioSelection, languages: list[str]) -> list[SourceStream]:
    if selection == AudioSelection.ALL or not streams:
        return list(streams)
    chosen = _by_languages(streams, languages)
    if not chosen:
        logger.warning(
            "no audio track matches languages=%s (source has %s); keeping the first audio track",
            ",".join(languages),
            ",".join(s.language for s in streams),
        )
        return [streams[0]]
    return chosen


def select_subtitles(
    streams: list[SourceStream], selection: SubtitleSelection, languages: list[str]
) -> list[SourceStream]:
    if selection == SubtitleSelection.NONE:
        return []
    if selection == SubtitleSelection.ALL:
        return list(streams)
    chosen = _by_languages(streams, languages)
    if not chosen and streams:
        logger.info(
            "no subtitle track matches languages=%s (source has %s); keeping none",
            ",".join(languages),
            ",".join(s.language for s in streams),
        )
    return chosen


def embeddable(streams: list[SourceStream], container: ContainerFormat) -> list[SourceStream]:
    """The subset of `streams` the output container can carry.

    MKV carries every subtitle codec; MP4 only text (it cannot hold PGS or
    VobSub pictures, and HandBrake would burn one into the video instead);
    other containers carry none here.
    """
    if container == ContainerFormat.MKV:
        return list(streams)
    keep = [s for s in streams if s.codec in TEXT_SUBTITLE_CODECS] if container == ContainerFormat.MP4 else []
    dropped = [s for s in streams if s not in keep]
    if dropped:
        logger.warning(
            "%s cannot embed subtitle tracks %s; write them as separate files instead",
            container.value,
            ",".join(f"{s.position}:{s.codec}" for s in dropped),
        )
    return keep


def sidecar_name(stem: str, stream: SourceStream, ext: str, taken: set[str]) -> str:
    """`<stem>.<lang>[.<n>][.forced].<ext>`, unique within `taken` (which it updates).

    The counter sits between the language and the flags, so Jellyfin still
    reads the language and the forced flag; it starts at 2 for the second
    track that would otherwise get the same name."""
    flags = ".forced" if stream.forced else ""
    counter = 1
    while True:
        number = f".{counter}" if counter > 1 else ""
        name = f"{stem}.{stream.language}{number}{flags}.{ext}"
        if name not in taken:
            taken.add(name)
            return name
        counter += 1


def plan_sidecars(final_output: Path, streams: list[SourceStream]) -> list[Sidecar]:
    sidecars: list[Sidecar] = []
    taken: set[str] = set()
    for stream in streams:
        fmt = SIDECAR_FORMATS.get(stream.codec)
        if fmt is None:
            logger.warning("subtitle track %d (%s) has no lossless file format; skipped", stream.position, stream.codec)
            continue
        ext, muxer, codec = fmt
        name = sidecar_name(final_output.stem, stream, ext, taken)
        sidecars.append(Sidecar(stream=stream, path=final_output.with_name(name), muxer=muxer, codec=codec))
    return sidecars


def plan_tracks(
    preset: TranscodePresetView, source: SourceTracks, container: ContainerFormat, final_output: Path
) -> TrackPlan:
    audio: list[SourceStream] | None = None
    audio_out: list[AudioOut] | None = None
    if preset.audio_selection != AudioSelection.PRESET:
        audio = select_audio(source.audio, preset.audio_selection, preset.audio_languages)
        audio_out = plan_audio(audio, preset.audio_format)
        if audio_out is not None:
            audio = [o.stream for o in audio_out]

    embedded: list[SourceStream] | None = None
    sidecars: list[Sidecar] = []
    if preset.subtitle_selection != SubtitleSelection.PRESET:
        subtitles = select_subtitles(source.subtitles, preset.subtitle_selection, preset.subtitle_languages)
        embed = preset.subtitle_output in (SubtitleOutput.EMBEDDED, SubtitleOutput.BOTH)
        separate = preset.subtitle_output in (SubtitleOutput.SEPARATE, SubtitleOutput.BOTH)
        embedded = embeddable(subtitles, container) if embed else []
        if separate:
            sidecars = plan_sidecars(final_output, subtitles)

    logger.info(
        "track plan audio=%s embedded_subtitles=%s sidecars=%s",
        "engine"
        if audio is None
        else [f"{s.position}:{s.language}" for s in audio]
        if audio_out is None
        else [f"{o.stream.position}:{o.stream.language}:{'copy' if o.copy else f'aac{o.channels}'}" for o in audio_out],
        "engine" if embedded is None else [f"{s.position}:{s.language}" for s in embedded],
        [p.path.name for p in sidecars],
    )
    return TrackPlan(audio=audio, embedded_subtitles=embedded, sidecars=sidecars, audio_out=audio_out)


async def run_tool(cmd: list[str], what: str) -> str:
    """Run `cmd`, return its stdout; kill it if the caller is cancelled."""
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        tail = " | ".join(stderr.decode(errors="replace").splitlines()[-_STDERR_TAIL_LINES:])
        raise RuntimeError(f"{what} exited rc={proc.returncode}: {tail}")
    return stdout.decode(errors="replace")


async def probe_source(input_path: Path) -> SourceTracks:
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(input_path)]
    return parse_ffprobe(await run_tool(cmd, "ffprobe"))


def extract_command(input_path: Path, sidecars: list[Sidecar]) -> list[str]:
    """One ffmpeg run that writes every sidecar (one output per track)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(input_path)]
    for sidecar in sidecars:
        cmd += ["-map", f"0:s:{sidecar.stream.position}", "-c:s", sidecar.codec, "-f", sidecar.muxer]
        cmd.append(str(sidecar.tmp_path))
    return cmd


async def subtitle_packets(sidecar: Sidecar) -> int:
    """How many subtitle packets the sidecar's temp file holds (0 when it is
    missing, empty, or unreadable)."""
    if not sidecar.tmp_path.exists() or sidecar.tmp_path.stat().st_size == 0:
        return 0
    cmd = ["ffprobe", "-v", "error", "-f", sidecar.muxer, "-count_packets", "-select_streams", "s:0"]
    cmd += ["-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(sidecar.tmp_path)]
    try:
        out = await run_tool(cmd, "ffprobe")
    except RuntimeError as exc:
        # ffmpeg's header-only Matroska file for a track with no events.
        logger.info("subtitle file %s holds no readable subtitles; dropped", sidecar.path.name)
        logger.debug("ffprobe of %s: %s", sidecar.path.name, exc)
        return 0
    value = out.strip().splitlines()[0] if out.strip() else ""
    return int(value) if value.isdigit() else 0


async def extract_sidecars(input_path: Path, sidecars: list[Sidecar]) -> None:
    """Write each sidecar's `.arm-inprogress` file from the source.

    A track with no subtitle events in this title (common on DVD extras,
    where every title carries every subtitle stream) still gets a file from
    ffmpeg, an empty `.sup` or a header-only `.mks`; those are removed here,
    so `sidecar_outputs` skips them."""
    if not sidecars:
        return
    logger.info("extracting %d subtitle file(s) from %s", len(sidecars), input_path.name)
    await run_tool(extract_command(input_path, sidecars), "ffmpeg subtitle extract")
    for sidecar in sidecars:
        if await subtitle_packets(sidecar) == 0:
            sidecar.tmp_path.unlink(missing_ok=True)


@contextlib.contextmanager
def sidecar_outputs(sidecars: list[Sidecar]) -> Iterator[list[Sidecar]]:
    """Rename every sidecar's temp file into place on a clean exit; remove
    them all when the body raises or is cancelled.

    Used around the video's `atomic_output`, so the sidecars land just after
    the video does and never without it. Creates the video's folder, since
    the sidecars are extracted before the encode starts."""
    for parent in {s.path.parent for s in sidecars}:
        parent.mkdir(parents=True, exist_ok=True)
    try:
        yield sidecars
    except BaseException:
        for sidecar in sidecars:
            with contextlib.suppress(OSError):
                sidecar.tmp_path.unlink(missing_ok=True)
        raise
    for sidecar in sidecars:
        if not sidecar.tmp_path.exists() or sidecar.tmp_path.stat().st_size == 0:
            # A track with no subtitle events in this title: no file to keep.
            sidecar.tmp_path.unlink(missing_ok=True)
            logger.info("subtitle track %d has no subtitles in this title; no file written", sidecar.stream.position)
            continue
        os.replace(sidecar.tmp_path, sidecar.path)
        logger.info("subtitle file written: %s", sidecar.path)


def written_sidecars(sidecars: list[Sidecar]) -> list[Path]:
    """The sidecars that exist on disk after `sidecar_outputs` exited."""
    return [s.path for s in sidecars if s.path.exists()]
