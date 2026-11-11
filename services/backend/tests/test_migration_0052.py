"""0052_rip_disc_menus: the "Rip disc menus" setting on `config` (a plain BOOLEAN column, no Postgres enum)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_common import Config  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0051_subtitle_ocr_settings"
_REV = "0052_rip_disc_menus"


def test_0052_upgrade_adds_the_column() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "ALTER TABLE config ADD COLUMN rip_disc_menus BOOLEAN DEFAULT 'true' NOT NULL" in sql
    assert "CREATE TYPE" not in sql


def test_0052_downgrade_drops_it() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    assert "ALTER TABLE config DROP COLUMN rip_disc_menus" in sql


def test_0052_model_matches() -> None:
    column = Config.__table__.columns["rip_disc_menus"]
    assert not column.nullable and column.server_default.arg == "true"
    assert Config(id=1).rip_disc_menus is True
