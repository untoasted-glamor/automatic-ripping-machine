"""0049_transcode_audio_format: transcode_presets.audio_format, VARCHAR with
`preset` for existing rows and `ac3_else_aac51` for the HandBrake built-ins
(checked by value against an in-memory SQLite table)."""

from __future__ import annotations

import os
import re
import sqlite3

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_backend import seeders  # noqa: E402
from arm_common import TranscodePreset  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0048_job_track_selection"
_REV = "0049_transcode_audio_format"


def test_0049_upgrade_adds_a_varchar_column() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "ALTER TABLE transcode_presets ADD COLUMN audio_format VARCHAR DEFAULT 'preset' NOT NULL" in sql
    assert "CREATE TYPE" not in sql


def test_0049_backfills_only_the_handbrake_builtins_like_the_seeder() -> None:
    match = re.search(r"UPDATE transcode_presets SET.*?;", _render_sql(_PARENT, _REV), re.DOTALL)
    assert match is not None
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE transcode_presets (id TEXT, is_builtin BOOLEAN, audio_format TEXT DEFAULT 'preset')")
    rows = [(row["id"], True) for row in seeders.TRANSCODE_PRESETS] + [("tpr_user_hb", False)]
    conn.executemany("INSERT INTO transcode_presets (id, is_builtin) VALUES (?, ?)", rows)
    conn.execute(match.group(0))
    got = dict(conn.execute("SELECT id, audio_format FROM transcode_presets").fetchall())
    for row in seeders.TRANSCODE_PRESETS:
        assert got[row["id"]] == row["audio_format"], row["id"]
    assert got["tpr_builtin_tv_plex_1080p_h265"] == "ac3_else_aac51"
    assert got["tpr_user_hb"] == "preset"


def test_0049_downgrade_and_model() -> None:
    assert "ALTER TABLE transcode_presets DROP COLUMN audio_format" in _render_sql(_REV, _PARENT, downgrade=True)
    column = TranscodePreset.__table__.columns["audio_format"]
    assert not column.nullable and column.server_default.arg == "preset"
