"""Mandatory-confirm sub-mode for the review gate.

Adds `config.review_requires_confirm` (BOOLEAN NOT NULL, default false). With
`hold_for_review` on, true parks each held disc in AWAITING_REVIEW until the
operator confirms (no countdown); false keeps the timed auto-start after
`manual_wait_seconds`.

Revision ID: 0045_review_requires_confirm
Revises: 0044_previously_ripped
Create Date: 2026-10-18
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_review_requires_confirm"
down_revision: Union[str, None] = "0044_previously_ripped"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "config",
        sa.Column("review_requires_confirm", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("config", "review_requires_confirm")
