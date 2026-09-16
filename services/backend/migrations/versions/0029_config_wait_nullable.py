"""Make config.manual_wait_seconds nullable.

NULL now means "mandatory review mode" — the pre-rip review gate never
auto-starts the rip, only an explicit rip-start-review click does. A
non-null value keeps today's timed auto-start behavior.

Revision ID: 0029_config_wait_nullable
Revises: 0028_user_role_disabled
Create Date: 2026-09-16

Revision IDs are capped at 32 chars by alembic_version.version_num
(VARCHAR(32)); a longer id fails the version bump *after* the DDL runs.

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_config_wait_nullable"
down_revision: Union[str, None] = "0028_user_role_disabled"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("config", "manual_wait_seconds", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE config SET manual_wait_seconds = 60 WHERE manual_wait_seconds IS NULL")
    op.alter_column("config", "manual_wait_seconds", existing_type=sa.Integer(), nullable=False)
