"""Transcode track selection: audio/subtitle languages and subtitle sidecars.

`transcode_presets` gains five columns that choose which source audio and
subtitle tracks a transcode keeps and where the subtitles go:

- `audio_selection` (`preset` | `all` | `languages`) and `audio_languages`
  (ordered ISO 639-2 codes, JSONB list).
- `subtitle_selection` (`preset` | `all` | `languages` | `none`) and
  `subtitle_languages`.
- `subtitle_output` (`embedded` | `separate` | `both`).

Enum values are VARCHAR, validated in the app layer. Existing rows get the
server defaults (`preset` / `[]` / `preset` / `[]` / `embedded`): a custom
preset keeps exactly the tracks it kept before, including any choice made
through `extra_args`. The HandBrake built-ins are backfilled to keep every
audio track and write every subtitle as a separate file, because HandBrake's
own presets drop every audio track after the first and all subtitles. The
seeder keeps the built-ins on the same values on every boot.

`transcode_tasks.sidecar_paths` (JSONB, nullable) records the subtitle files a
finished task wrote next to its video, so job delete and an overwrite
eviction can remove them with it.

Revision ID: 0047_transcode_track_select
Revises: 0046_drive_scan_activity
Create Date: 2026-10-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_transcode_track_select"
down_revision: Union[str, None] = "0046_drive_scan_activity"
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
    op.add_column(
        "transcode_presets", sa.Column("audio_selection", sa.String(), nullable=False, server_default="preset")
    )
    op.add_column(
        "transcode_presets",
        sa.Column("audio_languages", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "transcode_presets", sa.Column("subtitle_selection", sa.String(), nullable=False, server_default="preset")
    )
    op.add_column(
        "transcode_presets",
        sa.Column("subtitle_languages", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "transcode_presets", sa.Column("subtitle_output", sa.String(), nullable=False, server_default="embedded")
    )
    ids = ", ".join(f"'{preset_id}'" for preset_id in _HANDBRAKE_BUILTINS)
    op.execute(
        "UPDATE transcode_presets SET audio_selection = 'all', subtitle_selection = 'all', "
        f"subtitle_output = 'separate' WHERE is_builtin AND id IN ({ids})"
    )
    op.add_column("transcode_tasks", sa.Column("sidecar_paths", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("transcode_tasks", "sidecar_paths")
    op.drop_column("transcode_presets", "subtitle_output")
    op.drop_column("transcode_presets", "subtitle_languages")
    op.drop_column("transcode_presets", "subtitle_selection")
    op.drop_column("transcode_presets", "audio_languages")
    op.drop_column("transcode_presets", "audio_selection")
