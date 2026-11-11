"""The "Rip disc menus" setting (fork only).

Adds `config.rip_disc_menus` (BOOLEAN NOT NULL, default true): whether a DVD or
Blu-ray job's "Disc menu" track starts ticked. The new track kind `disc_menu` and
track status `skipped` need no change here: track kinds and statuses are VARCHAR,
checked in the app layer.

Revision ID: 0052_rip_disc_menus
Revises: 0051_subtitle_ocr_settings
Create Date: 2026-11-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052_rip_disc_menus"
down_revision: Union[str, None] = "0051_subtitle_ocr_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("config", sa.Column("rip_disc_menus", sa.Boolean(), nullable=False, server_default="true"))


def downgrade() -> None:
    op.drop_column("config", "rip_disc_menus")
