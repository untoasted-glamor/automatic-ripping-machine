"""At most one live disc menu pack build per menu track (fork only).

A partial unique index on `transcode_tasks.source_track_id` for `menu_pack`
rows that are queued or in progress: the dispatcher's sweep and the job
page's Build both queue one, and the index stops a second while one is live.

Revision ID: 0055_menu_pack_one_live
Revises: 0054_menu_rip_any_length
Create Date: 2026-11-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055_menu_pack_one_live"
down_revision: Union[str, None] = "0054_menu_rip_any_length"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WHERE = sa.text("kind = 'menu_pack' AND status IN ('queued', 'in_progress')")


def upgrade() -> None:
    op.create_index(
        "uq_transcode_tasks_live_menu_pack",
        "transcode_tasks",
        ["source_track_id"],
        unique=True,
        postgresql_where=_WHERE,
        sqlite_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index("uq_transcode_tasks_live_menu_pack", table_name="transcode_tasks")
