"""Names read from a disc's menu (fork only).

Adds `menu_proposals`: one row per menu button that plays a title, made by a
`menu_preview` task's walk of the job's copied disc menu, with the button's
label, the role and name it proposes, and where the owner's decision stands.
Plain `create_table`; `role` and `status` are VARCHAR checked in the app layer
(no `CREATE TYPE`). Rows go with their job and their track (CASCADE); a deleted
preview task leaves them (SET NULL). The new task kind `menu_preview` and track
role `special` are VARCHAR values and need no change here.

Revision ID: 0053_menu_proposals
Revises: 0052_rip_disc_menus
Create Date: 2026-11-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0053_menu_proposals"
down_revision: Union[str, None] = "0052_rip_disc_menus"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "menu_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("track_id", sa.String(), sa.ForeignKey("tracks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("transcode_tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("page", sa.String(), nullable=False),
        sa.Column("button", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=""),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("plays", sa.String(), nullable=False, server_default=""),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("episode_number", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="proposed"),
        sa.Column("applied_path", sa.String(), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_menu_proposals_job_id", "menu_proposals", ["job_id"])
    op.create_index("ix_menu_proposals_status", "menu_proposals", ["status"])


def downgrade() -> None:
    op.drop_index("ix_menu_proposals_status", table_name="menu_proposals")
    op.drop_index("ix_menu_proposals_job_id", table_name="menu_proposals")
    op.drop_table("menu_proposals")
