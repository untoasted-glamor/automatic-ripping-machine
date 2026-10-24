"""0048_job_track_selection: jobs.track_selection and
transcode_tasks.replaced_sidecar_paths, both nullable (rendered offline like
test_migration_chain)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_common import Job, TranscodeTask  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0047_transcode_track_select"
_REV = "0048_job_track_selection"


def test_0048_upgrade_adds_nullable_columns() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "ALTER TABLE jobs ADD COLUMN track_selection JSON;" in sql
    assert "ALTER TABLE transcode_tasks ADD COLUMN replaced_sidecar_paths JSONB;" in sql
    assert "CREATE TYPE" not in sql


def test_0048_downgrade_drops_them() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    assert "ALTER TABLE transcode_tasks DROP COLUMN replaced_sidecar_paths" in sql
    assert "ALTER TABLE jobs DROP COLUMN track_selection" in sql


def test_0048_matches_the_models() -> None:
    assert Job.__table__.columns["track_selection"].nullable
    assert TranscodeTask.__table__.columns["replaced_sidecar_paths"].nullable
