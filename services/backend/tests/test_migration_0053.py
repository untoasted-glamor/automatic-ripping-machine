"""0053_menu_proposals: the table of names read from a disc's menu (VARCHAR role and status, no Postgres enum)."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://x:x@localhost/x")
os.environ.setdefault("ARM_SERVICE_TOKEN", "tok-service")

from arm_common import MenuProposal, MenuProposalStatus  # noqa: E402

from tests.test_migration_chain import _render_sql  # noqa: E402

_PARENT = "0052_rip_disc_menus"
_REV = "0053_menu_proposals"


def test_0053_upgrade_creates_the_table() -> None:
    sql = _render_sql(_PARENT, _REV)
    assert "CREATE TABLE menu_proposals" in sql
    assert "REFERENCES jobs (id) ON DELETE CASCADE" in sql
    assert "REFERENCES tracks (id) ON DELETE CASCADE" in sql
    assert "REFERENCES transcode_tasks (id) ON DELETE SET NULL" in sql
    assert "CREATE INDEX ix_menu_proposals_job_id" in sql
    assert "CREATE TYPE" not in sql


def test_0053_downgrade_drops_it() -> None:
    sql = _render_sql(_REV, _PARENT, downgrade=True)
    assert "DROP TABLE menu_proposals" in sql


def test_0053_model_matches() -> None:
    table = MenuProposal.__table__
    assert {c.name for c in table.columns} == {
        "id", "job_id", "track_id", "task_id", "page", "button", "label", "score", "plays", "role", "name",
        "episode_number", "note", "status", "applied_path", "last_error", "created_at", "updated_at",
    }  # fmt: skip
    assert table.columns["status"].server_default.arg == MenuProposalStatus.PROPOSED.value
    assert MenuProposal(job_id="job_x", page="p1", button=1, status=MenuProposalStatus.PROPOSED).id.startswith("mnp_")
