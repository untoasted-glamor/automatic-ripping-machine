"""0051_subtitle_ocr_settings: the text subtitles settings on `config` and the
per-disc choice on `jobs` (plain BOOLEAN columns, no Postgres enum)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_common import Config, Job  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0050_transcode_task_kind"
_REV = "0051_subtitle_ocr_settings"


def test_0051_upgrade_adds_the_three_columns() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "ALTER TABLE config ADD COLUMN subtitle_ocr_enabled BOOLEAN DEFAULT 'false' NOT NULL" in sql
    assert "ALTER TABLE config ADD COLUMN subtitle_ocr_default_on BOOLEAN DEFAULT 'true' NOT NULL" in sql
    assert "ALTER TABLE jobs ADD COLUMN subtitle_ocr BOOLEAN" in sql
    assert "CREATE TYPE" not in sql


def test_0051_downgrade_drops_them() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    assert "ALTER TABLE jobs DROP COLUMN subtitle_ocr" in sql
    assert "ALTER TABLE config DROP COLUMN subtitle_ocr_default_on" in sql
    assert "ALTER TABLE config DROP COLUMN subtitle_ocr_enabled" in sql


def test_0051_models_match() -> None:
    config_columns = Config.__table__.columns
    assert not config_columns["subtitle_ocr_enabled"].nullable
    assert config_columns["subtitle_ocr_enabled"].server_default.arg == "false"
    assert not config_columns["subtitle_ocr_default_on"].nullable
    assert config_columns["subtitle_ocr_default_on"].server_default.arg == "true"
    assert Job.__table__.columns["subtitle_ocr"].nullable
    assert Config(id=1).subtitle_ocr_enabled is False
    assert Config(id=1).subtitle_ocr_default_on is True
