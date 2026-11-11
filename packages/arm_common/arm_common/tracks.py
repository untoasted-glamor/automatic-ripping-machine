"""What a title is.

Every track kind but `disc_menu` is one of the disc's titles: ripped by the
ripper's title paths, counted in rip counts and progress, matched to an
identity and encoded. The disc menu is a line item of its own (fork only), so
everything that walks a job's tracks as titles asks `is_title` rather than
testing kinds itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypeVar

from arm_common.enums import TrackKind

MENU_SOURCE_REF = "menu"
MENU_INDEX = -1


class _HasKind(Protocol):
    @property
    def kind(self) -> TrackKind: ...


T = TypeVar("T", bound=_HasKind)


def is_title(track: _HasKind) -> bool:
    """True for a disc title, False for the disc menu."""
    return track.kind != TrackKind.DISC_MENU


def titles(tracks: Iterable[T]) -> list[T]:
    """The tracks that are titles, in their order."""
    return [t for t in tracks if is_title(t)]
