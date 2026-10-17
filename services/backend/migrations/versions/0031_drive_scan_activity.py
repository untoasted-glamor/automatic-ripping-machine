"""Drive scan-activity columns (scan_started_at / scan_attempt / scan_max_attempts).

The ripper's MakeMKV disc scan runs before identify creates a job and can take
many minutes across not-ready retries, so the UI showed the drive as idle the
whole time. Each ripper heartbeat now carries the scan in progress (if any);
the backend stores the latest value here and the drives API exposes it.

All nullable: NULL = not scanning (existing rows, and rippers that predate the
field and never send it).

Revision ID: 0031_drive_scan_activity
Revises: 0030_job_season_number
Create Date: 2026-10-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_drive_scan_activity"
down_revision: Union[str, None] = "0030_job_season_number"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("drives", sa.Column("scan_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("drives", sa.Column("scan_attempt", sa.Integer(), nullable=True))
    op.add_column("drives", sa.Column("scan_max_attempts", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("drives", "scan_max_attempts")
    op.drop_column("drives", "scan_attempt")
    op.drop_column("drives", "scan_started_at")
