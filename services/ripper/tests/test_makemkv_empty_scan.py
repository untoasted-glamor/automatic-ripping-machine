"""A 0-title makemkvcon scan must log *why* — MakeMKV's own MSG texts and
the drives it could see — instead of discarding them."""

from __future__ import annotations

import logging
from typing import Any

import pytest

import arm_ripper.scan.makemkv as makemkv_mod
from arm_ripper.scan.makemkv import scan_disc, summarize_empty_scan

# Shape of a scan where MakeMKV can't open the drive (no /dev/sgN in the
# container): drive slots are all empty, no CINFO/TINFO.
_NO_DRIVE_LINES = [
    'MSG:1005,0,1,"MakeMKV v1.18.3 linux(x64-release) started","%1 started","MakeMKV v1.18.3"',
    'DRV:0,256,999,0,"","",""',
    'DRV:1,256,999,0,"","",""',
    'MSG:5010,0,0,"Failed to open disc","Failed to open disc"',
]


def test_summary_reports_no_drives_and_makemkv_messages() -> None:
    summary = summarize_empty_scan(_NO_DRIVE_LINES)

    assert "drives seen: none" in summary
    assert "/dev/sgN passthrough" in summary
    assert "[5010] Failed to open disc" in summary
    assert "[1005] MakeMKV v1.18.3 linux(x64-release) started" in summary


def test_summary_lists_visible_drive_and_keeps_commas_in_messages() -> None:
    lines = [
        'DRV:0,2,999,12,"BD-RE HL-DT-ST BD-RE BU40N FR07","ACCA_13","/dev/sr0"',
        'MSG:3007,0,1,"AACS: no keys, VUK not found","%1","x"',
        'MSG:3007,0,1,"AACS: no keys, VUK not found","%1","x"',
    ]

    summary = summarize_empty_scan(lines)

    assert 'drives seen: "BD-RE HL-DT-ST BD-RE BU40N FR07" dev=/dev/sr0 disc="ACCA_13"' in summary
    # Quoted comma survives, and the repeated message is deduped.
    assert summary.count("[3007] AACS: no keys, VUK not found") == 1


def test_summary_caps_message_count() -> None:
    lines = [f'MSG:{2000 + i},0,1,"read error {i}","%1"' for i in range(40)]

    summary = summarize_empty_scan(lines)

    assert "read error 14" in summary
    assert "read error 15" not in summary
    assert "... 25 more" in summary


class _Stream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> "_Stream":
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _Proc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _Stream(lines)
        self.stderr = _Stream([])
        self.returncode: int | None = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def kill(self) -> None:  # pragma: no cover - not reached
        pass


@pytest.mark.asyncio
async def test_scan_disc_logs_summary_on_zero_titles(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from arm_ripper.scan.disc_probe import DiscProbe

    async def _factory(*_a: Any, **_kw: Any) -> _Proc:
        return _Proc([(line + "\n").encode() for line in _NO_DRIVE_LINES])

    async def _probe(_device: str) -> DiscProbe:
        return DiscProbe(crc64=None)

    monkeypatch.setattr(makemkv_mod.asyncio, "create_subprocess_exec", _factory)
    monkeypatch.setattr(makemkv_mod, "probe_disc", _probe)

    with caplog.at_level(logging.WARNING, logger="arm_ripper.scan.makemkv"):
        result = await scan_disc("/dev/sr0")

    assert result.titles == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("makemkvcon found 0 titles device=/dev/sr0" in m and "drives seen: none" in m for m in msgs)


@pytest.mark.asyncio
async def test_scan_disc_silent_when_titles_found(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from arm_ripper.scan.disc_probe import DiscProbe

    async def _factory(*_a: Any, **_kw: Any) -> _Proc:
        return _Proc([b'CINFO:1,0,"DVD disc"\n', b'TINFO:0,9,0,"1:00:00"\n'])

    async def _probe(_device: str) -> DiscProbe:
        return DiscProbe(crc64=None)

    monkeypatch.setattr(makemkv_mod.asyncio, "create_subprocess_exec", _factory)
    monkeypatch.setattr(makemkv_mod, "probe_disc", _probe)

    with caplog.at_level(logging.WARNING, logger="arm_ripper.scan.makemkv"):
        await scan_disc("/dev/sr0")

    assert not any("0 titles" in r.getMessage() for r in caplog.records)
