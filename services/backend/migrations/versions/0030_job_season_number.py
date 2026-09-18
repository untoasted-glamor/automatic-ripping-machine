"""Add season_number column to jobs table.

Companion to `disc_number`/`disc_total` (0022). Arch §02 declares season and
disc number for TV box sets both user-supplied — disc already had a column,
season only ever existed as a metadata_json key nothing wrote, so the built-in
TV session's `{season}` token could never resolve.

Revision ID: 0030_job_season_number
Revises: 0029_config_wait_nullable
Create Date: 2026-09-17

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_job_season_number"
down_revision: Union[str, None] = "0029_config_wait_nullable"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("season_number", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "season_number")
