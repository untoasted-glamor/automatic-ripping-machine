"""0050_transcode_task_kind: transcode_tasks.kind (VARCHAR, `encode` for
existing rows, never a Postgres enum) and the nullable self-referencing
parent_task_id (SET NULL)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_common import TranscodeTask, TranscodeTaskKind  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0049_transcode_audio_format"
_REV = "0050_transcode_task_kind"


def test_0050_upgrade_adds_kind_and_parent() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "ALTER TABLE transcode_tasks ADD COLUMN kind VARCHAR DEFAULT 'encode' NOT NULL" in sql
    assert "ALTER TABLE transcode_tasks ADD COLUMN parent_task_id VARCHAR" in sql
    assert "REFERENCES transcode_tasks (id) ON DELETE SET NULL" in sql
    assert "CREATE INDEX ix_transcode_tasks_parent_task_id" in sql
    # One live encode per output_path; a subtitle OCR task shares its encode's.
    assert "DROP INDEX uq_transcode_tasks_output_path_live" in sql
    assert (
        "CREATE UNIQUE INDEX uq_transcode_tasks_output_path_live ON transcode_tasks (output_path) "
        "WHERE status IN ('queued', 'in_progress', 'done') AND kind = 'encode'"
    ) in sql
    assert "CREATE TYPE" not in sql


def test_0050_downgrade_drops_both() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    assert sql.index("DELETE FROM transcode_tasks WHERE kind <> 'encode'") < sql.index("DROP COLUMN kind")
    assert (
        "CREATE UNIQUE INDEX uq_transcode_tasks_output_path_live ON transcode_tasks (output_path) "
        "WHERE status IN ('queued', 'in_progress', 'done');"
    ) in sql
    assert "ALTER TABLE transcode_tasks DROP COLUMN parent_task_id" in sql
    assert "ALTER TABLE transcode_tasks DROP COLUMN kind" in sql


def test_0050_model_matches() -> None:
    columns = TranscodeTask.__table__.columns
    assert not columns["kind"].nullable and columns["kind"].server_default.arg == "encode"
    assert columns["parent_task_id"].nullable
    assert TranscodeTask(session_application_id="sap", source_track_id="trk").kind == TranscodeTaskKind.ENCODE
