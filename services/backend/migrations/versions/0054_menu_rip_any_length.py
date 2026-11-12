"""Rip a title the disc menu plays whatever its length (fork only).

Adds `tracks.any_length` (BOOLEAN NOT NULL DEFAULT false): the ripper remuxes
such a track from the kept disc backup even when it is shorter than the rip's
minimum. And `menu_proposals.scan_title` (INTEGER NULL): the disc scan title a
menu button plays, kept also when the job has no track for it, so the job
page can rip it.

Revision ID: 0054_menu_rip_any_length
Revises: 0053_menu_proposals
Create Date: 2026-11-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0054_menu_rip_any_length"
down_revision: Union[str, None] = "0053_menu_proposals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("any_length", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("menu_proposals", sa.Column("scan_title", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("menu_proposals", "scan_title")
    op.drop_column("tracks", "any_length")
