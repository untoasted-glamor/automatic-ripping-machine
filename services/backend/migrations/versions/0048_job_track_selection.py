"""A job's own track selection, and sidecars handed to a replacing task.

- `jobs.track_selection` (JSON, nullable): the job's own audio/subtitle track
  selection (`arm_common.schemas.TrackSelectionSettings`), set from the review
  step, the job page or Apply session. NULL lets the transcode preset decide,
  which is what every existing job keeps. JSON like the table's other
  structured columns (`metadata_json`, `identity_provenance`).
- `transcode_tasks.replaced_sidecar_paths` (JSONB, nullable): the subtitle
  files of the task an overwrite re-apply replaced. They are removed when the
  replacing task completes instead of at the re-apply, so a failed re-encode
  leaves the old video with its subtitles.

Revision ID: 0048_job_track_selection
Revises: 0047_transcode_track_select
Create Date: 2026-10-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048_job_track_selection"
down_revision: Union[str, None] = "0047_transcode_track_select"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("track_selection", sa.JSON(), nullable=True))
    op.add_column("transcode_tasks", sa.Column("replaced_sidecar_paths", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("transcode_tasks", "replaced_sidecar_paths")
    op.drop_column("jobs", "track_selection")
