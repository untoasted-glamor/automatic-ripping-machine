"""0047_transcode_track_select: track-selection columns on transcode_presets
and sidecar_paths on transcode_tasks. Rendered offline like
test_migration_chain; the built-in backfill UPDATE is executed against an
in-memory SQLite table so it is checked by value."""

from __future__ import annotations

import os
import re
import sqlite3

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_backend import seeders  # noqa: E402
from arm_common import TranscodePreset, TranscodeTask, TranscodeTool  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0046_drive_scan_activity"
_REV = "0047_transcode_track_select"


def test_0047_upgrade_adds_varchar_and_jsonb_columns_with_safe_defaults() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "ALTER TABLE transcode_presets ADD COLUMN audio_selection VARCHAR DEFAULT 'preset' NOT NULL" in sql
    assert "ALTER TABLE transcode_presets ADD COLUMN audio_languages JSONB DEFAULT '[]' NOT NULL" in sql
    assert "ALTER TABLE transcode_presets ADD COLUMN subtitle_selection VARCHAR DEFAULT 'preset' NOT NULL" in sql
    assert "ALTER TABLE transcode_presets ADD COLUMN subtitle_languages JSONB DEFAULT '[]' NOT NULL" in sql
    assert "ALTER TABLE transcode_presets ADD COLUMN subtitle_output VARCHAR DEFAULT 'embedded' NOT NULL" in sql
    assert "ALTER TABLE transcode_tasks ADD COLUMN sidecar_paths JSONB;" in sql
    # Enums stay VARCHAR: never a Postgres enum type.
    assert "CREATE TYPE" not in sql


def test_0047_backfills_only_the_handbrake_builtins() -> None:
    sql = _render_sql(_PARENT, _REV)
    match = re.search(r"UPDATE transcode_presets SET.*?;", sql, re.DOTALL)
    assert match is not None
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE transcode_presets (id TEXT, is_builtin BOOLEAN, "
        "audio_selection TEXT DEFAULT 'preset', subtitle_selection TEXT DEFAULT 'preset', "
        "subtitle_output TEXT DEFAULT 'embedded')"
    )
    rows = [(row["id"], True) for row in seeders.TRANSCODE_PRESETS] + [("tpr_user_hb", False)]
    conn.executemany("INSERT INTO transcode_presets (id, is_builtin) VALUES (?, ?)", rows)
    conn.execute(match.group(0))
    got = {
        r[0]: r[1:]
        for r in conn.execute("SELECT id, audio_selection, subtitle_selection, subtitle_output FROM transcode_presets")
    }
    for row in seeders.TRANSCODE_PRESETS:
        expected = (
            ("all", "all", "separate") if row["tool"] == TranscodeTool.HANDBRAKE else ("preset", "preset", "embedded")
        )
        assert got[row["id"]] == expected, row["id"]
        # The migration and the seeder agree on every built-in.
        assert got[row["id"]] == (row["audio_selection"], row["subtitle_selection"], row["subtitle_output"])
    # A user's preset keeps the engine's choice: nothing about it changes.
    assert got["tpr_user_hb"] == ("preset", "preset", "embedded")


def test_0047_downgrade_drops_the_columns() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    for column in ("audio_selection", "audio_languages", "subtitle_selection", "subtitle_languages", "subtitle_output"):
        assert f"ALTER TABLE transcode_presets DROP COLUMN {column}" in sql
    assert "ALTER TABLE transcode_tasks DROP COLUMN sidecar_paths" in sql


def test_0047_matches_the_models() -> None:
    preset_columns = TranscodePreset.__table__.columns
    for column in ("audio_selection", "subtitle_selection", "subtitle_output", "audio_languages", "subtitle_languages"):
        assert not preset_columns[column].nullable, column
    assert preset_columns["audio_selection"].server_default.arg == "preset"
    assert preset_columns["subtitle_output"].server_default.arg == "embedded"
    assert TranscodeTask.__table__.columns["sidecar_paths"].nullable
