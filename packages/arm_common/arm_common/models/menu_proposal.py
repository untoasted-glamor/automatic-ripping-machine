"""A name for a disc title read from the disc's own menu (fork only).

A `menu_preview` task walks a job's copied disc menu and reads every visible
button's label; the backend turns each button that plays a title into one row
here (`arm_backend.disc_menus.proposals`). The owner accepts, edits or rejects
it on the job page, and an accepted name is applied by renaming the track's
finished encode (`arm_backend.disc_menus.rename`). Disc menus plan v5, M2-M4.
"""

from datetime import datetime

from sqlalchemy import Column, Float, ForeignKey, Integer, String
from sqlmodel import Field, SQLModel

from arm_common.enums import MenuProposalStatus, TrackRole
from arm_common.models._columns import created_at_column, enum_column, updated_at_column
from arm_common.ulid import new_id


def _menu_proposal_id() -> str:
    return new_id("mnp")


class MenuProposal(SQLModel, table=True):
    __tablename__ = "menu_proposals"

    id: str = Field(default_factory=_menu_proposal_id, primary_key=True)
    job_id: str = Field(sa_column=Column(String, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True))
    # The title the button plays, or None when the job has no track for it (it
    # wasn't ripped, or its length was below the rip's minimum).
    track_id: str | None = Field(
        default=None, sa_column=Column(String, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=True)
    )
    # The menu preview task whose walk made it.
    task_id: str | None = Field(
        default=None, sa_column=Column(String, ForeignKey("transcode_tasks.id", ondelete="SET NULL"), nullable=True)
    )
    # Where the button is: the preview page id (`preview.json`) and button id.
    page: str = Field(sa_column=Column(String, nullable=False))
    button: int = Field(sa_column=Column(Integer, nullable=False))
    # The button's label as read, and the reader's confidence (0-1).
    label: str = Field(default="", sa_column=Column(String, nullable=False, server_default=""))
    score: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    # What the button plays, for reading: "00022.mpls", "00008.mpls from 24:07",
    # "title 3, chapter 1".
    plays: str = Field(default="", sa_column=Column(String, nullable=False, server_default=""))
    # The proposal: a role and a name, and for a special its season 0 number.
    role: TrackRole | None = Field(default=None, sa_column=enum_column(TrackRole, "menu_proposal_role", nullable=True))
    name: str | None = Field(default=None)
    episode_number: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    # Why it is info only, or what a mismatch is ("identity has E09, the menu says 8").
    note: str | None = Field(default=None)
    status: MenuProposalStatus = Field(
        sa_column=enum_column(
            MenuProposalStatus,
            "menu_proposal_status",
            server_default=MenuProposalStatus.PROPOSED.value,
            index=True,
        )
    )
    # The encode's media-root-relative path after the rename (applied).
    applied_path: str | None = Field(default=None)
    last_error: str | None = Field(default=None)
    created_at: datetime | None = Field(sa_column=created_at_column())
    updated_at: datetime | None = Field(sa_column=updated_at_column())
