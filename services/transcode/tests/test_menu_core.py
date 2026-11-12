"""The vendored disc menu walk (`services/transcode/menu_core/`, fork only): the bundle, its SHA-256 (which the
Dockerfile's menu-preview target checks) and the plugin commit it came from agree. `devtools/sync-menu-core.sh`
writes all three."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

MENU_CORE = Path(__file__).resolve().parents[1] / "menu_core"


def test_the_walk_bundle_matches_its_hash_and_names_its_source() -> None:
    digest, name = (MENU_CORE / "walk.mjs.sha256").read_text().split()
    assert name == "walk.mjs"
    assert hashlib.sha256((MENU_CORE / "walk.mjs").read_bytes()).hexdigest() == digest
    assert re.fullmatch(r"[0-9a-f]{40}\n", (MENU_CORE / "SOURCE_COMMIT").read_text())
    assert "GNU General Public License" in (MENU_CORE / "NOTICE").read_text()


def test_the_image_puts_the_walk_where_the_preview_looks() -> None:
    from arm_transcode.menus import preview

    dockerfile = (MENU_CORE.parent / "Dockerfile").read_text()
    assert f"{preview.WALK_BUNDLE.parent}/" in dockerfile or str(preview.WALK_BUNDLE) in dockerfile
    assert "walk.mjs.sha256" in dockerfile
