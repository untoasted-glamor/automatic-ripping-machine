"""Transcode task kind: encode tasks and subtitle OCR tasks.

`transcode_tasks.kind` (VARCHAR, validated in the app layer as
`arm_common.TranscodeTaskKind`): `encode` | `subtitle_ocr`. Existing rows are
encodes. A `subtitle_ocr` task reads the image subtitle sidecars of its
encode task, `transcode_tasks.parent_task_id` (nullable, SET NULL so a
deleted encode row leaves the subtitle task and its recorded `.srt` files).

A subtitle OCR task's `output_path` is its encode's video, so the partial
unique index that lets one live task own an `output_path`
(`uq_transcode_tasks_output_path_live`, 0001) is rebuilt over encode rows
only.

Downgrade deletes the subtitle OCR rows (their `.srt` files stay on disk,
no longer recorded) before restoring the old index.

Revision ID: 0050_transcode_task_kind
Revises: 0049_transcode_audio_format
Create Date: 2026-11-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0050_transcode_task_kind"
down_revision: Union[str, None] = "0049_transcode_audio_format"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LIVE_INDEX = "uq_transcode_tasks_output_path_live"
_LIVE = "status IN ('queued', 'in_progress', 'done')"


def upgrade() -> None:
    op.add_column("transcode_tasks", sa.Column("kind", sa.String(), nullable=False, server_default="encode"))
    op.add_column("transcode_tasks", sa.Column("parent_task_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "transcode_tasks_parent_task_id_fkey",
        "transcode_tasks",
        "transcode_tasks",
        ["parent_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_transcode_tasks_parent_task_id", "transcode_tasks", ["parent_task_id"])
    op.drop_index(_LIVE_INDEX, table_name="transcode_tasks")
    op.create_index(
        _LIVE_INDEX,
        "transcode_tasks",
        ["output_path"],
        unique=True,
        postgresql_where=sa.text(f"{_LIVE} AND kind = 'encode'"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM transcode_tasks WHERE kind <> 'encode'")
    op.drop_index(_LIVE_INDEX, table_name="transcode_tasks")
    op.create_index(_LIVE_INDEX, "transcode_tasks", ["output_path"], unique=True, postgresql_where=sa.text(_LIVE))
    op.drop_index("ix_transcode_tasks_parent_task_id", table_name="transcode_tasks")
    op.drop_constraint("transcode_tasks_parent_task_id_fkey", "transcode_tasks", type_="foreignkey")
    op.drop_column("transcode_tasks", "parent_task_id")
    op.drop_column("transcode_tasks", "kind")
