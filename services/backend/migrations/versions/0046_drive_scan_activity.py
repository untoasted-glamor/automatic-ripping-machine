"""Drive scan-activity columns (scan_started_at / scan_attempt / scan_max_attempts).

A physical drive's disc scan runs before identify creates a job and can take
minutes across not-ready retries, so the UI showed the drive as idle the whole
time. Each ripper heartbeat now carries the scan in progress (if any); the
backend stores the latest value here and the drives API exposes it.

All nullable: NULL = not scanning (existing rows, and rippers that never send
the field).

Revision ID: 0046_drive_scan_activity
Revises: 0045_review_requires_confirm
Create Date: 2026-10-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0046_drive_scan_activity"
down_revision: Union[str, None] = "0045_review_requires_confirm"
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
