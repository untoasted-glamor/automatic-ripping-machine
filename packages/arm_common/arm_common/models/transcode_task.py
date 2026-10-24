from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from arm_common.models._columns import created_at_column, enum_column, updated_at_column
from arm_common.enums import TranscodeTaskStatus
from arm_common.ulid import new_id


def _transcode_task_id() -> str:
    return new_id("txt")


class TranscodeTask(SQLModel, table=True):
    __tablename__ = "transcode_tasks"

    id: str = Field(default_factory=_transcode_task_id, primary_key=True)
    session_application_id: str = Field(
        sa_column=Column(
            String,
            ForeignKey("session_applications.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    source_track_id: str = Field(sa_column=Column(String, ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False))
    status: TranscodeTaskStatus = Field(
        sa_column=enum_column(
            TranscodeTaskStatus,
            "transcode_task_status",
            server_default=TranscodeTaskStatus.QUEUED.value,
            index=True,
        )
    )
    claimed_by: str | None = Field(default=None)
    claim_heartbeat_at: datetime | None = Field(sa_column=Column(DateTime(timezone=True), nullable=True))
    attempts: int = Field(sa_column=Column(Integer, nullable=False, server_default="0"))
    output_path: str | None = Field(default=None)
    # Subtitle files written next to `output_path` (same media-root-relative
    # form), reported by the transcoder at /complete. Job delete removes them
    # with the video; an overwrite re-apply hands them to the replacing task
    # (`replaced_sidecar_paths`).
    sidecar_paths: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # The sidecars of the task an overwrite re-apply replaced at this
    # `output_path`. They stay next to the old video until this task
    # completes (its video then replaces the old one), and are removed then
    # unless this task wrote the same file again.
    replaced_sidecar_paths: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    progress_pct: int = Field(sa_column=Column(Integer, nullable=False, server_default="0"))
    last_error: str | None = Field(default=None)
    created_at: datetime | None = Field(sa_column=created_at_column())
    updated_at: datetime | None = Field(sa_column=updated_at_column())
