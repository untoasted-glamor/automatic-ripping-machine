"""Text subtitles settings and the per-disc choice.

Adds `config.subtitle_ocr_enabled` (BOOLEAN NOT NULL, default false) and
`config.subtitle_ocr_default_on` (BOOLEAN NOT NULL, default true): whether ARM
makes `.srt` files from encodes' image subtitles, and what a job with no choice
of its own gets. Adds `jobs.subtitle_ocr` (BOOLEAN NULL): the job's own choice,
NULL meaning the Settings default.

Revision ID: 0051_subtitle_ocr_settings
Revises: 0050_transcode_task_kind
Create Date: 2026-11-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0051_subtitle_ocr_settings"
down_revision: Union[str, None] = "0050_transcode_task_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "config",
        sa.Column("subtitle_ocr_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "config",
        sa.Column("subtitle_ocr_default_on", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column("jobs", sa.Column("subtitle_ocr", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "subtitle_ocr")
    op.drop_column("config", "subtitle_ocr_default_on")
    op.drop_column("config", "subtitle_ocr_enabled")
