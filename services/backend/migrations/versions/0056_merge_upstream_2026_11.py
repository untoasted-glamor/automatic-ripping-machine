"""Join the fork's revision line and upstream's into one head (upstream sync, 2026-11).

The fork's 0045..0055 and upstream's 0045..0049 both start at
0044_previously_ripped and touch different columns, so the merge itself
changes nothing: it only gives `alembic upgrade head` a single head to reach.

Revision ID: 0056_merge_upstream_2026_11
Revises: 0055_menu_pack_one_live, 0049_transcode_task_telemetry
Create Date: 2026-11-16
"""

from typing import Sequence, Union

revision: str = "0056_merge_upstream_2026_11"
down_revision: Union[str, Sequence[str], None] = ("0055_menu_pack_one_live", "0049_transcode_task_telemetry")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
