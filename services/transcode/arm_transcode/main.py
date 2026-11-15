"""Transcoder entry point — single-task, ephemeral.

Run order:
1. Load env (`TranscoderConfig`).
2. Open the WS connection (subscribe `transcoder.commands.{task_id}` for cancel).
3. POST /register, POST /claim. Refuse if claim returns anything other than IN_PROGRESS.
4. Pick the encoder by `transcode_preset.tool`:
   - HANDBRAKE → `handbrake.transcode_handbrake`
   - ABCDE → `ffmpeg_audio.transcode_audio`
   - NONE → `arm_common.fileops.transcode_none` (no atomic-rename needed)
5. Wrap the output write in `arm_common.fileops.atomic_output` so partial
   files land as `*.arm-inprogress` and only get renamed on success. When the
   preset selects tracks (`arm_transcode.tracks`), probe the source first,
   and write any subtitle sidecars as `*.arm-inprogress` too: they are
   renamed just after the video, and removed if the encode fails or is
   cancelled.
6. PATCH /complete with size + duration metadata and the sidecar paths.
7. On exception or cancel: PATCH /fail with the error string.
8. If a heartbeat shows the Backend revoked our claim, stop the encode and
   exit without /fail: the task is no longer ours to report on.

A `menu_preview` task (fork only) runs `menus.preview.make_preview` on the
register's `raw_input_path` (the job's copied disc menu, `<job raw>/menu/`),
which writes `<job raw>/menu/preview/`; /complete reports that folder as its
absolute `output_path` (it is under `/raw`, not the media root).

A `menu_pack` task (fork only) runs `menus.task.build_pack` on the register's
`menu_pack` plan and `raw_input_path` (the menu copy), which writes the pack
into `<show folder>/.discmenus/<disc>/` under the media root; /complete
reports that folder, relative to the media root.

A `subtitle_ocr` task (register says so in `task.kind`) runs
`subtitle_ocr.task.make_text_subtitles` over the register's
`input_sidecar_paths` instead of an encoder, and reports the `.srt` files as
its sidecars: at /complete, or at /fail for the ones finished before a
sidecar failed.

Container exits when the run() coroutine returns.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
import sys
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from arm_common import configure_service_logging, with_log_context
from arm_common.encoders import EncoderSpec
from arm_common.schemas import (
    HardwareCaps,
    MenuPackPlan,
    RegisterTranscoderResponse,
    TranscodePresetView,
    WSEnvelope,
)
from arm_common.enums import GpuVendor, TranscodeTaskKind, TranscodeTool
from arm_common.fileops import atomic_output, transcode_none

from arm_transcode.api_client import BackendClient
from arm_transcode.config import TranscoderConfig
from arm_transcode.device_probe import ProbeSetupError, probe_device
from arm_transcode.engines import selected_encoder
from arm_transcode.engines.ffmpeg_vaapi import transcode_ffmpeg_vaapi
from arm_transcode.engines.handbrake_engine import encoder_args as handbrake_encoder_args
from arm_transcode.engines.handbrake_engine import preset_audio, track_args
from arm_transcode.ffmpeg_audio import transcode_audio
from arm_transcode.handbrake import ProgressCallback, transcode_handbrake
from arm_transcode.heartbeat import HeartbeatPump, ProgressState
from arm_transcode.subtitle_ocr.task import SubtitleOcrFailed, make_text_subtitles
from arm_transcode.tracks import (
    TrackPlan,
    extract_sidecars,
    needs_plan,
    plan_tracks,
    probe_source,
    sidecar_outputs,
    written_sidecars,
)
from arm_transcode.ws_client import EnvelopeHandler, WSClient


logger = logging.getLogger("arm_transcode.main")

# Upper bound on the error string we PATCH to `transcode_tasks.last_error`.
# `last_error` is an unbounded TEXT column, but a pathological multi-MB stderr
# shouldn't bloat the row / WS frame. Generous enough to hold handbrake.py's
# 1000-line stderr tail (~80 KB) intact; see `_clip_error`.
_MAX_ERROR_CHARS = 100_000


def _clip_error(msg: str) -> str:
    """Bound an error string for `last_error`, keeping the *tail*.

    Encoder failures (HandBrake/ffmpeg) print the decisive line last, so we
    truncate from the front, not the back — the historical `str(exc)[:500]`
    kept the front and dropped exactly the line that explained the failure.
    """
    if len(msg) <= _MAX_ERROR_CHARS:
        return msg
    return "…(truncated)…\n" + msg[-_MAX_ERROR_CHARS:]


class CancelRequested(Exception):
    pass


class ClaimRevoked(Exception):
    pass


def _detect_hw_caps() -> HardwareCaps:
    """Diagnostic-only: reports the GPU vendor the dispatcher assigned us.

    The Backend's `gpu_probe` is the authoritative inventory source — this
    function exists so the `register` payload reflects what the *spawned
    container* actually sees (useful for log triage when device passthrough
    fails). The Backend ignores these flags for dispatch decisions.
    """
    vendor = os.environ.get("ARM_GPU_VENDOR", "").lower()
    return HardwareCaps(
        cpu_count=os.cpu_count() or 1,
        has_vaapi=(vendor == "vaapi"),
        has_nvenc=(vendor == "nvenc"),
        has_qsv=(vendor == "qsv"),
    )


def _progress_recorder(state: ProgressState) -> ProgressCallback:
    """Build a progress callback that records each tick on `state`. (Fork: a
    cancel or revoke stops the encode from outside, `_encode_until_stopped`.)"""

    async def _on_progress(
        pct: int, eta: int | None, current: str | None, fps: float | None, avg_fps: float | None
    ) -> None:
        state.pct = pct
        state.eta_seconds = eta
        state.current_pass = current
        state.fps = fps
        state.avg_fps = avg_fps

    return _on_progress


@dataclass(frozen=True)
class EncodeResult:
    size: int
    sidecars: list[Path] = field(default_factory=list)


async def _track_plan(preset: TranscodePresetView | None, raw_input: Path, final_output: Path) -> TrackPlan | None:
    """The preset's track selection for this source, or None when the preset
    leaves both audio and subtitles to the engine (no probe needed)."""
    if preset is None or not needs_plan(preset):
        return None
    source = await probe_source(raw_input)
    return plan_tracks(preset, source, preset.container, final_output)


def _require_gpu_device() -> str:
    """Return the GPU device path from ARM_GPU_DEVICE; raise RuntimeError when it is unset or empty."""
    device = os.environ.get("ARM_GPU_DEVICE")
    if not device:
        raise RuntimeError("ffmpeg_vaapi encoder requires ARM_GPU_DEVICE")
    return device


async def _encode_ffmpeg_vaapi(
    *,
    preset: TranscodePresetView,
    spec: EncoderSpec,
    raw_input: Path,
    final_output: Path,
    duration_seconds: int | None,
    progress_callback: ProgressCallback,
) -> EncodeResult:
    """Run the ffmpeg VAAPI engine into an atomic output, with the preset's
    track selection and subtitle sidecars (fork); return what it wrote."""
    device = _require_gpu_device()
    plan = await _track_plan(preset, raw_input, final_output)
    sidecars = plan.sidecars if plan is not None else []
    with sidecar_outputs(sidecars):
        await extract_sidecars(raw_input, sidecars)
        with atomic_output(final_output) as tmp:
            size = await transcode_ffmpeg_vaapi(
                input_path=raw_input,
                output_path=tmp,
                spec=spec,
                device=device,
                container=preset.container,
                extra_args=preset.extra_args,
                duration_seconds=duration_seconds,
                progress_callback=progress_callback,
                plan=plan,
            )
    return EncodeResult(size=size, sidecars=written_sidecars(sidecars))


async def _encode_handbrake(
    *,
    preset: TranscodePresetView,
    preset_ref: str,
    raw_input: Path,
    final_output: Path,
    progress_callback: ProgressCallback,
) -> EncodeResult:
    """Run HandBrakeCLI into an atomic output, with the preset's track
    selection and subtitle sidecars (fork); return what it wrote."""
    plan = await _track_plan(preset, raw_input, final_output)
    hb_track_args: list[str] = []
    sidecars = []
    if plan is not None:
        audio = await preset_audio(preset_ref) if plan.audio and plan.audio_out is None else None
        hb_track_args = track_args(plan, audio)
        sidecars = plan.sidecars
    with sidecar_outputs(sidecars):
        await extract_sidecars(raw_input, sidecars)
        with atomic_output(final_output) as tmp:
            size = await transcode_handbrake(
                input_path=raw_input,
                output_path=tmp,
                preset_ref=preset_ref,
                extra_args=preset.extra_args,
                encoder_args=handbrake_encoder_args(selected_encoder()),
                progress_callback=progress_callback,
                track_args=hb_track_args,
            )
    return EncodeResult(size=size, sidecars=written_sidecars(sidecars))


async def _encode_audio(
    *,
    preset: TranscodePresetView,
    raw_input: Path,
    final_output: Path,
    duration_seconds: int | None,
    progress_callback: ProgressCallback,
) -> int:
    """Run the ffmpeg audio engine into an atomic output and return the size written."""
    # The ripper's abcde stage produced .wav files; we re-encode to
    # `preset.container.value` (flac, mp3) here.
    target = preset.preset_ref or preset.container.value
    with atomic_output(final_output) as tmp:
        return await transcode_audio(
            input_path=raw_input,
            output_path=tmp,
            preset_ref=target,
            duration_seconds=duration_seconds,
            progress_callback=progress_callback,
        )


async def _encode(
    *,
    tool: TranscodeTool,
    preset: TranscodePresetView | None,
    raw_input: Path,
    final_output: Path,
    duration_seconds: int | None,
    progress_callback: ProgressCallback,
) -> EncodeResult:
    """Run the engine `tool` selects and return what it wrote."""
    if tool == TranscodeTool.NONE:
        return EncodeResult(size=transcode_none(raw_input, final_output))

    spec = selected_encoder()
    if tool == TranscodeTool.HANDBRAKE and spec is not None and spec.engine == "ffmpeg_vaapi":
        # `tool` only equals HANDBRAKE when `preset` produced it (see `run()`
        # below), so `preset` is never None on this branch.
        assert preset is not None
        return await _encode_ffmpeg_vaapi(
            preset=preset,
            spec=spec,
            raw_input=raw_input,
            final_output=final_output,
            duration_seconds=duration_seconds,
            progress_callback=progress_callback,
        )

    if preset is None or preset.preset_ref is None:
        raise RuntimeError(f"transcode tool={tool.value} requires a preset_ref")

    if tool == TranscodeTool.HANDBRAKE:
        return await _encode_handbrake(
            preset=preset,
            preset_ref=preset.preset_ref,
            raw_input=raw_input,
            final_output=final_output,
            progress_callback=progress_callback,
        )

    if tool == TranscodeTool.ABCDE:
        size = await _encode_audio(
            preset=preset,
            raw_input=raw_input,
            final_output=final_output,
            duration_seconds=duration_seconds,
            progress_callback=progress_callback,
        )
        return EncodeResult(size=size)

    raise RuntimeError(f"unknown transcode tool={tool.value}")


async def _run_encoder(
    *,
    tool: TranscodeTool,
    preset: TranscodePresetView | None,
    raw_input: Path,
    final_output: Path,
    duration_seconds: int | None,
    state: ProgressState,
) -> EncodeResult:
    """Encode with the engine for `tool`, recording progress on `state`;
    returns the size of the video written and the subtitle sidecars beside it.
    (Fork: no cancel_event; a cancel or revoke stops the encode from outside,
    `_encode_until_stopped`.)"""
    result = await _encode(
        tool=tool,
        preset=preset,
        raw_input=raw_input,
        final_output=final_output,
        duration_seconds=duration_seconds,
        progress_callback=_progress_recorder(state),
    )
    state.pct = 100
    return result


def _cancel_listener(cancel_event: asyncio.Event) -> EnvelopeHandler:
    """Build a WS handler that sets `cancel_event` on a task.cancel envelope and ignores the rest."""

    async def _on_cancel(env: WSEnvelope) -> None:
        if env.event_type == "task.cancel":
            logger.warning("task.cancel received over WS; signalling encoder")
            cancel_event.set()

    return _on_cancel


def _backend_ssl_context() -> ssl.SSLContext:
    """Build the TLS context for the Backend's wss:// endpoint from the system trust store.

    The Backend serves wss:// with a cert chained to the install CA. The entrypoint already merges that CA into the
    system trust store, at the same /etc/ssl/certs/ca-certificates.crt path api_client.py uses for the HTTP client.
    `websockets` refuses to connect over wss:// without an explicit ssl context, so build one from the system store.
    """
    return ssl.create_default_context()


async def _report_complete(
    *,
    api: BackendClient,
    task_id: str,
    media_root: str,
    final_output: Path,
    result: EncodeResult,
    duration: int | None,
    preview: bool = False,
) -> None:
    """Tell the Backend the task completed, with the output and sidecar paths
    relative to media_root (a menu preview's output path is sent as is)."""
    relative = final_output.as_posix() if preview else final_output.relative_to(media_root).as_posix()
    sidecars = [p.relative_to(media_root).as_posix() for p in result.sidecars]
    await api.complete(
        task_id,
        output_path=relative,
        size_bytes=result.size,
        duration_seconds=duration,
        sidecar_paths=sidecars,
    )
    logger.info("task complete output=%s size=%d sidecars=%d", relative, result.size, len(sidecars))


async def _encode_and_report(
    *,
    api: BackendClient,
    ws: WSClient,
    task_id: str,
    registered: RegisterTranscoderResponse,
    cancel_event: asyncio.Event,
) -> int:
    """Run the registered task (an encode, or on the fork a subtitle OCR, menu
    preview or menu pack) under a heartbeat, report the outcome to the
    Backend, and return the exit code."""
    media_root = Path(registered.media_root)
    final_output = media_root / (registered.task.output_path or "")
    preset = registered.transcode_preset
    tool = preset.tool if preset is not None else TranscodeTool.NONE
    duration = registered.source_track.duration_seconds

    state = ProgressState()
    ocr = registered.task.kind == TranscodeTaskKind.SUBTITLE_OCR
    preview = registered.task.kind == TranscodeTaskKind.MENU_PREVIEW
    pack = registered.task.kind == TranscodeTaskKind.MENU_PACK
    if ocr or preview or pack:
        duration = None
    if preview:
        final_output = Path(registered.raw_input_path or "") / "preview"
    pack_built: list[Path] = []

    def _work() -> Coroutine[Any, Any, EncodeResult]:
        if pack:
            if registered.menu_pack is None:
                raise RuntimeError("register sent no menu pack plan")
            return _run_menu_pack(
                registered.menu_pack,
                Path(registered.raw_input_path or ""),
                media_root,
                state,
                pack_built,
            )
        if preview:
            return _run_menu_preview(Path(registered.raw_input_path or ""), state)
        if ocr:
            return _run_subtitle_ocr(media_root, registered.input_sidecar_paths, state)
        return _run_encoder(
            tool=tool,
            preset=preset,
            raw_input=Path(registered.raw_input_path or ""),
            final_output=final_output,
            duration_seconds=duration,
            state=state,
        )

    try:
        async with HeartbeatPump(api=api, ws=ws, task_id=task_id, state=state) as pump:
            result = await _encode_until_stopped(_work(), cancel_event=cancel_event, revoked_event=pump.revoked)
    except CancelRequested:
        await api.fail(task_id, last_error="cancelled by user")
        logger.info("task cancelled cleanly")
        return 0
    except ClaimRevoked:
        logger.warning("encode stopped: the Backend revoked this task's claim")
        return 0
    except SubtitleOcrFailed as exc:
        logger.exception("subtitle OCR failed: %s", exc)
        kept = [p.relative_to(media_root).as_posix() for p in exc.written]
        await api.fail(task_id, last_error=_clip_error(str(exc)), sidecar_paths=kept)
        return 1
    except Exception as exc:
        logger.exception("encoder failed: %s", exc)
        await api.fail(task_id, last_error=_clip_error(str(exc)))
        return 1
    if pack_built:
        final_output = pack_built[0]
    await _report_complete(
        api=api,
        task_id=task_id,
        media_root=registered.media_root,
        final_output=final_output,
        result=result,
        duration=duration,
        preview=preview,
    )
    return 0


async def _run_task(*, cfg: TranscoderConfig, api: BackendClient, ssl_ctx: ssl.SSLContext) -> int:
    """Open the WS session, register and claim the task, then encode it under its log context; return the exit code."""
    cancel_event = asyncio.Event()
    async with WSClient(
        url=cfg.ws_url,
        service_token=cfg.service_token,
        hostname=cfg.hostname,
        task_id=cfg.task_id,
        ssl_context=ssl_ctx,
    ) as ws:
        await ws.subscribe(f"transcoder.commands.{cfg.task_id}", _cancel_listener(cancel_event))
        await ws.wait_until_connected(timeout=10.0)

        registered = await api.register(task_id=cfg.task_id, hw_caps=_detect_hw_caps())
        # Phase 12 — once register lands we have job_id / track_id /
        # session_application_id; everything below logs with full context.
        # `job_id` lives on `source_track`, not on the task row.
        with with_log_context(
            job_id=registered.source_track.job_id,
            track_id=registered.task.source_track_id,
            session_application_id=registered.task.session_application_id,
        ):
            await api.claim(cfg.task_id)

            return await _encode_and_report(
                api=api, ws=ws, task_id=cfg.task_id, registered=registered, cancel_event=cancel_event
            )


async def _run_subtitle_ocr(media_root: Path, inputs: list[str], state: ProgressState) -> EncodeResult:
    """Write a `.srt` beside each image subtitle sidecar (media-root-relative
    `inputs`). The `.srt` files are the result's sidecars; no video is written."""

    def _on_progress(pct: int) -> None:
        state.pct = pct
        state.current_pass = "text subtitles"

    written = await make_text_subtitles([media_root / p for p in inputs], on_progress=_on_progress)
    return EncodeResult(size=sum(p.stat().st_size for p in written), sidecars=written)


async def _run_menu_preview(menu_dir: Path, state: ProgressState) -> EncodeResult:
    """Write `<menu_dir>/preview/`: the menu's pages, button labels and walk."""
    from arm_transcode.menus.preview import make_preview

    def _on_progress(pct: int, what: str) -> None:
        state.pct = pct
        state.current_pass = what

    result = await asyncio.to_thread(make_preview, menu_dir, progress=_on_progress)
    return EncodeResult(size=result.size())


async def _run_menu_pack(
    plan: MenuPackPlan, menu_dir: Path, media_root: Path, state: ProgressState, built: list[Path]
) -> EncodeResult:
    """Build the job's menu pack and swap it in; `built` gets the folder it wrote."""
    from arm_transcode.menus.media import FfmpegTools
    from arm_transcode.menus.task import build_pack

    def _on_progress(pct: int, what: str) -> None:
        state.pct = pct
        state.current_pass = what

    result = await asyncio.to_thread(build_pack, plan, menu_dir, media_root, FfmpegTools(), _on_progress)
    built.append(result.folder)
    return EncodeResult(size=result.report.bytes)


_T = TypeVar("_T")


async def _encode_until_stopped(
    encode: Coroutine[Any, Any, _T],
    *,
    cancel_event: asyncio.Event,
    revoked_event: asyncio.Event,
) -> _T:
    """Run `encode`, cancelling it if the user cancels or our claim is revoked.

    Cancelling the encoder task kills its subprocess (see `handbrake.py`,
    `ffmpeg_audio.py` and `engines/ffmpeg_vaapi.py`) and leaves the
    `.arm-inprogress` file unrenamed.
    """
    encoder = asyncio.ensure_future(encode)
    waiters = [asyncio.ensure_future(cancel_event.wait()), asyncio.ensure_future(revoked_event.wait())]
    try:
        await asyncio.wait([encoder, *waiters], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        if not encoder.done():
            encoder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await encoder
    if not encoder.cancelled():
        return encoder.result()
    if cancel_event.is_set():
        raise CancelRequested("task.cancel received")
    raise ClaimRevoked("Backend refused our heartbeat")


async def run() -> int:
    cfg = TranscoderConfig()
    configure_service_logging(cfg.service_name, level=cfg.log_level)
    logger.info("transcoder starting task_id=%s host=%s", cfg.task_id, cfg.hostname)

    api = BackendClient(
        base_url=cfg.backend_url,
        service_token=cfg.service_token,
        hostname=cfg.hostname,
    )

    ssl_ctx = _backend_ssl_context()

    try:
        return await _run_task(cfg=cfg, api=api, ssl_ctx=ssl_ctx)
    finally:
        await api.close()


def main() -> int:
    if "--probe-encoders" in sys.argv[1:]:
        # Deprecated compatibility mode. The install-time HandBrakeCLI --help
        # scrape this used to run is gone; an installer or drill script that
        # still invokes this flag gets an empty result rather than a crash.
        # The backend now treats install-time encoder lists as hints only and
        # probes each GPU device for real via --probe-device, so an empty
        # {} here is a truthful "nothing probed", not an over-claim.
        print("--probe-encoders is deprecated; the backend probes each GPU with --probe-device", file=sys.stderr)
        print(json.dumps({}, separators=(",", ":")))
        return 0
    if "--probe-device" in sys.argv[1:]:
        vendor, device = os.environ.get("ARM_GPU_VENDOR"), os.environ.get("ARM_GPU_DEVICE")
        if not vendor or not device:
            print("--probe-device needs ARM_GPU_VENDOR and ARM_GPU_DEVICE", file=sys.stderr)
            return 2
        try:
            gpu_vendor = GpuVendor(vendor)
        except ValueError:
            valid = ", ".join(v.value for v in GpuVendor)
            print(f"--probe-device: unknown ARM_GPU_VENDOR={vendor!r} (valid: {valid})", file=sys.stderr)
            return 2
        try:
            result = probe_device(gpu_vendor, device)
        except ProbeSetupError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print(json.dumps(result, separators=(",", ":")))
        return 0
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
