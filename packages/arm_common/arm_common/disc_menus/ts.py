"""Blu-ray transport streams (`.m2ts`): 192-byte packets (a 4-byte arrival timestamp, then a 188-byte TS packet)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import IO

PACKET = 192
SYNC = 0x47
_CHUNK = PACKET * 8192  # 1.5 MiB


def packets(f: IO[bytes], max_bytes: int | None = None) -> Iterator[bytes]:
    """Every 192-byte packet of a stream, read in chunks. A short tail is dropped."""
    read, tail = 0, b""
    while max_bytes is None or read < max_bytes:
        chunk = f.read(_CHUNK)
        if not chunk:
            return
        read += len(chunk)
        buf = tail + chunk
        end = len(buf) - len(buf) % PACKET
        for i in range(0, end, PACKET):
            yield buf[i : i + PACKET]
        tail = buf[end:]


def pid_of(pk: bytes) -> int | None:
    """The PID of a 192-byte packet, or None when its TS sync byte is wrong."""
    if pk[4] != SYNC:
        return None
    return ((pk[5] & 0x1F) << 8) | pk[6]


def pes_payloads(pkts: Iterable[bytes], pids: set[int]) -> Iterator[tuple[int, int | None, bytes]]:
    """(pid, PTS in 90 kHz or None, payload) for each PES packet on `pids`, reassembled across TS packets."""
    cur: dict[int, tuple[int | None, bytearray]] = {}
    for pk in pkts:
        pid = pid_of(pk)
        if pid is None or pid not in pids:
            continue
        ts = pk[4:]
        afc = (ts[3] >> 4) & 3
        if not afc & 1:
            continue
        o = 4 + (1 + ts[4] if afc & 2 else 0)
        payload = ts[o:]
        if ts[1] & 0x40:  # payload unit start: a new PES packet
            if pid in cur:
                pts, data = cur.pop(pid)
                yield pid, pts, bytes(data)
            if len(payload) < 9 or payload[:3] != b"\x00\x00\x01":
                continue
            pts = None
            if payload[7] & 0x80 and len(payload) >= 14:
                h = payload[9:14]
                pts = (
                    ((h[0] >> 1) & 7) << 30
                    | (int.from_bytes(h[1:3], "big") >> 1) << 15
                    | (int.from_bytes(h[3:5], "big") >> 1)
                )
            cur[pid] = (pts, bytearray(payload[9 + payload[8] :]))
        elif pid in cur:
            cur[pid][1].extend(payload)
    for pid, (pts, data) in cur.items():
        yield pid, pts, bytes(data)


def filter_pids(src: IO[bytes], dst: IO[bytes], pids: set[int]) -> int:
    """Copy only the packets on `pids` from `src` to `dst`; returns the packets written."""
    n = 0
    out = bytearray()
    for pk in packets(src):
        if pid_of(pk) in pids:
            out += pk
            n += 1
            if len(out) >= _CHUNK:
                dst.write(out)
                out.clear()
    if out:
        dst.write(out)
    return n
