"""Unit tests for arm_transcode.main helpers."""

from __future__ import annotations

import asyncio

import pytest

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


def test_probe_encoders_mode_prints_json(monkeypatch, capsys) -> None:
    import json

    import arm_transcode.main as m

    monkeypatch.setattr(m.sys, "argv", ["arm_transcode", "--probe-encoders"])
    monkeypatch.setattr(m, "probe_encoders", lambda: {"qsv": ["h264"]})
    rc = m.main()
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out) == {"qsv": ["h264"]}
    assert "\n" not in out  # exactly one JSON line


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
