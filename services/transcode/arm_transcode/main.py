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
from arm_common.schemas import (
    HardwareCaps,
    TranscodePresetView,
    WSEnvelope,
)
from arm_common.enums import GpuVendor, TranscodeTool
from arm_common.fileops import atomic_output, transcode_none

from arm_transcode.api_client import BackendClient
from arm_transcode.config import TranscoderConfig
from arm_transcode.device_probe import ProbeSetupError, probe_device
from arm_transcode.engines import selected_encoder
from arm_transcode.engines.ffmpeg_vaapi import transcode_ffmpeg_vaapi
from arm_transcode.engines.handbrake_engine import encoder_args as handbrake_encoder_args
from arm_transcode.engines.handbrake_engine import preset_audio, track_args
from arm_transcode.ffmpeg_audio import transcode_audio
from arm_transcode.handbrake import transcode_handbrake
from arm_transcode.heartbeat import HeartbeatPump, ProgressState
from arm_transcode.tracks import (
    TrackPlan,
    extract_sidecars,
    needs_plan,
    plan_tracks,
    probe_source,
    sidecar_outputs,
    written_sidecars,
)
from arm_transcode.ws_client import WSClient


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


async def _run_encoder(
    *,
    tool: TranscodeTool,
    preset: TranscodePresetView | None,
    raw_input: Path,
    final_output: Path,
    duration_seconds: int | None,
    state: ProgressState,
) -> EncodeResult:
    """Dispatch on `tool`. Returns the size of the video we wrote and the
    subtitle sidecars written next to it."""

    async def _on_progress(pct: int, eta: int | None, current: str | None) -> None:
        state.pct = pct
        state.eta_seconds = eta
        state.current_pass = current

    if tool == TranscodeTool.NONE:
        size = transcode_none(raw_input, final_output)
        state.pct = 100
        return EncodeResult(size=size)

    spec = selected_encoder()
    if tool == TranscodeTool.HANDBRAKE and spec is not None and spec.engine == "ffmpeg_vaapi":
        # `tool` only equals HANDBRAKE when `preset` produced it (see `run()`
        # above), so `preset` is never None on this branch.
        assert preset is not None
        device = os.environ.get("ARM_GPU_DEVICE")
        if not device:
            raise RuntimeError("ffmpeg_vaapi encoder requires ARM_GPU_DEVICE")
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
                    progress_callback=_on_progress,
                    plan=plan,
                )
        state.pct = 100
        return EncodeResult(size=size, sidecars=written_sidecars(sidecars))

    if preset is None or preset.preset_ref is None:
        raise RuntimeError(f"transcode tool={tool.value} requires a preset_ref")

    if tool == TranscodeTool.HANDBRAKE:
        plan = await _track_plan(preset, raw_input, final_output)
        hb_track_args: list[str] = []
        sidecars = []
        if plan is not None:
            audio = await preset_audio(preset.preset_ref) if plan.audio else None
            hb_track_args = track_args(plan, audio)
            sidecars = plan.sidecars
        with sidecar_outputs(sidecars):
            await extract_sidecars(raw_input, sidecars)
            with atomic_output(final_output) as tmp:
                size = await transcode_handbrake(
                    input_path=raw_input,
                    output_path=tmp,
                    preset_ref=preset.preset_ref,
                    extra_args=preset.extra_args,
                    encoder_args=handbrake_encoder_args(selected_encoder()),
                    progress_callback=_on_progress,
                    track_args=hb_track_args,
                )
        state.pct = 100
        return EncodeResult(size=size, sidecars=written_sidecars(sidecars))

    if tool == TranscodeTool.ABCDE:
        # The ripper's abcde stage produced .wav files; we re-encode to
        # `preset.container.value` (flac, mp3) here.
        target = preset.preset_ref or preset.container.value
        with atomic_output(final_output) as tmp:
            size = await transcode_audio(
                input_path=raw_input,
                output_path=tmp,
                preset_ref=target,
                duration_seconds=duration_seconds,
                progress_callback=_on_progress,
            )
        state.pct = 100
        return EncodeResult(size=size)

    raise RuntimeError(f"unknown transcode tool={tool.value}")


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

    cancel_event = asyncio.Event()

    async def _on_cancel(env: WSEnvelope) -> None:
        if env.event_type == "task.cancel":
            logger.warning("task.cancel received over WS; signalling encoder")
            cancel_event.set()

    # The Backend serves wss:// with a cert chained to the install CA,
    # which the entrypoint already merges into the system trust store
    # (see api_client.py — same `/etc/ssl/certs/ca-certificates.crt` path
    # the HTTP client uses). `websockets` refuses to connect over `wss://`
    # without an explicit ssl context, so build one from the system store.
    ssl_ctx = ssl.create_default_context()

    rc = 1
    try:
        async with WSClient(
            url=cfg.ws_url,
            service_token=cfg.service_token,
            hostname=cfg.hostname,
            task_id=cfg.task_id,
            ssl_context=ssl_ctx,
        ) as ws:
            await ws.subscribe(f"transcoder.commands.{cfg.task_id}", _on_cancel)
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

                raw_input = Path(registered.raw_input_path)
                final_output = Path(registered.media_root) / (registered.task.output_path or "")
                preset = registered.transcode_preset
                tool = preset.tool if preset is not None else TranscodeTool.NONE
                duration = registered.source_track.duration_seconds

                state = ProgressState()
                try:
                    async with HeartbeatPump(api=api, ws=ws, task_id=cfg.task_id, state=state) as pump:
                        result = await _encode_until_stopped(
                            _run_encoder(
                                tool=tool,
                                preset=preset,
                                raw_input=raw_input,
                                final_output=final_output,
                                duration_seconds=duration,
                                state=state,
                            ),
                            cancel_event=cancel_event,
                            revoked_event=pump.revoked,
                        )
                except CancelRequested:
                    await api.fail(cfg.task_id, last_error="cancelled by user")
                    logger.info("task cancelled cleanly")
                    rc = 0
                except ClaimRevoked:
                    logger.warning("encode stopped: the Backend revoked this task's claim")
                    rc = 0
                except Exception as exc:
                    logger.exception("encoder failed: %s", exc)
                    await api.fail(cfg.task_id, last_error=_clip_error(str(exc)))
                    rc = 1
                else:
                    relative = final_output.relative_to(registered.media_root).as_posix()
                    sidecars = [p.relative_to(registered.media_root).as_posix() for p in result.sidecars]
                    await api.complete(
                        cfg.task_id,
                        output_path=relative,
                        size_bytes=result.size,
                        duration_seconds=duration,
                        sidecar_paths=sidecars,
                    )
                    logger.info("task complete output=%s size=%d sidecars=%d", relative, result.size, len(sidecars))
                    rc = 0
    finally:
        await api.close()

    return rc


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
