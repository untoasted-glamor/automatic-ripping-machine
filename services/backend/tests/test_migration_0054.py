"""0054_menu_rip_any_length: `tracks.any_length` and `menu_proposals.scan_title` (fork only)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_common import MenuProposal, Track  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0053_menu_proposals"
_REV = "0054_menu_rip_any_length"


def test_0054_upgrade_adds_both_columns() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "ALTER TABLE tracks ADD COLUMN any_length BOOLEAN DEFAULT false NOT NULL" in sql
    assert "ALTER TABLE menu_proposals ADD COLUMN scan_title INTEGER" in sql


def test_0054_downgrade_drops_them() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    assert "ALTER TABLE menu_proposals DROP COLUMN scan_title" in sql
    assert "ALTER TABLE tracks DROP COLUMN any_length" in sql


def test_0054_models_match() -> None:
    assert Track.__table__.columns["any_length"].server_default.arg == "false"
    assert "scan_title" in MenuProposal.__table__.columns
