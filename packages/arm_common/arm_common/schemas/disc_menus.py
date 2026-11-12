"""The job page's Disc menu panel: the menu preview and the names it proposes (fork only).

A `menu_preview` task draws each menu page the disc can reach, reads each
button's label and works out what it plays; the backend proposes a name for
each bonus title from that (`arm_backend.disc_menus`). Accepting a proposal
renames the title's finished encode into Jellyfin's bonus layout.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arm_common.enums import MenuProposalStatus, TrackRole, TrackStatus, TranscodeTaskStatus


class MenuProposalView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    track_id: str | None
    scan_title: int | None = None
    page: str
    button: int
    label: str
    score: float | None
    plays: str
    role: TrackRole | None
    name: str | None
    episode_number: int | None
    note: str | None
    status: MenuProposalStatus
    applied_path: str | None
    last_error: str | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MenuPreviewButtonView(BaseModel):
    id: int
    # Left, top, right, bottom at the page's `size`.
    box: list[int]
    visible: bool
    label: str = ""
    score: float | None = None
    # Its crop as the reader saw it, a file of this preview (`crops/...png`).
    crop: str | None = None
    # What pressing it does: plays a title (`plays` says which), shows another
    # page (`goes_to`, a page id of this preview; None when the walk didn't
    # list it), stays on this page, or stops.
    outcome: Literal["plays", "page", "stays", "stopped"]
    plays: str | None = None
    goes_to: str | None = None
    # The job's track it plays, and that title's proposal.
    track_id: str | None = None
    proposal_id: str | None = None


class MenuPreviewPageView(BaseModel):
    id: str
    # The walk's place: `<menu> p<page>` (Blu-ray) or the DVD VM's.
    at: str
    language: str
    # The drawn page, a file of this preview (`pages/<id>.jpg`).
    image: str
    size: list[int]
    buttons: list[MenuPreviewButtonView]


class MenuPreviewView(BaseModel):
    task_id: str
    status: TranscodeTaskStatus
    progress_pct: int
    last_error: str | None = None
    updated_at: datetime | None = None
    disc: Literal["bd", "dvd"] | None = None
    pages: list[MenuPreviewPageView] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
    truncated: bool = False


class JobDiscMenuView(BaseModel):
    track_id: str
    track_status: TrackStatus
    track_error: str | None = None
    size_bytes: int | None = None
    # The newest preview task, or None before the first one is queued.
    preview: MenuPreviewView | None = None
    proposals: list[MenuProposalView] = Field(default_factory=list)
    # Another preview can be asked for (the menu is copied and none is live).
    can_preview: bool = False
    # Why a title the menu plays but the job has no track for can't be ripped
    # now (no kept disc backup, the drive is busy, ...); None when it can.
    rip_refusal: str | None = None


class MenuProposalUpdate(BaseModel):
    """Edit a proposal and/or decide it. `accept` applies it: the track takes
    the role and name, and its finished encode is renamed (or will be, when
    its encode finishes). `reject` sets it aside; `reopen` puts a rejected one
    back."""

    role: TrackRole | None = None
    name: str | None = Field(default=None, max_length=200)
    episode_number: int | None = Field(default=None, ge=0, le=9999)
    action: Literal["accept", "reject", "reopen"] | None = None


class MenuProposalCreate(BaseModel):
    """Name a title by hand from the menu panel, and apply it."""

    track_id: str
    role: TrackRole
    name: str = Field(min_length=1, max_length=200)
    episode_number: int | None = Field(default=None, ge=0, le=9999)
    page: str = ""
    button: int = 0


class MenuProposalAcceptAllResponse(BaseModel):
    proposals: list[MenuProposalView]
    # Proposals that couldn't be applied, with why.
    refused: dict[str, str] = Field(default_factory=dict)
