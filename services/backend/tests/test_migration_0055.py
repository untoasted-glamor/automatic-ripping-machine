"""0055_menu_pack_one_live: at most one queued or running `menu_pack` task per menu track (fork only)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0054_menu_rip_any_length"
_REV = "0055_menu_pack_one_live"


def test_0055_upgrade_adds_the_partial_unique_index() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "CREATE UNIQUE INDEX uq_transcode_tasks_live_menu_pack ON transcode_tasks (source_track_id)" in sql
    assert "WHERE kind = 'menu_pack' AND status IN ('queued', 'in_progress')" in sql


def test_0055_downgrade_drops_it() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    assert "DROP INDEX uq_transcode_tasks_live_menu_pack" in sql
