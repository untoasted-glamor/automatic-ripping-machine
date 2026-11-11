"""Big-endian field readers that raise `MenuParseError` instead of `struct.error` or `IndexError`."""

from __future__ import annotations

from arm_common.disc_menus import MenuParseError


def need(b: bytes, o: int, n: int) -> None:
    if o < 0 or o + n > len(b):
        raise MenuParseError(f"read of {n} bytes at {o} past the end ({len(b)} bytes)")


def u8(b: bytes, o: int) -> int:
    need(b, o, 1)
    return b[o]


def u16(b: bytes, o: int) -> int:
    need(b, o, 2)
    return int.from_bytes(b[o : o + 2], "big")


def u24(b: bytes, o: int) -> int:
    need(b, o, 3)
    return int.from_bytes(b[o : o + 3], "big")


def u32(b: bytes, o: int) -> int:
    need(b, o, 4)
    return int.from_bytes(b[o : o + 4], "big")


def text(b: bytes, o: int, n: int) -> str:
    need(b, o, n)
    return b[o : o + n].decode("latin-1")
