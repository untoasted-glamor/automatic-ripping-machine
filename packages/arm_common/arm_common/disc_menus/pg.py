"""Presentation graphics (PG, Blu-ray bitmap subtitles) event counts.

A pack pairs each of a disc's PG streams with the encode's `.sup` sidecar that came from it by counting the events
each one shows: a presentation composition segment (PCS) that places at least one object. The same count works on a
`.sup` file and on a PG PID's PES payloads, since both carry the same segments.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import IO

SEG_PCS = 0x16
_SUP_HEADER = 13  # "PG", PTS, DTS, segment type, segment length
_PCS_OBJECTS = 10  # offset of number_of_composition_objects in a PCS


def count_sup(f: IO[bytes]) -> int:
    """Events shown by a `.sup` file."""
    b = f.read()
    o = shown = 0
    while o + _SUP_HEADER <= len(b) and b[o : o + 2] == b"PG":
        t, ln = b[o + 10], int.from_bytes(b[o + 11 : o + 13], "big")
        seg = b[o + _SUP_HEADER : o + _SUP_HEADER + ln]
        if t == SEG_PCS and len(seg) > _PCS_OBJECTS and seg[_PCS_OBJECTS] > 0:
            shown += 1
        o += _SUP_HEADER + ln
    return shown


def count_payloads(payloads: Iterable[bytes]) -> int:
    """Events shown by one PG PID, from its PES payloads."""
    shown = 0
    for data in payloads:
        o = 0
        while o + 3 <= len(data):
            t, ln = data[o], int.from_bytes(data[o + 1 : o + 3], "big")
            seg = data[o + 3 : o + 3 + ln]
            if t == SEG_PCS and len(seg) > _PCS_OBJECTS and seg[_PCS_OBJECTS] > 0:
                shown += 1
            o += 3 + ln
    return shown
