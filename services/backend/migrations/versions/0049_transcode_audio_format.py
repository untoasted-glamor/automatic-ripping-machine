"""Transcode audio format: what each kept audio track becomes.

`transcode_presets.audio_format` (VARCHAR, validated in the app layer as
`arm_common.AudioFormat`): `preset` | `ac3_else_aac51` | `aac_stereo` |
`aac_51` | `copy`. Existing rows get `preset`, so a custom preset keeps its
HandBrake preset's audio encoders. The HandBrake built-ins are backfilled to
`ac3_else_aac51`: one track per language, an untouched copy of its AC-3
track, else its best track as AAC with up to 5.1 channels (they re-encoded
every kept track to AAC stereo before). The seeder keeps them there.

A job's own track selection (`jobs.track_selection`, JSON) carries the same
field; a stored override without it reads as `ac3_else_aac51`.

Revision ID: 0049_transcode_audio_format
Revises: 0048_job_track_selection
Create Date: 2026-10-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0049_transcode_audio_format"
down_revision: Union[str, None] = "0048_job_track_selection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The built-in HandBrake presets (arm_backend.seeders.TRANSCODE_PRESETS).
_HANDBRAKE_BUILTINS = (
    "tpr_builtin_plex_1080p_h265",
    "tpr_builtin_plex_1080p_h265_gpu",
    "tpr_builtin_plex_2160p_hevc",
    "tpr_builtin_tv_plex_1080p_h265",
)


def upgrade() -> None:
    op.add_column("transcode_presets", sa.Column("audio_format", sa.String(), nullable=False, server_default="preset"))
    ids = ", ".join(f"'{preset_id}'" for preset_id in _HANDBRAKE_BUILTINS)
    op.execute(f"UPDATE transcode_presets SET audio_format = 'ac3_else_aac51' WHERE is_builtin AND id IN ({ids})")


def downgrade() -> None:
    op.drop_column("transcode_presets", "audio_format")
