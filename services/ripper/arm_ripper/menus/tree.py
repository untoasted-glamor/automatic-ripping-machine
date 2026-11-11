"""A read-only view of a disc's files, from a folder, an image or the device, with no mount.

The ripper runs unprivileged, so an image or a drive is read the way the scan already reads them: pycdlib (UDF,
then Joliet or ISO 9660), and 7-Zip for the UDF 2.50 images pycdlib refuses (`scan.udf_image`). Paths are relative
to the disc's root (`BDMV/index.bdmv`), with ISO 9660's `;1` stripped, and looked up without regard to case: a
folder copied off Windows or a Joliet view can differ in case from the disc.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any, Protocol, Self

logger = logging.getLogger(__name__)

_VIEW_KEYS = (("udf", "udf_path"), ("joliet", "joliet_path"), ("iso9660", "iso_path"))
_7Z_TIMEOUT_SECONDS = 4 * 3600


class DiscTree(Protocol):
    """Every regular file of a disc, by relative path, with size; and a way to read one."""

    def files(self) -> dict[str, int]: ...

    def open(self, path: str) -> contextlib.AbstractContextManager[IO[bytes]]: ...

    def close(self) -> None: ...


class _Base:
    _files: dict[str, int]

    def _index(self, files: dict[str, int]) -> None:
        self._files = files

    def files(self) -> dict[str, int]:
        return self._files

    def close(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def find(tree: DiscTree, path: str) -> str | None:
    """The tree's own spelling of `path` (case-insensitive), or None."""
    want = path.strip("/").lower()
    return next((k for k in tree.files() if k.lower() == want), None)


def read(tree: DiscTree, path: str) -> bytes | None:
    """The whole of one (small) file, or None when the tree lacks it."""
    name = find(tree, path)
    if name is None:
        return None
    with tree.open(name) as f:
        return f.read()


def listdir(tree: DiscTree, folder: str) -> dict[str, int]:
    """Files directly in `folder` (case-insensitive), by their own name, with size."""
    prefix = folder.strip("/").lower() + "/"
    return {
        k[len(prefix) :]: v
        for k, v in tree.files().items()
        if k.lower().startswith(prefix) and "/" not in k[len(prefix) :]
    }


class FolderTree(_Base):
    """A disc folder (a decrypted backup, a folder source, or the menu copy itself)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        files: dict[str, int] = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.is_symlink():
                files[p.relative_to(root).as_posix()] = p.stat().st_size
        self._index(files)

    @contextlib.contextmanager
    def open(self, path: str) -> Iterator[IO[bytes]]:
        with (self.root / path).open("rb") as f:
            yield f


class PycdlibTree(_Base):
    """An image or the device, through pycdlib."""

    def __init__(self, source: str) -> None:
        from pycdlib import PyCdlib  # lazy: keep import cost off the hot path

        self._iso: Any = PyCdlib()
        self._iso.open(source)
        try:
            self._key, view = self._view()
            files: dict[str, int] = {}
            self._raw: dict[str, str] = {}
            for dirpath, _dirs, names in self._iso.walk(**{self._key: "/"}):
                prefix = dirpath if dirpath.endswith("/") else dirpath + "/"
                for name in names:
                    full = prefix + name
                    rel = full.lstrip("/")
                    if view == "iso9660":
                        rel = rel.split(";", 1)[0]
                    files[rel] = self._iso.get_record(**{self._key: full}).get_data_length()
                    self._raw[rel] = full
            self._index(files)
        except Exception:
            self._iso.close()
            raise

    def _view(self) -> tuple[str, str]:
        for view, key in _VIEW_KEYS:
            with contextlib.suppress(Exception):
                if view == "udf" and self._iso.has_udf():
                    return key, view
                if view == "joliet" and self._iso.has_joliet():
                    return key, view
        return "iso_path", "iso9660"

    @contextlib.contextmanager
    def open(self, path: str) -> Iterator[IO[bytes]]:
        with self._iso.open_file_from_iso(**{self._key: self._raw[path]}) as f:
            yield f

    def close(self) -> None:
        self._iso.close()


class SevenZipTree(_Base):
    """A UDF 2.50 image pycdlib refuses, through 7-Zip (each file streamed from `7z e -so`)."""

    def __init__(self, source: str) -> None:
        from arm_ripper.scan import udf_image

        self.source = source
        listed = udf_image.list_files(source)
        if not listed:
            raise OSError(f"7-Zip could not list {source}")
        self._index(dict(listed))

    @contextlib.contextmanager
    def open(self, path: str) -> Iterator[IO[bytes]]:
        exe = shutil.which("7z") or shutil.which("7zz")
        if exe is None:
            raise OSError("7-Zip is not installed")
        proc = subprocess.Popen(
            [exe, "e", "-so", "--", self.source, path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        assert proc.stdout is not None
        try:
            yield proc.stdout
        finally:
            proc.stdout.close()
            try:
                proc.wait(timeout=_7Z_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()


def open_tree(source: str | Path) -> DiscTree | None:
    """A tree for a folder, an image or a device, or None when nothing can read it. Never raises."""
    path = Path(source)
    if path.is_dir():
        try:
            return FolderTree(path)
        except OSError as e:
            logger.debug("menu tree: folder %s unreadable: %s", path, e)
            return None
    try:
        tree: DiscTree = PycdlibTree(str(source))
        if tree.files():
            return tree
        tree.close()
    except Exception as e:  # noqa: BLE001 - pycdlib raises several flavours (no PVD on a UDF 2.50 image)
        logger.debug("menu tree: pycdlib can't read %s: %s; trying 7z", source, e)
    try:
        return SevenZipTree(str(source))
    except Exception as e:  # noqa: BLE001
        logger.debug("menu tree: 7z can't read %s: %s", source, e)
        return None
